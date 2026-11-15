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
    ("fill", "/fill <id or words>", "Fill the application form in the agent browser. Never submits; you press Submit.", "Prepare"),
    ("mark", "/mark <id or words> applied|skip|dismissed [reason]", "Record an outcome; reasons teach the agent.", "Track"),
    ("add", "/add <link or text> [-- your context]", "Put an opportunity you found into the inbox and process it.", "Track"),
    ("retry", "/retry", "Retry inbox items that failed or are waiting on quota.", "Track"),
    ("inbox", "/inbox", "Show the inbox queue and each item's status.", "Track"),
    ("remember", "/remember <fact>", "Save a fact or preference to the agent's memory.", "Track"),
    ("scout", "/scout", "Scout programs, residencies and high-stakes openings now.", "Run"),
    ("today", "/today", "Build today's plan.", "Run"),
    ("poll", "/poll", "Poll all sources for new postings (no model tokens).", "Run"),
    ("cycle", "/cycle", "Run one full cycle (heavy: triage, packets, referrals).", "Run"),
    ("yc-profile", "/yc-profile", "Draft a truthful rewrite of your YC Work at a Startup profile.", "Run"),
    ("reflect", "/reflect", "Update the agent's memory and scoring from your outcomes.", "Run"),
    ("agent", "/agent <goal>", "Hand a multi-step goal to the autonomous agent loop (tools: search, packets, referrals, web).", "Run"),
]
CMD = {c[0]: c for c in COMMANDS}
JOB_CMDS = ("packet", "referral", "linkedin", "resume", "fill")
PLAIN = ("scout", "today", "poll", "cycle", "yc-profile", "reflect")
# what natural-language chat may start by itself; these never change a job's status and never submit anything
NL_AUTO = JOB_CMDS[:4] + PLAIN[:3] + ("yc-profile", "retry", "remember", "ingest", "connections")
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
def launch(cmd, arg="", chat=True):
    """Start `run.py <cmd> <arg>` as a hidden background process. When `chat`, post a message into the chat when it finishes. Returns a status line."""
    if cmd == "inbox-retry":
        cmd, arg = "retry", arg
    argv = {"retry": ["inbox", "retry", arg or "all"], "mark": ["mark"] + arg.split()}.get(cmd, [cmd] + (arg.split() if cmd in JOB_CMDS else ([arg] if arg else [])))
    key = f"{cmd} {arg[:40]}".strip()
    with _lock:
        p = RUNNING.get(key)
        if p and p.poll() is None:
            return f"already running: {key}"
        logp = os.path.join(RUNS_DIR, f"{int(time.time())}-{re.sub(r'[^a-z0-9]+', '-', key.lower())[:40]}.log")
        logf = open(logp, "w", encoding="utf-8")
        p = subprocess.Popen([sys.executable, "-u", RUN_PY] + argv, stdout=logf, stderr=subprocess.STDOUT, cwd=CORE)
        RUNNING[key] = p
    t = threading.Thread(target=_watch, args=(key, cmd, arg, p, logp, logf, chat, _CUR["thread"]), daemon=True)
    t.start()
    THREADS.append(t)
    return f"started: {key}"


def wait_all():
    for t in list(THREADS):
        t.join()


def _tail(path, n=12, maxc=1400):
    try:
        lines = [l for l in open(path, encoding="utf-8", errors="replace").read().splitlines() if l.strip()]
    except OSError:
        return ""
    return "\n".join(lines[-n:])[-maxc:]


def _watch(key, cmd, arg, p, logp, logf, chat, thread=1):
    rc = p.wait()
    logf.close()
    out = _tail(logp, 400, 20000)
    try:
        with open(os.path.join(LOGS, "gui_runs.log"), "a", encoding="utf-8") as g:  # keep the single run log the GUI/Doctor read
            g.write(f"\n[{now()}] {key} (exit {rc})\n{out[-4000:]}\n")
    except OSError:
        pass
    with _lock:
        RUNNING.pop(key, None)
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
    cmd = {"referrals": "referral", "yc": "yc-profile", "ycprofile": "yc-profile", "search": "jobs", "find": "jobs", "list": "top", "commands": "help", "?": "help", "retry-inbox": "retry"}.get(cmd, cmd)
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
        memory.add_memory(rest[:500])
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
        c = db()
        c.execute("UPDATE jobs SET status=? WHERE id=?", (st, job["id"]))
        c.commit()
        common.record_outcome(job["id"], st, " ".join(words[idx + 1:]))
        return f"Marked {_fmt_job(job)} as {st}."
    if cmd in JOB_CMDS:
        force = bool(re.search(r"(^|\s)(--)?force$", rest.strip()))
        job, msg = resolve(re.sub(r"(^|\s)(--)?force$", "", rest.strip()).split(" --")[0])
        if not job:
            return msg
        common.add_pick(job["id"], {"linkedin": "referral"}.get(cmd, cmd))
        notes = {"linkedin": " I open your logged-in agent Chrome read-only (no Connect/Message/Follow clicks), look at 1st then 2nd degree people, and report names, profile links and how to approach each.  This takes a few minutes.",
                 "referral": " Uses LinkedIn when allowed, else public web; I will say which.", "fill": " It only fills the form and stops; you press Submit."}.get(cmd, "")
        return f"{cmd} for {_fmt_job(job)}.{notes}\n" + launch(cmd, job["id"] + (" --force" if force else ""))
    return "Unknown command. /help lists them."


# ------------------------------------------------------------------ natural language
def execute_nl_actions(user_text, actions, thread=1):
    _CUR["thread"] = thread
    """Actions the chat model chose from a plain-language request. Prep actions run at once (the user asked for them in words); status-changing or
    form-filling ones only run when the user's own words call for them, otherwise they come back as confirm-buttons. Returns (done_lines, proposals)."""
    done, props = [], []
    for kind, arg in actions:
        k = {"retry-inbox": "retry"}.get(kind, kind)
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
