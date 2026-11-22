"""Chat command layer: slash commands for the 'Ask the agent' box (same verbs as the CLI) and the executor for actions the chat model chooses from natural language.
Rules: slash commands run exactly what they say, immediately; long jobs run as background processes and the chat gets a message when each one finishes
(with the people found, or the honest reason it failed). Nothing here ever submits an application; `fill` only fills the form and stops."""
import json, os, re, subprocess, sys, threading, time
import common
from common import db, now, CORE, LOGS

RUN_PY = os.path.join(CORE, "agent", "run.py")
RUNS_DIR = os.path.join(LOGS, "runs")
os.makedirs(RUNS_DIR, exist_ok=True)
RUNNING, THREADS, _lock = {}, [], threading.Lock()

# name, usage, one-line description, group
COMMANDS = [
    ("help", "/help [command]", "Show this list, or details for one command.", "Basics"),
    ("status", "/status", "Pipeline counts, today's budgets, which providers are cooling down.", "Basics"),
    ("pause", "/pause [all|local|api|subscription] [--stop]", "Stop all AI use (local model + GPU, API keys, subscriptions) until you resume; scheduled runs stay off too. --stop also aborts running tasks.", "Basics"),
    ("resume-ai", "/resume-ai [all|local|api|subscription]", "Undo /pause: models and scheduled runs work again.", "Basics"),
    ("tasks", "/tasks", "What is running, queued, and how recent tasks ended (with the reason when one failed).", "Basics"),
    ("abort", "/abort <task id | job id | all>", "Stop a running or queued task (kills its whole process tree). Nothing is ever submitted.", "Basics"),
    ("usage", "/usage", "Model calls today per provider and kind, plus quota cooldowns.", "Basics"),
    ("top", "/top [n]", "Best open matches right now, with ids and links.", "Find"),
    ("jobs", "/jobs <words>", "Search the job board by company/title words, e.g. /jobs datadog applied science.", "Find"),
    ("picks", "/picks", "Your picks and how far each one is (packet / referral / resume / fill).", "Find"),
    ("watch", "/watch", "High-stakes roles on your watchlist.", "Find"),
    ("needs", "/needs", "Jobs waiting on you: forms that need a few fields, or employers that need you to sign in.", "Find"),
    ("connections", "/connections <picks | needs-you | company, company…>", "Who you know at each company: reads LinkedIn in your logged-in browser (1st degree, then 2nd with mutuals). No model cost.", "Prepare"),
    ("packet", "/packet <id or words>", "Prepare the application packet: cover letter, answers, tailored resume.", "Prepare"),
    ("referral", "/referral <id or words>", "Plan who to approach and draft the messages (uses LinkedIn when allowed, else the public web; says which).", "Prepare"),
    ("linkedin", "/linkedin <id or words>", "Scout LinkedIn read-only through your logged-in agent Chrome: 1st then 2nd degree, who is likely to answer, how to approach.", "Prepare"),
    ("resume", "/resume <id or words>", "Resume tailoring advice for that job (truthful, nothing invented).", "Prepare"),
    ("signals", "/signals", "Find fresh hiring posts (X, LinkedIn, under 24 h) and new seed rounds, and fill today's outreach queue (Outreach view).", "Run"),
    ("rebuild", "/rebuild <id or words>", "Redo the application, referral plan and resume advice for that job, using any notes you left for it (Details -> Rebuild).", "Prepare"),
    ("fill", "/fill <id or words> [--refill]", "Fill the application form in the agent browser. Never submits; you press Submit. --refill replays the last fill's values (no model call) after the browser was closed.", "Prepare"),
    ("mark", "/mark <id or words> applied|skip|dismissed [reason]", "Record an outcome; reasons teach the agent.", "Track"),
    ("add", "/add <link or text> [-- your context]", "Put an opportunity you found into the inbox and process it.", "Track"),
    ("retry", "/retry", "Retry inbox items that failed or are waiting on quota.", "Track"),
    ("inbox", "/inbox", "Show the inbox queue and each item's status.", "Track"),
    ("remember", "/remember <fact>", "Save a fact or preference to the agent's memory.", "Track"),
    ("regate", "/regate", "Re-check every match, pick and packet against its full posting with the current rules (years by lower bound, degree, seniority, visa).", "Run"),
    ("scout", "/scout", "Scout programs, residencies and high-stakes openings now.", "Run"),
    ("today", "/today", "Build today's plan.", "Run"),
    ("poll", "/poll", "Poll all sources for new postings (no model tokens).", "Run"),
    ("cycle", "/cycle", "Run one full cycle (heavy: triage, packets, referrals).", "Run"),
    ("yc-profile", "/yc-profile", "Draft a truthful rewrite of your YC Work at a Startup profile.", "Run"),
    ("reflect", "/reflect", "Update the agent's memory and scoring from your outcomes.", "Run"),
    ("agent", "/agent <goal>", "Hand a multi-step goal to the autonomous agent loop (tools: search, packets, referrals, web).", "Run"),
]
CMD = {c[0]: c for c in COMMANDS}
JOB_CMDS = ("packet", "referral", "linkedin", "resume", "fill", "rebuild")
PLAIN = ("scout", "today", "poll", "cycle", "yc-profile", "reflect", "signals", "regate")
# what natural-language chat may start by itself; these never change a job's status and never submit anything
NL_AUTO = JOB_CMDS[:4] + PLAIN[:3] + ("yc-profile", "retry", "remember", "ingest", "connections", "signals")
NL_NEEDS_WORDS = {"fill": r"\bfill|auto-?fill", "mark": r"\b(mark|applied|skip|dismiss|ignore|reject)", "agent": r"\bagent|work on|figure out|research|find out|do this|take care"}


