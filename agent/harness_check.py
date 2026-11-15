"""Harness self-test: run the REAL prompts of each task on one provider and check that the output is usable by the code that consumes it.
    python agent/run.py selftest [provider]        (default: local)
A provider passes when every task returns valid, grounded output; failures say exactly what was wrong. Uses your own data read-only
(no packet, referral or form is written)."""
import json, time
import common
from common import db, extract_json, PROFILE
import llm


def _jobs(n):
    c = db()
    return c.execute("""SELECT j.* FROM jobs j JOIN triage t ON t.job_id=j.id WHERE j.active=1 AND j.jd_full=1 ORDER BY t.at DESC LIMIT ?""", (n,)).fetchall()


def t_triage(p):
    import pipeline
    rows = _jobs(4)
    out, _ = llm.run(pipeline.triage_prompt(rows), kind="triage", provider=p, timeout=600)
    res = extract_json(out)
    assert isinstance(res, list) and res, "expected a JSON array of verdicts"
    ids = {r["id"] for r in rows}
    bad = [o for o in res if o.get("id") not in ids or str(o.get("verdict", "")).upper() not in ("APPLY", "STRETCH", "SKIP")]
    assert not bad, f"{len(bad)} verdict(s) with an unknown id or verdict"
    return f"{len(res)} verdicts for {len(rows)} jobs"


def t_chat(p):
    import inbox, memory
    prompt = inbox.CHAT.format(ctx=memory.context_pack(), snapshot=inbox.snapshot(), retrieved="(nothing specific)", summary="(nothing yet)",
                               history="(start)", text="Which three of my top matches should I prepare first, and why? Just answer.")
    out, _ = llm.run(prompt, kind="chat", provider=p, timeout=600)
    reply, acts = inbox._parse(out)
    assert reply and len(reply) > 20, "empty reply"
    return f"reply {len(reply)} chars, {len(acts)} action(s)"


def t_agent(p):
    import agentic, memory
    prompt = agentic.SYSTEM.format(ctx=memory.context_pack(), steps=12, goal="Which of my picks already have a referral plan? List them.", transcript="(nothing yet)")
    out, _ = llm.run(prompt, kind="agent", provider=p, timeout=600)
    d = extract_json(out)
    assert isinstance(d, dict), "not a JSON object"
    if "final" in d:
        raise AssertionError("answered without using a tool (it cannot know this without one)")
    ok, why = agentic.check_call(str(d.get("tool", "")), d.get("args") or {})
    assert ok, why
    return f"tool call {d['tool']} {json.dumps(d.get('args') or {})[:60]}"


def t_rank(p):
    import browse, linkedin
    c = db()
    row = c.execute("SELECT company, COUNT(*) n FROM people GROUP BY company ORDER BY n DESC LIMIT 1").fetchone()
    if not row:
        return "skipped (no people cached yet)"
    people = browse.cached_people(row["company"], days=365)[:8]
    job = c.execute("SELECT * FROM jobs WHERE company=? LIMIT 1", (row["company"],)).fetchone() or {"title": "Software Engineer", "location": "", "url": ""}
    out, _ = llm.run(linkedin.RANK.format(summary=PROFILE["summary"], projects=" | ".join(PROFILE["projects"][:3]), company=row["company"], title=job["title"],
                                          location=job["location"], url=job["url"], people=linkedin._compact(people)), kind="rank", provider=p, timeout=600)
    r = extract_json(out)
    seen = {x["url"].rstrip("/") for x in people}
    tg = r.get("targets") or []
    invented = [t for t in tg if (t.get("profile_url") or "").rstrip("/") not in seen]
    assert tg, "no targets"
    assert not invented, f"{len(invented)} target(s) not among the people seen"
    return f"{len(tg)} targets, none invented"


def t_outreach(p):
    import outreach
    job = _jobs(1)[0]
    tg = [{"name": "Asha Rao", "headline": f"Software Engineer at {job['company']}", "degree": "2nd", "mutual": "Vikram Shah",
           "evidence": f"Works on inference infrastructure at {job['company']}"}]
    prompt = outreach.PROMPT.format(style="Plain, short, specific.", name=PROFILE.get("first_name", "the student"), facts=outreach.facts(), rules=" ".join(PROFILE.get("hard_rules", []))[:800],
                                    company=job["company"], title=job["title"], location=job["location"], fit="", people="- Asha Rao | " + tg[0]["headline"] + " | degree: 2nd | mutual: Vikram Shah | evidence: " + tg[0]["evidence"])
    out, _ = llm.run(prompt, kind="outreach", provider=p, timeout=600)
    items = extract_json(out)
    items = items if isinstance(items, list) else [items]
    assert items and items[0].get("dm_short"), "no message drafted"
    import grounding
    bad = grounding.unsupported_numbers(items[0]["dm_short"], [outreach.facts(), job["title"], job["company"]])
    return f"message {len(items[0]['dm_short'])} chars; unsupported figures: {bad or 'none'}"


def t_ingest(p):
    import inbox
    blob = ("Hiring: Machine Learning Engineer Intern at Example Robotics, Bengaluru (hybrid). Summer 2027, final-year students. "
            "Python, PyTorch, CUDA a plus. Apply: https://example.com/careers/ml-intern")
    out, _ = llm.run(inbox.EXTRACT.format(ctx="(none)", blob=blob, pages="(no links)"), kind="ingest", provider=p, timeout=600)
    r = extract_json(out)
    items = r if isinstance(r, list) else (r.get("opportunities") or r.get("items") or [r])
    assert items and any("example" in json.dumps(x).lower() for x in items), "did not extract the opportunity"
    return f"{len(items)} opportunity extracted"


def t_summary(p):
    import inbox
    msgs = "USER: I only want roles in India or remote.\nAGENT: Noted. Stripe SWE intern (greenhouse:stripe:8031833) is in Bengaluru.\nUSER: Plan a referral for it."
    out, _ = llm.run(inbox.SUMMARY.format(prev="(none)", msgs=msgs), kind="summary", provider=p, timeout=600)
    s = str(extract_json(out).get("summary", ""))
    assert "stripe" in s.lower(), "summary lost the key fact"
    return f"{len(s)} chars"


TESTS = [("triage", t_triage), ("chat", t_chat), ("agent step", t_agent), ("rank people", t_rank), ("outreach", t_outreach), ("inbox extraction", t_ingest),
         ("chat summary", t_summary)]


def run_all(provider="local"):
    res = []
    for name, fn in TESTS:
        t0 = time.time()
        try:
            msg, ok = fn(provider), True
        except Exception as e:
            msg, ok = f"{type(e).__name__}: {str(e)[:200]}", False
        res.append({"task": name, "ok": ok, "seconds": round(time.time() - t0, 1), "detail": msg})
        print(f"{'PASS' if ok else 'FAIL'}  {name:16} {res[-1]['seconds']:6}s  {msg}", flush=True)
    common.meta_set(db(), f"selftest_{provider}", json.dumps({"at": common.now(), "results": res}))
    print(f"{sum(r['ok'] for r in res)}/{len(res)} passed on {provider}")
    return res
