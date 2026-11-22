"""JobAgent: Find the role. Reach the people. You press Submit.
    python agent/run.py <command> [args]      (or: jobagent <command>)

 RUN        cycle                  poll all sources -> alert -> triage -> scout -> packets -> referral plans -> fill forms -> reflect -> dashboard
            poll | triage | scout  individual stages (poll costs no tokens)
 JOBS       top [n] | watch | status | usage | today [--force]
            packet <id> | referral <id> | linkedin <id> | resume <id> | fill <id>   (fill never submits; linkedin = read-only scouting)
            add --force to prepare a job the eligibility gate rejected;  audit   re-check picks/packets against their full JDs
            picks                 add earlier hand-prepared jobs to My picks
            selftest [provider]   run every task's real prompt on one provider and check the output (default: local)
            refresh [--no-plans]  re-read postings you work on, re-triage teasers, rebuild stale referral plans
            mark <id> applied|skip|dismissed [reason words]            (reasons teach the agent)
 YOU FIND   inbox                 process pending inbox items;  inbox add "<text or link>" ["<your context>"]
            chat "<message>"      talk to the agent;  agent "<goal>"   let it work a multi-step goal with tools
 SETUP      init                  create your workspace + profile (interactive)      doctor   check everything
            providers [test <name>]   list/test LLM providers      bench [n]   benchmark providers on your data
            app / shortcuts       desktop window / Start-menu entry
            rebuild <id> [packet,referral,resume|all]   redo one job's material with your notes for it
            routing [list|use <preset>]   model presets: recommended, low-cost, hosted, local-only
            signals [collect|queue|list]   fresh hiring posts + seed rounds -> today's 20 direct-outreach targets
            gui [--no-open]       local web GUI                       yc-profile  rewrite your YC profile (truthful)
 SCHEDULE   install-schedule | remove-schedule | enable-schedule | disable-schedule | set-times HH:MM [HH:MM]   (default 09:00 18:00)
 FIND MORE  discover              LinkedIn Jobs (24 h), Work at a Startup, X (programs, competitions, hackathons), boards of funded startups
            events [strategy <id>]   conference deadlines, competitions, hackathons;  programs [strategy <id>]   research internships & fellowships
 AFTER      reply <id> "<their reply>"   classify an employer reply, record it, next step;  interview <id>   prep plan;  stories [--force]   your story bank
            mark <id> replied|screen|interview|offer|rejected|ghosted    funnel   conversion by channel;  github   propose project notes from GitHub
 CV         cv <id>               tailored one-page PDF resume: your verified bullets chosen and ordered for the job (never rewritten), compiled, one page enforced
 RESEARCH   research <company> [--force]   read the company's site, LinkedIn page and news; show the facts and every page read
            sponsor <id>          settle a 'Visa unclear' job: research whether the employer sponsors visas
 GATE       regate [--no-browser]  re-check every match/pick/packet against its full posting (years, degree, seniority, visa)
 PAUSE      pause [all|local|api|subscription] | resume [..]   stop all AI use (local model/GPU, API keys, subscriptions) until you resume
 MEMORY     reflect               update the agent's memory + scoring tweaks from your outcomes
Your personal data lives in the workspace (see `doctor`), never in this repository."""
import datetime as dt, json, os, re, subprocess, sys, time

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
    import pipeline, packets, referrals, fill, scout, dashboard, stakes, today, inbox, memory, jd
    if common.needs_setup():
        log("no profile yet: run `python agent/run.py init` (or open the GUI setup)")
        return
    import pause
    if pause.state()["all"]:
        log("cycle skipped: JobAgent is paused (" + pause.describe() + "); press Resume to run again")
        return
    if not acquire_lock():
        return
    common.BACKGROUND = True  # scheduled work must leave quota headroom for the user's own requests
    try:
        c = db()
        deadline = time.time() + SETTINGS.get("cycle_minutes_budget", 25) * 60  # heavy model work is time-boxed so polling stays fresh; the rest resumes next cycle
        stage = lambda name: meta_set(c, "cycle_stage", f"{now()}|{name}")  # shown live on the GUI's Run-pipeline button
        stage("Inbox")
        inbox.process_pending()
        stage("Polling job boards")
        new_ids, first = pipeline.poll()
        pipeline.instant_alerts(new_ids, first)
        stage("Catching fresh hiring posts and seed rounds")
        try:
            import signals
            signals.run_all()
        except Exception as ex:
            log(f"signals failed: {ex}")
        stage("Finding more opportunities")
        try:
            import discover
            discover.run_all()
        except Exception as ex:
            log(f"discovery failed: {ex}")
        try:  # weekly: what changed on your GitHub becomes proposals in About me (you accept or reject them)
            import github_sync
            github_sync.sync()
        except Exception as ex:
            log(f"github sync failed: {ex}")
        stage("Refreshing what you are working on")
        try:
            import maintenance
            maintenance.refresh()
        except Exception as ex:
            log(f"maintenance failed: {ex}")
        stage("Reading full job descriptions")
        try:
            import visa
            visa.enrich(SETTINGS.get("visa_checks_per_cycle", 40))
        except Exception as ex:
            log(f"visa enrichment failed: {ex}")
        stakes.promote_triaged()
        if budget_used(c, "triage_calls") < SETTINGS["max_triage_calls_per_day"]:
            stage("Judging new matches")
            pipeline.triage()
        if dt.datetime.now().hour >= 7 and scout.due():
            try:
                scout.run_scout()
            except Exception as ex:
                log(f"scout failed: {ex}")
        stage("Preparing applications")
        built = packets.run_packets(SETTINGS.get("packets_per_cycle", 4))
        built += packets.run_watch_drafts(2)
        # referral plans for EVERY good match (APPLY/STRETCH), watchlist first; LinkedIn browsing is capped separately inside plan_referral
        stage("Planning referrals")
        left = SETTINGS.get("max_referrals_per_day", 12) - budget_used(c, "referrals")
        if left > 0:
            for r in c.execute("""SELECT j.id FROM jobs j JOIN triage t ON t.job_id=j.id
                                  WHERE j.active=1 AND t.verdict IN ('APPLY','STRETCH') AND j.status NOT IN ('skipped','applied','ineligible','dismissed') AND COALESCE(j.gate,'')='' AND j.ghost IS NULL
                                    AND NOT EXISTS (SELECT 1 FROM referrals r WHERE r.job_id=j.id) AND NOT EXISTS (SELECT 1 FROM packets p WHERE p.job_id=j.id AND p.referral_at IS NOT NULL)
                                  ORDER BY (j.status='watch') DESC, t.fit DESC LIMIT ?""", (min(left, SETTINGS.get("referrals_per_cycle", 6)),)).fetchall():
                if time.time() > deadline:
                    break
                try:
                    referrals.plan_referral(r["id"])
                except jd.GateError as ex:
                    packets._mark_ineligible(r["id"], str(ex))
                except Exception as ex:
                    log(f"referral failed {r['id']}: {ex}")
        filled = []
        stage("Filling forms")
        if SETTINGS["auto_fill"] and in_fill_window():
            room = SETTINGS["max_fills_per_day"] - budget_used(c, "fills")
            for r in c.execute("""SELECT j.id FROM jobs j JOIN packets p ON p.job_id=j.id JOIN triage t ON t.job_id=j.id
                                  WHERE p.filled_at IS NULL AND j.status='packet' AND t.verdict='APPLY' ORDER BY t.fit DESC LIMIT ?""", (max(room, 0),)).fetchall():
                if time.time() > deadline + 10 * 60:
                    break
                try:
                    fill.fill_job(r["id"])
                    filled.append(r["id"])
                except jd.GateError as ex:
                    packets._mark_ineligible(r["id"], str(ex))
                except Exception as ex:
                    log(f"fill failed {r['id']}: {ex}")
        stage("Learning from outcomes")
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
        stage("done")
        log(f"cycle done: packets={len(built)} fills={len(filled)} dashboard={path}")
    finally:
        try:
            os.remove(LOCK)
        except OSError:
            pass
        try:  # give the GPU back after a scheduled run (the model reloads in ~20 s next time); keep it with llm.local.keep_running
            import local_ai
            if local_ai.STARTED and not SETTINGS["llm"].get("local", {}).get("keep_running", False):
                log("local AI: " + local_ai.stop())
        except Exception:
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