_CUR = {"thread": 1}  # the chat thread a command was typed in; background results are posted back there


def say(role, text, thread=None):
    t = thread or _CUR["thread"]
    c = db()
    c.execute("INSERT INTO chat(ts,role,text,thread) VALUES(?,?,?,?)", (now(), role, text, t))
    c.execute("UPDATE threads SET updated=? WHERE id=?", (now(), t))
    c.commit()


def help_text(which=""):
    if which and which.lstrip("/") in CMD:
        n, u, d, _ = CMD[which.lstrip("/")]
        return f"{u}\n{d}"
    out, grp = ["Slash commands (same verbs as the CLI). <id or words> = a job id from /top or /jobs, or company/title words; if the words match several jobs I list them.\n"], ""
    for n, u, d, g in COMMANDS:
        if g != grp:
            grp = g
            out.append(f"\n{g}")
        out.append(f"  {u}\n      {d}")
    out.append("\nYou can also just write what you want in plain words, e.g. \"scout LinkedIn for people at Datadog for the applied science intern role\". "
               "Anything I start runs in the background and I post the result here when it finishes. I never submit an application.")
    return "\n".join(out)


# ------------------------------------------------------------------ resolving jobs
def resolve(ref):
    """-> (job_row or None, message_if_not_unique)."""
    c = db()
    ref = (ref or "").strip().strip("\"'")
    if not ref:
        return None, "Tell me which job: a job id (see /top or /jobs) or company/title words."
    r = c.execute("SELECT id,company,title,location,url,score FROM jobs WHERE id=?", (ref,)).fetchone()
    if r:
        return r, ""
    words = [w for w in re.findall(r"[\w+#./-]+", ref.lower()) if len(w) > 1]
    if not words:
        return None, "I could not read that as a job reference."
    where = " AND ".join("(lower(j.company) LIKE ? OR lower(j.title) LIKE ?)" for _ in words)
    args = [x for w in words for x in (f"%{w}%", f"%{w}%")]
    rows = c.execute(f"""SELECT j.id,j.company,j.title,j.location,j.url,j.score, (p.job_id IS NOT NULL) picked, COALESCE(t.fit,j.score) fit FROM jobs j LEFT JOIN picks p ON p.job_id=j.id
                         LEFT JOIN triage t ON t.job_id=j.id WHERE {where} AND j.active=1 ORDER BY picked DESC, fit DESC LIMIT 6""", args).fetchall()
    if not rows:
        return None, f"No open job matches \"{ref}\". Try /jobs {ref}"
    if len(rows) == 1 or (rows[0]["picked"] and not rows[1]["picked"]):
        return rows[0], ""
    return None, "That matches several jobs, repeat the command with an id:\n" + "\n".join(f"  {r['id']}  {r['company']} - {r['title'][:60]} ({r['location'][:24]})" for r in rows)


