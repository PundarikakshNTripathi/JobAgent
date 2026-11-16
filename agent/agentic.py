"""A provider-agnostic agent loop (ReAct-style): the model sees a goal, a toolbox and the running transcript, picks one tool per step, JobAgent executes it and
feeds the result back, until the model answers. Because tool use is mediated by JobAgent (not by a vendor CLI), the same loop works with Claude, Gemini/Antigravity,
Codex, OpenAI-compatible APIs, Ollama, Hermes, oh-my-pi... Safety: no tool can submit an application or change settings; heavy tools respect the daily caps.
Design notes (what the strongest open agents share, and what is adopted here): persistent memory (memory.py), reusable skills (defaults/skills), a closed
feedback loop from outcomes to memory and scoring (memory.reflect), tool use under a step budget, and plain-text transcripts that can be inspected."""
import json, os, re, subprocess, sys
from common import db, log, CORE, SETTINGS, extract_json
import memory, web
from llm import run

SYSTEM = """You are JobAgent's autonomous operator for ONE user. Achieve the GOAL using the tools below, one tool per step. Be economical: prefer reading state before spending model calls or browser time.
{ctx}

TOOLS (call exactly one per step as JSON {{"thought": "one or two sentences of reasoning", "tool": name, "args": {{...}}, "note": "what the previous result told you that matters"}}; on your FIRST step also add
"plan": "the 2-5 tool calls you intend"; to finish return {{"final": "plain-language report of what you did and found"}}):
- recall {{"q": "", "kinds": "job,referral,person,fact,memory,chat"}}   search everything already known (jobs + JD eligibility, plans, people seen, facts); cheapest tool
- status {{}}                                   counts by pipeline stage and today's usage
- top {{"n": 15}}                               best current matches with ids
- query_jobs {{"verdict": "APPLY|STRETCH|SKIP", "loc_class": "india|remote|relocate", "status": "", "has_referral": true|false, "limit": 10}}   structured filter (all keys optional); PREFER this for any "best / which / missing" question
- search_jobs {{"q": "text", "limit": 10}}      keyword search by company/title/location
- get_job {{"id": ""}}                          full record of one job incl. triage verdict
- get_plan {{"id": ""}}                         READ an existing referral plan (people, links, messages) and the packet's state
- outreach_queue {{}}                           today's direct-outreach targets: fresh hiring posts and seed rounds, with found emails and the suggested angle
- job_notes {{"id": ""}}                        the user's own instructions saved for one job
ACTIONS (these CREATE or CHANGE things and take minutes; use them only when the goal asks to prepare, plan, draft or mark something;
to check what already exists, use picks_list / get_job / get_plan instead):
- packet {{"id": ""}}                           prepare the application packet (cover letter, answers, resume pick)
- referral {{"id": ""}}                         make a NEW referral plan (who to contact, drafted messages)
- resume {{"id": ""}}                           tailored resume suggestions (only for strong fits)
- mark {{"id": "", "status": "applied|skip", "reason": ""}}
- remember {{"note": ""}}                       store a durable fact about the user
- web_search {{"q": ""}}                        search the web
- fetch {{"url": ""}}                           read a web page as text
- inbox_list {{}}                              what the user pasted into the inbox and what happened to each item
- inbox_retry {{"id": "all"}}                   re-run inbox items that are waiting for quota or errored
- picks_list {{"plan": "outdated|current|none|any", "packet": "yes|no|any"}}   jobs the user chose to work on, filtered (use the filters instead of reading a long list)
- inbox {{"text": "", "context": ""}}           add an opportunity found outside the feeds and process it
- needs_you_list {{}}                           jobs waiting on the user (forms that need a few fields / blocked fills)
- linkedin_people {{"company": "", "degree": "1st|2nd"}}   REAL people at a company in the user's LinkedIn network (read-only, logged-in browser; cached 7 days)
- linkedin_profile {{"url": ""}}                 one person's headline, about, experience (read-only)
- connections_scan {{"list": "picks|needs-you|<comma-separated companies>"}}   who the user knows at each company (batch, read-only, no model cost)
- open_page {{"url": ""}}                       read ANY page through the user's logged-in browser (job boards, careers pages, YC Work at a Startup, LinkedIn)
- find_on_page {{"url": "", "q": ""}}           passages of a page that mention q
- browse_task {{"goal": "", "url": ""}}         multi-step read-only browsing from url to answer goal (e.g. list open roles on a careers page)
Browser tools are real: use them whenever the goal involves LinkedIn, YC, a careers page or any site. Report only people/jobs you actually saw, with URLs.
Choosing the first call (copy the pattern):
- "which picks have outdated referral plans?"   -> {{"tool": "picks_list", "args": {{"plan": "outdated"}}}}
- "which picks have no packet yet?"              -> {{"tool": "picks_list", "args": {{"packet": "no"}}}}
- "who do I know at Stripe?"                     -> {{"tool": "linkedin_people", "args": {{"company": "Stripe", "degree": "1st"}}}}
- "best remote matches"                          -> {{"tool": "query_jobs", "args": {{"loc_class": "remote", "verdict": "APPLY", "limit": 10}}}}
- "what did we decide about Datadog?"            -> {{"tool": "recall", "args": {{"q": "Datadog"}}}}
A step looks like: {{"thought": "...", "plan": "picks_list -> final", "tool": "picks_list", "args": {{"plan": "outdated"}}, "note": ""}}
Finishing looks like: {{"final": "3 picks have outdated plans: Baseten - ..., ... (exactly as the result lists them)."}}
Rules: never submit an application (the user does); never invent ids, names, links or numbers: everything in your final answer must come from a tool result
or the context above (links that did not appear there are removed automatically); if a tool fails, say so instead of guessing; when a result already
answers the goal, give the final answer from it straight away (count and list exactly what the result shows); at most {steps} steps.

GOAL: {goal}

TRANSCRIPT SO FAR
{transcript}

Next step (JSON only):"""


