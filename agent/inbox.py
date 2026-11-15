"""Inbox: the user pastes links, job descriptions, messages, screenshots' text or notes they found anywhere (X, WhatsApp, a friend, a newsletter...)
together with their own context. The agent extracts the opportunities, fetches the pages, triages them against the profile (user-sourced items get priority),
and, for the promising ones, prepares the packet and the referral plan right away. Works with any provider."""
import hashlib, json, re
import common
from common import db, log, now, extract_json, COMPANIES, PROFILE
import memory, scoring, web
from llm import run
import llm as llm_mod

EXTRACT = """The user found the material below (links, pasted job text, messages, notes) and wants it handled as job opportunities.

USER'S CONTEXT/NOTES: {ctx}

MATERIAL:
{blob}

FETCHED PAGE TEXT FOR LINKS IN THE MATERIAL:
{pages}

Extract every distinct opportunity (job, internship, fellowship, pre-doc, residency, program, or a person/lead worth contacting). Return ONLY a JSON array:
[{{"company":"","title":"","url":"best direct link or ''","location":"","deadline":"","kind":"job|program|lead","description":"what the role needs and offers, up to 1200 chars, only from the material","user_note":"anything the user said about this item (why they like it, who told them, constraints)"}}]
A hiring post, a job link, a role description or a program announcement IS an opportunity: always extract it. Return [] only when the
material contains no role, program or person at all (for example a plain question)."""


def _tier(company):
    for c in COMPANIES:
        if c["name"].lower() == company.lower() or company.lower() in c["name"].lower():
            return c["tier"], c.get("category", "")
    return "B", "manual"


def _split(text):
    """One inbox row per opportunity when the paste is a list of links (so a failure never loses the others)."""
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    link_lines = [ln for ln in lines if re.search(r"https?://", ln)]
    if len(link_lines) >= 2 and len(link_lines) >= len(lines) * 0.6:
        return link_lines
    return [text]


def add(text, context=""):
    c = db()
    ids = []
    for part in _split(text):
        cur = c.execute("INSERT INTO inbox(ts,text,context,status) VALUES(?,?,?, 'new')", (now(), part, context))
        ids.append(cur.lastrowid)
    c.commit()
    return ids[0] if len(ids) == 1 else ids


def process(item_id, build=True):
    c = db()
    row = c.execute("SELECT * FROM inbox WHERE id=?", (item_id,)).fetchone()
    if not row:
        return None
    c.execute("UPDATE inbox SET status='processing' WHERE id=?", (item_id,))
    c.commit()
    blob = row["text"]
    urls = list(dict.fromkeys(re.findall(r"https?://[^\s)>\]]+", blob)))[:5]
    pages = []
    for u in urls:
        t, body = web.fetch_text(u, 3500)
        pages.append(f"URL {u}\nTITLE {t}\n{body}")
    try:
        text, _ = run(EXTRACT.format(ctx=row["context"] or "(none)", blob=blob[:6000], pages="\n\n".join(pages)[:14000] or "(no links)"), kind="ingest", timeout=300, max_turns=2)
        items = extract_json(text)
    except Exception as e:
        quota = bool(llm_mod.QUOTA_RX.search(str(e)))
        c.execute("UPDATE inbox SET status=?, result=? WHERE id=?", ("waiting" if quota else "error", json.dumps({"error": str(e)[:300], "retry": "automatic when the model quota resets" if quota else "press Retry"}), item_id))
        c.commit()
        log(f"inbox #{item_id} {'waiting for quota' if quota else 'failed'}: {str(e)[:120]}")
        return None
    result = []
    new_ids = []
    for it in items if isinstance(items, list) else []:
        if not it.get("title") and not it.get("company"):
            continue
        key = it.get("url") or f"{it.get('company')}|{it.get('title')}"
        jid = "manual:" + hashlib.md5(key.encode()).hexdigest()[:14]
        dup = c.execute("SELECT id FROM jobs WHERE (url!='' AND url=?) OR (lower(company)=lower(?) AND lower(title)=lower(?)) LIMIT 1", (it.get("url") or "~", it.get("company") or "~", it.get("title") or "~")).fetchone()
        if dup:  # already tracked through a feed: attach the user's note and raise its priority instead of duplicating
            jid = dup["id"]
            c.execute("UPDATE jobs SET score=MAX(score,90), desc=COALESCE(desc,'') || ? WHERE id=?", (f" USER NOTE: {it.get('user_note','')}"[:300], jid))
            c.execute("DELETE FROM triage WHERE job_id=?", (jid,))
        tier, cat = _tier(it.get("company", ""))
        desc = (it.get("description") or "")[:1200] + (f" USER NOTE: {it.get('user_note')}" if it.get("user_note") else "") + (f" Deadline: {it.get('deadline')}" if it.get("deadline") else "")
        job = {"title": it.get("title", ""), "location": it.get("location", ""), "desc": desc, "ats": "manual", "_tier": tier}
        sc = scoring.score(job)
        existing = c.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone()
        if not existing:
            c.execute("""INSERT INTO jobs(id,company,tier,category,title,location,url,ats,posted,first_seen,last_seen,score,loc_class,family,early,yoe,desc,visa,status)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new')""",
                      (jid, it.get("company") or "?", tier, cat or "manual", it.get("title") or "(untitled)", it.get("location", ""), it.get("url") or "", "manual", "", now(), now(),
                       96.0, sc["loc_class"], sc["family"] or "ai_research", 1, sc["yoe"], desc, sc["visa"]))
            new_ids.append(jid)
        c.commit()  # release the write lock before add_pick() uses its own connection
        common.add_pick(jid, "inbox")
        result.append({"id": jid, "company": it.get("company"), "title": it.get("title"), "url": it.get("url"), "kind": it.get("kind"), "new": not existing})
    c.commit()
    if new_ids:
        import pipeline, stakes
        pipeline.triage(len(new_ids) + 5)
        stakes.promote_triaged()
        for r in result:
            t = c.execute("SELECT verdict, fit, reason FROM triage WHERE job_id=?", (r["id"],)).fetchone()
            r["verdict"], r["fit"], r["reason"] = (t["verdict"], t["fit"], t["reason"]) if t else (None, None, None)
            st = c.execute("SELECT status FROM jobs WHERE id=?", (r["id"],)).fetchone()["status"]
            r["status"] = st
            if build and r["new"] and r["verdict"] in ("APPLY", "STRETCH"):
                try:
                    import packets, referrals
                    packets.build_packet(r["id"])
                    referrals.plan_referral(r["id"])
                    r["prepared"] = True
                except Exception as e:
                    log(f"inbox prepare failed for {r['id']}: {e}")
    c.execute("UPDATE inbox SET status='done', result=? WHERE id=?", (json.dumps(result), item_id))
    c.commit()
    log(f"inbox #{item_id}: {len(result)} opportunities, {len(new_ids)} new")
    return result