def local_setup(local_ai, assume_yes=False):
    hw = local_ai.detect()
    print(f"This computer: {hw['os']} {hw['arch']}, GPU: {hw['gpu'] or 'none'} ({hw['vram_gb']} GB), RAM {hw['ram_gb']} GB, free disk {hw['disk_free_gb']} GB")
    rec = local_ai.recommend(hw)
    if not rec:
        print("No local model fits well here; keep using hosted models. (Needs about 6 GB of VRAM, Apple Silicon with 16 GB, or 16 GB RAM for a slow CPU model.)")
        return
    print(f"Recommended: {rec['label']}, about {rec['size_gb']} GB download: {rec['why']}.")
    print("It would run triage, inbox extraction, JD checks, ranking people, browsing steps, the daily plan and reflection; Claude keeps cover letters, messages and resume advice.")
    if not assume_yes and input("Download llama.cpp and the model now? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Skipped. Run `python agent/run.py local-setup` any time.")
        return
    common.save_override("settings.json", {"llm": {"local": {"model": rec["repo"], "autostart": True}}})
    common.reload_config()
    print("llama-server:", local_ai.install())
    print("starting (the first start downloads the model; this can take several minutes) ...")
    if not local_ai.start():
        print("still loading; run `python agent/run.py local-bench --apply` once it is up")
        return
    print("checking quality against your main model on your own jobs ...")
    res = local_ai.bench(apply=True)
    print(res)
    print("Routed to the local model: " + ", ".join(res.get("routed", [])) if res.get("routed") else "Kept your hosted models for now (agreement below the bar); the local model stays available.")