def _run_cli(*args):
    p = subprocess.run([sys.executable, os.path.join(CORE, "agent", "run.py"), *args], capture_output=True, text=True, encoding="utf-8", errors="replace", cwd=CORE, timeout=1500)
    return (p.stdout or "")[-1800:] or (p.stderr or "")[-600:]


def tool(name, a):
    c = db()
    if name == "status":
        return _run_cli("status")
    if name == "top":
        return _run_cli("top", str(int(a.get("n", 15))))
    if name == "query_jobs":
        w, args = ["j.active=1"], []
        if a.get("verdict"):
            w.append("t.verdict=?"); args.append(a["verdict"])
        if a.get("loc_class"):
            w.append("j.loc_class=?"); args.append(a["loc_class"])
        if a.get("status"):
            w.append("j.status=?"); args.append(a["status"])
        if a.get("has_referral") is True:
            w.append("(EXISTS (SELECT 1 FROM referrals r WHERE r.job_id=j.id) OR EXISTS (SELECT 1 FROM packets p WHERE p.job_id=j.id AND p.referral_at IS NOT NULL))")
        if a.get("has_referral") is False:
            w.append("NOT EXISTS (SELECT 1 FROM referrals r WHERE r.job_id=j.id) AND NOT EXISTS (SELECT 1 FROM packets p WHERE p.job_id=j.id AND p.referral_at IS NOT NULL)")
        rows = c.execute(f"""SELECT j.id,j.company,j.title,j.location,j.loc_class,t.verdict,t.fit,j.status,
                             (EXISTS (SELECT 1 FROM referrals r WHERE r.job_id=j.id) OR EXISTS (SELECT 1 FROM packets p WHERE p.job_id=j.id AND p.referral_at IS NOT NULL)) hr
                             FROM jobs j LEFT JOIN triage t ON t.job_id=j.id WHERE {' AND '.join(w)} ORDER BY COALESCE(t.fit,j.score) DESC LIMIT ?""", (*args, int(a.get("limit", 10)))).fetchall()
        return "\n".join(f"[{r['id']}] {r['company']} | {r['title'][:55]} | {r['location'][:28]} | {r['loc_class']} | {r['verdict']} {r['fit']} | {r['status']} | referral_plan={'yes' if r['hr'] else 'no'}" for r in rows) or "no matches"
    if name == "search_jobs":
        q = f"%{a.get('q','')}%"
        rows = c.execute("""SELECT j.id,j.company,j.title,j.location,t.verdict,t.fit,j.status FROM jobs j LEFT JOIN triage t ON t.job_id=j.id
                            WHERE j.active=1 AND (j.company LIKE ? OR j.title LIKE ? OR j.location LIKE ?) ORDER BY COALESCE(t.fit,j.score) DESC LIMIT ?""",
                         (q, q, q, int(a.get("limit", 10)))).fetchall()
        return "\n".join(f"[{r['id']}] {r['company']} | {r['title'][:60]} | {r['location'][:30]} | {r['verdict']} {r['fit']} | {r['status']}" for r in rows) or "no matches"
    if name == "get_job":
        r = c.execute("SELECT j.*, t.verdict, t.fit, t.reason FROM jobs j LEFT JOIN triage t ON t.job_id=j.id WHERE j.id=?", (a.get("id", ""),)).fetchone()
        return json.dumps({k: (str(r[k])[:600] if r[k] is not None else None) for k in r.keys()}) if r else "no such job"
    if name in ("packet", "referral", "resume"):
        return _run_cli(name, a.get("id", ""))
    if name == "mark":
        return _run_cli("mark", a.get("id", ""), a.get("status", "skip"), a.get("reason", ""))
    if name == "remember":
        memory.add_memory(a.get("note", ""))
        return "saved"
    if name == "web_search":
        return "\n".join(f"{r['title']} - {r['url']} - {r['snippet'][:160]}" for r in web.search(a.get("q", ""), 6)) or "no results"
    if name == "fetch":
        t, body = web.fetch_text(a.get("url", ""), 3000)
        return f"{t}\n{body}"
    if name == "inbox_list":
        import inbox
        return inbox.summary(30)
    if name == "inbox_retry":
        import inbox
        inbox.retry(str(a.get("id", "all")))
        return inbox.summary(10)
    if name == "picks_list":
        rows = c.execute("""SELECT p.job_id, j.company, j.title, j.status, pk.at packet_at, rf.at plan_at, rf.stale FROM picks p JOIN jobs j ON j.id=p.job_id
                            LEFT JOIN packets pk ON pk.job_id=p.job_id LEFT JOIN referrals rf ON rf.job_id=p.job_id ORDER BY p.ts DESC""").fetchall()
        plan, pkt = str(a.get("plan", "any")).lower(), str(a.get("packet", "any")).lower()
        keep = {"outdated": lambda r: r["plan_at"] and r["stale"], "current": lambda r: r["plan_at"] and not r["stale"], "none": lambda r: not r["plan_at"]}.get(plan, lambda r: True)
        keep2 = {"yes": lambda r: r["packet_at"], "no": lambda r: not r["packet_at"]}.get(pkt, lambda r: True)
        rows = [r for r in rows if keep(r) and keep2(r)]
        head = f"{len(rows)} pick(s) match (plan={plan}, packet={pkt}):\n"
        lines = [f"[{r['job_id']}] {r['company']} | {r['title'][:50]} | status {r['status']} | packet: {(r['packet_at'] or 'none')[:10]} | "
                 f"referral plan: {(r['plan_at'] or 'none')[:10]}" + (f" (OUTDATED: {r['stale'][:40]})" if r['stale'] else "") for r in rows[:40]]
        return head + "\n".join(lines) if rows else "no picks match"
    if name == "outreach_queue":
        import signals
        return signals.text_list(signals.today())
    if name == "job_notes":
        import common
        ns = common.job_notes(a.get("id", ""))
        return "\n".join(f"- {n['ts'][:16]} {n['text']}" for n in ns) or "no notes for this job"
    if name == "get_plan":
        r = c.execute("SELECT r.dir, r.at, r.how, r.stale, j.company, j.title FROM referrals r JOIN jobs j ON j.id=r.job_id WHERE r.job_id=?", (a.get("id", ""),)).fetchone()
        if not r:
            return "no referral plan for this job yet"
        try:
            t = open(os.path.join(r["dir"], "REFERRAL.md"), encoding="utf-8").read()
        except OSError:
            t = "(plan file missing)"
        return (f"Plan for {r['company']} - {r['title']}, made {r['at'][:10]} via {r['how']}" + (f"; OUTDATED: {r['stale']}" if r["stale"] else "") +
                "\n" + t[:3000])
    if name == "inbox":
        import inbox
        i = inbox.add(a.get("text", ""), a.get("context", "from agent"))
        return json.dumps(inbox.process(i))[:1500]
    if name == "needs_you_list":
        return needs_you_text()
    if name == "recall":
        import rag
        kinds = [k.strip() for k in str(a.get("kinds", "")).split(",") if k.strip()] or None
        return rag.context(a.get("q", ""), k=10, kinds=kinds, max_chars=3500) or "nothing relevant stored"
    if name in ("linkedin_people", "linkedin_profile", "open_page", "find_on_page", "browse_task", "connections_scan"):
        import browse
        try:
            if name == "linkedin_people":
                ps = browse.linkedin_people(a.get("company", ""), a.get("degree", "1st"))
                return "\n".join(f"{p['name']} ({p['degree']}) | {p['headline'][:110]} | mutual: {p.get('mutual','')[:60]} | {p['url']}" for p in ps) or "nobody at that company in this degree"
            if name == "linkedin_profile":
                return browse.linkedin_profile(a.get("url", ""))[:2500]
            if name == "open_page":
                r = browse.open_page(a.get("url", ""), 4000, 30)
                return f"{r['title']} | {r['url']}\n{r['text']}\nLINKS: " + "; ".join(f"{l['t']} -> {l['h']}" for l in r.get("links", [])[:30])
            if name == "find_on_page":
                return "\n---\n".join(browse.find_on_page(a.get("url", ""), a.get("q", ""))) or "not found on the page"
            if name == "browse_task":
                return browse.browser_task(a.get("goal", ""), a.get("url", ""))[:3000]
            if name == "connections_scan":
                import linkedin
                return connections_text(linkedin.scan_list(company_list(a.get("list", "picks"))))
        except browse.BrowseError as e:
            return f"browser: {e}"
    return f"unknown tool {name}"