def pending():
    """Items to (re)try: new ones, and ones that were waiting for quota or errored, once the provider is available again."""
    ok = any(llm_mod.available(p)[0] for p in ("claude", "agy", "codex", "gemini", "openai", "anthropic", "openrouter", "gemini_api", "ollama"))
    st = "('new','waiting','error')" if ok else "('new')"
    return [r["id"] for r in db().execute(f"SELECT id FROM inbox WHERE status IN {st} ORDER BY id").fetchall()]


def process_pending():
    for i in pending():
        try:
            process(i)
        except Exception as e:
            c = db()
            c.execute("UPDATE inbox SET status='error', result=? WHERE id=?", (json.dumps({"error": str(e)[:300]}), i))
            c.commit()
            log(f"inbox #{i} failed: {e}")


def retry(which="all"):
    c = db()
    if which == "all":
        c.execute("UPDATE inbox SET status='new' WHERE status IN ('waiting','error')")
    else:
        c.execute("UPDATE inbox SET status='new' WHERE id=?", (int(which),))
    c.commit()
    process_pending()


def summary(limit=12):
    """Text for the agent's context so chat and tools can see the inbox (what is queued, done, failed, waiting)."""
    rows = db().execute("SELECT id, status, substr(text,1,140) t, result FROM inbox ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    if not rows:
        return "INBOX: empty"
    out = ["INBOX (items the user pasted; statuses: new=queued, waiting=quota, error, done):"]
    for r in rows:
        extra = ""
        try:
            x = json.loads(r["result"] or "null")
            if isinstance(x, list):
                extra = " -> " + "; ".join(f"{o.get('company')} {str(o.get('title'))[:40]} [{o.get('verdict') or 'untriaged'}]" for o in x[:3])
            elif isinstance(x, dict):
                extra = " -> " + str(x.get("error", ""))[:110]
        except ValueError:
            pass
        out.append(f"  #{r['id']} {r['status']}: {r['t']}{extra}")
    return "\n".join(out)


# ------------------------------------------------------------------ chat (several threads; memory = recent turns + rolling summary + long-term memory + retrieval)
CHAT = """{ctx}

You are JobAgent, the user's job-search agent. Answer like a sharp, honest friend who knows the pipeline: short, concrete, no fluff, no flattery.
You DO things by listing actions. The system runs each action the moment you reply and posts the result into this chat when it finishes, so:
 - If the user asks you to do something, include the action for it. Never say you will do it without the action; never claim a result you have not seen.
 - Do exactly what was asked, for exactly the job(s) named. Do not substitute generic advice for an action. Do not add actions that were not asked for.
 - Use only facts from the sections below. If something is not there, say you don't know or start an action that finds out. Never invent names, links,
   numbers or job ids.
 - <target> is a job id from the snapshot, OR company/title words (e.g. "datadog applied science"); the system resolves it.
ACTION KINDS (kind: arg)
  packet: <target>          cover letter, answers, tailored resume
  referral: <target>        who to approach + drafted messages (LinkedIn if allowed, else public web)
  linkedin: <target>        read-only scouting of LinkedIn in the user's logged-in browser: real people, profile links, how to approach
  connections: <picks | needs-you | company, company>   who the user knows at each company (use for "who do I know at X")
  resume: <target>          resume tailoring advice
  fill: <target>            fill the form in the agent browser (never submits); only when the user asked to fill
  mark: <target> applied|skip|dismissed [reason]     only when the user said so
  scout | today | poll | yc-profile | retry-inbox: (no arg)
  remember: <fact about the user or a preference>
  ingest: <text or link the user gave>
  agent: <goal>             a multi-step goal for the autonomous agent loop (research that needs several steps)
At most 4 actions. Never submit an application. Read-only questions (top matches, status, inbox) need no action: answer from the snapshot.

PIPELINE SNAPSHOT
{snapshot}

RETRIEVED FOR THIS MESSAGE (jobs, plans, people seen on LinkedIn, profile facts, memory, earlier chats; use these ids and facts)
{retrieved}

EARLIER IN THIS CONVERSATION (summary)
{summary}

RECENT MESSAGES
{history}

USER: {text}

Reply with ONE JSON object and nothing else:
{{"reply": "your answer to the user", "actions": [{{"kind": "packet", "arg": "datadog applied science"}}]}}   (actions may be [])"""

ALLOWED_ACTIONS = ("packet", "referral", "linkedin", "connections", "resume", "fill", "mark", "remember", "ingest", "agent", "retry-inbox", "scout", "today", "poll", "yc-profile")
SUMMARY = """Summarise the older part of a conversation between a user and their job-search assistant, for the assistant's own memory.
Keep only facts that were actually said: decisions, preferences, jobs and companies discussed (with ids), people named, actions started and their results,
open questions. No advice, no new facts, no guesses. At most 12 short lines.

PREVIOUS SUMMARY
{prev}

MESSAGES TO ADD
{msgs}

Return ONLY JSON: {{"summary": "..."}}"""


def snapshot():
    c = db()
    top = c.execute("""SELECT j.id,j.company,j.title,j.location,t.fit,t.verdict,j.status FROM jobs j LEFT JOIN triage t ON t.job_id=j.id
                       WHERE j.active=1 AND j.status NOT IN ('skipped','applied','ineligible','dismissed','closed','duplicate') ORDER BY COALESCE(t.fit,j.score) DESC LIMIT 12""").fetchall()
    watch = c.execute("SELECT j.id,j.company,j.title FROM watch w JOIN jobs j ON j.id=w.job_id WHERE j.status='watch' LIMIT 8").fetchall()
    lines = ["TOP MATCHES: " + "; ".join(f"[{r['id']}] {r['company']} - {r['title'][:50]} ({r['verdict'] or 'untriaged'} {int(r['fit'] or 0)}, {r['status']})" for r in top)]
    lines.append(summary())
    lines.append("WATCHLIST: " + "; ".join(f"[{r['id']}] {r['company']} - {r['title'][:40]}" for r in watch))
    return "\n".join(lines)


# ---- threads
def ensure_threads():
    c = db()
    if not c.execute("SELECT 1 FROM threads WHERE id=1").fetchone():
        c.execute("INSERT INTO threads(id,title,created,updated) VALUES(1,'First conversation',?,?)", (now(), now()))
        c.commit()


def new_thread(title="New chat"):
    ensure_threads()
    c = db()
    cur = c.execute("INSERT INTO threads(title,created,updated) VALUES(?,?,?)", (title[:80], now(), now()))
    c.commit()
    return cur.lastrowid


def list_threads():
    ensure_threads()
    return [dict(r) for r in db().execute("""SELECT t.id, t.title, t.updated, (SELECT COUNT(*) FROM chat m WHERE m.thread=t.id AND m.role IN ('user','agent')) n
                                             FROM threads t ORDER BY t.updated DESC""").fetchall()]


def rename_thread(tid, title):
    c = db()
    c.execute("UPDATE threads SET title=? WHERE id=?", (title[:80], tid))
    c.commit()


def delete_thread(tid):
    c = db()
    c.execute("DELETE FROM chat WHERE thread=?", (tid,))
    c.execute("DELETE FROM threads WHERE id=?", (tid,))
    c.commit()
    ensure_threads()


def say(role, text, thread=1):
    c = db()
    c.execute("INSERT INTO chat(ts,role,text,thread) VALUES(?,?,?,?)", (now(), role, text, thread))
    c.execute("UPDATE threads SET updated=? WHERE id=?", (now(), thread))
    c.commit()


def _roll_summary(thread, keep=8):
    """Messages older than the last `keep` are folded into the thread's summary (facts only), so long chats stay small and don't drift."""
    c = db()
    t = c.execute("SELECT summary, summarized_upto FROM threads WHERE id=?", (thread,)).fetchone()
    rows = c.execute("SELECT id, role, text FROM chat WHERE thread=? AND role IN ('user','agent') AND id>? ORDER BY id", (thread, t["summarized_upto"] or 0)).fetchall()
    if len(rows) <= keep + 4:
        return t["summary"] or ""
    old = rows[:-keep]
    msgs = "\n".join(f"{r['role'].upper()}: {r['text'][:700]}" for r in old)
    try:
        out, _ = run(SUMMARY.format(prev=t["summary"] or "(none)", msgs=msgs), kind="summary", timeout=180, max_turns=1)
        new = str(extract_json(out).get("summary", "")).strip()[:2500]
    except Exception as e:
        log(f"chat summary skipped: {e}")
        return t["summary"] or ""
    c.execute("UPDATE threads SET summary=?, summarized_upto=? WHERE id=?", (new, old[-1]["id"], thread))
    c.commit()
    return new


def _parse(out):
    """{reply, actions} from the model; accepts the JSON object or the older 'ACTION: kind arg' lines."""
    try:
        d = extract_json(out)
        if isinstance(d, dict) and "reply" in d:
            acts = []
            for a in d.get("actions") or []:
                if isinstance(a, dict) and str(a.get("kind", "")).strip() in ALLOWED_ACTIONS:
                    acts.append((str(a["kind"]).strip(), str(a.get("arg", "")).strip()))
            return str(d["reply"]).strip(), acts
    except ValueError:
        pass
    acts, body = [], []
    for ln in out.splitlines():
        m = re.match(r"\s*ACTION:\s*([\w-]+)\s*(.*)", ln)
        if m and m.group(1) in ALLOWED_ACTIONS:
            acts.append((m.group(1), m.group(2).strip()))
        else:
            body.append(ln)
    return "\n".join(body).strip(), acts


def reply(text, thread=1):
    import chatcmd
    ensure_threads()
    c = db()
    if not c.execute("SELECT 1 FROM threads WHERE id=?", (thread,)).fetchone():
        thread = new_thread()
    first = c.execute("SELECT COUNT(*) FROM chat WHERE thread=? AND role='user'", (thread,)).fetchone()[0] == 0
    say("user", text, thread)
    if first and c.execute("SELECT title FROM threads WHERE id=?", (thread,)).fetchone()[0] in ("New chat", ""):
        rename_thread(thread, re.sub(r"\s+", " ", text).strip()[:48])
    if text.lstrip().startswith("/"):  # slash commands never touch the model: instant, exact, work even when every provider is out of quota
        try:
            answer = chatcmd.run_slash(text, thread=thread)
        except Exception as e:
            answer = f"That command failed: {e}"
        say("agent", answer, thread)
        return answer, []
    recent = c.execute("SELECT role,text FROM chat WHERE thread=? AND role IN ('user','agent') ORDER BY id DESC LIMIT 9", (thread,)).fetchall()[1:]
    hist = "\n".join(f"{r['role'].upper()}: {r['text'][:700]}" for r in reversed(recent))
    summ = _roll_summary(thread)
    try:
        try:
            import rag
            rag.refresh()
            retrieved = rag.context(text, k=10, max_chars=3500) or "(nothing specific)"
        except Exception as e:
            retrieved = f"(retrieval unavailable: {e})"
        snap = snapshot()
        out, _ = run(CHAT.format(ctx=memory.context_pack(), snapshot=snap, retrieved=retrieved, summary=summ or "(nothing yet)", history=hist or "(start)",
                                 text=text), kind="chat", timeout=240, max_turns=2)
    except Exception as e:
        answer = f"I cannot think right now ({str(e)[:220]}). Slash commands still work without a model, e.g. /top, /jobs datadog, /packet <id>, /linkedin <id>, /usage. Type /help."
        say("agent", answer, thread)
        return answer, []
    answer, actions = _parse(out)
    import grounding
    answer, _ = grounding.strip_unseen_urls(answer, "\n".join([snap, retrieved, summ, hist, text, memory.context_pack()]))
    done, props = chatcmd.execute_nl_actions(text, actions[:4], thread=thread)
    if done:
        answer += "\n\nStarted:\n" + "\n".join("- " + d for d in done)
    say("agent", answer or "(no answer)", thread)
    # state-changing / form-filling actions the user did not ask for in words stay as confirm buttons
    if props:
        say("proposal", json.dumps(props), thread)
    return answer, props