def doctor():
    import llm
    print("workspace:", HOME, "(personal data, never committed)")
    print("profile  :", "OK " + common.PROFILE.get("name", "") if not common.needs_setup() else "MISSING: run `init`")
    for n, p in llm.registry().items():
        ok, why = llm.available(n)
        print(f"provider {n:10} {'ready' if ok else 'not ready':9} {'' if ok else why}  {'(verified adapter)' if p.get('verified') else '(adapter follows vendor docs; test it)'}")
    print("routing  :", {k: v.get("provider") + ":" + (v.get("model") or "default") for k, v in SETTINGS["llm"]["routing"].items()})
    chrome = [p for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe") if os.path.exists(p)]
    print("browser  :", chrome[0] if chrome else "no Chrome/Edge/Chromium found")
    try:
        import browse
        ok, why = browse.available()
    except Exception as e:
        ok, why = False, str(e)
    print("browsing :", ("OK " if ok else "NOT READY: ") + why + ("" if ok else "  (required: LinkedIn, YC, job boards, form filling)"))
    import schedule as sch
    print("schedule :", sch.info())


def main():
    for s in (sys.stdout, sys.stderr):  # names and posts contain emoji/non-Latin text; never crash on a Windows console code page
        try:
            s.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    a = sys.argv[1:]
    force = "--force" in a  # you insist on a job the eligibility gate rejected
    refill = "--refill" in a  # refill a form whose tab was closed, with the values saved from the last fill
    a = [x for x in a if x not in ("--force", "--refill")]
    cmd = a[0] if a else "status"
    if cmd in ("packet", "referral", "linkedin", "resume", "fill") and len(a) > 1:
        common.add_pick(a[1], {"linkedin": "referral"}.get(cmd, cmd))  # the user chose this job: list it under My picks
    if cmd == "cycle":
        cycle()
    elif cmd == "reply":
        import interview
        print(json.dumps(interview.reply(a[1], " ".join(a[2:]) or sys.stdin.read()), indent=1, ensure_ascii=False))
    elif cmd == "interview":
        import interview
        print(json.dumps(interview.prep(a[1]), indent=1, ensure_ascii=False))
    elif cmd == "stories":
        import interview
        for s in interview.stories(force="--force" in sys.argv):
            print(f"- {s.get('title')} [{', '.join(s.get('use_for') or [])}]\n  {s.get('result', '')}")
    elif cmd == "github":
        import github_sync
        print(github_sync.sync(force=True))
    elif cmd == "funnel":
        import interview
        print(json.dumps(interview.funnel(), indent=1))
    elif cmd == "cv":
        import cv
        try:
            print(json.dumps(cv.build(a[1]), indent=1))
        except cv.CVError as e:
            print(f"cv: {e}")
            sys.exit(1)
    elif cmd == "discover":
        import discover
        print(json.dumps(discover.run_all(force=True), indent=1, default=str))
    elif cmd == "events":
        import events
        if len(a) > 2 and a[1] == "strategy":
            print(json.dumps(events.strategy(a[2]), indent=1, ensure_ascii=False))
        else:
            print(json.dumps(events.refresh(force=True), indent=1))
            for e in events.listing()[:40]:
                print(f"{(e['deadline'] or 'rolling')[:10]:10}  {e['kind']:11} {e['name'][:50]:50} {e['prize'][:14]:14} fit {int(e['fit'] or 0)}")
    elif cmd == "programs":
        import programs
        if len(a) > 2 and a[1] == "strategy":
            print(json.dumps(programs.strategy(a[2]), indent=1, ensure_ascii=False))
        else:
            print(f"checked {programs.refresh(limit=20, force='--force' in sys.argv)} program pages")
    elif cmd == "sponsor":
        import research
        print(json.dumps(research.sponsorship(a[1]), indent=1))
    elif cmd == "research":
        import research
        d = research.company(" ".join(a[1:]), force="--force" in sys.argv)
        print(research.brief(d, 6000) or "nothing found")
        print("\nWHAT I READ:\n" + "\n".join(f"  {p['chars']:>6} chars  {p['why']:<22} {p['url']}" + (f"  FLAGGED: {p['flags']}" if p['flags'] else "") for p in research.what_i_read(d)))
    elif cmd == "regate":
        import maintenance
        print(json.dumps(maintenance.regate(use_browser="--no-browser" not in sys.argv), indent=1))
    elif cmd in ("pause", "resume"):
        import pause
        scopes = tuple(a[1:]) or ("all",)
        print(pause.pause(scopes) if cmd == "pause" else pause.resume(scopes))
        print(pause.describe())
    elif cmd == "poll":
        import pipeline
        ids, first = pipeline.poll()
        pipeline.instant_alerts(ids, first)
        print(len(ids), "new postings pass the filter")
    elif cmd == "triage":
        import pipeline
        pipeline.triage()
    elif cmd == "routing":
        # routing [list] | routing use <recommended|low-cost|hosted|local-only>
        import presets
        if len(a) > 2 and a[1] == "use":
            missing = presets.apply(a[2])
            print(f"Routing set to '{a[2]}'." + (f" Not ready on this machine: {', '.join(missing)} (their tasks use the fallback chain)." if missing else ""))
        print(presets.text())
    elif cmd in ("signals", "outreach"):
        # signals [collect|queue|list]: fresh hiring posts (X, LinkedIn) and seed rounds -> today's direct-outreach queue (you write every email)
        import signals
        sub = a[1] if len(a) > 1 else "collect"
        if sub == "list":
            print(signals.text_list(signals.today()))
        else:
            print(signals.run_all(collect=(sub != "queue")))
            print(signals.text_list(signals.today()))
    elif cmd == "rebuild":
        # rebuild <id> [packet,referral,resume|all] [--force]: redo the chosen material for one job, in order, with your notes for it attached
        import packets, referrals, resume
        parts = [x for x in (a[2] if len(a) > 2 and not a[2].startswith("--") else "all").split(",") if x]
        parts = ["packet", "referral", "resume"] if "all" in parts else parts
        common.add_pick(a[1], parts[0] if parts else "packet")
        for part in parts:
            print(f"=== {part.upper()} ===")
            try:
                if part == "packet":
                    print(packets.build_packet(a[1], force=True if force else False))
                elif part == "referral":
                    print(referrals.plan_referral(a[1], manual=True, force=force))
                elif part == "resume":
                    print(resume.advise(a[1]))
                else:
                    print(f"unknown part {part!r} (use packet, referral, resume or all)")
            except Exception as e:
                print(f"{part} failed: {str(e)[:400]}")
    elif cmd == "packet":
        import packets
        print(packets.build_packet(a[1], force=force))
    elif cmd in ("referral", "linkedin"):
        import referrals
        # you asked for this one by hand: the background LinkedIn cap does not apply; `linkedin` insists on LinkedIn and warns loudly if it could not run
        print(referrals.plan_referral(a[1], manual=True, require_linkedin=(cmd == "linkedin"), force=force))
    elif cmd == "connections":
        import agentic, linkedin
        res = linkedin.scan_list(agentic.company_list(" ".join(a[1:]) or "picks"), limit=SETTINGS.get("connections_max_companies", 15))
        print("=== CONNECTIONS ===")
        print(agentic.connections_text(res))
    elif cmd in ("local-setup", "local-start", "local-stop", "local-bench", "local-status"):
        import local_ai
        if cmd == "local-status":
            print(json.dumps({"hardware": local_ai.detect(), "recommended": local_ai.recommend(), "installed": local_ai.server_bin(), "running": local_ai.running()}, indent=1))
        elif cmd == "local-start":
            print("ready" if local_ai.start() else "still loading; check logs/llama-server.log")
        elif cmd == "local-stop":
            print(local_ai.stop())
        elif cmd == "local-bench":
            print(local_ai.bench(apply="--apply" in a))
        else:
            local_setup(local_ai, assume_yes="--yes" in a)
    elif cmd == "selftest":
        import harness_check
        harness_check.run_all(a[1] if len(a) > 1 else "local")
    elif cmd == "refresh":
        import maintenance
        print(maintenance.refresh(refresh_plans="--no-plans" not in a))
    elif cmd == "audit":
        import jd
        rows = jd.audit()
        for jid, co, ti, probs in rows:
            print(f"{co[:22]:22} {ti[:50]:50} {'; '.join(probs)}  [{jid}]")
        print(len(rows), "of your picks/packets/watchlist jobs may not fit you (marked in My picks; nothing deleted)")
    elif cmd == "picks":
        print(common.backfill_picks(), "earlier picks added to My picks")
    elif cmd == "resume":
        import resume
        print(resume.advise(a[1]))
    elif cmd == "fill":
        import fill
        print(fill.fill_job(a[1], force=force, refill=refill))
    elif cmd == "scout":
        import scout
        scout.run_scout(force=True)
    elif cmd == "inbox":
        import inbox
        if len(a) > 1 and a[1] == "retry":
            inbox.retry(a[2] if len(a) > 2 else "all")
            return
        if len(a) > 1 and a[1] == "add":
            i = inbox.add(a[2], a[3] if len(a) > 3 else "")
            print("queued", i)
        inbox.process_pending()
    elif cmd == "chat":
        import inbox
        ans, done = inbox.reply(" ".join(a[1:]))
        print(ans, "\n", done or "")
        import chatcmd
        chatcmd.wait_all()
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
        setup.wizard(defaults="--defaults" in a, reconfigure="--reconfigure" in a)
    elif cmd == "rescore":
        import visa
        print(visa.rescore_all(), "jobs rescored")
    elif cmd == "doctor":
        doctor()
    elif cmd == "app":
        import desktop
        desktop.open_window()
    elif cmd == "shortcuts":
        import desktop
        desktop.install_shortcuts()
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
        if st in ("replied", "screen", "interview", "offer", "rejected", "ghosted"):
            import interview
            interview.record(a[1], st, " ".join(a[3:]))
            print("ok")
            return
        common.set_status(c, a[1], st, by="user")
        record_outcome(a[1], st, " ".join(a[3:]))
        print("ok")
    elif cmd == "notify-test":
        notify("JobAgent test", "If you can read this, notifications work.")
    elif cmd in ("install-schedule", "remove-schedule", "enable-schedule", "disable-schedule", "set-times", "set-interval"):
        import schedule as sch
        if cmd == "install-schedule":
            print(sch.install())
        elif cmd == "remove-schedule":
            print(sch.remove())
        elif cmd in ("enable-schedule", "disable-schedule"):
            print(sch.enable(cmd == "enable-schedule"))
        else:  # set-times 09:00 18:00   (set-interval is the old name; it now takes times too)
            ts = [x for x in a[1:] if re.fullmatch(r"\d{1,2}:\d\d", x)]
            print(sch.install([x.zfill(5) for x in ts]) if ts else "usage: set-times HH:MM [HH:MM ...]   e.g. set-times 09:00 18:00")
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