def needs_you_text():
    rows = db().execute("""SELECT j.id,j.company,j.title,j.status,p.fill_report FROM jobs j LEFT JOIN packets p ON p.job_id=j.id
                           WHERE j.active=1 AND j.status LIKE 'fill-%' ORDER BY j.status LIMIT 40""").fetchall()
    out = []
    for r in rows:
        need = ""
        try:
            rep = json.loads(r["fill_report"] or "{}")
            need = "; ".join(str(x) for x in (rep.get("needs_user") or [])[:3])[:200]
        except ValueError:
            pass
        out.append(f"[{r['id']}] {r['company']} | {r['title'][:50]} | {r['status']} | {need}")
    return "\n".join(out) or "nothing is waiting on you"


def company_list(spec):
    """'picks' / 'needs-you' / 'a, b, c' -> distinct company names."""
    c = db()
    spec = (spec or "picks").strip()
    if spec.lower() in ("picks", "my picks"):
        rows = c.execute("SELECT j.company FROM picks p JOIN jobs j ON j.id=p.job_id ORDER BY p.ts DESC").fetchall()
    elif spec.lower().replace(" ", "-") in ("needs-you", "needs", "needsyou"):
        rows = c.execute("SELECT company FROM jobs WHERE active=1 AND status LIKE 'fill-%'").fetchall()
    else:
        return [x.strip() for x in spec.split(",") if x.strip()]
    seen, out = set(), []
    for r in rows:
        k = r[0].lower()
        if k not in seen:
            seen.add(k)
            out.append(r[0])
    return out