def _fmt_job(r):
    return f"{r['id']}  {r['company']} - {r['title'][:64]} ({(r['location'] or '')[:26]})"


# ------------------------------------------------------------------ launching background work
# Every background task is a row in `tasks` (survives GUI restarts): queued -> running -> done | failed | aborted | interrupted | timed-out.
# Up to tasks.max_parallel run at once; the rest wait in the queue. Each has an overall time limit and can be aborted (whole process tree).
TASK_LIMIT_MIN = {"cycle": 120, "signals": 60, "agent": 45, "connections": 45, "inbox": 60, "retry": 60}
_JOBS_ARG = JOB_CMDS + ("mark",)


def _argv(cmd, arg):
    return {"retry": ["inbox", "retry", arg or "all"], "mark": ["mark"] + arg.split()}.get(cmd, [cmd] + (arg.split() if cmd in JOB_CMDS else ([arg] if arg else [])))


def _max_parallel():
    return max(1, int(common.SETTINGS.get("tasks", {}).get("max_parallel", 3)))


def _limit_min(cmd):
    t = common.SETTINGS.get("tasks", {})
    return float(t.get("timeout_min", {}).get(cmd) or TASK_LIMIT_MIN.get(cmd) or t.get("default_timeout_min", 40))


def launch(cmd, arg="", chat=True):
    """Queue `run.py <cmd> <arg>` as a hidden background task (starts at once when a slot is free). When `chat`, post a message into
    the chat when it finishes. Returns a status line."""
    if cmd == "inbox-retry":
        cmd, arg = "retry", arg
    key = f"{cmd} {arg[:40]}".strip()
    c = db()
    with _lock:
        p = RUNNING.get(key)
        if (p and p.poll() is None) or c.execute("SELECT 1 FROM tasks WHERE key=? AND status IN ('queued','starting','running')", (key,)).fetchone():
            return f"already running: {key}"
        jid = arg.split()[0] if (arg and cmd in _JOBS_ARG) else None
        tid = c.execute("INSERT INTO tasks(key,cmd,arg,job_id,status,started,thread,rc) VALUES(?,?,?,?,?,?,?,?)",
                        (key, cmd, arg, jid, "queued", now(), _CUR["thread"] if chat else 0, None)).lastrowid
        c.commit()
    started = _start_queued()
    return f"started: {key}" if tid in started else f"queued: {key} (waiting for a free slot; {_max_parallel()} tasks run at once)"


def _start_queued():
    """Start queued tasks while slots are free. Returns the ids started."""
    started = []
    with _lock:
        c = db()
        busy = sum(1 for r in c.execute("SELECT pid FROM tasks WHERE status='running'").fetchall() if pid_alive(r["pid"]))
        for t in c.execute("SELECT * FROM tasks WHERE status='queued' ORDER BY id").fetchall():
            if busy >= _max_parallel():
                break
            if c.execute("UPDATE tasks SET status='starting' WHERE id=? AND status='queued'", (t["id"],)).rowcount != 1:
                c.commit()
                continue  # another JobAgent process took it
            c.commit()
            logp = os.path.join(RUNS_DIR, f"{int(time.time())}-{re.sub(r'[^a-z0-9]+', '-', t['key'].lower())[:40]}.log")
            logf = open(logp, "w", encoding="utf-8")
            kw = {"creationflags": 0x00000200} if os.name == "nt" else {"start_new_session": True}  # own process group: abort kills the whole tree
            p = subprocess.Popen([sys.executable, "-u", RUN_PY] + _argv(t["cmd"], t["arg"] or ""), stdout=logf, stderr=subprocess.STDOUT, cwd=CORE, **kw)
            RUNNING[t["key"]] = p
            c.execute("UPDATE tasks SET status='running', pid=?, started=?, log=? WHERE id=?", (p.pid, now(), logp, t["id"]))
            c.commit()
            th = threading.Thread(target=_watch, args=(t["id"], t["key"], t["cmd"], t["arg"] or "", p, logp, logf, bool(t["thread"]), t["thread"] or 1), daemon=True)
            th.start()
            THREADS.append(th)
            started.append(t["id"])
            busy += 1
    return started


def wait_all():
    """CLI use: block until every task this process started (and the queued ones they started in turn) has ended."""
    while any(t.is_alive() for t in THREADS):
        for t in list(THREADS):
            t.join()


