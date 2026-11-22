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
    rows = c.execute("""SELECT j.id, j.jd_full FROM jobs j WHERE j.active=1 AND j.closed_at IS NULL AND (
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
        st = c.execute("SELECT status, active, closed_at FROM jobs WHERE id=?", (r["id"],)).fetchone()
        if st and (not st["active"] or st["closed_at"]):
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


def regate(use_browser=True, limit=None, deadline=None):
    """Re-check every job you might act on (APPLY/STRETCH matches, picks, packets, watchlist) against its full description with the current gate:
    years of experience by lower bound, degree, seniority, visa. Unreadable postings become 'JD unread' (never APPLY). Jobs that now fail are
    marked ineligible (your applied/interview statuses are never touched; files are kept)."""
    import jd
    c = db()
    worked = ",".join("'%s'" % s for s in common.USER_STATUS)
    rows = c.execute(f"""SELECT j.id, COALESCE(j.jd_full,0) jf FROM jobs j LEFT JOIN triage t ON t.job_id=j.id
                         WHERE (j.active=1 OR j.closed_at IS NULL) AND j.status NOT IN ({worked},'ineligible','dismissed','skipped','duplicate','closed')
                           AND (t.verdict IN ('APPLY','STRETCH') OR j.id IN (SELECT job_id FROM picks) OR j.id IN (SELECT job_id FROM packets) OR j.status='watch')
                         ORDER BY (j.id IN (SELECT job_id FROM picks)) DESC, COALESCE(t.fit, j.score) DESC""").fetchall()
    if limit:
        rows = rows[:limit]
    out = {"checked": 0, "ineligible": 0, "visa_unclear": 0, "unread": 0, "ok": 0}
    for r in rows:
        if deadline and time.time() > deadline:
            break
        res = jd.check(r["id"], use_browser=use_browser and not r["jf"], refresh=False)
        out["checked"] += 1
        if not res["jd_read"]:
            out["unread"] += 1
        elif not res["ok"]:
            if common.set_status(c, r["id"], "ineligible"):
                c.execute("DELETE FROM watch WHERE job_id=?", (r["id"],))
                c.commit()
                out["ineligible"] += 1
        elif res.get("visa") == "unclear":
            out["visa_unclear"] += 1
        else:
            out["ok"] += 1
    log("regate: " + json.dumps(out))
    return out


def quick_gate_all():
    """No fetching: mark visa-unclear / visa-blocked / unread from what is stored, for every APPLY/STRETCH match (cheap, every run)."""
    import jd
    c = db()
    n = 0
    for r in c.execute("SELECT t.job_id FROM triage t JOIN jobs j ON j.id=t.job_id WHERE t.verdict IN ('APPLY','STRETCH') AND j.active=1 AND j.checked IS NULL").fetchall():
        jd.quick_gate(r["job_id"])
        n += 1
    return n


GHOST_AGE_DAYS = 60
_VAGUE_NEED = re.compile(r"\b(require|qualif|you have|you will|responsib|what you|experience with|we're looking|we are looking|about you|must|should)", re.I)


def ghost_reasons(row, repost=False):
    """Why a posting may be a ghost job (not really hiring): very old, reposted under a new id, or too vague to be a real role. '' = looks real."""
    why = []
    try:
        if row["posted"] and row["posted_src"] in ("feed", "age"):
            age = (dt.date.today() - dt.date.fromisoformat(row["posted"][:10])).days
            if age > GHOST_AGE_DAYS:
                why.append(f"posted {age} days ago")
    except ValueError:
        pass
    if repost:
        why.append("reposted under a new id (the same role was listed before)")
    d = row["desc"] or ""
    if row["jd_full"] and len(d) < 900 and not _VAGUE_NEED.search(d):
        why.append("description is very short and names no requirements or duties")
    return "; ".join(why)


def flag_ghosts():
    """Mark postings that look stale or fake (shown as 'Possibly stale'; never auto-prepared). Cheap: SQL + regex only."""
    c = db()
    norm = lambda s: re.sub(r"[^a-z0-9]+", "", (s or "").lower())
    earlier = {}
    for r in c.execute("SELECT id, company, title, first_seen FROM jobs ORDER BY first_seen").fetchall():
        earlier.setdefault(norm(r["company"]) + "|" + norm(r["title"]), []).append((r["first_seen"] or "", r["id"]))
    n = 0
    for r in c.execute("SELECT id, company, title, posted, posted_src, desc, jd_full, first_seen, ghost FROM jobs WHERE active=1").fetchall():
        prev = earlier.get(norm(r["company"]) + "|" + norm(r["title"]), [])
        repost = any(fs and fs < (r["first_seen"] or "") and pid != r["id"] and (dt.datetime.fromisoformat(r["first_seen"][:19]) - dt.datetime.fromisoformat(fs[:19])).days >= 30
                     for fs, pid in prev) if r["first_seen"] else False
        g = ghost_reasons(r, repost) or None
        if g != r["ghost"]:
            c.execute("UPDATE jobs SET ghost=? WHERE id=?", (g, r["id"]))
            n += 1
    c.commit()
    return n


def refresh(refresh_plans=True):
    cfg = _cfg()
    deadline = time.time() + cfg["time_box_s"]
    out = recheck_postings(cfg, deadline)
    try:
        out["quick_gated"] = quick_gate_all()
        out["regate"] = regate(use_browser=False, limit=cfg["per_run"], deadline=deadline)
    except Exception as e:
        log(f"regate failed: {e}")
    try:
        out["ghost_flags_changed"] = flag_ghosts()
    except Exception as e:
        log(f"ghost check failed: {e}")
    out["marked_ineligible"] = mark_ineligible()
    out["stale_plans"] = flag_stale_plans(cfg)
    if refresh_plans:
        out["plans_rebuilt"] = len(refresh_stale_plans(cfg, deadline))
        flag_stale_plans(cfg)
    common.meta_set(db(), "last_maintenance", json.dumps(dict(out, at=now())))
    log("maintenance: " + json.dumps(out))
    return out
