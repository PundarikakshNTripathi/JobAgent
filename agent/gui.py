"""Local web GUI for JobAgent (stdlib only). `python agent/run.py gui` -> http://127.0.0.1:8777
Binds to localhost only; every state-changing request needs a custom header (basic CSRF protection); only whitelisted commands run."""
import http.server, json, os, re, subprocess, sys, threading, webbrowser, urllib.parse, datetime as dt
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import common
from common import db, CORE, HOME, OUT, LOGS, DATA, meta_get, budget_used  # noqa: E402

RUN_PY = os.path.join(CORE, "agent", "run.py")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "gui_static", "index.html")
# command -> needs an argument?
ALLOWED = {"cycle": False, "poll": False, "triage": False, "scout": False, "today": False, "dashboard": False, "usage": False, "yc-profile": False, "inbox": False,
           "reflect": False, "bench": False, "inbox-retry": False, "enable-schedule": False, "disable-schedule": False, "packet": True, "referral": True, "resume": True, "fill": True,
           "linkedin": True, "mark": True, "agent": True, "retry": False}
SETTING_KEYS = {"poll_every_minutes", "min_heuristic_score_for_llm", "instant_alert_score", "max_triage_calls_per_day", "triage_batch_size", "max_triage_per_cycle", "triage_desc_chars",
                "max_packets_per_day", "packet_min_fit", "max_referrals_per_day", "max_linkedin_per_day", "linkedin_browse", "max_fills_per_day", "auto_fill", "fill_window_local_hours",
                "scout_every_days", "guard_minutes", "notify", "web", "humanize", "memory", "agent_mode", "llm", "include_new_grad_list", "recent_days_github_lists"}
JOBS, _lock, CHAT_BUSY = {}, threading.Lock(), {"on": False}
_agy_cache = {"t": 0, "models": []}


def page():
    with open(STATIC, encoding="utf-8") as f:
        return f.read()


def jread(name, default):
    try:
        return json.load(open(os.path.join(OUT, name), encoding="utf-8"))
    except (OSError, ValueError):
        return default


def packet_files(job_id):
    c = db()
    r = c.execute("SELECT dir FROM packets WHERE job_id=?", (job_id,)).fetchone() or c.execute("SELECT dir FROM referrals WHERE job_id=?", (job_id,)).fetchone()
    if not r or not r["dir"] or not os.path.isdir(r["dir"]) or not os.path.abspath(r["dir"]).startswith(os.path.abspath(OUT)):
        return {"exists": False, "files": {}}
    d = r["dir"]
    rd = lambda n: open(os.path.join(d, n), encoding="utf-8").read() if os.path.exists(os.path.join(d, n)) else ""
    files = {"README": rd("README.md"), "COVER": rd("cover_letter.txt"), "REFERRAL": rd("REFERRAL.md"), "RESUME": rd("RESUME.md"), "FILL": rd("FILL_REPORT.md")}
    return {"exists": any(files.values()), "files": files, "dir": d}


def state():
    c = db()
    rows = c.execute("""SELECT j.id,j.company,j.tier,j.title,j.location,j.url,j.ats,j.first_seen,j.score,j.loc_class,j.visa,j.yoe,j.status,t.verdict,t.fit,t.reason,t.yoe_gap,t.resume,
                               p.dir,w.note watch_note, (SELECT 1 FROM referrals r WHERE r.job_id=j.id) has_ref
                        FROM jobs j LEFT JOIN triage t ON t.job_id=j.id LEFT JOIN packets p ON p.job_id=j.id LEFT JOIN watch w ON w.job_id=j.id
                        WHERE j.active=1 AND j.status NOT IN ('skipped','dismissed','ineligible') AND (j.status!='new' OR j.score>=65)
                        ORDER BY COALESCE(t.fit,j.score) DESC LIMIT 1500""").fetchall()
    progs = c.execute("SELECT name,org,kind,window,eligibility,url FROM programs ORDER BY added DESC").fetchall()
    import chatcmd, time as _t
    running = [k for k, p in list(chatcmd.RUNNING.items()) if p.poll() is None] + (["chat"] if CHAT_BUSY["on"] else [])
    lock = os.path.join(DATA, "cycle.lock")
    if "cycle" not in running and os.path.exists(lock) and _t.time() - os.path.getmtime(lock) < 100 * 60:
        running.append("cycle")  # a scheduled run is in progress
    tp = os.path.join(OUT, "today.md")
    return {"jobs": [dict(r) for r in rows], "programs": [dict(r) for r in progs], "last_poll": meta_get(c, "last_poll"), "setup_needed": common.needs_setup(),
            "active": c.execute("SELECT COUNT(*) FROM jobs WHERE active=1").fetchone()[0], "running": running, "day": dt.date.today().isoformat(), "plan": jread("today.json", None),
            "budget": {k: budget_used(c, k) for k in ("triage_calls", "packets", "referrals", "fills", "scout", "linkedin")},
            "inbox_pending": c.execute("SELECT COUNT(*) FROM inbox WHERE status IN ('new','processing')").fetchone()[0], "name": common.PROFILE.get("first_name", ""), "home": HOME, "cooldowns": cooldowns(), "stage": (meta_get(c, "cycle_stage") or "|").split("|", 1)[1], "schedule": schedule_info()}