def _kill_tree(pid):
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=30)
        else:
            import signal
            os.killpg(os.getpgid(int(pid)), signal.SIGTERM)
    except Exception:
        pass


def pid_alive(pid):
    if not pid:
        return False
    try:
        if os.name == "nt":
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
            if not h:
                return False
            code = ctypes.c_ulong()
            ctypes.windll.kernel32.GetExitCodeProcess(h, ctypes.byref(code))
            ctypes.windll.kernel32.CloseHandle(h)
            return code.value == 259  # STILL_ACTIVE
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def _cleanup_after(cmd):
    """What a killed task can leave behind: the cycle lock and inbox items marked 'processing'."""
    c = db()
    if cmd == "cycle":
        try:
            import run as _r
            lock = _r.LOCK
        except Exception:
            lock = os.path.join(common.DATA, "cycle.lock")
        try:
            os.remove(lock)
        except OSError:
            pass
    if cmd in ("cycle", "inbox", "retry", "agent"):
        c.execute("UPDATE inbox SET status='error', result=? WHERE status='processing'", (json.dumps({"error": "stopped before it finished", "retry": "press Retry"}),))
        c.commit()


def abort(ref):
    """Abort a queued or running task by id, key, or job id. Returns a message."""
    c = db()
    rows = c.execute("SELECT * FROM tasks WHERE status IN ('queued','starting','running') AND (CAST(id AS TEXT)=? OR key=? OR job_id=?)", (str(ref), str(ref), str(ref))).fetchall()
    if not rows:
        return f"nothing running matches {ref}"
    out = []
    for t in rows:
        c.execute("UPDATE tasks SET status='aborted', ended=?, error='aborted by you' WHERE id=?", (now(), t["id"]))
        c.commit()
        if t["status"] == "running":
            _kill_tree(t["pid"])
            _cleanup_after(t["cmd"])
        out.append(t["key"])
    return "aborted: " + ", ".join(out)


def abort_all():
    c = db()
    ids = [r["id"] for r in c.execute("SELECT id FROM tasks WHERE status IN ('queued','starting','running')").fetchall()]
    if not ids:
        return "no tasks were running"
    return "; ".join(abort(i) for i in ids)


def reconcile():
    """At GUI start: tasks whose process is gone (GUI or computer restarted) become 'interrupted'; stale locks and stuck inbox rows are cleared;
    queued tasks start."""
    c = db()
    n = 0
    for t in c.execute("SELECT * FROM tasks WHERE status IN ('running','starting')").fetchall():
        if not pid_alive(t["pid"]):
            c.execute("UPDATE tasks SET status='interrupted', ended=?, error='stopped when JobAgent or the computer restarted' WHERE id=?", (now(), t["id"]))
            n += 1
    c.commit()
    lock = os.path.join(common.DATA, "cycle.lock")
    try:
        pid = int(open(lock).read().strip() or 0)
        if not pid_alive(pid):
            os.remove(lock)
    except (OSError, ValueError):
        pass
    stuck = c.execute("SELECT COUNT(*) FROM inbox WHERE status='processing'").fetchone()[0]
    if stuck and not any(pid_alive(t["pid"]) for t in c.execute("SELECT pid FROM tasks WHERE status='running'").fetchall()):
        c.execute("UPDATE inbox SET status='error', result=? WHERE status='processing'", (json.dumps({"error": "interrupted by a restart", "retry": "press Retry"}),))
        c.commit()
    _start_queued()
    return n


def tasks_json(limit=40):
    c = db()
    rows = [dict(r) for r in c.execute("SELECT id,key,cmd,arg,job_id,status,started,ended,rc,error FROM tasks ORDER BY id DESC LIMIT ?", (limit,)).fetchall()]
    return rows


def last_task(job_id):
    """Latest task for a job (any prep kind), for the picks view: running / failed with reason / interrupted."""
    r = db().execute("SELECT id,cmd,status,error,started,ended FROM tasks WHERE job_id=? ORDER BY id DESC LIMIT 1", (job_id,)).fetchone()
    return dict(r) if r else None


def _tail(path, n=12, maxc=1400):
    try:
        lines = [l for l in open(path, encoding="utf-8", errors="replace").read().splitlines() if l.strip()]
    except OSError:
        return ""
    return "\n".join(lines[-n:])[-maxc:]