def connections_text(res):
    lines = []
    for co, ps in res.items():
        if isinstance(ps, dict):
            lines.append(f"{co}: could not check ({ps.get('error')})")
            continue
        if not ps:
            lines.append(f"{co}: nobody in your 1st/2nd-degree network works there now")
            continue
        lines.append(f"{co}:")
        lines += [f"  - {p['name']} ({p['degree']}) {p['headline'][:90]}" + (f" | mutual: {p['mutual'][:60]}" if p.get("mutual") and p["degree"] != "1st" else "") + f" | {p['url']}" for p in ps]
    return "\n".join(lines)


# name -> (required args, {arg: type}); used to check every call before it runs, so a model gets a precise correction instead of a crash
SPEC = {
    "recall": (["q"], {"q": str, "kinds": str}), "status": ([], {}), "top": ([], {"n": int}),
    "query_jobs": ([], {"verdict": str, "loc_class": str, "status": str, "has_referral": bool, "limit": int}),
    "search_jobs": (["q"], {"q": str, "limit": int}), "get_job": (["id"], {"id": str}), "packet": (["id"], {"id": str}),
    "referral": (["id"], {"id": str}), "resume": (["id"], {"id": str}), "get_plan": (["id"], {"id": str}), "outreach_queue": ([], {}), "job_notes": (["id"], {"id": str}), "mark": (["id", "status"], {"id": str, "status": str, "reason": str}),
    "remember": (["note"], {"note": str}), "web_search": (["q"], {"q": str}), "fetch": (["url"], {"url": str}), "inbox_list": ([], {}),
    "inbox_retry": ([], {"id": str}), "picks_list": ([], {"plan": str, "packet": str}), "inbox": (["text"], {"text": str, "context": str}), "needs_you_list": ([], {}),
    "linkedin_people": (["company"], {"company": str, "degree": str}), "linkedin_profile": (["url"], {"url": str}),
    "connections_scan": ([], {"list": str}), "open_page": (["url"], {"url": str}), "find_on_page": (["url", "q"], {"url": str, "q": str}),
    "browse_task": (["goal", "url"], {"goal": str, "url": str}),
}


