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

PAGE = r"""<!doctype html><meta charset=utf-8><title>JobAgent</title><meta name=viewport content="width=device-width,initial-scale=1">
<style>
:root{--bg:#0f1115;--card:#171a21;--bd:#262b36;--tx:#e6e6e6;--mut:#9aa3b2;--ac:#7cc4ff;--ok:#14532d;--warn:#713f12;--bad:#7f1d1d}
*{box-sizing:border-box}body{font:14px system-ui,Segoe UI,sans-serif;background:var(--bg);color:var(--tx);margin:0}
header{position:sticky;top:0;background:#0b0d11;border-bottom:1px solid var(--bd);padding:10px 16px;z-index:5;display:flex;gap:10px;flex-wrap:wrap;align-items:center}
h1{font-size:16px;margin:0 12px 0 0}main{max-width:1180px;margin:0 auto;padding:12px 16px 80px}
button{background:#232938;color:var(--tx);border:1px solid #333b4d;border-radius:6px;padding:5px 10px;cursor:pointer;font-size:12px}button:hover{border-color:var(--ac)}
button.p{background:#1d4ed8;border-color:#1d4ed8}button.g{background:var(--ok)}button.w{background:var(--warn)}
.tabs{display:flex;gap:6px;flex-wrap:wrap;margin:10px 0}.tabs button.on{background:var(--ac);color:#06121f;border-color:var(--ac)}
.card{background:var(--card);border:1px solid var(--bd);border-radius:8px;padding:9px 12px;margin:8px 0}
.m{color:var(--mut);font-size:12px;margin-top:3px}.pill{font-size:11px;padding:1px 7px;border-radius:9px;background:#2a3040;margin-left:4px}
.APPLY{background:var(--ok)}.STRETCH{background:var(--warn)}.visa-no{background:var(--bad)}.visa-yes{background:var(--ok)}
a{color:var(--ac)}input[type=search]{background:#0b0d11;color:var(--tx);border:1px solid var(--bd);border-radius:6px;padding:6px 10px;min-width:260px}
pre{background:#0b0d11;border:1px solid var(--bd);border-radius:8px;padding:10px;max-height:280px;overflow:auto;font-size:11px;white-space:pre-wrap}
.row{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}.stat{color:var(--mut);font-size:12px}
</style>
<header><h1>JobAgent</h1>
<button class=p onclick="run('cycle')">Run full cycle</button><button onclick="run('poll')">Poll</button><button onclick="run('triage')">Triage</button>
<button onclick="run('scout')">Scout</button><button onclick="run('today')">Today plan</button><button onclick="run('dashboard')">Rebuild</button>
<span class=stat id=stat></span></header>
<main>
<div class=tabs id=tabs></div>
<div id=toolbar style="margin:6px 0"><input type=search id=q placeholder="filter by company / title / location" oninput="render()"> <span class=stat id=count></span></div>
<div id=list></div>
<h3>Today's plan</h3><pre id=today></pre>
<h3>Activity log</h3><pre id=log></pre>
</main>
<script>
const H={'X-JobAgent':'1','Content-Type':'application/json'};
let S={jobs:[],programs:[]},TAB='ready';
const TABS=[['ready','Ready to submit'],['fill','Needs you'],['watch','Watchlist'],['packet','Packets'],['todo','Triaged (APPLY/STRETCH)'],['new','New strong'],['applied','Applied'],['programs','Programs']];
const e=s=>String(s??'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
async function api(p,o){const r=await fetch(p,o);return r.json()}
async function load(){S=await api('/api/state');document.getElementById('stat').textContent=
 `last poll ${S.last_poll||'-'} | ${S.active} active | calls today: ${S.budget.triage_calls||0}t ${S.budget.packets||0}p ${S.budget.referrals||0}r ${S.budget.fills||0}f | ${S.running.length?('running: '+S.running.join(', ')):'idle'}`;
 document.getElementById('today').textContent=S.today||'(run Today plan)';render()}
function tabs(){document.getElementById('tabs').innerHTML=TABS.map(([k,l])=>`<button class="${k==TAB?'on':''}" onclick="TAB='${k}';render()">${l} (${count(k)})</button>`).join('')}
function sel(k){return S.jobs.filter(j=>k=='ready'?j.status=='ready':k=='fill'?j.status.startsWith('fill-'):k=='watch'?j.status=='watch':k=='packet'?j.status=='packet':
 k=='todo'?(j.status=='triaged'&&['APPLY','STRETCH'].includes(j.verdict)):k=='new'?(j.status=='new'&&j.score>=65):k=='applied'?j.status=='applied':false)}
function count(k){return k=='programs'?S.programs.length:sel(k).length}
function btn(label,cmd,id,cls=''){return `<button class="${cls}" onclick="run('${cmd}','${e(id).replace(/'/g,"\\'")}')">${label}</button>`}
function card(j){const f=j.dir?`<button onclick="openp('${e(j.dir).replace(/\\/g,'/')}')">Open packet folder</button>`:'';
 return `<div class=card><b>${e(j.company)}</b> <span class=pill>tier ${e(j.tier)}</span> <span class="pill ${e(j.verdict)}">${e(j.verdict||'untriaged')} ${Math.round(j.fit??j.score)}</span> <span class=pill>${e(j.loc_class)}</span> <span class="pill visa-${e(j.visa)}">visa:${e(j.visa)}</span>
 <div><a href="${e(j.url)}" target=_blank>${e(j.title)}</a> <span class=m>${e((j.location||'').slice(0,80))} | ${e(j.ats)} | ${e((j.first_seen||'').slice(0,10))}</span></div>
 <div class=m>${e(j.reason||'')} ${j.yoe_gap?('| gap: '+e(j.yoe_gap)):''} ${j.resume?('| resume: '+e(j.resume)):''} ${j.watch_note?('| '+e(j.watch_note)):''}</div>
 <div class=row>${btn('Packet','packet',j.id)}${btn('Referral plan (LinkedIn)','referral',j.id)}${btn('Fill form','fill',j.id,'p')}${f}${btn('Applied','mark',j.id+' applied','g')}${btn('Skip','mark',j.id+' skip','w')}</div></div>`}
function render(){tabs();const q=document.getElementById('q').value.toLowerCase();let h='';
 if(TAB=='programs'){h=S.programs.filter(p=>JSON.stringify(p).toLowerCase().includes(q)).map(p=>`<div class=card><b>${e(p.name)}</b> <span class=pill>${e(p.kind)}</span><div class=m>${e(p.org)} | window: ${e(p.window)} | eligible: ${e(p.eligibility)} | <a href="${e(p.url)}" target=_blank>link</a></div></div>`).join('')}
 else{const rows=sel(TAB).filter(j=>(j.company+j.title+j.location).toLowerCase().includes(q));document.getElementById('count').textContent=rows.length+' shown';h=rows.slice(0,150).map(card).join('')}
 document.getElementById('list').innerHTML=h||'<div class=m>nothing here</div>'}
async function run(cmd,id=''){const r=await api('/api/run',{method:'POST',headers:H,body:JSON.stringify({cmd,arg:id})});setTimeout(load,800);if(r.error)alert(r.error)}
async function openp(p){await api('/api/open',{method:'POST',headers:H,body:JSON.stringify({path:p})})}
async function logs(){const r=await fetch('/api/log');document.getElementById('log').textContent=await r.text()}
load();logs();setInterval(load,15000);setInterval(logs,4000);
</script>"""