def _csv(q, k):
    return [x for x in (q.get(k) or [""])[0].split(",") if x]


def browse_jobs(q):
    """Job-board style search over ALL tracked postings (analysed or not). q = parsed query string."""
    c = db()
    w, a = ["j.active=1"], []
    one = lambda k, d="": (q.get(k) or [d])[0]
    status = one("status")
    if status:
        w.append("j.status IN (%s)" % ",".join("?" * len(status.split(","))))
        a += status.split(",")
    elif one("hidden") != "1" and not one("id"):
        w.append("j.status NOT IN ('skipped','dismissed','ineligible')")
    for term in one("q").lower().split():
        w.append("(lower(j.company) LIKE ? OR lower(j.title) LIKE ? OR lower(j.location) LIKE ?)")
        a += [f"%{term}%"] * 3
    for key, col in (("family", "j.family"), ("loc", "j.loc_class"), ("tier", "j.tier"), ("source", "j.ats"), ("visa", "j.visa"), ("verdict", "t.verdict"), ("category", "j.category")):
        vals = _csv(q, key)
        if vals:
            w.append(f"{col} IN ({','.join('?' * len(vals))})")
            a += vals
    if one("id"):
        w.append("j.id=?")
        a.append(one("id"))
    if one("maxyoe") != "":
        w.append("(j.yoe IS NULL OR j.yoe<=?)")
        a.append(int(one("maxyoe")))
    if one("company"):
        w.append("lower(j.company) LIKE ?")
        a.append(f"%{one('company').lower()}%")
    intern = "(lower(j.title) LIKE '%intern%' OR lower(j.title) LIKE '%co-op%' OR lower(j.title) LIKE '%apprentice%' OR lower(j.title) LIKE '%residen%' OR lower(j.title) LIKE '%fellow%')"
    lv = one("level")
    if lv == "intern":
        w.append(intern)
    elif lv == "newgrad":
        w.append(f"j.early=1 AND NOT {intern}")
    elif lv == "earlyany":
        w.append("j.early=1")
    elif lv == "experienced":
        w.append("j.early=0")
    an = one("analysis")
    if an == "analysed":
        w.append("t.job_id IS NOT NULL")
    elif an == "not":
        w.append("t.job_id IS NULL")
    hr = "(EXISTS (SELECT 1 FROM referrals r WHERE r.job_id=j.id) OR EXISTS (SELECT 1 FROM packets p WHERE p.job_id=j.id AND p.referral_at IS NOT NULL))"
    if one("hasref") == "1":
        w.append(hr)
    if one("hasref") == "0":
        w.append("NOT " + hr)
    if one("haspacket") == "1":
        w.append("EXISTS (SELECT 1 FROM packets p WHERE p.job_id=j.id)")
    if one("minscore"):
        w.append("COALESCE(t.fit,j.score)>=?")
        a.append(float(one("minscore")))
    if one("days"):
        w.append("j.first_seen>=date('now',?,'localtime')")
        a.append(f"-{int(one('days'))} day")
    order = {"fit": "COALESCE(t.fit,j.score) DESC", "score": "j.score DESC", "new": "j.first_seen DESC", "company": "lower(j.company), COALESCE(t.fit,j.score) DESC"}.get(one("sort", "fit"), "COALESCE(t.fit,j.score) DESC")
    per = max(5, min(100, int(one("per", "25"))))
    page = max(1, int(one("page", "1")))
    base = f"FROM jobs j LEFT JOIN triage t ON t.job_id=j.id WHERE {' AND '.join(w)}"
    total = c.execute(f"SELECT COUNT(*) {base}", a).fetchone()[0]
    rows = c.execute(f"""SELECT j.id,j.company,j.tier,j.title,j.location,j.url,j.ats,j.posted,j.first_seen,j.score,j.loc_class,j.family,j.early,j.yoe,j.visa,j.status,j.category,
                                substr(j.desc,1,900) snippet,t.verdict,t.fit,t.reason,t.yoe_gap,t.resume,{hr} has_ref,(EXISTS (SELECT 1 FROM packets p WHERE p.job_id=j.id)) has_packet,
                                (t.job_id IS NOT NULL) analysed {base} ORDER BY {order} LIMIT ? OFFSET ?""", (*a, per, (page - 1) * per)).fetchall()
    items = [dict(r) for r in rows]
    for it in items:
        it["snippet"] = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", it.get("snippet") or "")).strip()[:600]
    return {"total": total, "page": page, "per": per, "items": items}


