"""JobAgent CLI.   python agent/run.py <command> [args]

  cycle               poll all sources -> alert -> triage -> packets -> referral plans -> fill forms -> dashboard (what the scheduler runs)
  poll                fetch + score only (no Claude), instant alerts for strong new roles
  triage              Claude judges the best untriaged postings
  packet <id>         build cover letter / answers / resume choice for a job
  referral <id>       research connections + draft outreach for a job
  fill <id>           fill the application in the agent Chrome (never submits)
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
import pipeline, packets, referrals, fill, scout, dashboard  # noqa: E402

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
    if not acquire_lock():
        return
    try:
        c = db()
        new_ids, first = pipeline.poll()
        pipeline.instant_alerts(new_ids, first)
        # LLM work, each capped per day to protect the subscription
        if budget_used(c, "triage_calls") < SETTINGS["max_triage_calls_per_day"]:
            pipeline.triage()
        if budget_used(c, "scout") < SETTINGS.get("scout_per_day", 1) and dt.datetime.now().hour >= 7:
            try:
                scout.run_scout()
                pipeline.triage(10)
            except Exception as ex:
                log(f"scout failed: {ex}")
        built = packets.run_packets()
        # referral plans for the strongest new packets
        left = SETTINGS.get("max_referrals_per_day", 4) - budget_used(c, "referrals")
        if left > 0:
            for r in c.execute("""SELECT j.id FROM jobs j JOIN packets p ON p.job_id=j.id JOIN triage t ON t.job_id=j.id
                                  WHERE p.referral_at IS NULL AND (t.needs_referral=1 OR j.tier IN ('S','A')) ORDER BY t.fit DESC LIMIT ?""", (left,)).fetchall():
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
        if built and not filled:
            notify(f"{len(built)} application packet(s) prepared", f"Open {path}. Cover letters, answers and referral plans are ready.")
        meta_set(c, "last_cycle", now())
        log(f"cycle done: packets={len(built)} fills={len(filled)} dashboard={path}")
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass


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
        scout.run_scout()
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