def _watch(tid, key, cmd, arg, p, logp, logf, chat, thread=1):
    limit = _limit_min(cmd) * 60
    timed_out = False
    try:
        rc = p.wait(timeout=limit)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(p.pid)
        _cleanup_after(cmd)
        try:
            rc = p.wait(timeout=30)
        except subprocess.TimeoutExpired:
            rc = -9
    logf.close()
    out = _tail(logp, 400, 20000)
    c = db()
    st = c.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()
    if st and st["status"] == "aborted":
        status, err = "aborted", "aborted by you"
    elif timed_out:
        status, err = "timed-out", f"stopped after {int(limit // 60)} minutes (time limit for {cmd})"
    elif rc == 0:
        status, err = "done", ""
    else:
        gate = re.search(r"GateError: (.*)", out)
        status, err = "failed", (gate.group(1)[:300] if gate else (_tail_text(out, 1) or f"exit {rc}")[:300])
    c.execute("UPDATE tasks SET status=?, ended=?, rc=?, error=? WHERE id=?", (status, now(), rc, err, tid))
    c.commit()
    if status == "aborted":
        with _lock:
            RUNNING.pop(key, None)
        _start_queued()
        if chat:
            say("agent", f"Stopped {key}: aborted by you. Nothing was submitted.", thread)
        return
    if timed_out:
        out += f"\n{err}"
    try:
        with open(os.path.join(LOGS, "gui_runs.log"), "a", encoding="utf-8") as g:  # keep the single run log the GUI/Doctor read
            g.write(f"\n[{now()}] {key} (exit {rc})\n{out[-4000:]}\n")
    except OSError:
        pass
    with _lock:
        RUNNING.pop(key, None)
    _start_queued()
    if not chat:
        return
    try:
        say("agent", summarize(cmd, arg, rc, out), thread)
    except Exception as e:  # never let a summary problem hide that the job ended
        say("agent", f"{key} finished (exit {rc}); could not summarise it: {e}", thread)


def _targets_md(path):
    """People from a REFERRAL.md: (note, [(name+role, profile, reach)])."""
    try:
        t = open(path, encoding="utf-8").read()
    except OSError:
        return "", []
    note = (re.search(r"^> NOTE: (.*)$", t, re.M) or [None, ""])[1]
    people = []
    for blk in re.split(r"\n## Target \d+: ", t)[1:]:
        head = blk.splitlines()[0].strip()
        prof = (re.search(r"^Profile: (.*)$", blk, re.M) or [None, ""])[1].strip()
        reach = (re.search(r"^Reach via: (.*)$", blk, re.M) or [None, ""])[1].strip()
        why = (re.search(r"^Why: (.*)$", blk, re.M) or [None, ""])[1].strip()
        people.append((head, prof, reach, why))
    return note, people


def summarize(cmd, arg, rc, out):
    c = db()
    jid = arg.split()[0] if arg and cmd in JOB_CMDS + ("mark",) else ""
    arg = jid or arg
    job = c.execute("SELECT company,title FROM jobs WHERE id=?", (jid,)).fetchone() if jid else None
    label = f"{job['company']} - {job['title'][:50]}" if job else cmd
    gate = re.search(r"GateError: (.*)", out)
    if gate:
        return f"{cmd} for {label} was not started: {gate.group(1)[:400]}"
    if rc != 0:
        return f"{cmd} for {label} FAILED (exit {rc}). Last lines:\n{_tail_text(out)}\n\nNothing was submitted. Check /usage if this looks like a quota problem, then try again."
    if cmd in ("referral", "linkedin") and job:
        r = c.execute("SELECT dir,how FROM referrals WHERE job_id=?", (jid,)).fetchone()
        note, people = _targets_md(os.path.join(r["dir"], "REFERRAL.md")) if r else ("", [])
        warn = (re.search(r"^WARNING: (.*)$", out, re.M) or [None, ""])[1]
        lines = [f"{'LinkedIn scouting' if cmd == 'linkedin' else 'Referral plan'} for {label} is ready (source: {r['how'] if r else '?'})."]
        if note or warn:
            lines.append("Heads-up: " + (note or warn))
        for i, (h, prof, reach, why) in enumerate(people[:5], 1):
            lines.append(f"{i}. {h}" + (f"\n   {prof}" if prof else "") + (f"\n   reach: {reach}" if reach else "") + (f"\n   why: {why[:160]}" if why else ""))
        if not people:
            lines.append("No specific people came out of it.")
        lines.append("Full plan with drafted messages: My picks -> this job -> Referral.")
        return "\n".join(lines)
    if cmd == "packet" and job:
        return f"Packet ready for {label}: cover letter, answers and tailored resume are in My picks. {_tail_text(out, 2)}"
    if cmd == "resume" and job:
        return f"Resume advice for {label} is ready in My picks."
    if cmd == "fill" and job:
        return f"Form for {label} filled in the agent browser. NOT submitted: review it and press Submit yourself.\n{_tail_text(out, 4)}"
    if cmd == "connections":
        body = out.split("=== CONNECTIONS ===", 1)[-1].strip() if "=== CONNECTIONS ===" in out else _tail_text(out, 60, 3500)
        return f"Who you know (LinkedIn, read-only):\n{body[:6000]}\n\nAsk /linkedin <job> for a full plan with messages for any of these."
    if cmd in ("agent", "yc-profile", "reflect", "today", "scout", "poll", "cycle", "retry"):
        return f"{cmd} finished.\n{_tail_text(out, 14, 2200)}"
    return f"{cmd} finished.\n{_tail_text(out, 6)}"