def job_facets():
    c = db()
    g = lambda col, lim=15: [[r[0] or "(none)", r[1]] for r in c.execute(f"SELECT {col}, COUNT(*) n FROM jobs WHERE active=1 GROUP BY {col} ORDER BY n DESC LIMIT {lim}").fetchall()]
    return {"family": g("family"), "loc": g("loc_class"), "tier": g("tier"), "source": g("ats", 20), "visa": g("visa"), "company": g("company", 60),
            "category": g("category", 15), "total": c.execute("SELECT COUNT(*) FROM jobs WHERE active=1").fetchone()[0]}


def cooldowns():
    import llm
    return {n: cd[11:16] for n in llm.registry() for cd in [llm.cooldown_until(n)] if cd}


def picks_info():
    """Jobs the user chose to work on manually (prepare / referral / resume / fill clicks, or the inbox), with how far each one is."""
    c = db()
    out = []
    for r in c.execute("""SELECT p.job_id, p.ts, p.actions, j.company, j.title, j.location, j.url, j.score, j.tier, j.status, j.visa, j.yoe, j.loc_class, t.verdict, t.fit,
                                 pk.dir pdir, pk.filled_at, pk.referral_at, rf.dir rdir
                          FROM picks p JOIN jobs j ON j.id=p.job_id LEFT JOIN triage t ON t.job_id=p.job_id LEFT JOIN packets pk ON pk.job_id=p.job_id LEFT JOIN referrals rf ON rf.job_id=p.job_id
                          ORDER BY p.ts DESC LIMIT 200""").fetchall():
        d = r["pdir"] or r["rdir"]
        has = {"packet": bool(r["pdir"]), "referral": bool(r["rdir"] or r["referral_at"]), "resume": bool(d and os.path.exists(os.path.join(d, "RESUME.md"))), "fill": bool(r["filled_at"])}
        want = [a for a in (r["actions"] or "").split(",") if a in has]
        if not want and r["verdict"] in ("APPLY", "STRETCH"):
            want = ["packet", "referral"]  # added via the inbox and worth pursuing: the agent should prepare both
        done = all(has[a] for a in want) if want else True
        probs = []
        try:
            jr = c.execute("SELECT req FROM jobs WHERE id=?", (r["job_id"],)).fetchone()["req"]
            probs = (json.loads(jr) or {}).get("problems", []) if jr else []
        except (ValueError, TypeError):
            pass
        import browse
        ppl = [{"name": x["name"], "degree": x["degree"], "url": x["url"], "headline": x["headline"][:90]} for x in browse.cached_people(r["company"], days=30)[:4]]
        out.append({"problems": probs, "people": ppl, "id": r["job_id"], "ts": r["ts"], "company": r["company"], "title": r["title"], "location": r["location"], "url": r["url"], "score": r["score"], "tier": r["tier"],
                    "status": r["status"], "visa": r["visa"], "yoe": r["yoe"], "loc_class": r["loc_class"], "verdict": r["verdict"], "fit": r["fit"], "wanted": want, "has": has, "ready": done})
    return out


def providers_info():
    import llm
    now = dt.datetime.now().timestamp()
    if now - _agy_cache["t"] > 600:
        _agy_cache.update(t=now, models=llm.agy_models() if llm.available("agy")[0] else [])
    out = []
    for n, p in llm.registry().items():
        ok, why = llm.available(n)
        models = _agy_cache["models"] if n == "agy" else p.get("models", [])
        out.append({"name": n, "label": p.get("label", n), "type": p["type"], "ready": ok, "why": "" if ok else why, "verified": p.get("verified", False), "web": p["capabilities"].get("web", False),
                    "browser": p["capabilities"].get("browser", False), "models": models, "efforts": p.get("efforts", []), "note": p.get("note", ""),
                    "needs_key": bool(p.get("api_key_env")), "key_env": p.get("api_key_env", "")})
    return out


