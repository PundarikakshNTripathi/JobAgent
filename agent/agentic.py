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

TOOLS (call exactly one per step as JSON {{"tool": name, "args": {{...}}, "note": "what the previous result told you that matters"}}; on your FIRST step also add
"plan": "the 2-5 tool calls you intend"; to finish return {{"final": "plain-language report of what you did and found"}}):
- recall {{"q": "", "kinds": "job,referral,person,fact,memory,chat"}}   search everything already known (jobs + JD eligibility, plans, people seen, facts); cheapest tool
- status {{}}                                   counts by pipeline stage and today's usage
- top {{"n": 15}}                               best current matches with ids
- query_jobs {{"verdict": "APPLY|STRETCH|SKIP", "loc_class": "india|remote|relocate", "status": "", "has_referral": true|false, "limit": 10}}   structured filter (all keys optional); PREFER this for any "best / which / missing" question
- search_jobs {{"q": "text", "limit": 10}}      keyword search by company/title/location
- get_job {{"id": ""}}                          full record of one job incl. triage verdict
- packet {{"id": ""}}                           prepare the application packet (cover letter, answers, resume pick)
- referral {{"id": ""}}                         plan who to contact and draft messages
- resume {{"id": ""}}                           tailored resume suggestions (only for strong fits)
- mark {{"id": "", "status": "applied|skip", "reason": ""}}
- remember {{"note": ""}}                       store a durable fact about the user
- web_search {{"q": ""}}                        search the web
- fetch {{"url": ""}}                           read a web page as text
- inbox_list {{}}                              what the user pasted into the inbox and what happened to each item
- inbox_retry {{"id": "all"}}                   re-run inbox items that are waiting for quota or errored
- picks_list {{}}                               jobs the user chose to work on manually
- inbox {{"text": "", "context": ""}}           add an opportunity found outside the feeds and process it
- needs_you_list {{}}                           jobs waiting on the user (forms that need a few fields / blocked fills)
- linkedin_people {{"company": "", "degree": "1st|2nd"}}   REAL people at a company in the user's LinkedIn network (read-only, logged-in browser; cached 7 days)
- linkedin_profile {{"url": ""}}                 one person's headline, about, experience (read-only)
- connections_scan {{"list": "picks|needs-you|<comma-separated companies>"}}   who the user knows at each company (batch, read-only, no model cost)
- open_page {{"url": ""}}                       read ANY page through the user's logged-in browser (job boards, careers pages, YC Work at a Startup, LinkedIn)
- find_on_page {{"url": "", "q": ""}}           passages of a page that mention q
- browse_task {{"goal": "", "url": ""}}         multi-step read-only browsing from url to answer goal (e.g. list open roles on a careers page)
Browser tools are real: use them whenever the goal involves LinkedIn, YC, a careers page or any site. Report only people/jobs you actually saw, with URLs.
Rules: never submit an application (the user does); never invent ids; stop as soon as the goal is met; at most {steps} steps.

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
        rows = c.execute("SELECT p.job_id, p.actions, j.company, j.title, j.status FROM picks p JOIN jobs j ON j.id=p.job_id ORDER BY p.ts DESC LIMIT 25").fetchall()
        return "\n".join(f"[{r['job_id']}] {r['company']} | {r['title'][:50]} | actions={r['actions']} | {r['status']}" for r in rows) or "no picks yet"
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
        lines += [f"  - {p['name']} ({p['degree']}) {p['headline'][:90]}" + (f" | mutual: {p['mutual'][:60]}" if p.get("mutual") and p["degree"] != "1st" else "") + f"\n    {p['url']}" for p in ps]
    return "\n".join(lines)


def _fold(steps, raw_last=3500, folded=350):
    """Context folding: the latest tool result stays verbatim, earlier ones shrink to their head (plus the model's own note), so long browsing or
    search sessions don't grow the prompt step after step."""
    out = []
    for j, (head, note, res) in enumerate(steps):
        last = j == len(steps) - 1
        body = res[:raw_last] if last else (res[:folded] + (" …(folded)" if len(res) > folded else ""))
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
    for i in range(1, steps + 1):
        tr = (f"PLAN: {plan}\n" if plan else "") + (_fold(hist) or "(nothing yet)")
        prompt = SYSTEM.format(ctx=ctx, steps=steps, goal=goal, transcript=tr[-12000:])  # stable prefix first (system, profile, tools), volatile parts last
        text, _ = run(prompt, kind="chat", timeout=240, max_turns=1)
        try:
            d = extract_json(text)
        except ValueError:
            hist.append((f"STEP {i}: (unparseable reply, ignored)", "", ""))
            continue
        if d.get("plan") and not plan:
            plan = str(d["plan"])[:600]
        if "final" in d:
            final = d["final"]
            break
        name, args = d.get("tool", ""), d.get("args", {}) or {}
        try:
            out = tool(name, args)
        except Exception as e:
            out = f"tool error: {e}"
        log(f"agent step {i}: {name} {json.dumps(args)[:80]}")
        hist.append((f"STEP {i}: {name} {json.dumps(args)[:200]}", str(d.get("note", ""))[:300], str(out)))
    return final or "Stopped at the step limit. Progress:\n" + _fold(hist[-4:])