def _tail_text(out, n=8, maxc=900):
    return "\n".join([l for l in out.splitlines() if l.strip()][-n:])[-maxc:]


# ------------------------------------------------------------------ read-only commands
def cmd_status():
    import llm
    c = db()
    n = c.execute("SELECT COUNT(*) FROM jobs WHERE active=1").fetchone()[0]
    st = ", ".join(f"{s}: {k}" for s, k in c.execute("SELECT status,COUNT(*) FROM jobs WHERE active=1 GROUP BY status ORDER BY 2 DESC LIMIT 7"))
    bud = ", ".join(f"{k} {common.budget_used(c, k)}" for k in ("triage_calls", "packets", "referrals", "linkedin", "fills"))
    cd = [f"{p} until {llm.cooldown_until(p)[11:16]}" for p in llm.registry() if llm.cooldown_until(p)]
    return (f"{n} open jobs ({st}).\nToday's work: {bud}.\nProviders cooling down: {', '.join(cd) if cd else 'none'}.\nRunning now: {', '.join(RUNNING) if RUNNING else 'nothing'}.")


def cmd_usage():
    import llm
    c = db()
    rows = c.execute("SELECT COALESCE(provider,'claude') p, kind, COUNT(*) n FROM llm_log WHERE ts>=date('now','localtime') GROUP BY p,kind ORDER BY p,n DESC").fetchall()
    lines = [f"{r['p']:8} {r['kind']:9} {r['n']} calls" for r in rows] or ["no model calls today"]
    cd = [f"{p}: paused until {llm.cooldown_until(p)[11:16]} (quota)" for p in llm.registry() if llm.cooldown_until(p)]
    return "Model calls today:\n" + "\n".join(lines) + ("\n" + "\n".join(cd) if cd else "")


def cmd_top(n=10):
    c = db()
    rows = c.execute("""SELECT j.id,j.company,j.title,j.location,j.url,t.verdict,COALESCE(t.fit,j.score) fit FROM jobs j LEFT JOIN triage t ON t.job_id=j.id
                        WHERE j.active=1 AND j.status NOT IN ('skipped','applied','ineligible','dismissed') ORDER BY fit DESC LIMIT ?""", (max(1, min(n, 25)),)).fetchall()
    return "\n".join(f"{int(r['fit']):3} {(r['verdict'] or '-'):7} {r['id']}  {r['company']} - {r['title'][:56]} ({(r['location'] or '')[:22]})\n     {r['url']}" for r in rows) or "Nothing open."


def cmd_jobs(q):
    words = [w for w in re.findall(r"[\w+#./-]+", q.lower()) if len(w) > 1]
    if not words:
        return "Usage: /jobs <company or title words>"
    c = db()
    where = " AND ".join("(lower(j.company) LIKE ? OR lower(j.title) LIKE ? OR lower(j.location) LIKE ?)" for _ in words)
    args = [x for w in words for x in (f"%{w}%",) * 3]
    rows = c.execute(f"SELECT j.id,j.company,j.title,j.location,j.url,COALESCE(t.fit,j.score) fit FROM jobs j LEFT JOIN triage t ON t.job_id=j.id WHERE {where} AND j.active=1 ORDER BY fit DESC LIMIT 10", args).fetchall()
    return "\n".join(f"{int(r['fit']):3} {r['id']}  {r['company']} - {r['title'][:56]} ({(r['location'] or '')[:22]})\n     {r['url']}" for r in rows) or f"No open job matches \"{q}\"."


