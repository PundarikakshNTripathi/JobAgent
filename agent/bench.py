"""Benchmark providers on YOUR data instead of trusting claims: same triage batch and same research question through every available provider,
compared on agreement, speed, tokens and (for research) whether the URLs it returns are actually alive and mention the claimed entity."""
import json, os, random, re, time
from common import db, PROFILE, OUT, today, extract_json, log
import llm, pipeline, web


def _triage_prompt(rows):
    return pipeline.TRIAGE_PROMPT.format(summary=PROFILE.get("summary", ""), education=PROFILE.get("education", ""), projects=" | ".join(PROFILE.get("projects", [])[:4]),
                                         experience=" | ".join(PROFILE.get("experience", [])), achievements=" | ".join(PROFILE.get("achievements", [])),
                                         variants=json.dumps(PROFILE.get("resume_variant_guidance", {})), persona=__import__("memory").persona(), postings="\n---\n".join(pipeline._fmt(r) for r in rows))


def bench_triage(providers=None, n=10, seed=7, variants=None):
    c = db()
    rows = c.execute("SELECT * FROM jobs WHERE active=1 AND score>=60 ORDER BY id").fetchall()
    random.Random(seed).shuffle(rows)
    rows = rows[:n]
    prompt = _triage_prompt(rows)
    res = {}
    combos = variants or [(x, "") for x in (providers or [x for x in llm.registry() if llm.available(x)[0] and x not in ("custom", "ollama")])]
    for prov, mdl in combos:
        p = f"{prov}:{mdl}" if mdl else prov
        t0 = time.time()
        try:
            text, raw = llm.run(prompt, kind="bench", provider=prov, model=mdl, timeout=500)
            verdicts = {o["id"]: (o.get("verdict"), o.get("fit")) for o in extract_json(text) if isinstance(o, dict)}
            res[p] = {"secs": round(time.time() - t0, 1), "verdicts": verdicts, "usage": raw["usage"]}
        except Exception as e:
            res[p] = {"error": str(e)[:200]}
    names = [p for p in res if "verdicts" in res[p]]
    ref = names[0] if names else None
    lines = [f"# Triage benchmark ({n} postings, {today()})", "", "| provider | seconds | judged | agrees with " + (ref or "-") + " (pursue vs skip) | mean fit | tokens in/out |", "|---|---|---|---|---|---|"]
    pursue = lambda v: v in ("APPLY", "STRETCH")
    for p, r in res.items():
        if "error" in r:
            lines.append(f"| {p} | - | - | error: {r['error']} | - | - |")
            continue
        both = [i for i in r["verdicts"] if ref and i in res[ref]["verdicts"]]
        ag = sum(pursue(r["verdicts"][i][0]) == pursue(res[ref]["verdicts"][i][0]) for i in both)
        fits = [float(v[1]) for v in r["verdicts"].values() if v[1] is not None]
        u = r["usage"]
        lines.append(f"| {p} | {r['secs']} | {len(r['verdicts'])} | {ag}/{len(both)} | {sum(fits)/len(fits):.0f} | {u.get('inp',0)+u.get('cache_read',0)}/{u.get('outp',0)} |" if fits else f"| {p} | {r['secs']} | 0 | - | - | - |")
    return res, "\n".join(lines)


RESEARCH_Q = ("Find 4 recent public facts about the research or engineering work of {company}'s team behind '{title}' (papers, repos, blog posts, talks). "
              "Return ONLY JSON: [{{\"fact\":\"\",\"url\":\"\"}}]. Every url must be real and open.")


def bench_research(company=None, title=None, providers=None):
    """Does the provider's research return URLs that are alive and actually mention the company? (native web tools or JobAgent's own retrieval)"""
    out = {}
    if not company:
        r = db().execute("SELECT company,title FROM jobs WHERE status IN ('packet','triaged','watch') ORDER BY score DESC LIMIT 1").fetchone()
        company, title = (r["company"], r["title"]) if r else ("Stripe", "Software Engineer")
    for p in providers or [x for x in llm.registry() if llm.available(x)[0] and x not in ("custom", "ollama")]:
        t0 = time.time()
        try:
            text, raw = llm.run(RESEARCH_Q.format(company=company, title=title), kind="bench", provider=p, allowed_tools="WebSearch,WebFetch", timeout=400)
            facts = [f for f in extract_json(text) if isinstance(f, dict) and f.get("url")]
            alive = mention = 0
            for f in facts:
                t, body = web.fetch_text(f["url"], 4000)
                if body and not body.startswith("(fetch failed"):
                    alive += 1
                    mention += company.lower() in (t + body).lower()
            out[p] = {"secs": round(time.time() - t0, 1), "facts": len(facts), "alive": alive, "mention_company": mention, "web": "native" if llm.registry()[p]["capabilities"].get("web") else "JobAgent retrieval"}
        except Exception as e:
            out[p] = {"error": str(e)[:200]}
    lines = [f"# Research benchmark: {company} / {title} ({today()})", "", "| provider | web access | seconds | facts | URLs alive | mention the company |", "|---|---|---|---|---|---|"]
    for p, r in out.items():
        lines.append(f"| {p} | - | - | error: {r['error']} | - | - |" if "error" in r else f"| {p} | {r['web']} | {r['secs']} | {r['facts']} | {r['alive']} | {r['mention_company']} |")
    return out, "\n".join(lines)


def run_all(n=10):
    _, a = bench_triage(n=n)
    _, b = bench_research()
    path = os.path.join(OUT, f"bench-{today()}.md")
    open(path, "w", encoding="utf-8").write(a + "\n\n" + b + "\n")
    log(f"benchmark written to {path}")
    return path, a + "\n\n" + b
