"""poll -> score -> store -> alert -> LLM triage."""
import json, re
import feeds, scoring, memory
from common import (db, log, now, notify, meta_get, meta_set, run_claude, extract_json, SETTINGS, PROFILE,
                    budget_add, budget_used)


# ------------------------------------------------------------------ poll
AGGREGATORS = ("speedyapply", "getro", "hnwh", "simplify", "github", "newgrad", "remoteok", "wwr", "manual")


def dedupe():
    """The same posting often arrives twice (an aggregator copy of an ATS posting, or one role posted under several requisition ids with the same text).
    Keep one per company+title+location: the one you worked on, else the direct ATS posting, else the fully-read one; hide the rest (active=0, status
    'duplicate'). Different texts under the same title are kept (different teams). Returns how many were hidden."""
    c = db()
    norm = lambda s: re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()
    groups = {}
    for r in c.execute("""SELECT j.id, j.company, j.title, j.location, j.desc, COALESCE(j.jd_full,0) jf, j.score,
                                 (EXISTS(SELECT 1 FROM picks p WHERE p.job_id=j.id) OR EXISTS(SELECT 1 FROM packets k WHERE k.job_id=j.id)
                                  OR EXISTS(SELECT 1 FROM referrals f WHERE f.job_id=j.id) OR j.status IN ('applied','ready','watch')) mine
                          FROM jobs j WHERE j.active=1""").fetchall():
        groups.setdefault((norm(r["company"]), norm(r["title"]), norm(r["location"])[:24]), []).append(r)
    hidden = 0
    for rows in groups.values():
        if len(rows) < 2:
            continue
        agg = lambda r: r["id"].split(":")[0].startswith(AGGREGATORS)  # e.g. speedyapply-ai, speedyapply-swe
        rows.sort(key=lambda r: (-r["mine"], agg(r), -r["jf"], -(r["score"] or 0)))
        keep = rows[0]
        kd = norm(keep["desc"])[:600]
        for r in rows[1:]:
            if r["mine"]:
                continue
            if agg(r) or not kd or norm(r["desc"])[:600] == kd:
                c.execute("UPDATE jobs SET active=0, status='duplicate' WHERE id=?", (r["id"],))
                hidden += 1
    c.commit()
    if hidden:
        log(f"dedupe: hid {hidden} duplicate postings")
    return hidden


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
    try:
        dedupe()
        live = {r[0] for r in c.execute("SELECT id FROM jobs WHERE active=1")}
        new_ids = [i for i in new_ids if i in live]  # no alerts or triage for duplicate copies
    except Exception as e:
        log(f"dedupe failed: {e}")
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
{persona}

Stance: the candidate needs an offer soon and is playing a numbers game on normal-stakes roles, so be generous: prefer APPLY over STRETCH when a realistic shot exists, and STRETCH over SKIP when only a soft bar blocks them.
READ the requirements: if the posting requires 2 or more years of professional experience (look for 'N+ years', 'minimum N years', 'at least N years'), the verdict is SKIP unless it explicitly accepts equivalent project/research experience; always state the years required in yoe_gap.
For each posting return a verdict:
 APPLY   = realistic shot or strong upside; the requirements are plausibly met or the YOE bar is soft.
 STRETCH = worth a referral-led attempt only (hard bar but not impossible).
 SKIP    = wrong level, wrong skill area, hard requirement not met (PhD required, 4+ yrs, clearance, citizenship), or location impossible.

Return ONLY a JSON array, one object per posting, in this exact schema:
[{{"id":"<id>","fit":0-100,"verdict":"APPLY|STRETCH|SKIP","resume":"<one of the variants>","yoe_gap":"<short: what experience/degree bar vs candidate>","reason":"<one or two sentences, concrete, name the matching project/skill or the blocker>","needs_referral":true|false,"flags":["visa","phd-required","onsite-US","deadline","closing-soon","no-description"]}}]

