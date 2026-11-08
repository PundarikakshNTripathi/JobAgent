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

TOOLS (call exactly one per step as JSON {{"tool": name, "args": {{...}}}}; to finish return {{"final": "plain-language report of what you did and found"}}):
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
- inbox {{"text": "", "context": ""}}           add an opportunity found outside the feeds and process it
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
    if name == "inbox":
        import inbox
        i = inbox.add(a.get("text", ""), a.get("context", "from agent"))
        return json.dumps(inbox.process(i))[:1500]
    return f"unknown tool {name}"


def loop(goal):
    steps = SETTINGS.get("agent_mode", {}).get("max_steps", 12)
    transcript, final = [], None
    for i in range(1, steps + 1):
        prompt = SYSTEM.format(ctx=memory.context_pack(), steps=steps, goal=goal, transcript="\n".join(transcript)[-9000:] or "(nothing yet)")
        text, _ = run(prompt, kind="chat", timeout=240, max_turns=1)
        try:
            d = extract_json(text)
        except ValueError:
            transcript.append(f"STEP {i}: (unparseable reply, ignored)")
            continue
        if "final" in d:
            final = d["final"]
            break
        name, args = d.get("tool", ""), d.get("args", {}) or {}
        try:
            out = tool(name, args)
        except Exception as e:
            out = f"tool error: {e}"
        log(f"agent step {i}: {name} {json.dumps(args)[:80]}")
        transcript.append(f"STEP {i}: {name} {json.dumps(args)[:200]}\nRESULT: {str(out)[:1500]}")
    return final or "Stopped at the step limit. Progress:\n" + "\n".join(transcript[-4:])
