"""Local web GUI for JobAgent (stdlib only). `python agent/run.py gui` -> http://127.0.0.1:8777
Binds to localhost only; state-changing requests need a custom header (basic CSRF protection) and only whitelisted commands run."""
import http.server, json, os, re, subprocess, sys, threading, webbrowser, urllib.parse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from common import db, ROOT, OUT, LOGS, SETTINGS, meta_get, budget_used  # noqa: E402

PORT = SETTINGS.get("gui_port", 8777)
RUN_PY = os.path.join(ROOT, "agent", "run.py")
ALLOWED = {"cycle": False, "poll": False, "triage": False, "scout": False, "today": False, "dashboard": False, "usage": False, "yc-profile": False,
           "packet": True, "referral": True, "fill": True, "linkedin": True, "mark": True, "enable-schedule": False, "disable-schedule": False}
JOBS = {}   # name -> Popen
_lock = threading.Lock()

STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_static", "index.html")


def page():
    with open(STATIC, encoding="utf-8") as f:
        return f.read()


def packet_files(job_id):
    c = db()
    r = c.execute("SELECT dir FROM packets WHERE job_id=?", (job_id,)).fetchone()
    if not r or not r["dir"] or not os.path.isdir(r["dir"]) or not os.path.abspath(r["dir"]).startswith(os.path.abspath(OUT)):
        return {"exists": False, "files": {}}
    d = r["dir"]
    rd = lambda n: open(os.path.join(d, n), encoding="utf-8").read() if os.path.exists(os.path.join(d, n)) else ""
    return {"exists": True, "files": {"README": rd("README.md"), "COVER": rd("cover_letter.txt"), "REFERRAL": rd("REFERRAL.md"), "FILL": rd("FILL_REPORT.md")}}


def state():
    c = db()
    rows = c.execute("""SELECT j.id,j.company,j.tier,j.title,j.location,j.url,j.ats,j.first_seen,j.score,j.loc_class,j.visa,j.status,t.verdict,t.fit,t.reason,t.yoe_gap,t.resume,p.dir,w.note watch_note
                        FROM jobs j LEFT JOIN triage t ON t.job_id=j.id LEFT JOIN packets p ON p.job_id=j.id LEFT JOIN watch w ON w.job_id=j.id
                        WHERE j.active=1 AND j.status NOT IN ('skipped','dismissed','ineligible') AND (j.status!='new' OR j.score>=65)
                        ORDER BY COALESCE(t.fit,j.score) DESC LIMIT 1500""").fetchall()
    progs = c.execute("SELECT name,org,kind,window,eligibility,url FROM programs ORDER BY added DESC").fetchall()
    today_md, plan = "", None
    pj = os.path.join(OUT, "today.json")
    if os.path.exists(pj):
        try:
            plan = json.load(open(pj, encoding="utf-8"))
        except json.JSONDecodeError:
            plan = None
    tp = os.path.join(OUT, "today.md")
    if os.path.exists(tp):
        today_md = open(tp, encoding="utf-8").read()
    with _lock:
        running = [k for k, p in JOBS.items() if p.poll() is None]
    return {"jobs": [dict(r) for r in rows], "programs": [dict(r) for r in progs], "last_poll": meta_get(c, "last_poll"),
            "active": c.execute("SELECT COUNT(*) FROM jobs WHERE active=1").fetchone()[0],
            "budget": {k: budget_used(c, k) for k in ("triage_calls", "packets", "referrals", "fills", "scout", "linkedin")}, "running": running, "today": today_md, "day": __import__("datetime").date.today().isoformat(), "plan": plan}


class H(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Content-Length", str(len(b)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(b)

    def _host_ok(self):
        return (self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost"))

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "{}")
        path = urllib.parse.urlparse(self.path).path
        if path == "/":
            return self._send(200, page(), "text/html")
        if path == "/api/state":
            return self._send(200, json.dumps(state(), default=str))
        if path == "/api/packet":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            return self._send(200, json.dumps(packet_files((q.get("id") or [""])[0])))
        if path == "/api/log":
            lp = os.path.join(LOGS, "agent.log")
            txt = ""
            if os.path.exists(lp):
                with open(lp, encoding="utf-8", errors="replace") as f:
                    txt = "".join(f.readlines()[-60:])
            txt = re.sub(r"feed health: .*", "feed health: (omitted)", txt)
            return self._send(200, txt, "text/plain")
        self._send(404, "{}")

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-JobAgent") != "1":
            return self._send(403, json.dumps({"error": "forbidden"}))
        n = int(self.headers.get("Content-Length") or 0)
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._send(400, json.dumps({"error": "bad json"}))
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/run":
            cmd, arg = d.get("cmd", ""), str(d.get("arg", "")).strip()
            if cmd not in ALLOWED:
                return self._send(400, json.dumps({"error": "command not allowed"}))
            if ALLOWED[cmd] and not arg:
                return self._send(400, json.dumps({"error": "needs a job id"}))
            args = [sys.executable, RUN_PY, cmd] + (arg.split() if cmd == "mark" else ([arg] if arg else []))
            key = f"{cmd} {arg[:30]}".strip()
            with _lock:
                p = JOBS.get(key)
                if p and p.poll() is None:
                    return self._send(200, json.dumps({"error": "already running"}))
                logf = open(os.path.join(LOGS, "gui_runs.log"), "a", encoding="utf-8")
                JOBS[key] = subprocess.Popen(args, stdout=logf, stderr=subprocess.STDOUT, cwd=ROOT)
            return self._send(200, json.dumps({"started": key}))
        if path == "/api/open":
            p = os.path.abspath(str(d.get("path", "")))
            if not p.startswith(os.path.abspath(OUT)) or not os.path.exists(p):
                return self._send(400, json.dumps({"error": "path not allowed"}))
            os.startfile(p)  # Windows: open folder/file in Explorer
            return self._send(200, json.dumps({"ok": True}))
        self._send(404, "{}")


def serve(open_browser=True):
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", PORT), H)
    url = f"http://127.0.0.1:{PORT}"
    print("JobAgent GUI at", url, "(Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    serve()