POSTINGS
{postings}
"""


def _excerpt(desc, n):
    """Intro + the requirements/qualifications section, because years of experience live there."""
    d = desc or ""
    if len(d) <= n:
        return d
    m = re.search(r"(minimum qualifications|basic qualifications|requirements|qualifications|what you.ll need|what we.re looking for|you have|you bring|who you are)", d[300:], re.I)
    head = d[:min(500, n // 3)]
    return head + " ... " + (d[300 + m.start():300 + m.start() + (n - len(head) - 5)] if m else d[len(head):n])


def _fmt(r):
    return (f"id={r['id']}\ncompany={r['company']} (tier {r['tier']}, {r['category']})\ntitle={r['title']}\nlocation={r['location']}\n"
            f"url={r['url']}\nposted={r['posted']}\nheuristic_family={r['family']} early_career={bool(r['early'])} min_yoe_seen={r['yoe']}\n"
            f"description_excerpt={_excerpt(r['desc'] or '(none in feed; judge by title/company)', SETTINGS.get('triage_desc_chars', 1600))}\n")


def triage_prompt(batch):
    """The triage prompt for a batch of job rows (also used by the local-model agreement benchmark)."""
    mem = memory.memory_text(1200)
    return TRIAGE_PROMPT.format(
        summary=PROFILE["summary"], education=PROFILE["education"], projects=" | ".join(PROFILE["projects"][:4]),
        experience=" | ".join(PROFILE["experience"]), achievements=" | ".join(PROFILE["achievements"]),
        variants=json.dumps(PROFILE["resume_variant_guidance"]), persona=memory.persona() + ('\nLEARNED MEMORY (apply it): ' + mem if mem else ''),
        postings="\n---\n".join(_fmt(r) for r in batch))


def _second_opinion(res, by_id):
    """Safety net for local triage: a SKIP on a job the rules score highly gets one look from the main hosted model (few per run), so a small
    model never silently throws away a good opportunity. Its verdict replaces the local one."""
    lc = SETTINGS["llm"].get("local", {})
    floor, cap = lc.get("second_opinion_min_score", 66), lc.get("second_opinion_max", 6)
    doubt = [o for o in res if (o.get("verdict") or "").upper() == "SKIP" and o.get("id") in by_id and (by_id[o["id"]]["score"] or 0) >= floor][:cap]
    if not doubt:
        return res
    main = next((p for p in SETTINGS["llm"].get("fallbacks", []) if p != "local"), None)
    if not main:
        return res
    try:
        import llm
        text, _ = llm.run(triage_prompt([by_id[o["id"]] for o in doubt]), kind="triage", provider=main, timeout=SETTINGS["triage_timeout_s"], max_turns=2)
        better = {o.get("id"): o for o in extract_json(text) if isinstance(o, dict)}
    except Exception as e:
        log(f"triage second opinion skipped: {e}")
        return res
    changed = sum(1 for o in doubt if (better.get(o["id"], {}).get("verdict") or "SKIP").upper() != "SKIP")
    log(f"triage: {main} re-checked {len(doubt)} local SKIPs on high-scoring jobs, {changed} changed to pursue")
    return [better.get(o.get("id"), o) for o in res]


def triage(max_jobs=None):
    c = db()
    limit = max_jobs or SETTINGS["max_triage_per_cycle"]
    try:  # read the full postings first (HTTP, no tokens) and drop the ones the candidate does not qualify for before any model call
        import jd
        jd.prefetch(limit)
    except Exception as e:
        log(f"jd prefetch failed: {e}")
    rows = c.execute("""SELECT * FROM jobs j WHERE active=1 AND score>=? AND status NOT IN ('dismissed','ineligible','closed') AND NOT EXISTS (SELECT 1 FROM triage t WHERE t.job_id=j.id)
                        ORDER BY COALESCE(jd_full,0) DESC, score DESC LIMIT ?""", (SETTINGS["min_heuristic_score_for_llm"], limit)).fetchall()
    if not rows:
        log("triage: nothing pending")
        return []
    done = []
    bs = SETTINGS["triage_batch_size"]
    for i in range(0, len(rows), bs):
        batch = rows[i:i + bs]
        prompt = triage_prompt(batch)
        try:
            text, raw = run_claude(prompt, kind="triage", timeout=SETTINGS["triage_timeout_s"], max_turns=2)
            res = extract_json(text)
            budget_add(c, "triage_calls")
        except Exception as e:
            log(f"triage batch failed: {e}")
            continue
        by_id = {r["id"]: r for r in batch}
        if raw.get("provider") == "local" and isinstance(res, list):
            res = _second_opinion(res, by_id)
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
