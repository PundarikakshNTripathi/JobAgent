"""poll -> score -> store -> alert -> LLM triage."""
import json, re
import feeds, scoring
from common import (db, log, now, notify, meta_get, meta_set, run_claude, extract_json, SETTINGS, PROFILE,
                    budget_add, budget_used)


# ------------------------------------------------------------------ poll
def poll():
    """Fetch every source, upsert jobs, return list of ids that are new AND pass the heuristic bar."""
    c = db()
    first_run = meta_get(c, "baseline_done") is None
    jobs, health = feeds.fetch_all()
    log("feed health: " + json.dumps({k: v for k, v in health.items()}, default=str)[:1500])
    bad = {k: v for k, v in health.items() if isinstance(v, str)}
    seen_ids, new_ids = set(), []
    ts = now()
    key = lambda co, ti: re.sub(r"[^a-z0-9]", "", re.sub(r"\(yc [a-z]\d+\)", "", co.lower())) + "|" + re.sub(r"[^a-z0-9]", "", ti.lower())
    first_ats = {}  # same posting often appears on the company ATS, a VC board and a GitHub list: keep the first (ATS jobs come first)
    for r in c.execute("SELECT company, title, ats FROM jobs WHERE active=1").fetchall():
        first_ats.setdefault(key(r["company"], r["title"]), r["ats"])
    for j in jobs:
        if not j["title"] or not j["url"] or j["id"] in seen_ids:
            continue
        k = key(j["company"], j["title"])
        if first_ats.setdefault(k, j["ats"]) != j["ats"]:
            continue
        seen_ids.add(j["id"])
        s = scoring.score(j)
        if s["drop"]:
            continue
        row = c.execute("SELECT id FROM jobs WHERE id=?", (j["id"],)).fetchone()
        if row:
            c.execute("UPDATE jobs SET last_seen=?, active=1, score=?, loc_class=?, family=?, early=?, yoe=?, visa=? WHERE id=?",
                      (ts, s["score"], s["loc_class"], s["family"], s["early"], s["yoe"], s["visa"], j["id"]))
        else:
            c.execute("""INSERT INTO jobs(id,company,tier,category,title,location,url,ats,posted,first_seen,last_seen,score,loc_class,family,early,yoe,desc,baseline,visa)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (j["id"], j["company"], j.get("_tier", "C"), j.get("_cat", ""), j["title"], j["location"], j["url"], j["ats"], j["posted"],
                       ts, ts, s["score"], s["loc_class"], s["family"], s["early"], s["yoe"], j["desc"][:6000], 1 if first_run else 0, s["visa"]))
            if s["score"] >= SETTINGS["min_heuristic_score_for_llm"]:
                new_ids.append(j["id"])
    # mark disappeared jobs only for sources that answered (avoid mass-deactivating on an outage)
    ok_companies = {k for k, v in health.items() if not isinstance(v, str)}
    for r in c.execute("SELECT id, company, last_seen FROM jobs WHERE active=1").fetchall():
        if r["id"] not in seen_ids and r["company"] in ok_companies and r["last_seen"] < ts:
            c.execute("UPDATE jobs SET active=0 WHERE id=?", (r["id"],))
    c.commit()
    if first_run:
        meta_set(c, "baseline_done", ts)
        log(f"baseline run: {len(seen_ids)} postings seen, {len(new_ids)} pass the bar (backlog goes to triage, no instant alerts)")
    meta_set(c, "last_poll", ts)
    if bad:
        log(f"WARNING failing sources: {list(bad)[:12]}")
    return new_ids, first_run


def instant_alerts(new_ids, first_run):
    """Brand-new high-scoring postings: tell the user right away (before any LLM work)."""
    if first_run or not new_ids:
        return
    c = db()
    q = ",".join("?" * len(new_ids))
    rows = c.execute(f"SELECT * FROM jobs WHERE id IN ({q}) AND score>=? AND alerted=0 ORDER BY score DESC",
                     (*new_ids, SETTINGS["instant_alert_score"])).fetchall()
    if not rows:
        return
    top = rows[:4]
    body = "; ".join(f"{r['company']}: {r['title']} ({r['location'][:30]})" for r in top)
    notify(f"{len(rows)} new strong role(s) just went live", body)
    for r in rows:
        c.execute("UPDATE jobs SET alerted=1 WHERE id=?", (r["id"],))
    c.commit()


# ------------------------------------------------------------------ triage
TRIAGE_PROMPT = """You are triaging job postings for ONE candidate. Be honest and calibrated; do not flatter.

CANDIDATE
{summary}
Education: {education}
Projects (all real, measured): {projects}
Experience: {experience}
Achievements: {achievements}
Resume variants available: {variants}
Track priority (highest first): ML systems/inference/kernels > AI/ML engineer, data scientist, applied scientist > software engineering > AI/ML research > AI safety. Available from late Dec 2026/Jan 2027 for any role type; all work modes OK.
Visa reality: US/Canada/UK internships normally need existing right to work or student status there (CPT) or an employer-sponsored J-1; big tech rarely recruits India-based interns into those. Mark such roles APPLY only if the posting or company clearly sponsors/relocates international candidates, otherwise STRETCH with flag 'visa'. Roles in China are not realistic (SKIP). India, remote-from-India and Singapore/Europe roles with sponsorship are the realistic core.
Constraints: lives in India; prefers roles open to India-based candidates or remote-global; will relocate for strong opportunities (visa friction is real: US/UK/EU on-site is harder than India/Singapore/remote). Final-year undergrad (graduates Aug 2027), one internship, no publications, non-top-tier university. Wants lenient YOE bars: internships, new-grad, residencies, fellowships, and 'early-career friendly' roles in AI research/engineering, ML systems and inference, software engineering, and AI safety at strong employers.

Stance: the candidate needs an offer soon and is playing a numbers game on normal-stakes roles, so be generous: prefer APPLY over STRETCH when a realistic shot exists, and STRETCH over SKIP when only a soft bar blocks them.
For each posting return a verdict:
 APPLY   = realistic shot or strong upside; the requirements are plausibly met or the YOE bar is soft.
 STRETCH = worth a referral-led attempt only (hard bar but not impossible).
 SKIP    = wrong level, wrong skill area, hard requirement not met (PhD required, 4+ yrs, clearance, citizenship), or location impossible.

Return ONLY a JSON array, one object per posting, in this exact schema:
[{{"id":"<id>","fit":0-100,"verdict":"APPLY|STRETCH|SKIP","resume":"<one of the variants>","yoe_gap":"<short: what experience/degree bar vs candidate>","reason":"<one or two sentences, concrete, name the matching project/skill or the blocker>","needs_referral":true|false,"flags":["visa","phd-required","onsite-US","deadline","closing-soon","no-description"]}}]

POSTINGS
{postings}
"""


def _fmt(r):
    return (f"id={r['id']}\ncompany={r['company']} (tier {r['tier']}, {r['category']})\ntitle={r['title']}\nlocation={r['location']}\n"
            f"url={r['url']}\nposted={r['posted']}\nheuristic_family={r['family']} early_career={bool(r['early'])} min_yoe_seen={r['yoe']}\n"
            f"description_excerpt={(r['desc'] or '(none in feed; judge by title/company)')[:SETTINGS.get('triage_desc_chars', 900)]}\n")


def triage(max_jobs=None):
    c = db()
    limit = max_jobs or SETTINGS["max_triage_per_cycle"]
    rows = c.execute("""SELECT * FROM jobs j WHERE active=1 AND score>=? AND NOT EXISTS (SELECT 1 FROM triage t WHERE t.job_id=j.id)
                        ORDER BY score DESC LIMIT ?""", (SETTINGS["min_heuristic_score_for_llm"], limit)).fetchall()
    if not rows:
        log("triage: nothing pending")
        return []
    done = []
    bs = SETTINGS["triage_batch_size"]
    for i in range(0, len(rows), bs):
        batch = rows[i:i + bs]
        prompt = TRIAGE_PROMPT.format(
            summary=PROFILE["summary"], education=PROFILE["education"], projects=" | ".join(PROFILE["projects"][:4]),
            experience=" | ".join(PROFILE["experience"]), achievements=" | ".join(PROFILE["achievements"]),
            variants=json.dumps(PROFILE["resume_variant_guidance"]), postings="\n---\n".join(_fmt(r) for r in batch))
        try:
            text, _ = run_claude(prompt, kind="triage", model=SETTINGS["claude_model"], timeout=SETTINGS["triage_timeout_s"], max_turns=2)
            res = extract_json(text)
            budget_add(c, "triage_calls")
        except Exception as e:
            log(f"triage batch failed: {e}")
            continue
        by_id = {r["id"]: r for r in batch}
        for o in res if isinstance(res, list) else []:
            jid = o.get("id")
            if jid not in by_id:
                continue
            resume = o.get("resume") if o.get("resume") in PROFILE["resume_variants"] else "AI_Engineer"
            verdict = o.get("verdict", "SKIP").upper()
            c.execute("""INSERT OR REPLACE INTO triage(job_id,fit,verdict,resume,yoe_gap,reason,needs_referral,flags,at) VALUES(?,?,?,?,?,?,?,?,?)""",
                      (jid, float(o.get("fit", 0)), verdict, resume, o.get("yoe_gap", ""), o.get("reason", ""),
                       1 if o.get("needs_referral") else 0, json.dumps(o.get("flags", [])), now()))
            c.execute("UPDATE jobs SET status=? WHERE id=?", ("triaged" if verdict != "SKIP" else "skipped", jid))
            done.append(jid)
        c.commit()
    import stakes
    w = stakes.promote_triaged()
    log(f"triage: {len(done)} of {len(rows)} judged; {w} moved to high-stakes watchlist")
    return done
