"""JobAgent CLI.   python agent/run.py <command> [args]

 RUN        cycle                  poll all sources -> alert -> triage -> scout -> packets -> referral plans -> fill forms -> reflect -> dashboard
            poll | triage | scout  individual stages (poll costs no tokens)
 JOBS       top [n] | watch | status | usage | today [--force]
            packet <id> | referral <id> | resume <id> | fill <id>      (fill never submits)
            mark <id> applied|skip|dismissed [reason words]            (reasons teach the agent)
 YOU FIND   inbox                 process pending inbox items;  inbox add "<text or link>" ["<your context>"]
            chat "<message>"      talk to the agent;  agent "<goal>"   let it work a multi-step goal with tools
 SETUP      init                  create your workspace + profile (interactive)      doctor   check everything
            providers [test <name>]   list/test LLM providers      bench [n]   benchmark providers on your data
            gui [--no-open]       local web GUI                       yc-profile  rewrite your YC profile (truthful)
 SCHEDULE   install-schedule | remove-schedule | enable-schedule | disable-schedule | set-interval <minutes>
 MEMORY     reflect               update the agent's memory + scoring tweaks from your outcomes
Your personal data lives in the workspace (see `doctor`), never in this repository."""
import datetime as dt, os, subprocess, sys, time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common  # noqa: E402
from common import db, log, notify, SETTINGS, CORE, DATA, HOME, budget_used, meta_get, meta_set, now, record_outcome  # noqa: E402

LOCK = os.path.join(DATA, "cycle.lock")
TASK = "JobAgentCycle"


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
    import pipeline, packets, referrals, fill, scout, dashboard, stakes, today, inbox, memory
    if common.needs_setup():
        log("no profile yet: run `python agent/run.py init` (or open the GUI setup)")
        return
    if not acquire_lock():
        return
    try:
        c = db()
        deadline = time.time() + SETTINGS.get("cycle_minutes_budget", 25) * 60  # heavy model work is time-boxed so polling stays fresh; the rest resumes next cycle
        inbox.process_pending()
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
        built = packets.run_packets(SETTINGS.get("packets_per_cycle", 4))
        built += packets.run_watch_drafts(2)
        # referral plans for EVERY good match (APPLY/STRETCH), watchlist first; LinkedIn browsing is capped separately inside plan_referral
        left = SETTINGS.get("max_referrals_per_day", 12) - budget_used(c, "referrals")
        if left > 0:
            for r in c.execute("""SELECT j.id FROM jobs j JOIN triage t ON t.job_id=j.id
                                  WHERE j.active=1 AND t.verdict IN ('APPLY','STRETCH') AND j.status NOT IN ('skipped','applied','ineligible','dismissed')
                                    AND NOT EXISTS (SELECT 1 FROM referrals r WHERE r.job_id=j.id) AND NOT EXISTS (SELECT 1 FROM packets p WHERE p.job_id=j.id AND p.referral_at IS NOT NULL)
                                  ORDER BY (j.status='watch') DESC, t.fit DESC LIMIT ?""", (min(left, SETTINGS.get("referrals_per_cycle", 6)),)).fetchall():
                if time.time() > deadline:
                    break
                try:
                    referrals.plan_referral(r["id"])
                except Exception as ex:
                    log(f"referral failed {r['id']}: {ex}")
        filled = []
        if SETTINGS["auto_fill"] and in_fill_window():
            room = SETTINGS["max_fills_per_day"] - budget_used(c, "fills")
            for r in c.execute("""SELECT j.id FROM jobs j JOIN packets p ON p.job_id=j.id JOIN triage t ON t.job_id=j.id
                                  WHERE p.filled_at IS NULL AND j.status='packet' AND t.verdict='APPLY' ORDER BY t.fit DESC LIMIT ?""", (max(room, 0),)).fetchall():
                if time.time() > deadline + 10 * 60:
                    break
                try:
                    fill.fill_job(r["id"])
                    filled.append(r["id"])
                except Exception as ex:
                    log(f"fill failed {r['id']}: {ex}")
        try:
            memory.reflect()
        except Exception as ex:
            log(f"reflect failed: {ex}")
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
        rows = c.execute(f"SELECT COALESCE(provider,'claude') p, kind, COUNT(*) n, SUM(inp+cache_create+cache_read) tin, SUM(outp) tout, SUM(cost) cost, SUM(secs) secs FROM llm_log WHERE {q} GROUP BY p, kind ORDER BY p, cost DESC").fetchall()
        print(f"-- model usage, {label} (cost = list-price equivalent; subscriptions are rate-limited, not billed per token)")
        for r in rows:
            print(f"   {r['p']:10} {r['kind']:10} calls={r['n']:3} in={r['tin'] or 0:>9} out={r['tout'] or 0:>7} ~${r['cost'] or 0:.3f} {int(r['secs'] or 0)}s")