# actions run only when the goal itself asks for that kind of work (small models otherwise "check" things by redoing them)
INTENT = {
    "packet": r"\b(prepare|packet|cover letter|apply|application|draft|write)\b",
    "referral": r"\b(make|create|new|draft|write|prepare|build|redo|refresh|regenerate|update)\b[^.]{0,40}\b(referral|plan|outreach|message)|\bplan (a|the|new) referral",
    "resume": r"\b(resume|cv)\b",
    "mark": r"\b(mark|applied|skip|dismiss|reject|not interested)\b",
    "inbox": r"\b(add|ingest|process|save|track)\b",
    "inbox_retry": r"\b(retry|again|rerun|re-run)\b",
    "remember": r"\b(remember|note that|keep in mind|from now on|prefer)\b",
}


def allowed_by_goal(name, goal):
    rx = INTENT.get(name)
    return True if not rx else re.search(rx, goal or "", re.I) is not None


def check_call(name, args):
    """(ok, args_or_error). Coerces simple types, resolves job references given as words, and explains any mistake in one line."""
    if name not in SPEC:
        import difflib
        close = difflib.get_close_matches(name or "", list(SPEC), n=1, cutoff=0.6)
        return False, f"ERROR: unknown tool '{name}'. Valid tools: {', '.join(SPEC)}." + (f" Did you mean {close[0]}?" if close else "")
    if not isinstance(args, dict):
        return False, f"ERROR: args must be a JSON object, got {type(args).__name__}."
    req, types = SPEC[name]
    missing = [k for k in req if not str(args.get(k, "")).strip()]
    if missing:
        return False, f"ERROR: {name} needs {', '.join(missing)}. Expected args: {json.dumps({k: t.__name__ for k, t in types.items()})}."
    out = {}
    for k, v in args.items():
        t = types.get(k)
        if t is None:
            continue  # ignore unknown keys instead of failing
        try:
            out[k] = (v if isinstance(v, bool) else str(v).lower() in ("1", "true", "yes")) if t is bool else t(v)
        except (TypeError, ValueError):
            return False, f"ERROR: {name}.{k} must be {t.__name__}, got {v!r}."
    if "id" in out and name not in ("inbox_retry",):
        c = db()
        if not c.execute("SELECT 1 FROM jobs WHERE id=?", (out["id"],)).fetchone():
            import chatcmd
            job, msg = chatcmd.resolve(out["id"])
            if not job:
                return False, f"ERROR: no job with id {out['id']!r}. {msg} Use search_jobs or query_jobs to find the id."
            out["id"] = job["id"]
    return True, out


def _complete_list(goal, final, hist):
    """Small models often answer a "which/list/who" goal with a count ("32 picks match") instead of the items. When the goal asks for a list
    and the answer names fewer than half of the items the last list-shaped tool result returned, attach those items verbatim (they are tool
    output, so nothing is invented)."""
    if not re.search(r"\b(which|list|who|what are|show|name)\b", goal, re.I):
        return final
    for _, _, res in reversed(hist):
        rows = [ln for ln in res.splitlines() if re.match(r"\s*(\[[^\]]+\]|\d+\.|- )", ln) or ln.count(" | ") >= 2]
        if len(rows) < 2:
            continue
        names = [re.sub(r"\((1st|2nd|3rd)\)", "", re.sub(r"^\s*(\[[^\]]+\]|\d+\.|- )\s*", "", ln).split("|")[0]).strip().lower() for ln in rows]
        hit = sum(1 for n in names if n and n[:25] in final.lower())
        if hit * 2 >= len(names):
            return final
        shown = rows[:40]
        more = f"\n... and {len(rows) - 40} more" if len(rows) > 40 else ""
        return final.rstrip() + "\n\nFrom the tool result:\n" + "\n".join(ln.strip() for ln in shown) + more
    return final