def cmd_picks():
    import gui
    ps = gui.picks_info()
    if not ps:
        return "No picks yet. Use /packet, /referral or /linkedin on a job, or /add a link."
    mark = lambda b: "x" if b else "-"
    return "\n".join(f"[{'ready' if p['ready'] else 'in progress'}] {p['id']}  {p['company']} - {p['title'][:50]}   packet:{mark(p['has']['packet'])} referral:{mark(p['has']['referral'])} resume:{mark(p['has']['resume'])} fill:{mark(p['has']['fill'])}" for p in ps[:12]) + (f"\n...and {len(ps) - 12} more in My picks." if len(ps) > 12 else "")


def cmd_watch():
    rows = db().execute("SELECT j.id,j.company,j.title,w.referral_state,w.deadline FROM watch w JOIN jobs j ON j.id=w.job_id WHERE j.status='watch' LIMIT 20").fetchall()
    return "\n".join(f"{r['id']}  {r['company']} - {r['title'][:50]}  referral:{r['referral_state']} {r['deadline'] or ''}" for r in rows) or "Watchlist is empty."


# ------------------------------------------------------------------ slash entry point
def run_slash(text, thread=1):
    _CUR["thread"] = thread
    """text starts with '/'. Returns the reply (already stored by the caller)."""
    m = re.match(r"/([\w-]+)\s*(.*)", text.strip(), re.S)
    if not m:
        return "Type /help for the commands."
    cmd, rest = m.group(1).lower(), m.group(2).strip()
    cmd = {"referrals": "referral", "yc": "yc-profile", "ycprofile": "yc-profile", "search": "jobs", "find": "jobs", "list": "top", "commands": "help", "?": "help", "retry-inbox": "retry", "unpause": "resume-ai"}.get(cmd, cmd)
    if cmd not in CMD:
        near = [n for n in CMD if n.startswith(cmd[:2])]
        return f"Unknown command /{cmd}." + (f" Did you mean {', '.join('/' + n for n in near)}?" if near else "") + " /help lists them all."
    return execute(cmd, rest, source="slash")


