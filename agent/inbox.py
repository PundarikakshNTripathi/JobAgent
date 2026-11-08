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
If the material is a question or has no opportunity, return []."""


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


# ------------------------------------------------------------------ chat
CHAT = """{ctx}

You are JobAgent, the user's job-search agent. Answer like a sharp, honest friend who knows the pipeline: short, concrete, no fluff, no flattery.
You can take actions. To act, end your reply with one line per action, exactly in this form (the system runs them after your reply):
  ACTION: packet <job id>          prepare the application packet
  ACTION: referral <job id>        plan who to contact and draft messages
  ACTION: mark <job id> applied|skip [reason]
  ACTION: remember <fact about the user or a preference>
  ACTION: ingest <text or link the user gave>   add an opportunity from outside the feeds
  ACTION: agent <goal>             hand a multi-step goal to the autonomous agent loop
These are PROPOSALS: the user confirms each before it runs, so only propose what is genuinely useful (at most 3). Only use ids that appear below. Never propose submitting an application.

PIPELINE SNAPSHOT
{snapshot}

RECENT CHAT
{history}

USER: {text}
JOBAGENT:"""

ALLOWED_ACTIONS = ("packet", "referral", "mark", "remember", "ingest", "agent", "retry-inbox")


def snapshot():
    c = db()
    top = c.execute("""SELECT j.id,j.company,j.title,j.location,t.fit,t.verdict,j.status FROM jobs j LEFT JOIN triage t ON t.job_id=j.id
                       WHERE j.active=1 AND j.status NOT IN ('skipped','applied','ineligible') ORDER BY COALESCE(t.fit,j.score) DESC LIMIT 12""").fetchall()
    watch = c.execute("SELECT j.id,j.company,j.title FROM watch w JOIN jobs j ON j.id=w.job_id WHERE j.status='watch' LIMIT 8").fetchall()
    lines = ["TOP MATCHES: " + "; ".join(f"[{r['id']}] {r['company']} - {r['title'][:50]} ({r['verdict'] or 'untriaged'} {int(r['fit'] or 0)}, {r['status']})" for r in top)]
    lines.append(summary())
    lines.append("WATCHLIST: " + "; ".join(f"[{r['id']}] {r['company']} - {r['title'][:40]}" for r in watch))
    return "\n".join(lines)


def reply(text):
    c = db()
    c.execute("INSERT INTO chat(ts,role,text) VALUES(?,?,?)", (now(), "user", text))
    c.commit()
    hist = "\n".join(f"{r['role'].upper()}: {r['text'][:600]}" for r in reversed(c.execute("SELECT role,text FROM chat ORDER BY id DESC LIMIT 7").fetchall()[1:]))
    out, _ = run(CHAT.format(ctx=memory.context_pack(), snapshot=snapshot(), history=hist or "(start)", text=text), kind="chat", timeout=240, max_turns=2)
    actions = []
    body = []
    for ln in out.splitlines():
        m = re.match(r"\s*ACTION:\s*([\w-]+)\s*(.*)", ln)
        if m and m.group(1) in ALLOWED_ACTIONS:
            actions.append((m.group(1), m.group(2).strip()))
        else:
            body.append(ln)
    answer = "\n".join(body).strip()
    c.execute("INSERT INTO chat(ts,role,text) VALUES(?,?,?)", (now(), "agent", answer))
    c.commit()
    # Chat only PROPOSES actions; the user confirms each one (GUI button or `run.py <cmd>`). Nothing changes state on its own.
    proposals = [{"kind": k, "arg": arg[:300]} for k, arg in actions[:4]]
    if proposals:
        c.execute("INSERT INTO chat(ts,role,text) VALUES(?,?,?)", (now(), "proposal", json.dumps(proposals)))
        c.commit()
    return answer, proposals