def usage_info():
    c = db()
    q = lambda days: [dict(r) for r in c.execute("""SELECT COALESCE(provider,'claude') provider, kind, COUNT(*) calls, COALESCE(SUM(inp+cache_create+cache_read),0) tin, COALESCE(SUM(outp),0) tout,
                                                   ROUND(COALESCE(SUM(cost),0),3) cost, ROUND(COALESCE(SUM(secs),0)) secs FROM llm_log WHERE ts>=date('now',?,'localtime') GROUP BY provider, kind
                                                   ORDER BY provider, calls DESC""", (f"-{days} day",)).fetchall()]
    daily = [dict(r) for r in c.execute("""SELECT substr(ts,1,10) day, COALESCE(provider,'claude') provider, COUNT(*) calls, COALESCE(SUM(inp+cache_create+cache_read+outp),0) tokens
                                           FROM llm_log WHERE ts>=date('now','-13 day','localtime') GROUP BY day, provider ORDER BY day""").fetchall()]
    return {"today": q(0), "week": q(7), "month": q(30), "daily": daily, "budgets": common.SETTINGS["llm"].get("budgets", {})}


_sched_cache = {"t": 0, "v": None}


def schedule_info(fresh=False):
    import schedule as sch, time as _t
    if fresh or _t.time() - _sched_cache["t"] > 120:
        _sched_cache.update(t=_t.time(), v=sch.info())
    return _sched_cache["v"]


def spawn(cmd, arg=""):
    """Start a command as a hidden background process (shared with the chat commands). Jobs that produce something to read post a result message into the chat when they finish."""
    import chatcmd
    msg = chatcmd.launch(cmd, arg, chat=cmd in chatcmd.JOB_CMDS + chatcmd.PLAIN + ("agent", "inbox-retry", "retry"))
    return {"error": "already running"} if msg.startswith("already running") else {"started": msg.replace("started: ", "")}


