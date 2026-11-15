"""High-stakes watchlist. Prestigious / low-odds-without-a-referral roles (frontier-lab fellowships, residencies, pre-docs, safety programs)
are never auto-applied. They are tracked, a draft packet is prepared, and the daily 'Today' page nudges referral outreach and skill-building."""
import re
from common import db, now, SETTINGS, log

HS = SETTINGS.get("high_stakes", {})


def is_high_stakes(job):
    """job: sqlite Row/dict with company, title, category, tier."""
    title = (job["title"] or "").lower()
    if re.search("|".join(HS.get("title_regex", ["^$"])), title) and (job["tier"] in ("S", "A") or job["category"] in HS.get("categories", [])):
        return "program/research role at a top-tier organisation"
    if job["company"] in HS.get("extra_companies", []) and job["tier"] == "S" and job["early"]:
        return "top-tier lab; referral strongly advised before applying"
    return ""


def add_watch(job_id, reason, next_step="Get in touch with an employee first (see referral plan), then decide whether to apply", deadline="", note=""):
    c = db()
    c.execute("INSERT OR IGNORE INTO watch(job_id,added,reason,next_step,deadline,note) VALUES(?,?,?,?,?,?)", (job_id, now(), reason, next_step, deadline, note))
    c.execute("UPDATE jobs SET status='watch' WHERE id=?", (job_id,))
    c.commit()
    log(f"watchlist: {job_id} ({reason})")


def promote_triaged():
    """After triage: move APPLY/STRETCH high-stakes jobs onto the watchlist."""
    c = db()
    n = 0
    # independent of the (cheap, conservative) LLM verdict: prestige roles are tracked even when odds look low, unless clearly ineligible (PhD-only etc.)
    for r in c.execute("""SELECT j.* FROM jobs j LEFT JOIN triage t ON t.job_id=j.id
                          WHERE j.active=1 AND j.score>=60 AND j.status IN ('new','triaged','skipped')
                            AND (t.job_id IS NULL OR t.verdict!='SKIP' OR (t.flags NOT LIKE '%phd%' AND t.flags NOT LIKE '%clearance%'))
                            AND COALESCE(j.req,'') NOT LIKE '%"ok": false%'""").fetchall():  # the full-JD eligibility gate already ruled it out
        why = is_high_stakes(r)
        if why:
            add_watch(r["id"], why)
            n += 1
    return n
