"""poll -> score -> store -> alert -> LLM triage."""
import json, re
import feeds, scoring, memory, common
from common import (company_key as common_key, db, log, now, notify, meta_get, meta_set, run_claude, extract_json, SETTINGS, PROFILE,
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


def _company_meta(c, j):
    """Remember the company's own website or official logo when a feed states it (better than guessing a domain for the logo)."""
    site, logo = (j.get("_site") or "").strip(), (j.get("_logo") or "").strip()
    if not (site or logo):
        return
    import urllib.parse
    host = urllib.parse.urlparse(site).netloc.lower().split(":")[0] if site else ""
    host = host[4:] if host.startswith("www.") else host
    k = common_key(j["company"])
    old = c.execute("SELECT domain, logo_url FROM company_meta WHERE k=?", (k,)).fetchone()
    if old and (old["domain"] or "") == host and (old["logo_url"] or "") == logo:
        return
    c.execute("INSERT OR REPLACE INTO company_meta(k,company,domain,logo_url,source,at) VALUES(?,?,?,?,?,?)",
              (k, j["company"], host or (old["domain"] if old else ""), logo or (old["logo_url"] if old else ""), j["ats"], now()))
    try:
        import logos
        logos.forget(j["company"])  # re-resolve with the better information
    except Exception:
        pass


def backfill_posted():
    """Jobs with no date at all get their first-seen date, marked as such (shown as 'first seen', not as a posting date)."""
    c = db()
    n = c.execute("UPDATE jobs SET posted=substr(first_seen,1,10), posted_src='seen' WHERE posted IS NULL OR posted=''").rowcount
    c.execute("UPDATE jobs SET posted_src='feed' WHERE posted_src IS NULL AND posted IS NOT NULL AND posted!=''")
    c.commit()
    return n


SRC_BY_PREFIX = {"speedyapply-ai": "speedyapply", "speedyapply-swe": "speedyapply", "simplify-intern": "simplify-intern", "simplify-newgrad": "simplify-newgrad",
                 "getro": "vc-boards", "yc": "yc", "hnwh": "hn", "hnjob": "hn", "remoteok": "remoteok", "remotive": "remotive", "amazon": "Amazon",
                 "google": "Google (careers site)", "atlassian": "Atlassian", "workable": "Hugging Face"}


def src_key(row):
    """The feed (health key) a stored job comes from: stored since round 5, derived from the id for older rows."""
    if row["src"]:
        return row["src"]
    pre = row["id"].split(":")[0]
    if pre in SRC_BY_PREFIX:
        return SRC_BY_PREFIX[pre]
    if pre == "eightfold":
        return f"{row['company']} (careers site)"
    return row["company"]  # greenhouse / ashby / lever / smartrecruiters / workday: one feed per company


def gone_jobs(c, seen_ids, health, ts):
    """Postings missing from a feed that answered are gone from that board. A feed that suddenly returns far fewer postings than last time
    is treated as an outage, not as mass closure. Jobs you work on are never hidden: they get closed_at (shown as 'posting closed').
    Jobs you added yourself (manual:) are not on any feed and are never touched here."""
    import common
    prev = json.loads(meta_get(c, "feed_counts") or "{}")
    ok = set()
    for k, v in health.items():
        if isinstance(v, str):
            continue
        if v == 0 or (prev.get(k, 0) >= 20 and v < 0.3 * prev[k]):
            log(f"feed {k}: {v} postings (was {prev.get(k, 0)}); not treating missing postings as closed this time")
            continue
        ok.add(k)
    meta_set(c, "feed_counts", json.dumps({k: v for k, v in health.items() if not isinstance(v, str)} | {k: prev[k] for k in prev if isinstance(health.get(k), str)}))
    picked = {r[0] for r in c.execute("SELECT job_id FROM picks UNION SELECT job_id FROM packets UNION SELECT job_id FROM referrals")}
    hid = kept = 0
    for r in c.execute("SELECT id, company, status, src, last_seen, closed_at FROM jobs WHERE active=1").fetchall():
        if r["id"] in seen_ids or r["id"].startswith(("manual:", "prog:", "scout:", "sig:")) or (r["last_seen"] or "") >= ts:
            continue
        if src_key(r) not in ok:
            continue
        if r["status"] in common.WORKED_STATUS or r["id"] in picked:
            if not r["closed_at"]:
                c.execute("UPDATE jobs SET closed_at=? WHERE id=?", (ts, r["id"]))
                kept += 1
        else:
            c.execute("UPDATE jobs SET active=0 WHERE id=?", (r["id"],))
            hid += 1
    if hid or kept:
        log(f"poll: {hid} postings gone from their boards (hidden); {kept} you work on marked 'posting closed' and kept")


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
        seen_ids.add(j["id"])  # seen on its board, even when another source's copy is the one we keep (else it would be marked gone)
        if first_ats.setdefault(k, j["ats"]) != j["ats"]:
            c.execute("UPDATE jobs SET last_seen=?, src=COALESCE(src,?) WHERE id=?", (ts, j.get("_src"), j["id"]))
            continue
        s = scoring.score(j)
        if s["drop"]:
            continue
        _company_meta(c, j)
        posted, psrc = (j.get("posted") or "")[:10], (j.get("_posted_src") or ("feed" if j.get("posted") else ""))
        row = c.execute("SELECT id, posted, posted_src FROM jobs WHERE id=?", (j["id"],)).fetchone()
        if row:
            c.execute("""UPDATE jobs SET last_seen=?, active=CASE WHEN status IN ('closed','duplicate') THEN active ELSE 1 END, closed_at=NULL, src=?,
                         score=?, loc_class=?, family=?, early=?, yoe=?, visa=? WHERE id=?""",
                      (ts, j.get("_src"), s["score"], s["loc_class"], s["family"], s["early"], s["yoe"], s["visa"], j["id"]))
            # a real date beats an age-derived one, which beats 'first seen'; an age-derived date is not moved later by re-polls
            rank = {"feed": 3, "age": 2, "seen": 1, None: 0, "": 0}
            if posted and rank.get(psrc, 0) > rank.get(row["posted_src"], 0):
                c.execute("UPDATE jobs SET posted=?, posted_src=? WHERE id=?", (posted, psrc, j["id"]))
        else:
            c.execute("""INSERT INTO jobs(id,company,tier,category,title,location,url,ats,posted,first_seen,last_seen,score,loc_class,family,early,yoe,desc,baseline,visa)
                         VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                      (j["id"], j["company"], j.get("_tier", "C"), j.get("_cat", ""), j["title"], j["location"], j["url"], j["ats"], posted or ts[:10],
                       ts, ts, s["score"], s["loc_class"], s["family"], s["early"], s["yoe"], j["desc"][:6000], 1 if first_run else 0, s["visa"]))
            c.execute("UPDATE jobs SET posted_src=?, src=? WHERE id=?", (psrc if posted else "seen", j.get("_src"), j["id"]))
            if s["score"] >= SETTINGS["min_heuristic_score_for_llm"]:
                new_ids.append(j["id"])
    gone_jobs(c, seen_ids, health, ts)
    c.commit()
    if first_run:
        meta_set(c, "baseline_done", ts)
        log(f"baseline run: {len(seen_ids)} postings seen, {len(new_ids)} pass the bar (backlog goes to triage, no instant alerts)")
    meta_set(c, "last_poll", ts)
    if bad:
        log(f"WARNING failing sources: {list(bad)[:12]}")
    try:
        backfill_posted()
    except Exception as e:
        log(f"posted backfill failed: {e}")
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

Stance: be strict and honest. APPLY only when the candidate meets the stated requirements today; a weak APPLY wastes the candidate's time and referrals.
READ the requirements: if the posting requires 1 or more years of professional experience (a range counts by its lower bound: '1-3 years' = 1; look for 'N+ years', 'minimum N years', 'at least N years'), the verdict is SKIP unless it explicitly accepts internships/projects as that experience; '0-2 years', 'new grad' and internships are fine. Always state the years required in yoe_gap.
Location and visa: a role outside the candidate's home country is APPLY only if the posting (or the company, well known for it) sponsors visas; if it is silent, STRETCH with flag 'visa'; if it needs an existing work permit or citizenship, SKIP.
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
            verdict = str(o.get("verdict", "SKIP")).upper()
            if verdict not in ("APPLY", "STRETCH", "SKIP"):
                verdict = "SKIP" if not o.get("fit") else ("STRETCH" if float(o.get("fit") or 0) >= 50 else "SKIP")
            try:
                o["fit"] = max(0.0, min(100.0, float(o.get("fit", 0))))
            except (TypeError, ValueError):
                o["fit"] = 0.0
            c.execute("""INSERT OR REPLACE INTO triage(job_id,fit,verdict,resume,yoe_gap,reason,needs_referral,flags,at) VALUES(?,?,?,?,?,?,?,?,?)""",
                      (jid, float(o.get("fit", 0)), verdict, resume, o.get("yoe_gap", ""), o.get("reason", ""),
                       1 if o.get("needs_referral") else 0, json.dumps(o.get("flags", [])), now()))
            common.set_status(c, jid, "triaged" if verdict != "SKIP" else "skipped", commit=False)
            c.commit()
            try:  # the model's verdict cannot bypass the deterministic gate: visa-unclear roles never count as matches
                import jd
                if jd.quick_gate(jid) == "visa-blocked" and verdict != "SKIP":
                    c.execute("UPDATE triage SET verdict='SKIP', reason=reason || ' [gate: needs an existing work permit]' WHERE job_id=?", (jid,))
                    common.set_status(c, jid, "skipped", commit=False)
            except Exception as e:
                log(f"gate after triage failed for {jid}: {e}")
            done.append(jid)
        c.commit()
    import stakes
    w = stakes.promote_triaged()
    log(f"triage: {len(done)} of {len(rows)} judged; {w} moved to high-stakes watchlist")
    return done
