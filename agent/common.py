"""Shared helpers: workspace paths, layered config, SQLite store, logging, notifications.
Core code never contains personal data: everything personal lives in the user's WORKSPACE (see below) and stays local.
Stdlib only on purpose (no pip needed)."""
import json, os, re, sqlite3, subprocess, datetime as dt, urllib.request, shutil

# ------------------------------------------------------------------ silent subprocesses (Windows)
# A scheduled, windowless Python that starts console programs (claude, agy, powershell, schtasks, node...) makes Windows flash a console window for each one.
# Creating every child with CREATE_NO_WINDOW keeps background runs completely invisible. Applied once, here, so all modules inherit it.
if os.name == "nt":
    _CREATE_NO_WINDOW, _DETACH_OR_NEW = 0x08000000, 0x00000008 | 0x00000010   # DETACHED_PROCESS | CREATE_NEW_CONSOLE (incompatible with NO_WINDOW)
    _popen_init = subprocess.Popen.__init__

    def _quiet_popen_init(self, *args, **kw):
        flags = kw.get("creationflags", 0)
        if not (flags & _DETACH_OR_NEW):
            kw["creationflags"] = flags | _CREATE_NO_WINDOW
        _popen_init(self, *args, **kw)

    subprocess.Popen.__init__ = _quiet_popen_init


# ------------------------------------------------------------------ paths
CORE = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))     # the repository (public code)
ROOT = CORE                                                               # backward-compatible alias
DEFAULTS = os.path.join(CORE, "defaults")                                  # shipped defaults, templates, skills


def _find_home():
    """Workspace = where all personal data lives. Order: $JOBAGENT_HOME, <repo>/.jobagent_home (a one-line pointer file, gitignored), ~/JobAgent."""
    env = os.environ.get("JOBAGENT_HOME")
    if env:
        return os.path.abspath(os.path.expanduser(env))
    ptr = os.path.join(CORE, ".jobagent_home")
    if os.path.exists(ptr):
        p = open(ptr, encoding="utf-8").read().strip()
        if p:
            return os.path.abspath(os.path.expanduser(p))
    return os.path.join(os.path.expanduser("~"), "JobAgent")


HOME = _find_home()
CFG = os.path.join(HOME, "config")
DATA = os.path.join(HOME, "data")
OUT = os.path.join(HOME, "out")
APPS = os.path.join(OUT, "applications")
LOGS = os.path.join(HOME, "logs")
USER_SKILLS = os.path.join(HOME, "skills")
for d in (CFG, DATA, OUT, APPS, LOGS, USER_SKILLS, os.path.join(DATA, "memory")):
    os.makedirs(d, exist_ok=True)


# ------------------------------------------------------------------ layered config (defaults + workspace overrides)
def _deep_merge(a, b):
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _deep_merge(a[k], v) if k in a else v
        return out
    return b


def _read(path):
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load(name, personal_only=False):
    """defaults/<name> deep-merged with <workspace>/config/<name>. Lists (companies) merge by slug/name, workspace wins."""
    base = None if personal_only else _read(os.path.join(DEFAULTS, name))
    over = _read(os.path.join(CFG, name))
    if base is None:
        return over if over is not None else {}
    if over is None:
        return base
    if isinstance(base, list) and isinstance(over, list):
        key = lambda e: (e.get("slug") or e.get("name") or "").lower()
        merged = {key(e): e for e in base}
        for e in over:
            merged[key(e)] = {**merged.get(key(e), {}), **e}
        return list(merged.values())
    return _deep_merge(base, over)


def save_override(name, patch):
    """Write only the user's changes to <workspace>/config/<name> (never touches shipped defaults)."""
    cur = _read(os.path.join(CFG, name)) or {}
    new = _deep_merge(cur, patch) if isinstance(cur, dict) else patch
    with open(os.path.join(CFG, name), "w", encoding="utf-8") as f:
        json.dump(new, f, indent=2)
    return new


def reload_config():
    """(Re)load config IN PLACE so modules that imported the dicts see the change."""
    global SETTINGS, PROFILE, FILTERS, COMPANIES, PROVIDERS
    fresh = {"SETTINGS": load("settings.json"), "PROFILE": load("profile.json", personal_only=True), "FILTERS": load("filters.json"),
             "COMPANIES": load("companies.json"), "PROVIDERS": load("providers.json")}
    for name, val in fresh.items():
        cur = globals().get(name)
        if isinstance(cur, dict) and isinstance(val, dict):
            cur.clear()
            cur.update(val)
        elif isinstance(cur, list) and isinstance(val, list):
            cur[:] = val
        else:
            globals()[name] = val


