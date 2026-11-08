"""Free visa-clarity enrichment: for promising jobs abroad whose sponsorship is not stated in the feed text, fetch the full posting page and run the
sponsorship detector on it (HTTP only, no model tokens). Also rescore everything after the scoring rules change."""
import re
from common import db, log, SETTINGS
import scoring, web

MARK = " [visa-checked]"


def enrich(limit=40):
    """Fetch the FULL posting for promising jobs whose feed text is thin (HTTP only, no model tokens): store it, then re-read years of experience and
    visa/sponsorship from the real text. Jobs that turn out to need 3+ years are dismissed automatically."""
    c = db()
    rows = c.execute("""SELECT id, url, title, location, desc, ats, tier, early FROM jobs WHERE active=1 AND score>=50 AND url!='' AND status IN ('new','triaged')
                        AND LENGTH(COALESCE(desc,''))<2500 AND COALESCE(desc,'') NOT LIKE ? ORDER BY score DESC LIMIT ?""", ("%" + MARK + "%", limit)).fetchall()
    n = dismissed = 0
    for r in rows:
        title, body = web.fetch_text(r["url"], 14000)
        ok = body and not body.startswith("(fetch failed") and len(body) > 600
        if ok:
            desc = ((r["desc"] or "")[:400] + " " + body)[:9000]
            s = scoring.score({"title": r["title"], "location": r["location"], "desc": desc, "ats": r["ats"], "_tier": r["tier"]})
            if s["drop"] and "yrs" in str(s["drop"]):
                c.execute("UPDATE jobs SET status='dismissed', yoe=?, desc=? WHERE id=?", (s["yoe"], desc + f" [auto-dismissed: {s['drop']}]" + MARK, r["id"]))
                dismissed += 1
            else:
                c.execute("UPDATE jobs SET desc=?, score=?, loc_class=?, family=?, early=?, yoe=?, visa=? WHERE id=?",
                          (desc + MARK, s["score"], s["loc_class"], s["family"], s["early"], s["yoe"], s["visa"], r["id"]))
        else:
            c.execute("UPDATE jobs SET desc=COALESCE(desc,'') || ? WHERE id=?", (MARK, r["id"]))  # don't retry dead pages every cycle
        n += 1
    c.commit()
    if rows:
        log(f"JD enrichment: read {n} full postings, {dismissed} dismissed for needing 3+ years")
    return n, dismissed


def rescore_all():
    """Recompute heuristic scores for every stored job from its stored fields (after rules/weights change or enrichment)."""
    c = db()
    n = dismissed = 0
    for r in c.execute("SELECT id, title, location, desc, ats, tier, category, status FROM jobs WHERE active=1").fetchall():
        s = scoring.score({"title": r["title"], "location": r["location"], "desc": r["desc"] or "", "ats": r["ats"], "_tier": r["tier"]})
        if s["drop"]:
            if "yrs" in str(s["drop"]) and r["status"] in ("new", "triaged", "watch"):  # needs more experience than the candidate has: remove from every list
                c.execute("UPDATE jobs SET status='dismissed', yoe=? WHERE id=?", (s["yoe"], r["id"]))
                c.execute("DELETE FROM watch WHERE job_id=?", (r["id"],))
                dismissed += 1
            continue
        c.execute("UPDATE jobs SET score=?, loc_class=?, family=?, early=?, yoe=?, visa=? WHERE id=?", (s["score"], s["loc_class"], s["family"], s["early"], s["yoe"], s["visa"], r["id"]))
        n += 1
    c.commit()
    if dismissed:
        log(f"rescore: dismissed {dismissed} postings that need more experience than the candidate has")
    return n