def execute(cmd, rest="", source="slash"):
    """Run one command. `source` is 'slash' (typed by the user) or 'nl' (chosen by the chat model from a plain-language request)."""
    if cmd == "help":
        return help_text(rest)
    if cmd == "status":
        return cmd_status()
    if cmd == "usage":
        return cmd_usage()
    if cmd == "tasks":
        rows = tasks_json(15)
        if not rows:
            return "No tasks yet."
        return "\n".join(f"#{r['id']} [{r['status']}] {r['key']}" + (f" - {r['error'][:140]}" if r['error'] else "") for r in rows)
    if cmd == "abort":
        if not rest:
            return "Usage: /abort <task id | job id | all>  (see /tasks)"
        return abort_all() if rest.strip() == "all" else abort(rest.strip())
    if cmd in ("pause", "resume-ai", "unpause"):
        import pause
        words = rest.split()
        scopes = tuple(w for w in words if not w.startswith("--")) or ("all",)
        if cmd == "pause":
            msg = pause.pause(scopes)
            if "--stop" in words:
                msg += "\n" + abort_all()
            return msg
        return pause.resume(scopes)
    if cmd == "top":
        return cmd_top(int(rest) if rest.isdigit() else 10)
    if cmd == "jobs":
        return cmd_jobs(rest)
    if cmd == "picks":
        return cmd_picks()
    if cmd == "watch":
        return cmd_watch()
    if cmd == "needs":
        import agentic
        return agentic.needs_you_text()
    if cmd == "connections":
        import agentic
        cos = agentic.company_list(rest or "picks")
        if not cos:
            return "No companies in that list."
        return (f"Checking your LinkedIn network at {len(cos[:15])} companies ({', '.join(cos[:15])}{'…' if len(cos) > 15 else ''}). Read-only, paced like a person; "
                f"about 15-30 s per company. I will post who you know at each.\n" + launch("connections", rest or "picks"))
    if cmd == "inbox":
        import inbox
        return inbox.summary() or "Inbox is empty."
    if cmd == "retry":
        return "Retrying the inbox in the background. " + launch("retry", rest or "all")
    if cmd == "remember":
        if not rest:
            return "Usage: /remember <fact or preference>"
        import memory
        try:
            memory.add_memory(rest[:500])
        except ValueError as e:
            return str(e)
        return "Saved to memory."
    if cmd in ("add", "ingest"):
        if not rest:
            return "Usage: /add <link or text> [-- your context]"
        import inbox
        body, _, ctx = rest.partition(" -- ")
        inbox.add(body[:20000], ctx[:2000] or "from chat")
        return "Added to the inbox. " + launch("inbox")
    if cmd in PLAIN:
        return {"cycle": "Running a full cycle (heavy). ", "yc-profile": "Drafting your YC profile rewrite (read-only on the site; nothing is saved there). "}.get(cmd, f"Running {cmd}. ") + launch(cmd)
    if cmd == "agent":
        if not rest:
            return "Usage: /agent <goal>"
        return "Handing that to the agent loop; I will post what it finds. " + launch("agent", rest)
    if cmd == "mark":
        words = rest.split()
        idx = next((i for i, w in enumerate(words) if w.lower() in ("applied", "skip", "skipped", "dismissed", "dismiss")), -1)
        if idx < 1:
            return "Usage: /mark <id or words> applied|skip|dismissed [reason]"
        job, msg = resolve(" ".join(words[:idx]))
        if not job:
            return msg
        st = {"skip": "skipped", "skipped": "skipped", "applied": "applied", "dismiss": "dismissed", "dismissed": "dismissed"}[words[idx].lower()]
        common.set_status(db(), job["id"], st, by="user")
        common.record_outcome(job["id"], st, " ".join(words[idx + 1:]))
        return f"Marked {_fmt_job(job)} as {st}."
    if cmd in JOB_CMDS:
        refill = "--refill" in rest
        rest = rest.replace("--refill", "").strip()
        force = bool(re.search(r"(^|\s)(--)?force$", rest.strip()))
        job, msg = resolve(re.sub(r"(^|\s)(--)?force$", "", rest.strip()).split(" --")[0])
        if not job:
            return msg
        common.add_pick(job["id"], {"linkedin": "referral"}.get(cmd, cmd))
        notes = {"linkedin": " I open your logged-in agent Chrome read-only (no Connect/Message/Follow clicks), look at 1st then 2nd degree people, and report names, profile links and how to approach each.  This takes a few minutes.",
                 "referral": " Uses LinkedIn when allowed, else public web; I will say which.", "fill": " It only fills the form and stops; you press Submit."}.get(cmd, "")
        return f"{cmd} for {_fmt_job(job)}.{notes}\n" + launch(cmd, job["id"] + (" --force" if force else "") + (" --refill" if refill and cmd == "fill" else ""))
    return "Unknown command. /help lists them."


# ------------------------------------------------------------------ natural language
def execute_nl_actions(user_text, actions, thread=1):
    _CUR["thread"] = thread
    """Actions the chat model chose from a plain-language request. Prep actions run at once (the user asked for them in words); status-changing or
    form-filling ones only run when the user's own words call for them, otherwise they come back as confirm-buttons. Returns (done_lines, proposals)."""
    done, props = [], []
    for kind, arg in actions:
        k = {"retry-inbox": "retry"}.get(kind, kind)
        if k in ("ingest", "add") and " -- " not in arg:
            arg = f"{arg} -- {user_text[:1500]}"  # your own words travel with the item (they say whether to prepare it)
        need = NL_NEEDS_WORDS.get(k)
        if k in NL_AUTO or (need and re.search(need, user_text, re.I)):
            try:
                done.append(f"{k} {arg[:60]}".strip() + " -> " + execute(k, arg, source="nl").splitlines()[-1][:140])
            except Exception as e:
                done.append(f"{k} {arg[:40]} -> failed to start: {e}")
        else:
            props.append({"kind": k, "arg": arg[:300]})
    return done, props


def commands_json():
    return [{"name": n, "usage": u, "desc": d, "group": g} for n, u, d, g in COMMANDS]
