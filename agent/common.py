"""Shared helpers: paths, config, SQLite store, logging, notifications, Claude CLI wrapper.
Stdlib only on purpose (no pip needed)."""
import json, os, re, sqlite3, subprocess, datetime as dt, urllib.request, shutil

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
CFG = os.path.join(ROOT, "config")
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "out")
APPS = os.path.join(OUT, "applications")
LOGS = os.path.join(ROOT, "logs")
for d in (DATA, OUT, APPS, LOGS):
    os.makedirs(d, exist_ok=True)


def load(name):
    with open(os.path.join(CFG, name), encoding="utf-8") as f:
        return json.load(f)


SETTINGS = load("settings.json")
PROFILE = load("profile.json")
FILTERS = load("filters.json")
COMPANIES = load("companies.json")


def today():
    return dt.date.today().isoformat()


def now():
    return dt.datetime.now().isoformat(timespec="seconds")


def log(msg):
    line = f"[{now()}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(LOGS, "agent.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- http
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


# ---------------------------------------------------------------- db
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
CREATE TABLE IF NOT EXISTS llm_log(ts TEXT, kind TEXT, model TEXT, inp INTEGER, cache_create INTEGER, cache_read INTEGER, outp INTEGER, cost REAL);
CREATE TABLE IF NOT EXISTS watch(job_id TEXT PRIMARY KEY, added TEXT, reason TEXT, next_step TEXT, referral_state TEXT DEFAULT 'none', deadline TEXT, note TEXT);
CREATE TABLE IF NOT EXISTS programs(id TEXT PRIMARY KEY, name TEXT, org TEXT, kind TEXT, url TEXT, window TEXT, eligibility TEXT, notes TEXT, added TEXT);
CREATE TABLE IF NOT EXISTS budget(day TEXT, kind TEXT, n INTEGER, PRIMARY KEY(day, kind));
"""


def db():
    c = sqlite3.connect(os.path.join(DATA, "jobs.db"), timeout=90)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA busy_timeout=90000")
    c.executescript(SCHEMA)
    return c


def budget_used(c, kind):
    r = c.execute("SELECT n FROM budget WHERE day=? AND kind=?", (today(), kind)).fetchone()
    return r["n"] if r else 0


def budget_add(c, kind, n=1):
    c.execute("INSERT INTO budget(day,kind,n) VALUES(?,?,?) ON CONFLICT(day,kind) DO UPDATE SET n=n+?",
              (today(), kind, n, n))
    c.commit()


def meta_get(c, k, default=None):
    r = c.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return r["v"] if r else default


def meta_set(c, k, v):
    c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, v))
    c.commit()


# ---------------------------------------------------------------- notify
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
    subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                   capture_output=True, timeout=30)


def notify(title, body):
    """Desktop toast, optional phone push (ntfy). Always appended to out/notifications.log."""
    body = body[:400]
    try:
        with open(os.path.join(OUT, "notifications.log"), "a", encoding="utf-8") as f:
            f.write(f"[{now()}] {title} :: {body}\n")
    except OSError:
        pass
    n = SETTINGS.get("notify", {})
    if n.get("windows_toast"):
        try:
            _ps_toast(title, body)
        except Exception as e:  # never let a notification kill a run
            log(f"toast failed: {e}")
    if n.get("ntfy_topic"):
        try:
            rq = urllib.request.Request(f"{n.get('ntfy_server','https://ntfy.sh')}/{n['ntfy_topic']}",
                                        data=body.encode("utf-8"), headers={"Title": title.encode("ascii", "ignore").decode()})
            urllib.request.urlopen(rq, timeout=15).read()
        except Exception as e:
            log(f"ntfy failed: {e}")


# ---------------------------------------------------------------- claude
def claude_bin():
    return shutil.which("claude") or shutil.which("claude.cmd") or "claude"


LEAN_SYSTEM = "You are a precise assistant inside an automation pipeline. Follow the task exactly. When asked for JSON, output only the JSON."
_EMPTY_MCP = os.path.join(DATA, "mcp-empty.json")


def run_claude(prompt, *, kind="misc", allowed_tools="", model=None, timeout=300, extra_args=None, cwd=None, max_turns=None,
               mcp_config=None, persist=False):
    """Run `claude -p` headless on the user's subscription in LEAN mode: no user MCP servers, no skills, no default
    Claude Code system prompt, only the built-in tools actually needed (cuts ~27k input tokens/call to <1k).
    mcp_config: path of an MCP config to load (browser steps). persist=True keeps the session transcript for auditing.
    Logs token usage to the llm_log table. Returns (text, raw_json_or_None)."""
    if not os.path.exists(_EMPTY_MCP):
        json.dump({"mcpServers": {}}, open(_EMPTY_MCP, "w"))
    builtin = ",".join(x for x in allowed_tools.split(",") if x and not x.startswith("mcp__"))
    args = [claude_bin(), "-p", "--output-format", "json", "--strict-mcp-config", "--mcp-config", mcp_config or _EMPTY_MCP,
            "--disable-slash-commands", "--setting-sources", "user", "--system-prompt", LEAN_SYSTEM, "--tools", builtin]
    if not persist:
        args.append("--no-session-persistence")
    if model:
        args += ["--model", model]
    if allowed_tools:
        args += ["--allowedTools", allowed_tools]
    if max_turns:
        args += ["--max-turns", str(max_turns)]
    args += extra_args or []
    try:
        p = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8", timeout=timeout, cwd=cwd or ROOT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"claude timed out after {timeout}s")
    if p.returncode != 0 and not p.stdout.strip():
        raise RuntimeError(f"claude failed ({p.returncode}): {p.stderr[:500]}")
    try:
        j = json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.stdout, None
    try:
        u = j.get("usage", {})
        c = db()
        c.execute("INSERT INTO llm_log(ts,kind,model,inp,cache_create,cache_read,outp,cost) VALUES(?,?,?,?,?,?,?,?)",
                  (now(), kind, model or "default", u.get("input_tokens", 0), u.get("cache_creation_input_tokens", 0),
                   u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0), j.get("total_cost_usd", 0)))
        c.commit()
    except Exception:
        pass
    if j.get("is_error"):
        raise RuntimeError(f"claude error: {str(j.get('result'))[:500]}")
    return j.get("result", ""), j


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
