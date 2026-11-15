"""Keep everything already prepared up to date. Runs every cycle (and with `run.py refresh`), cheap first:

1. Re-read postings you are working on (picks, packets, referral plans, watchlist, triaged) whose full description was never read or was read
   more than `maintenance.recheck_days` ago: closed postings are hidden, requirements re-checked (HTTP; no model).
2. Jobs judged on a teaser are sent back for triage once their full description has been read (the verdict is redone on the real text).
3. Prepared jobs that the eligibility gate now rejects are marked ineligible (files are kept).
4. Referral plans are marked stale when they contain 'UNVERIFIED', name nobody with a real profile link, or are older than
   `maintenance.referral_max_age_days`; a few stale plans (your picks first) are rebuilt each run through LinkedIn.
Everything it changes is logged and shown in the GUI (status chips and 'plan outdated')."""
import datetime as dt, json, os, re, time
import common
from common import db, log, now, SETTINGS

UNVERIFIED = re.compile(r"\bunverified\b", re.I)


def _cfg():
    m = SETTINGS.get("maintenance", {})
    return {"recheck_days": m.get("recheck_days", 3), "per_run": m.get("jd_rechecks_per_run", 40), "referral_max_age_days": m.get("referral_max_age_days", 21),
            "referral_refresh_per_run": m.get("referral_refresh_per_run", 6), "time_box_s": m.get("time_box_s", 600)}


def recheck_postings(cfg, deadline):
    import jd
    c = db()
    cutoff = (dt.datetime.now() - dt.timedelta(days=cfg["recheck_days"])).isoformat(timespec="seconds")
    rows = c.execute("""SELECT j.id, j.jd_full FROM jobs j WHERE j.active=1 AND (
                            j.id IN (SELECT job_id FROM picks) OR j.id IN (SELECT job_id FROM packets) OR j.id IN (SELECT job_id FROM referrals)
                            OR j.id IN (SELECT job_id FROM watch) OR j.status IN ('triaged','packet','watch','ready','fill-partial','fill-blocked'))
                          AND (COALESCE(j.jd_full,0)=0 OR j.checked IS NULL OR j.checked < ?)
                        ORDER BY (j.id IN (SELECT job_id FROM picks)) DESC, j.score DESC LIMIT ?""", (cutoff, cfg["per_run"])).fetchall()
    read = closed = retriage = 0
    for r in rows:
        if time.time() > deadline:
            break
        had_full = bool(r["jd_full"])
        res = jd.check(r["id"], use_browser=False, refresh=True)
        read += res["jd_read"]
        st = c.execute("SELECT status, active FROM jobs WHERE id=?", (r["id"],)).fetchone()
        if st and not st["active"]:
            closed += 1
            continue
        if res["jd_read"] and not had_full:
            # judged on a teaser: redo the verdict on the full text (unless you already acted on it)
            n = c.execute("DELETE FROM triage WHERE job_id=? AND job_id NOT IN (SELECT job_id FROM packets) AND job_id NOT IN (SELECT job_id FROM picks)",
                          (r["id"],)).rowcount
            if n:
                c.execute("UPDATE jobs SET status='new' WHERE id=? AND status IN ('triaged','skipped')", (r["id"],))
                retriage += 1
        c.commit()
    return {"rechecked": len(rows), "read": read, "closed": closed, "sent_back_for_triage": retriage}


def mark_ineligible():
    c = db()
    n = c.execute("""UPDATE jobs SET status='ineligible' WHERE active=1 AND req LIKE '%"ok": false%' AND req NOT LIKE '%could not read%'
                     AND status IN ('packet','triaged','watch','ready','fill-partial','fill-blocked')""").rowcount
    c.execute("DELETE FROM watch WHERE job_id IN (SELECT id FROM jobs WHERE status='ineligible')")
    c.commit()
    return n


def stale_reason(path, at, max_age_days):
    try:
        with open(path, encoding="utf-8") as f:
            t = f.read()
    except OSError:
        return "missing file"
    if UNVERIFIED.search(t):
        return "contains UNVERIFIED claims"
    if "linkedin.com/in/" not in t and not re.search(r"^Profile: https?://", t, re.M):
        return "names nobody with a profile link"
    try:
        if dt.datetime.fromisoformat(at) < dt.datetime.now() - dt.timedelta(days=max_age_days):
            return f"older than {max_age_days} days"
    except (TypeError, ValueError):
        pass
    return ""


def flag_stale_plans(cfg):
    c = db()
    n = 0
    for r in c.execute("SELECT job_id, dir, at FROM referrals").fetchall():
        why = stale_reason(os.path.join(r["dir"] or "", "REFERRAL.md"), r["at"], cfg["referral_max_age_days"])
        c.execute("UPDATE referrals SET stale=? WHERE job_id=?", (why or None, r["job_id"]))
        n += bool(why)
    c.commit()
    return n


def refresh_stale_plans(cfg, deadline):
    import referrals
    c = db()
    rows = c.execute("""SELECT r.job_id FROM referrals r JOIN jobs j ON j.id=r.job_id WHERE r.stale IS NOT NULL AND j.active=1
                        AND j.status NOT IN ('ineligible','dismissed','skipped','applied','closed','duplicate')
                        ORDER BY (r.job_id IN (SELECT job_id FROM picks)) DESC, j.score DESC LIMIT ?""", (cfg["referral_refresh_per_run"],)).fetchall()
    done = []
    for r in rows:
        if time.time() > deadline:
            break
        try:
            referrals.plan_referral(r["job_id"], manual=False)
            done.append(r["job_id"])
        except Exception as e:  # gate refusals, login walls, quota: leave it flagged and say why
            log(f"maintenance: referral refresh skipped for {r['job_id']}: {str(e)[:160]}")
    return done


def refresh(refresh_plans=True):
    cfg = _cfg()
    deadline = time.time() + cfg["time_box_s"]
    out = recheck_postings(cfg, deadline)
    out["marked_ineligible"] = mark_ineligible()
    out["stale_plans"] = flag_stale_plans(cfg)
    if refresh_plans:
        out["plans_rebuilt"] = len(refresh_stale_plans(cfg, deadline))
        flag_stale_plans(cfg)
    common.meta_set(db(), "last_maintenance", json.dumps(dict(out, at=now())))
    log("maintenance: " + json.dumps(out))
    return out