def status():
    c = db()
    print("workspace:", HOME)
    print("last poll:", meta_get(c, "last_poll"), "| last cycle:", meta_get(c, "last_cycle"))
    print("jobs:", c.execute("SELECT COUNT(*) FROM jobs WHERE active=1").fetchone()[0], "active;", c.execute("SELECT COUNT(*) FROM jobs WHERE score>=58").fetchone()[0], "pass filter")
    for s, n in c.execute("SELECT status, COUNT(*) FROM jobs GROUP BY status").fetchall():
        print(f"  {s}: {n}")
    for kind in ("triage_calls", "packets", "referrals", "fills", "scout", "linkedin"):
        print(f"  today {kind}: {budget_used(c, kind)}")


def top(n=25):
    c = db()
    for r in c.execute("""SELECT j.id,j.company,j.title,substr(j.location,1,28) loc,j.score,j.loc_class,j.visa,t.verdict,t.fit FROM jobs j LEFT JOIN triage t ON t.job_id=j.id
                          WHERE j.active=1 ORDER BY COALESCE(t.fit,j.score) DESC LIMIT ?""", (n,)).fetchall():
        print(f"{(r['verdict'] or '-'):8} {int(r['fit'] or r['score']):3} {r['company'][:22]:22} {r['title'][:52]:52} {r['loc']:28} {r['loc_class']:8} visa:{r['visa']:7} {r['id']}")


def _schtasks(*a):
    r = subprocess.run(["schtasks", *a], capture_output=True, text=True)
    return (r.stdout or r.stderr).strip()


def schedule(install=True, minutes=None):
    if os.name != "nt":
        print("Scheduling: add a cron entry running `python agent/run.py cycle` (Windows Task Scheduler is used on Windows).")
        return
    if install:
        py = sys.executable.replace("python.exe", "pythonw.exe")
        py = py if os.path.exists(py) else sys.executable
        cmd = f'"{py}" "{os.path.join(CORE, "agent", "run.py")}" cycle'
        print(_schtasks("/Create", "/F", "/SC", "MINUTE", "/MO", str(minutes or SETTINGS["poll_every_minutes"]), "/TN", TASK, "/TR", cmd))
    else:
        print(_schtasks("/Delete", "/F", "/TN", TASK))