SETTINGS = PROFILE = FILTERS = COMPANIES = PROVIDERS = None
reload_config()


def needs_setup():
    return not PROFILE or not PROFILE.get("name")


def today():
    return dt.date.today().isoformat()


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def log(msg):
    line = f"[{now()}] {msg}"
    try:
        print(line, flush=True)
    except (UnicodeEncodeError, OSError):
        pass
    try:
        with open(os.path.join(LOGS, "agent.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ------------------------------------------------------------------ http
def http_get(url, timeout=25, headers=None):
    h = {"User-Agent": "Mozilla/5.0 (JobAgent personal use)", "Accept": "application/json, text/plain, */*"}
    h.update(headers or {})
    rq = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def http_json(url, timeout=25, data=None, headers=None):
    h = {"User-Agent": "Mozilla/5.0 (JobAgent personal use)", "Accept": "application/json"}
    h.update(headers or {})
    body = None
    if data is not None:
        body = json.dumps(data).encode()
        h["Content-Type"] = "application/json"
    rq = urllib.request.Request(url, data=body, headers=h)
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


# ------------------------------------------------------------------ db
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs(
  id TEXT PRIMARY KEY, company TEXT, tier TEXT, category TEXT, title TEXT, location TEXT, url TEXT,
  ats TEXT, posted TEXT, first_seen TEXT, last_seen TEXT, active INTEGER DEFAULT 1,
  score REAL, loc_class TEXT, family TEXT, early INTEGER, yoe INTEGER, desc TEXT,
  status TEXT DEFAULT 'new', alerted INTEGER DEFAULT 0, baseline INTEGER DEFAULT 0, visa TEXT DEFAULT 'unknown');
CREATE TABLE IF NOT EXISTS triage(
  job_id TEXT PRIMARY KEY, fit REAL, verdict TEXT, resume TEXT, yoe_gap TEXT, reason TEXT,
  needs_referral INTEGER, flags TEXT, at TEXT);
CREATE TABLE IF NOT EXISTS packets(
  job_id TEXT PRIMARY KEY, dir TEXT, at TEXT, referral_at TEXT, filled_at TEXT, fill_report TEXT);
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE IF NOT EXISTS budget(day TEXT, kind TEXT, n INTEGER, PRIMARY KEY(day, kind));
CREATE TABLE IF NOT EXISTS llm_log(ts TEXT, kind TEXT, model TEXT, inp INTEGER, cache_create INTEGER, cache_read INTEGER, outp INTEGER, cost REAL, provider TEXT, secs REAL);
CREATE TABLE IF NOT EXISTS watch(job_id TEXT PRIMARY KEY, added TEXT, reason TEXT, next_step TEXT, referral_state TEXT DEFAULT 'none', deadline TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS programs(id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, url TEXT, window TEXT, eligibility TEXT, notes TEXT, added TEXT);
CREATE TABLE IF NOT EXISTS inbox(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, text TEXT, context TEXT, status TEXT DEFAULT 'new', result TEXT);
CREATE TABLE IF NOT EXISTS chat(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, role TEXT, text TEXT);
CREATE TABLE IF NOT EXISTS referrals(job_id TEXT PRIMARY KEY, dir TEXT, at TEXT, how TEXT, state TEXT DEFAULT 'planned');
CREATE TABLE IF NOT EXISTS picks(job_id TEXT PRIMARY KEY, ts TEXT, actions TEXT);
CREATE TABLE IF NOT EXISTS outcomes(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, job_id TEXT, event TEXT, note TEXT);
"""


def db():
    c = sqlite3.connect(os.path.join(DATA, "jobs.db"), timeout=90)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=90000")
    c.executescript(SCHEMA)
    for col, typ in (("provider", "TEXT"), ("secs", "REAL")):  # forward-compatible migration of older workspaces
        try:
            c.execute(f"ALTER TABLE llm_log ADD COLUMN {col} {typ}")
        except sqlite3.OperationalError:
            pass
    return c


def budget_used(c, kind):
    r = c.execute("SELECT n FROM budget WHERE day=? AND kind=?", (today(), kind)).fetchone()
    return r["n"] if r else 0


def budget_add(c, kind, n=1):
    c.execute("INSERT INTO budget(day,kind,n) VALUES(?,?,?) ON CONFLICT(day,kind) DO UPDATE SET n=n+?", (today(), kind, n, n))
    c.commit()


def meta_get(c, k, default=None):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(c, k, v):
    c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    c.commit()


def record_outcome(job_id, event, note=""):
    """Feedback loop: applied / skipped (with reason) / replied ... feeds memory and learning."""
    c = db()
    c.execute("INSERT INTO outcomes(ts,job_id,event,note) VALUES(?,?,?,?)", (now(), job_id, event, note))
    c.commit()


# ------------------------------------------------------------------ notify
def _ps_toast(title, body):
    esc = lambda s: s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace("'", "''")
    ps = (
        "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] | Out-Null;"
        "[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] | Out-Null;"
        "$x = New-Object Windows.Data.Xml.Dom.XmlDocument;"
        f"$x.LoadXml('<toast><visual><binding template=\"ToastGeneric\"><text>{esc(title)}</text><text>{esc(body)}</text></binding></visual></toast>');"
        "$t = New-Object Windows.UI.Notifications.ToastNotification $x;"
        "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\\WindowsPowerShell\\v1.0\\powershell.exe').Show($t)"
    )
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps], capture_output=True, timeout=30)


def notify(title, body):
    """Desktop toast (Windows) and optional phone push (ntfy). Always appended to out/notifications.log."""
    body = body[:400]
    try:
        with open(os.path.join(OUT, "notifications.log"), "a", encoding="utf-8") as f:
            f.write(f"[{now()}] {title} :: {body}\n")
    except OSError:
        pass
    n = SETTINGS.get("notify", {})
    if n.get("windows_toast") and os.name == "nt":
        try:
            _ps_toast(title, body)
        except Exception as e:
            log(f"toast failed: {e}")
    if n.get("ntfy_topic"):
        try:
            rq = urllib.request.Request(f"{n.get('ntfy_server', 'https://ntfy.sh')}/{n['ntfy_topic']}", data=body.encode("utf-8"),
                                        headers={"Title": title.encode("ascii", "ignore").decode()})
            urllib.request.urlopen(rq, timeout=15).read()
        except Exception as e:
            log(f"ntfy failed: {e}")


# ------------------------------------------------------------------ LLM entry point (provider-agnostic; see llm.py)
def claude_bin():
    return shutil.which("claude") or shutil.which("claude.cmd") or "claude"


def run_claude(prompt, **kw):
    """Backward-compatible name. All LLM calls go through llm.run(): routing, provider adapters, budgets, usage log."""
    import llm
    return llm.run(prompt, **kw)


def extract_json(text):
    """Pull the first JSON object/array out of an LLM answer (handles ``` fences)."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    for opener, closer in (("{", "}"), ("[", "]")):
        i = t.find(opener)
        j = t.rfind(closer)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError("no JSON found in model output: " + text[:200])


def slug(s, n=48):
    return re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-")[:n].lower()


BACKGROUND = False  # set by the scheduled cycle: background work must leave headroom for interactive use (see llm.run)


def backfill_picks():
    """Picks made before the My-picks list existed. A referral plan with no packet is always something the user asked for (the scheduled cycle builds the packet first), and any
    application folder that shows up in the GUI/CLI run log was started by hand. Idempotent; returns how many jobs were added."""
    c = db()
    have = {r[0] for r in c.execute("SELECT job_id FROM picks")}
    found = {}
    for r in c.execute("SELECT job_id FROM referrals WHERE job_id NOT IN (SELECT job_id FROM packets)"):
        found.setdefault(r[0], set()).add("referral")
    try:
        txt = open(os.path.join(LOGS, "gui_runs.log"), encoding="utf-8", errors="replace").read()
    except OSError:
        txt = ""
    folders = set(re.findall(r"(20\d\d-\d\d-\d\d_[A-Za-z0-9._-]+)", txt))
    if folders:
        for tbl, act in (("packets", "packet"), ("referrals", "referral")):
            for r in c.execute(f"SELECT job_id, dir FROM {tbl}"):
                if any(f in (r["dir"] or "") for f in folders):
                    found.setdefault(r["job_id"], set()).add(act)
    n = 0
    for jid, acts in found.items():
        if jid in have:
            continue
        c.execute("INSERT OR IGNORE INTO picks(job_id,ts,actions) VALUES(?,?,?)", (jid, now(), ",".join(sorted(acts))))
        n += 1
    c.commit()
    return n


def add_pick(job_id, action):
    """A job the USER chose to work on (clicked prepare/referral/resume/fill, or added through the inbox). Shown in 'My picks'."""
    c = db()
    r = c.execute("SELECT actions FROM picks WHERE job_id=?", (job_id,)).fetchone()
    acts = sorted(set((r["actions"].split(",") if r and r["actions"] else []) + [action]))
    c.execute("INSERT OR REPLACE INTO picks(job_id,ts,actions) VALUES(?,?,?)", (job_id, now(), ",".join(acts)))
    c.commit()
