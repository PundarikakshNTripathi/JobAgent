"""JobAgent CLI.   python agent/run.py <command> [args]

  cycle               poll all sources -> alert -> triage -> packets -> referral plans -> fill forms -> dashboard (what the scheduler runs)
  poll                fetch + score only (no Claude), instant alerts for strong new roles
  triage              Claude judges the best untriaged postings
  packet <id>         build cover letter / answers / resume choice for a job
  referral <id>       research connections + draft outreach for a job
  fill <id>           fill the application in the agent Chrome (never submits)
  today [--force]     build out/today.md (improvement tasks, outreach, watchlist) + usage   usage   Claude token usage
  watch | linkedin <id> | yc-profile | enable-schedule | disable-schedule
  scout               Claude web scout (X, blocked career pages, safety boards, new companies)
  status | dashboard  summary / regenerate out/dashboard.html
  mark <id> applied|skip|dismissed
  top [n]             print best current matches
  notify-test         send a test notification
  install-schedule    create the Windows Task Scheduler jobs      remove-schedule   delete them
"""
import datetime as dt, os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import (db, log, notify, SETTINGS, ROOT, DATA, budget_used, meta_get, meta_set, now)  # noqa: E402
import pipeline, packets, referrals, fill, scout, dashboard, stakes, today  # noqa: E402

LOCK = os.path.join(DATA, "cycle.lock")


def acquire_lock():
    if os.path.exists(LOCK) and time.time() - os.path.getmtime(LOCK) < 100 * 60:
        log("another cycle is running; exiting")
        return False
    open(LOCK, "w").write(str(os.getpid()))
    return True


def in_fill_window():
    lo, hi = SETTINGS["fill_window_local_hours"]
    return lo <= dt.datetime.now().hour < hi


def cycle():
    """Economy cycle: polling is free; Claude steps are capped per day (see settings.json)."""
    if not acquire_lock():
        return
    try:
        c = db()
        new_ids, first = pipeline.poll()
        pipeline.instant_alerts(new_ids, first)
        stakes.promote_triaged()
        if budget_used(c, "triage_calls") < SETTINGS["max_triage_calls_per_day"]:
            pipeline.triage()
        if dt.datetime.now().hour >= 7 and scout.due():
            try:
                scout.run_scout()
            except Exception as ex:
                log(f"scout failed: {ex}")
        built = packets.run_packets()
        built += packets.run_watch_drafts(1)
        left = SETTINGS.get("max_referrals_per_day", 1) - budget_used(c, "referrals")
        if left > 0:
            # watchlist first (referral-first strategy), then normal packets
            for r in c.execute("""SELECT p.job_id id FROM packets p JOIN jobs j ON j.id=p.job_id LEFT JOIN triage t ON t.job_id=p.job_id
                                  WHERE p.referral_at IS NULL AND j.status IN ('watch','packet') ORDER BY (j.status='watch') DESC, COALESCE(t.fit,0) DESC LIMIT ?""", (left,)).fetchall():
                try:
                    referrals.plan_referral(r["id"])
                except Exception as ex:
                    log(f"referral failed {r['id']}: {ex}")
        filled = []
        if SETTINGS["auto_fill"] and in_fill_window():
            room = SETTINGS["max_fills_per_day"] - budget_used(c, "fills")
            for r in c.execute("""SELECT j.id FROM jobs j JOIN packets p ON p.job_id=j.id JOIN triage t ON t.job_id=j.id
                                  WHERE p.filled_at IS NULL AND j.status='packet' AND t.verdict='APPLY' ORDER BY t.fit DESC LIMIT ?""", (max(room, 0),)).fetchall():
                try:
                    fill.fill_job(r["id"])
                    filled.append(r["id"])
                except Exception as ex:
                    log(f"fill failed {r['id']}: {ex}")
        path = dashboard.build()
        if dt.datetime.now().hour >= 7:
            try:
                today.morning_toast()
            except Exception as ex:
                log(f"today failed: {ex}")
        meta_set(c, "last_cycle", now())
        log(f"cycle done: packets={len(built)} fills={len(filled)} dashboard={path}")
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


def usage():
    c = db()
    for label, q in (("today", "ts>=date('now','localtime')"), ("7 days", "ts>=date('now','-7 day')")):
        rows = c.execute(f"SELECT kind, COUNT(*) n, SUM(inp+cache_create+cache_read) tin, SUM(outp) tout, SUM(cost) cost FROM llm_log WHERE {q} GROUP BY kind ORDER BY cost DESC").fetchall()
        print(f"-- Claude usage, {label} (cost = list-price equivalent; your Pro plan is rate-limited, not billed per token)")
        for r in rows:
            print(f"   {r['kind']:12} calls={r['n']:3} in={r['tin']:>8} out={r['tout']:>7} ~${r['cost']:.3f}")


