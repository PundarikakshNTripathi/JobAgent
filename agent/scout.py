"""Claude web scout: finds postings that have no API -- X/Twitter hiring posts, lab career pages that block bots
(Google/DeepMind/Meta/Microsoft/Apple/Mistral), AI-safety boards (80,000 Hours, aisafety.com), fellowship programs --
and discovers NEW companies worth adding. Uses only public web search/fetch (no logins)."""
import hashlib, json, os
from common import db, log, now, run_claude, extract_json, SETTINGS, OUT, budget_add
import scoring

SCOUT_PROMPT = """You are a job scout for an India-based final-year undergrad (graduates Aug 2027; one ML internship; strong in CUDA/C++ kernels, low-bit inference, agent security, causal inference).
Target roles: (1) AI/ML research & engineering, applied science, data science; (2) low-level systems, ML systems, inference engineering; (3) software engineering; (4) AI safety research/engineering (alignment, interpretability, evals, security).
Both internships and full-time/new-grad; lenient on years of experience. Work authorization: India only; relocation welcome if the employer sponsors a visa. Remote/hybrid/on-site all fine.
Today is {today}. Use WebSearch and WebFetch (budget: at most ~25 calls). Only report postings you actually saw with a working URL, dated within roughly the last 45 days or clearly still open.

SEARCH THESE AREAS
A. X/Twitter public posts and threads announcing hiring: e.g. site:x.com "we're hiring" ("research engineer" OR "inference" OR "kernel" OR "ML systems") (intern OR "new grad" OR residency) 2027; also founders/researchers posting "DM me" openings. Report the post URL and the application link if present.
B. Careers pages that block scrapers: Google DeepMind / Google Research student researcher & India roles, Microsoft Research India, Meta (India / research intern), Apple ML, Mistral AI (Paris/London interns), IBM Research India, Adobe Research India, Samsung Research India, Qualcomm AI Research.
C. AI-safety ecosystem: 80,000 Hours job board (jobs.80000hours.org), aisafety.com/jobs, fellowships & residencies (Anthropic Fellows, MATS, SPAR, ERA, LASR, Astra, Pivotal, Constellation), UK AISI / AI safety institutes, METR/Apollo/Redwood/FAR/Goodfire/Transluce/CAIS postings.
D. Underrated: Hacker News 'Who is hiring', Lobsters, university-style research-engineer residencies, open-source-company jobs (vLLM, SGLang, PyTorch/Meta, Hugging Face, Modular, Mojo, Triton, LLVM/MLIR companies), recently funded (last 90 days) AI-infra/inference/safety startups that are hiring juniors.
E. NEW COMPANIES: names of 5-10 recently funded or fast-growing AI labs / inference / ML-systems / safety startups NOT in this list of already-tracked companies: {tracked}. For each give the careers URL.

Return ONLY JSON:
{{"jobs":[{{"company":"","title":"","url":"","location":"","posted":"YYYY-MM-DD or ''","source":"x|careers|safety-board|hn|other","visa_signal":"yes|no|unknown","evidence":"one line: what you saw"}}],
"new_companies":[{{"name":"","why":"","careers_url":"","ats_guess":"greenhouse|lever|ashby|workable|custom|unknown","slug_guess":""}}],
"notes":"anything the user should know (e.g. a page blocked you)"}}
"""


def run_scout():
    from common import COMPANIES
    c = db()
    prompt = SCOUT_PROMPT.format(today=now()[:10], tracked=", ".join(x["name"] for x in COMPANIES))
    text, _ = run_claude(prompt, allowed_tools="WebSearch,WebFetch", model=SETTINGS["claude_model_packets"], timeout=900, max_turns=40)
    d = extract_json(text)
    n_new = 0
    ts = now()
    for j in d.get("jobs", []):
        if not j.get("url") or not j.get("title"):
            continue
        jid = "scout:" + hashlib.md5(j["url"].encode()).hexdigest()[:14]
        job = {"id": jid, "company": j.get("company", "?"), "title": j["title"], "location": j.get("location", ""), "url": j["url"], "ats": "scout",
               "posted": j.get("posted", ""), "desc": f"Found via {j.get('source')}: {j.get('evidence','')}. Sponsorship signal: {j.get('visa_signal','unknown')}.", "_tier": "B"}
        s = scoring.score(job)
        if s["drop"] and s["family"] is None:
            s.update(score=50.0, family="ai_research")  # trust the scout's relevance judgement for odd titles
        if c.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
            continue
        c.execute("""INSERT INTO jobs(id,company,tier,category,title,location,url,ats,posted,first_seen,last_seen,score,loc_class,family,early,yoe,desc,visa)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (jid, job["company"], "B", "scout", job["title"], job["location"], job["url"], "scout", job["posted"], ts, ts, max(s["score"], 58.0),
                   s["loc_class"], s["family"] or "ai_research", s["early"], s["yoe"], job["desc"], j.get("visa_signal", "unknown")))
        n_new += 1
    c.commit()
    nc = d.get("new_companies", [])
    if nc:
        path = os.path.join(OUT, "new_companies_to_review.md")
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n## Scout {ts}\n" + "\n".join(f"- **{x.get('name')}**: {x.get('why')} | {x.get('careers_url')} | ats={x.get('ats_guess')} slug={x.get('slug_guess')}" for x in nc) + "\n")
            if d.get("notes"):
                f.write(f"\nNotes: {d['notes']}\n")
    budget_add(c, "scout")
    log(f"scout: {n_new} new postings, {len(nc)} new companies suggested")
    return n_new, nc
