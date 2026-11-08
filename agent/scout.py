"""Claude web scout (public web only, no logins). Finds what no API exposes:
 - X/Twitter hiring posts, bot-blocking career pages (Google/DeepMind/Meta/Microsoft/Apple/Mistral/IBM)
 - pre-doctoral programs, research residencies, fellowships (corporate, neolab, academic institutes, research orgs, AI-safety programs)
 - newly funded companies worth tracking.
Runs every `scout_every_days` days (token economy). Programs go to the high-stakes watchlist: tracked and reminded, never auto-applied."""
import hashlib, os, datetime as dt
from common import db, log, now, run_claude, extract_json, SETTINGS, OUT, budget_add, meta_get, meta_set, COMPANIES
import scoring, stakes, memory

SCOUT_PROMPT = """Job scout for the candidate below.
{persona}
Interns, new-grad, pre-docs, residencies, fellowships all welcome.
Today is {today}. Use WebSearch/WebFetch, at most ~20 calls total. Report only things you actually saw with a working URL and a current or upcoming window.

A. ROLES: X/Twitter hiring posts/threads (site:x.com "we're hiring" research engineer / inference / kernel / ML systems intern or new grad 2027); careers pages that block scrapers: Google/DeepMind/Google Research India, Microsoft Research India, Meta, Apple ML, Mistral, IBM Research India, Adobe/Samsung/Qualcomm research India.
B. PROGRAMS (pre-doctoral, research residency, fellowship, research internship): Google DeepMind pre-doc and student researcher; Meta FAIR pre-doc/residency; Microsoft Research (fellow, AI residency, MSR India research fellow); Apple/NVIDIA research residencies; OpenAI residency; neolab residencies/fellowships; Anthropic Fellows; Cohere Labs scholars; AI2 predoc; academic institutes and research orgs relevant to the candidate's region and field; AI-safety programs (MATS, SPAR, ERA, LASR, Astra, Pivotal, Constellation, UK AISI). For each: next opening/deadline, whether an Indian undergrad graduating 2027 is eligible, whether remote/visa-supported.
D. LOGISTICS-CLEAR OPPORTUNITIES ABROAD: roles or programs that state visa sponsorship, relocation support, funded stipends, invitation/visa letters or international eligibility, at neolabs, startups, frontier labs, research and academic institutes, big tech (e.g. funded summer research programs such as DAAD RISE, Mitacs Globalink, ETH/EPFL summer research, EMBL/CERN/MPI programs, Google Summer of Code, Outreachy, LFX; companies that explicitly sponsor). Prefer these over abroad roles with unclear logistics; for every abroad item say how clear the visa/relocation logistics are.
C. NEW COMPANIES: up to 6 recently funded AI-infra/inference/ML-systems/safety startups not already tracked: {tracked}.

Return ONLY JSON:
{{"jobs":[{{"company":"","title":"","url":"","location":"","posted":"YYYY-MM-DD or ''","source":"x|careers|other","visa_signal":"yes|no|unknown","logistics":"visa/relocation/funding clarity in one short phrase, or unclear","evidence":"one line"}}],
"programs":[{{"name":"","org":"","kind":"predoc|residency|fellowship|internship","url":"","window":"deadline / opening, with year","eligibility":"yes|unclear|no + one short reason","notes":"remote/visa/stipend if known"}}],
"new_companies":[{{"name":"","why":"","careers_url":"","ats_guess":"greenhouse|lever|ashby|workable|custom|unknown","slug_guess":""}}],"notes":""}}
"""


def due():
    c = db()
    last = meta_get(c, "last_scout")
    if not last:
        return True
    return (dt.datetime.now() - dt.datetime.fromisoformat(last)).days >= SETTINGS.get("scout_every_days", 3)