def doctor():
    import llm
    print("workspace:", HOME, "(personal data, never committed)")
    print("profile  :", "OK " + common.PROFILE.get("name", "") if not common.needs_setup() else "MISSING: run `init`")
    for n, p in llm.registry().items():
        ok, why = llm.available(n)
        print(f"provider {n:10} {'ready' if ok else 'not ready':9} {'' if ok else why}  {'(verified adapter)' if p.get('verified') else '(adapter follows vendor docs; test it)'}")
    print("routing  :", {k: v.get("provider") + ":" + (v.get("model") or "default") for k, v in SETTINGS["llm"]["routing"].items()})
    chrome = [p for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe") if os.path.exists(p)]
    print("browser  :", chrome[0] if chrome else "no Chrome/Edge found (needed only for form filling / LinkedIn)")
    print("schedule :", _schtasks("/Query", "/TN", TASK, "/FO", "LIST").splitlines()[:6] if os.name == "nt" else "n/a")


def main():
    a = sys.argv[1:]
    cmd = a[0] if a else "status"
    if cmd == "cycle":
        cycle()
    elif cmd == "poll":
        import pipeline
        ids, first = pipeline.poll()
        pipeline.instant_alerts(ids, first)
        print(len(ids), "new postings pass the filter")
    elif cmd == "triage":
        import pipeline
        pipeline.triage()
    elif cmd == "packet":
        import packets
        print(packets.build_packet(a[1]))
    elif cmd in ("referral", "linkedin"):
        import referrals
        print(referrals.plan_referral(a[1]))
    elif cmd == "resume":
        import resume
        print(resume.advise(a[1]))
    elif cmd == "fill":
        import fill
        print(fill.fill_job(a[1]))
    elif cmd == "scout":
        import scout
        scout.run_scout(force=True)
    elif cmd == "inbox":
        import inbox
        if len(a) > 1 and a[1] == "add":
            i = inbox.add(a[2], a[3] if len(a) > 3 else "")
            print("queued", i)
        inbox.process_pending()
    elif cmd == "chat":
        import inbox
        ans, done = inbox.reply(" ".join(a[1:]))
        print(ans, "\n", done or "")
    elif cmd == "agent":
        import agentic
        print(agentic.loop(" ".join(a[1:])))
    elif cmd == "reflect":
        import memory
        print(memory.reflect(force=True))
    elif cmd == "bench":
        import bench
        print(bench.run_all(int(a[1]) if len(a) > 1 else 10)[1])
    elif cmd == "providers":
        import llm
        if len(a) > 2 and a[1] == "test":
            print(llm.test(a[2]))
        else:
            doctor()
    elif cmd == "init":
        import setup
        setup.wizard()
    elif cmd == "doctor":
        doctor()
    elif cmd == "gui":
        import gui
        gui.serve(open_browser="--no-open" not in a)
    elif cmd == "today":
        import today
        print(today.build(force="--force" in a)[0])
    elif cmd == "dashboard":
        import dashboard
        print(dashboard.build())
    elif cmd == "usage":
        usage()
    elif cmd == "yc-profile":
        import yc_profile
        print(yc_profile.run())
    elif cmd == "watch":
        c = db()
        for r in c.execute("SELECT j.company,j.title,w.referral_state,w.deadline FROM watch w JOIN jobs j ON j.id=w.job_id").fetchall():
            print(f"{r['company'][:24]:24} {r['title'][:56]:56} referral:{r['referral_state']:12} {r['deadline'] or ''}")
    elif cmd == "status":
        status()
    elif cmd == "top":
        top(int(a[1]) if len(a) > 1 else 25)
    elif cmd == "mark":
        c = db()
        st = {"skip": "skipped"}.get(a[2], a[2])
        c.execute("UPDATE jobs SET status=? WHERE id=?", (st, a[1]))
        c.commit()
        record_outcome(a[1], st, " ".join(a[3:]))
        print("ok")
    elif cmd == "notify-test":
        notify("JobAgent test", "If you can read this, notifications work.")
    elif cmd == "install-schedule":
        schedule(True)
    elif cmd == "remove-schedule":
        schedule(False)
    elif cmd == "enable-schedule":
        print(_schtasks("/Change", "/TN", TASK, "/ENABLE"))
    elif cmd == "disable-schedule":
        print(_schtasks("/Change", "/TN", TASK, "/DISABLE"))
    elif cmd == "set-interval":
        common.save_override("settings.json", {"poll_every_minutes": int(a[1])})
        schedule(True, int(a[1]))
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