def chat_thread(text):
    import inbox
    try:
        CHAT_BUSY["on"] = True
        inbox.reply(text)
    except Exception as e:
        c = db()
        c.execute("INSERT INTO chat(ts,role,text) VALUES(?,?,?)", (common.now(), "agent", f"(failed: {str(e)[:200]})"))
        c.commit()
    finally:
        CHAT_BUSY["on"] = False


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

    def _j(self, obj, code=200):
        self._send(code, json.dumps(obj, default=str))

    def _host_ok(self):
        return self.headers.get("Host", "").split(":")[0] in ("127.0.0.1", "localhost")

    def do_GET(self):
        if not self._host_ok():
            return self._send(403, "{}")
        u = urllib.parse.urlparse(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        try:
            if path == "/":
                return self._send(200, page(), "text/html")
            if path == "/api/state":
                return self._j(state())
            if path == "/logo":
                import logos
                res = logos.logo_for((q.get("c") or [""])[0], (q.get("u") or [""])[0])
                if not res:
                    self.send_response(204)  # no logo: the page shows the company initial (204 keeps the browser console quiet)
                    self.send_header("Cache-Control", "public, max-age=3600")
                    self.end_headers()
                    return
                b, ctype = res
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(b)
                return
            if path in ("/favicon.svg", "/favicon.ico"):
                b = open(os.path.join(os.path.dirname(STATIC), "favicon.svg"), "rb").read()
                self.send_response(200)
                self.send_header("Content-Type", "image/svg+xml")
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "public, max-age=86400")
                self.end_headers()
                self.wfile.write(b)
                return
            if path == "/api/picks":
                return self._j(picks_info())
            if path == "/api/jobs":
                return self._j(browse_jobs(q))
            if path == "/api/facets":
                return self._j(job_facets())
            if path == "/api/packet":
                return self._j(packet_files((q.get("id") or [""])[0]))
            if path == "/api/log":
                lp = os.path.join(LOGS, "agent.log")
                txt = "".join(open(lp, encoding="utf-8", errors="replace").readlines()[-70:]) if os.path.exists(lp) else ""
                return self._send(200, re.sub(r"feed health: .*", "feed health: (omitted)", txt), "text/plain")
            if path == "/api/settings":
                return self._j({"settings": {k: v for k, v in common.SETTINGS.items() if k in SETTING_KEYS}, "providers": providers_info(), "kinds": list(common.SETTINGS["llm"]["routing"].keys()),
                                "schedule": schedule_info(), "home": HOME})
            if path == "/api/usage":
                return self._j(usage_info())
            if path == "/api/inbox":
                return self._j([dict(r) for r in db().execute("SELECT * FROM inbox ORDER BY id DESC LIMIT 30").fetchall()])
            if path == "/api/chat":
                rows = [dict(r) for r in db().execute("SELECT * FROM chat ORDER BY id DESC LIMIT 60").fetchall()][::-1]
                return self._j({"messages": rows, "busy": CHAT_BUSY["on"]})
            if path == "/api/commands":
                import chatcmd
                return self._j({"commands": chatcmd.commands_json()})
            if path == "/api/voice":
                import humanize
                rd = lambda p: open(p, encoding="utf-8").read() if os.path.exists(p) else ""
                return self._j({"samples": rd(humanize.VOICE), "edits": len(rd(humanize.EDITS).splitlines())})
            if path == "/api/memory":
                import memory
                return self._j({"memory": memory.memory_text(8000), "skills": memory.list_skills(), "tweaks": memory.score_tweaks(), "outcomes": len(memory.recent_outcomes(500))})
            if path == "/api/bench":
                p = os.path.join(OUT, f"bench-{dt.date.today().isoformat()}.md")
                return self._j({"report": open(p, encoding="utf-8").read() if os.path.exists(p) else ""})
            if path == "/api/doctor":
                return self._j({"home": HOME, "providers": providers_info(), "profile": not common.needs_setup()})
        except Exception as e:
            return self._j({"error": str(e)[:300]}, 500)
        self._send(404, "{}")

    def do_POST(self):
        if not self._host_ok() or self.headers.get("X-JobAgent") != "1":
            return self._j({"error": "forbidden"}, 403)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            d = json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            return self._j({"error": "bad json"}, 400)
        path = urllib.parse.urlparse(self.path).path
        try:
            if path == "/api/run":
                cmd, arg = d.get("cmd", ""), str(d.get("arg", "")).strip()
                if cmd not in ALLOWED:
                    return self._j({"error": "command not allowed"}, 400)
                if ALLOWED[cmd] and not arg:
                    return self._j({"error": "needs an argument"}, 400)
                if cmd in ("packet", "referral", "resume", "fill", "linkedin"):
                    common.add_pick(arg.split()[0], {"linkedin": "referral"}.get(cmd, cmd))
                return self._j(spawn(cmd, arg))
            if path == "/api/open":
                p = os.path.abspath(str(d.get("path", "")))
                if not p.startswith(os.path.abspath(OUT)) or not os.path.exists(p):
                    return self._j({"error": "path not allowed"}, 400)
                os.startfile(p)
                return self._j({"ok": True})
            if path == "/api/settings":
                patch = {k: v for k, v in (d.get("patch") or {}).items() if k in SETTING_KEYS}
                common.save_override("settings.json", patch)
                common.reload_config()
                return self._j({"ok": True})
            if path == "/api/provider-test":
                import llm
                return self._j(llm.test(d.get("name", "")))
            if path == "/api/schedule":
                import schedule as sch
                if d.get("times"):
                    return self._j({"out": sch.install([t for t in d["times"] if isinstance(t, str)][:4])})
                return self._j({"out": sch.enable(bool(d.get("enabled")))})
            if path == "/api/inbox":
                import inbox
                i = inbox.add(str(d.get("text", ""))[:20000], str(d.get("context", ""))[:2000])
                spawn("inbox")
                return self._j({"id": i})
            if path == "/api/chat":
                text = str(d.get("text", "")).strip()[:4000]
                if text and not CHAT_BUSY["on"]:
                    threading.Thread(target=chat_thread, args=(text,), daemon=True).start()
                return self._j({"ok": True})
            if path == "/api/voice":
                import humanize
                if d.get("sample"):
                    humanize.add_voice_sample(d["sample"])
                if d.get("draft") and d.get("final"):
                    humanize.add_edit(d["draft"], d["final"])
                return self._j({"ok": True})
            if path == "/api/memory":
                import memory
                if d.get("note"):
                    memory.add_memory(str(d["note"])[:500])
                return self._j({"ok": True})
            if path == "/api/setup":
                import setup
                setup.create_profile(d)
                return self._j({"ok": True})
        except Exception as e:
            return self._j({"error": str(e)[:300]}, 500)
        self._send(404, "{}")


def serve(open_browser=True):
    port = common.SETTINGS.get("gui_port", 8777)
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", port), H)
    url = f"http://127.0.0.1:{port}"
    print("JobAgent GUI at", url, "| workspace:", HOME, "(Ctrl+C to stop)")
    if open_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    serve()