def run_scout(force=False):
    c = db()
    if not force and not due():
        return 0, []
    prompt = SCOUT_PROMPT.format(persona=memory.persona(), today=now()[:10], tracked=", ".join(x["name"] for x in COMPANIES[:60]))
    text, _ = run_claude(prompt, kind="scout", allowed_tools="WebSearch,WebFetch", timeout=900, max_turns=30)
    open(os.path.join(OUT, "scout_last_raw.txt"), "w", encoding="utf-8").write(text)  # never lose a paid result to a downstream bug
    d = extract_json(text)
    ts, n_new = now(), 0
    for j in d.get("jobs", []):
        if not j.get("url") or not j.get("title"):
            continue
        jid = "scout:" + hashlib.md5(j["url"].encode()).hexdigest()[:14]
        if c.execute("SELECT 1 FROM jobs WHERE id=?", (jid,)).fetchone():
            continue
        norm = lambda s: "".join(ch for ch in (s or "").lower() if ch.isalnum())
        if any(norm(r["title"]) == norm(j["title"]) and norm(r["company"])[:6] == norm(j.get("company"))[:6]
               for r in c.execute("SELECT company, title FROM jobs WHERE company LIKE ?", ((j.get("company") or "?")[:6] + "%",)).fetchall()):
            continue  # already tracked via a company feed
        job = {"id": jid, "company": j.get("company", "?"), "title": j["title"], "location": j.get("location", ""), "url": j["url"], "ats": "scout", "posted": j.get("posted", ""),
               "desc": f"Found via {j.get('source')}: {j.get('evidence','')}. Sponsorship signal: {j.get('visa_signal','unknown')}.", "_tier": "B"}
        s = scoring.score(job)
        if s["drop"] and s["family"] is None:
            s.update(score=55.0, family="ai_research")
        c.execute("""INSERT INTO jobs(id,company,tier,category,title,location,url,ats,posted,first_seen,last_seen,score,loc_class,family,early,yoe,desc,visa)
                     VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                  (jid, job["company"], "B", "scout", job["title"], job["location"], job["url"], "scout", job["posted"], ts, ts, max(s["score"], 60.0), s["loc_class"],
                   s["family"] or "ai_research", s["early"], s["yoe"], job["desc"], j.get("visa_signal", "unknown")))
        n_new += 1
    # programs: stored, and each also becomes a watchlist job row so it gets reminders / drafts
    for p in d.get("programs", []):
        if not p.get("url") or not p.get("name"):
            continue
        pid = "prog:" + hashlib.md5(p["url"].encode()).hexdigest()[:14]
        if c.execute("SELECT 1 FROM programs WHERE id=?", (pid,)).fetchone():
            c.execute("UPDATE programs SET window=?, eligibility=?, notes=? WHERE id=?", (p.get("window", ""), p.get("eligibility", ""), p.get("notes", ""), pid))
            continue
        c.execute("INSERT INTO programs(id,name,org,kind,url,window,eligibility,notes,added) VALUES(?,?,?,?,?,?,?,?,?)",
                  (pid, p["name"], p.get("org", ""), p.get("kind", ""), p["url"], p.get("window", ""), p.get("eligibility", ""), p.get("notes", ""), ts))
        if not str(p.get("eligibility", "")).lower().startswith("no"):
            c.execute("""INSERT OR IGNORE INTO jobs(id,company,tier,category,title,location,url,ats,posted,first_seen,last_seen,score,loc_class,family,early,visa,desc,status)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'new')""",
                      (pid, p.get("org") or p["name"], "S", "program", p["name"], "see program", p["url"], "program", "", ts, ts, 75.0, "remote", "ai_research", 1, "unknown",
                       f"{p.get('kind')}: window {p.get('window')}. Eligibility: {p.get('eligibility')}. {p.get('notes','')}"))
            c.commit()
            stakes.add_watch(pid, f"{p.get('kind','program')} (prestigious)", deadline=p.get("window", ""), note=p.get("eligibility", ""))
    c.commit()
    nc = d.get("new_companies", [])
    if nc:
        with open(os.path.join(OUT, "new_companies_to_review.md"), "a", encoding="utf-8") as f:
            f.write(f"\n## Scout {ts}\n" + "\n".join(f"- **{x.get('name')}**: {x.get('why')} | {x.get('careers_url')} | ats={x.get('ats_guess')} slug={x.get('slug_guess')}" for x in nc) + "\n")
    meta_set(c, "last_scout", ts)
    budget_add(c, "scout")
    log(f"scout: {n_new} postings, {len(d.get('programs', []))} programs, {len(nc)} new companies")
    return n_new, nc