def status():
    c = db()
    print("last poll:", meta_get(c, "last_poll"), "| last cycle:", meta_get(c, "last_cycle"))
    print("jobs:", c.execute("SELECT COUNT(*) FROM jobs WHERE active=1").fetchone()[0], "active;", c.execute("SELECT COUNT(*) FROM jobs WHERE score>=58").fetchone()[0], "pass filter")
    for s, n in c.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall():
        print(f"  {s}: {n}")
    for kind in ("triage_calls", "packets", "referrals", "fills", "scout"):
        print(f"  today {kind}: {budget_used(c, kind)}")


def top(n=25):
    c = db()
    for r in c.execute("""SELECT j.id,j.company,j.title,substr(j.location,1,28) loc,j.score,j.loc_class,j.visa,t.verdict,t.fit FROM jobs j LEFT JOIN triage t ON t.job_id=j.id
                          WHERE j.active=1 ORDER BY COALESCE(t.fit,j.score) DESC LIMIT ?""", (n,)).fetchall():
        print(f"{(r['verdict'] or '-'):8} {int(r['fit'] or r['score']):3} {r['company'][:22]:22} {r['title'][:52]:52} {r['loc']:28} {r['loc_class']:8} visa:{r['visa']:7} {r['id']}")


def schedule(install=True):
    py = sys.executable.replace("python.exe", "pythonw.exe")
    if not os.path.exists(py):
        py = sys.executable
    script = os.path.join(ROOT, "agent", "run.py")
    tasks = [("JobAgentCycle", f"{SETTINGS['poll_every_minutes']}", f'"{py}" "{script}" cycle')]
    for name, mins, cmd in tasks:
        if install:
            r = subprocess.run(["schtasks", "/Create", "/F", "/SC", "MINUTE", "/MO", mins, "/TN", name, "/TR", cmd], capture_output=True, text=True)
        else:
            r = subprocess.run(["schtasks", "/Delete", "/F", "/TN", name], capture_output=True, text=True)
        print(name, "->", (r.stdout or r.stderr).strip())


def main():
    a = sys.argv[1:]
    cmd = a[0] if a else "status"
    if cmd == "cycle":
        cycle()
    elif cmd == "poll":
        ids, first = pipeline.poll()
        pipeline.instant_alerts(ids, first)
        print(len(ids), "new postings pass the filter")
    elif cmd == "triage":
        pipeline.triage()
    elif cmd == "packet":
        print(packets.build_packet(a[1]))
    elif cmd == "referral":
        print(referrals.plan_referral(a[1]))
    elif cmd == "fill":
        print(fill.fill_job(a[1]))
    elif cmd == "scout":
        scout.run_scout(force=True)
    elif cmd == "today":
        print(today.build(force="--force" in a)[0])
    elif cmd == "usage":
        usage()
    elif cmd == "linkedin":
        print(referrals.plan_referral(a[1]))
    elif cmd == "yc-profile":
        import yc_profile
        print(yc_profile.run())
    elif cmd == "watch":
        c = db()
        for r in c.execute("SELECT j.company,j.title,w.referral_state,w.deadline FROM watch w JOIN jobs j ON j.id=w.job_id").fetchall():
            print(f"{r['company'][:24]:24} {r['title'][:56]:56} referral:{r['referral_state']:12} {r['deadline'] or ''}")
    elif cmd == "enable-schedule":
        print(subprocess.run(["schtasks", "/Change", "/TN", "JobAgentCycle", "/ENABLE"], capture_output=True, text=True).stdout)
    elif cmd == "disable-schedule":
        print(subprocess.run(["schtasks", "/Change", "/TN", "JobAgentCycle", "/DISABLE"], capture_output=True, text=True).stdout)
    elif cmd == "dashboard":
        print(dashboard.build())
    elif cmd == "status":
        status()
    elif cmd == "top":
        top(int(a[1]) if len(a) > 1 else 25)
    elif cmd == "mark":
        c = db()
        c.execute("UPDATE jobs SET status=? WHERE id=?", ({"skip": "skipped"}.get(a[2], a[2]), a[1]))
        c.commit()
        print("ok")
    elif cmd == "notify-test":
        notify("JobAgent test", "If you can read this, notifications work.")
    elif cmd == "install-schedule":
        schedule(True)
    elif cmd == "remove-schedule":
        schedule(False)
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