def state():
    c = db()
    rows = c.execute("""SELECT j.id,j.company,j.tier,j.title,j.location,j.url,j.ats,j.first_seen,j.score,j.loc_class,j.visa,j.status,t.verdict,t.fit,t.reason,t.yoe_gap,t.resume,p.dir,w.note watch_note
                        FROM jobs j LEFT JOIN triage t ON t.job_id=j.id LEFT JOIN packets p ON p.job_id=j.id LEFT JOIN watch w ON w.job_id=j.id
                        WHERE j.active=1 AND j.status NOT IN ('skipped','dismissed','ineligible') AND (j.status!='new' OR j.score>=65)
                        ORDER BY COALESCE(t.fit,j.score) DESC LIMIT 1500""").fetchall()
    progs = c.execute("SELECT name,org,kind,window,eligibility,url FROM programs ORDER BY added DESC").fetchall()
    today_md = ""
    tp = os.path.join(OUT, "today.md")
    if os.path.exists(tp):
        today_md = open(tp, encoding="utf-8").read()
    with _lock:
        running = [k for k, p in JOBS.items() if p.poll() is None]
    return {"jobs": [dict(r) for r in rows], "programs": [dict(r) for r in progs], "last_poll": meta_get(c, "last_poll"),
            "active": c.execute("SELECT COUNT(*) FROM jobs WHERE active=1").fetchone()[0],
            "budget": {k: budget_used(c, k) for k in ("triage_calls", "packets", "referrals", "fills", "scout", "linkedin")}, "running": running, "today": today_md}


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
            return self._send(200, PAGE, "text/html")
        if path == "/api/state":
            return self._send(200, json.dumps(state(), default=str))
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