def _fold(steps, raw_last=9000, folded=350):
    """Context folding: the latest tool result stays verbatim, earlier ones shrink to their head (plus the model's own note), so long browsing or
    search sessions don't grow the prompt step after step."""
    out = []
    for j, (head, note, res) in enumerate(steps):
        last = j == len(steps) - 1
        if last:
            body = res[:raw_last] + (f"\n[... {len(res) - raw_last} more characters not shown; use a filter or a narrower query]" if len(res) > raw_last else "")
        else:
            body = res[:folded] + (" …(folded)" if len(res) > folded else "")
        out.append(f"{head}\n{'NOTE: ' + note if note else ''}\nRESULT: {body}".replace("\n\nRESULT", "\nRESULT"))
    return "\n".join(out)


def loop(goal):
    steps = SETTINGS.get("agent_mode", {}).get("max_steps", 12)
    hist, final, plan = [], None, ""
    try:
        import rag
        rag.refresh()
        known = rag.context(goal, k=8, max_chars=2500)
    except Exception:
        known = ""
    ctx = memory.context_pack() + ("\nRETRIEVED FOR THIS GOAL:\n" + known if known else "")
    done_calls = {}
    for i in range(1, steps + 1):
        tr = (f"PLAN: {plan}\n" if plan else "") + (_fold(hist) or "(nothing yet)")
        if i == steps:
            tr += "\nTHIS IS YOUR LAST STEP: return {\"final\": ...} now, using only what the results above show."
        prompt = SYSTEM.format(ctx=ctx, steps=steps, goal=goal, transcript=tr[-16000:])  # stable prefix first (system, profile, tools), volatile parts last
        text, _ = run(prompt, kind="agent", timeout=240, max_turns=1)
        try:
            d = extract_json(text)
            if not isinstance(d, dict):
                raise ValueError("not an object")
        except ValueError:
            hist.append((f"STEP {i}: (reply was not one JSON object)", "", "ERROR: reply with exactly one JSON object: a tool call or {\"final\": ...}."))
            continue
        if d.get("plan") and not plan:
            plan = str(d["plan"])[:600]
        if "final" in d:
            final = d["final"]
            break
        name, args = str(d.get("tool", "")).strip(), d.get("args", {}) or {}
        if name.lower() in ("final", "finish", "answer", "done", "respond"):  # small models sometimes 'call' the final answer
            txt = next((str(v) for k, v in (args.items() if isinstance(args, dict) else []) if str(v).strip()), "") or str(d.get("note") or "")
            if txt.strip():
                final = txt
                break
            hist.append((f"STEP {i}: {name}", "", 'ERROR: to finish, reply {"final": "your answer"} (it is not a tool).'))
            continue
        ok, checked = check_call(name, args)
        sig = name + json.dumps(checked if ok else args, sort_keys=True)
        if ok and not allowed_by_goal(name, goal):
            ok, checked = False, (f"ERROR: {name} creates or changes things, and the goal does not ask for that. To read what exists use "
                                  "picks_list, get_job or get_plan.")
        if not ok:
            out = checked
        elif sig in done_calls:
            prev_i, prev_out = done_calls[sig]
            out = (f"(Same call as step {prev_i}; its full result is repeated here. Do not call it again: answer from it now.)\n" + prev_out)
        else:
            try:
                out = tool(name, checked)
            except Exception as e:
                out = f"ERROR: the tool failed: {str(e)[:300]}. Do not guess its result; try another way or report the failure."
            done_calls[sig] = (i, str(out))
            args = checked
        log(f"agent step {i}: {name} {json.dumps(args)[:80]}")
        hist.append((f"STEP {i}: {name} {json.dumps(args)[:200]}", str(d.get("note", ""))[:300], str(out)))
    if final:
        final = _complete_list(goal, str(final), hist)
        import grounding
        final, _ = grounding.strip_unseen_urls(str(final), goal + "\n" + ctx + "\n" + "\n".join(r for _, _, r in hist))
        return final
    return "Stopped at the step limit. Progress:\n" + _fold(hist[-4:])
