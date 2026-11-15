"""Provider- and agent-agnostic LLM layer.
Every model call in JobAgent goes through run(). It (1) routes the task kind to a provider/model/effort from settings, (2) enforces per-provider budgets,
(3) falls back to another provider when one is unavailable/over budget/unsuitable, (4) gives text-only models web research by retrieving pages itself,
(5) logs tokens, time and cost per call. Adapters: claude_cli, agy_cli (Antigravity/Gemini), codex_cli, gemini_cli, openai_compat (OpenAI, OpenRouter,
Gemini API, Ollama, LM Studio...), anthropic_api, cli_template (Hermes, oh-my-pi, anything else)."""
import json, os, re, shutil, subprocess, tempfile, time, urllib.request, urllib.error
import common
from common import db, log, now, DATA

LEAN_SYSTEM = "You are a precise assistant inside an automation pipeline. Follow the task exactly. When asked for JSON, output only the JSON."
_EMPTY_MCP = os.path.join(DATA, "mcp-empty.json")
WEB_TOOLS = ("WebSearch", "WebFetch")


class LLMError(RuntimeError):
    pass


# ------------------------------------------------------------------ registry / routing
def registry():
    return common.PROVIDERS.get("providers", {})


def which(pdef):
    b = pdef.get("bin")
    return shutil.which(b) or (shutil.which(b + ".cmd") if b else None)


def key_of(pdef):
    env = pdef.get("api_key_env")
    return os.environ.get(env, "") if env else ""


QUOTA_RX = re.compile(r"(hit|reached|exceeded) your .{0,20}limit|usage limit|quota|rate.?limit|too many requests|\b429\b|resets? (at )?\d", re.I)


def cooldown_until(name):
    try:
        c = db()
        r = c.execute("SELECT v FROM meta WHERE k=?", ("cooldown:" + name,)).fetchone()
        if r and r["v"] > now():
            return r["v"]
    except Exception:
        pass
    return None


def _set_cooldown(name, msg):
    """A provider reported quota exhaustion: stop calling it until its stated reset time (default 60 min)."""
    import datetime as dt
    until = dt.datetime.now() + dt.timedelta(minutes=60)
    m = re.search(r"resets?\s+(?:at\s+)?(\d{1,2})(?::(\d{2}))?\s*(am|pm)", msg, re.I)
    if m:
        h = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "pm" else 0)
        t = dt.datetime.now().replace(hour=h, minute=int(m.group(2) or 0), second=0, microsecond=0)
        until = t if t > dt.datetime.now() else t + dt.timedelta(days=1)
    c = db()
    c.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v", ("cooldown:" + name, until.isoformat(timespec="seconds")))
    c.commit()
    log(f"{name} quota exhausted; paused until {until.strftime('%H:%M')}")
    return until


def bg_calls_5h(provider):
    c = db()
    return c.execute("SELECT COUNT(*) FROM llm_log WHERE provider=? AND kind NOT IN ('chat','ingest','test','bench') AND ts>=datetime('now','-5 hours','localtime')", (provider,)).fetchone()[0]


_autostart_tried = {}


def available(name):
    """(ok, reason). 'ok' means the tool/key exists; it does not prove the login works (use test())."""
    p = registry().get(name)
    if not p:
        return False, "unknown provider"
    cd = cooldown_until(name)
    if cd:
        return False, "quota reached; resumes " + cd[11:16]
    t = p["type"]
    if t in ("claude_cli", "agy_cli", "codex_cli", "gemini_cli"):
        return (True, which(p)) if which(p) else (False, f"'{p.get('bin')}' not found on PATH")
    if t == "openai_compat":
        if p.get("api_key_env") and not key_of(p):
            return False, f"set ${p['api_key_env']}"
        if "localhost" in p["base_url"] or "127.0.0.1" in p["base_url"]:  # local servers: is anything listening?
            import socket, urllib.parse
            u = urllib.parse.urlparse(p["base_url"])
            try:
                socket.create_connection((u.hostname, u.port or 80), 0.4).close()
            except OSError:
                if name == "local" and p.get("autostart") and common.SETTINGS["llm"].get("local", {}).get("autostart", True) and not _autostart_tried.get(name):
                    _autostart_tried[name] = True
                    try:
                        import local_ai
                        if local_ai.server_bin() and local_ai.start(wait=240):
                            return True, p["base_url"]
                    except Exception as e:
                        log(f"local AI autostart failed: {e}")
                return False, "server not running"
        return True, p["base_url"]
    if t == "anthropic_api":
        return (True, "api") if key_of(p) else (False, f"set ${p['api_key_env']}")
    if t == "cli_template":
        cmd = common.SETTINGS.get("llm", {}).get("custom", {}).get(name, {}).get("cmd") or p.get("cmd")
        return (True, cmd[0]) if cmd and shutil.which(cmd[0]) else (False, "command not found")
    return False, "unsupported"


def resolve_auto():
    for name in ("claude", "agy", "codex", "gemini", "openai", "anthropic", "openrouter", "gemini_api", "local", "ollama"):
        if available(name)[0]:
            return name
    raise LLMError("No LLM provider available. Install one (Claude Code, Antigravity, Codex, Gemini CLI) or set an API key; see Settings > Providers.")


def route_for(kind):
    r = common.SETTINGS["llm"]["routing"]
    route = dict(r.get(kind) or r["default"])
    if route.get("provider", "auto") == "auto":
        route["provider"] = resolve_auto()
    return route


def agy_models():
    try:
        out = subprocess.run([which(registry()["agy"]) or "agy", "models"], capture_output=True, text=True, timeout=40).stdout
        return [ln.split("\t")[0].strip() for ln in out.splitlines() if "\t" in ln]
    except Exception:
        return []


# ------------------------------------------------------------------ usage / budgets
def usage_today(provider):
    c = db()
    r = c.execute("SELECT COUNT(*) n, COALESCE(SUM(inp+cache_create+cache_read+outp),0) t FROM llm_log WHERE provider=? AND ts>=date('now','localtime')", (provider,)).fetchone()
    return r["n"], r["t"]


def over_budget(provider):
    b = common.SETTINGS["llm"].get("budgets", {}).get(provider)
    if not b:
        return False
    n, t = usage_today(provider)
    return (b.get("daily_calls") and n >= b["daily_calls"]) or (b.get("daily_tokens") and t >= b["daily_tokens"])


def _cost(model, inp, outp, given=None):
    if given:
        return given
    pr = common.PROVIDERS.get("prices_per_mtok", {}).get(model or "")
    return round((inp * pr["in"] + outp * pr["out"]) / 1e6, 5) if pr else 0.0


def _log_usage(kind, provider, model, u, secs):
    try:
        c = db()
        c.execute("INSERT INTO llm_log(ts,kind,model,inp,cache_create,cache_read,outp,cost,provider,secs) VALUES(?,?,?,?,?,?,?,?,?,?)",
                  (now(), kind, model or "default", u.get("inp", 0), u.get("cache_create", 0), u.get("cache_read", 0), u.get("outp", 0),
                   _cost(model, u.get("inp", 0) + u.get("cache_create", 0), u.get("outp", 0), u.get("cost")), provider, round(secs, 2)))
        c.commit()
        tin = u.get("inp", 0) + u.get("cache_create", 0) + u.get("cache_read", 0)
        if tin > common.SETTINGS["llm"].get("heavy_call_tokens", 50000):
            log(f"llm: heavy call: {kind} via {provider} read {tin} input tokens")
    except Exception as e:
        log(f"usage log failed: {e}")


# ------------------------------------------------------------------ response cache (deterministic tasks only)
def _cache_key(kind, route, prompt):
    import hashlib
    return hashlib.sha256(f"{kind}|{route.get('provider')}|{route.get('model')}|{prompt}".encode("utf-8", "replace")).hexdigest()


def cache_get(kind, route, prompt):
    hours = common.SETTINGS["llm"].get("cache_hours", {}).get(kind)
    if not hours:
        return None
    try:
        r = db().execute("SELECT text, at FROM llm_cache WHERE k=?", (_cache_key(kind, route, prompt),)).fetchone()
        return r["text"] if r and time.time() - r["at"] < hours * 3600 else None
    except Exception:
        return None


def cache_put(kind, route, prompt, text):
    if not common.SETTINGS["llm"].get("cache_hours", {}).get(kind):
        return
    try:
        c = db()
        c.execute("INSERT OR REPLACE INTO llm_cache(k,kind,text,at) VALUES(?,?,?,?)", (_cache_key(kind, route, prompt), kind, text, time.time()))
        c.commit()
    except Exception as e:
        log(f"llm cache write failed: {e}")


# ------------------------------------------------------------------ adapters: each returns (text, usage_dict)
def _sub(args, prompt=None, timeout=300, cwd=None, env=None):
    try:
        p = subprocess.run(args, input=prompt, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=cwd, env=env)
    except subprocess.TimeoutExpired:
        raise LLMError(f"timed out after {timeout}s")
    return p


def _claude(prompt, route, o):
    pdef = registry()["claude"]
    if not os.path.exists(_EMPTY_MCP):
        json.dump({"mcpServers": {}}, open(_EMPTY_MCP, "w"))
    builtin = ",".join(x for x in o["allowed_tools"].split(",") if x and not x.startswith("mcp__"))
    args = [which(pdef), "-p", "--output-format", "json", "--strict-mcp-config", "--mcp-config", o["mcp_config"] or _EMPTY_MCP, "--disable-slash-commands",
            "--setting-sources", "user", "--system-prompt", LEAN_SYSTEM, "--tools", builtin]
    if not o["persist"]:
        args.append("--no-session-persistence")
    if route.get("model"):
        args += ["--model", route["model"]]
    if route.get("effort"):
        args += ["--effort", route["effort"]]
    if o["allowed_tools"]:
        args += ["--allowedTools", o["allowed_tools"]]
    if o["max_turns"]:
        args += ["--max-turns", str(o["max_turns"])]
    args += o["extra_args"] or []
    p = _sub(args, prompt, o["timeout"], o["cwd"])
    if p.returncode != 0 and not p.stdout.strip():
        raise LLMError(f"claude failed ({p.returncode}): {p.stderr[:400]}")
    try:
        j = json.loads(p.stdout)
    except json.JSONDecodeError:
        return p.stdout, {}
    if j.get("is_error"):
        raise LLMError(f"claude error: {str(j.get('result'))[:400]}")
    u = j.get("usage", {})
    return j.get("result", ""), {"inp": u.get("input_tokens", 0), "cache_create": u.get("cache_creation_input_tokens", 0),
                                 "cache_read": u.get("cache_read_input_tokens", 0), "outp": u.get("output_tokens", 0), "cost": j.get("total_cost_usd", 0)}


def _agy(prompt, route, o):
    pdef = registry()["agy"]
    if len(prompt) > pdef.get("max_prompt_chars", 28000):
        raise LLMError("prompt too long for agy (command-line limit)")
    wd = os.path.join(DATA, "tmp-agy")
    os.makedirs(wd, exist_ok=True)
    prompt = "Answer directly in text from the material below. Do not call any tools, commands or MCP servers.\n\n" + prompt
    args = [which(pdef), "-p", prompt, "--output-format", "json"]
    if route.get("model"):
        args += ["--model", route["model"]]
    if route.get("effort"):
        args += ["--effort", route["effort"]]
    p = _sub(args, None, o["timeout"], wd)
    out = p.stdout.strip()
    try:
        j = json.loads(out[out.index("{"):])
    except (ValueError, json.JSONDecodeError):
        raise LLMError(f"agy returned no JSON: {(out or p.stderr)[:300]}")
    if j.get("status") not in (None, "SUCCESS"):
        raise LLMError(f"agy status {j.get('status')}")
    text = j.get("response", "")
    if not text.strip():
        den = j.get("denied_actions")
        raise LLMError(f"agy produced no answer" + (f" (tool permission denied: {den})" if den else ""))
    u = j.get("usage", {})
    return text, {"inp": u.get("input_tokens", 0), "cache_read": u.get("cache_read_tokens", 0), "outp": u.get("output_tokens", 0) + u.get("thinking_tokens", 0)}


def _codex(prompt, route, o):
    pdef = registry()["codex"]
    outf = tempfile.NamedTemporaryFile(delete=False, suffix=".txt").name
    args = [which(pdef), "exec", "--skip-git-repo-check", "--output-last-message", outf]
    if route.get("model"):
        args += ["-m", route["model"]]
    if route.get("effort"):
        args += ["-c", f"model_reasoning_effort={route['effort']}"]
    args.append("-")
    p = _sub(args, prompt, o["timeout"], DATA)
    try:
        text = open(outf, encoding="utf-8").read()
    finally:
        os.unlink(outf)
    if not text.strip():
        raise LLMError(f"codex returned nothing: {(p.stderr or p.stdout)[:300]}")
    return text, {"inp": len(prompt) // 4, "outp": len(text) // 4}


def _gemini(prompt, route, o):
    pdef = registry()["gemini"]
    args = [which(pdef), "--output-format", "json", "-p", " "]
    if route.get("model"):
        args += ["-m", route["model"]]
    p = _sub(args, prompt, o["timeout"], DATA)
    try:
        j = json.loads(p.stdout[p.stdout.index("{"):])
        text = j.get("response", "")
    except (ValueError, json.JSONDecodeError):
        text = p.stdout
    if not text.strip():
        raise LLMError(f"gemini cli returned nothing: {p.stderr[:300]}")
    return text, {"inp": len(prompt) // 4, "outp": len(text) // 4}


def _http(url, body, headers, timeout):
    rq = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(rq, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        raise LLMError(f"HTTP {e.code}: {e.read().decode('utf-8', 'replace')[:300]}")


def _openai_compat(prompt, route, o, name):
    pdef = registry()[name]
    body = {"model": route.get("model") or (pdef.get("models") or [""])[0], "messages": [{"role": "system", "content": LEAN_SYSTEM}, {"role": "user", "content": prompt}]}
    if route.get("effort") in ("low", "medium", "high") and pdef.get("efforts"):
        body["reasoning_effort"] = route["effort"]
    hdr = {"Authorization": f"Bearer {key_of(pdef)}"} if key_of(pdef) else {}
    j = _http(pdef["base_url"].rstrip("/") + "/chat/completions", body, hdr, o["timeout"])
    u = j.get("usage", {})
    return j["choices"][0]["message"]["content"], {"inp": u.get("prompt_tokens", 0), "outp": u.get("completion_tokens", 0)}


def _anthropic_api(prompt, route, o):
    pdef = registry()["anthropic"]
    j = _http("https://api.anthropic.com/v1/messages", {"model": route.get("model") or "claude-sonnet-5-5", "max_tokens": 8000, "system": LEAN_SYSTEM,
              "messages": [{"role": "user", "content": prompt}]}, {"x-api-key": key_of(pdef), "anthropic-version": "2023-06-01"}, o["timeout"])
    u = j.get("usage", {})
    return "".join(b.get("text", "") for b in j.get("content", [])), {"inp": u.get("input_tokens", 0), "outp": u.get("output_tokens", 0)}


def _template(prompt, route, o, name):
    pdef = registry()[name]
    user = common.SETTINGS.get("llm", {}).get("custom", {}).get(name, {})
    cmd = user.get("cmd") or pdef.get("cmd")
    via = user.get("prompt_via") or pdef.get("prompt_via", "arg")
    args = [a.replace("{model}", route.get("model", "")).replace("{effort}", route.get("effort", "")).replace("{prompt}", prompt) for a in cmd]
    p = _sub(args, prompt if via == "stdin" else None, o["timeout"], DATA)
    if not p.stdout.strip():
        raise LLMError(f"{name} returned nothing: {p.stderr[:300]}")
    return p.stdout, {"inp": len(prompt) // 4, "outp": len(p.stdout) // 4}


def call_provider(name, route, prompt, o):
    t = registry()[name]["type"]
    if t == "claude_cli":
        return _claude(prompt, route, o)
    if t == "agy_cli":
        return _agy(prompt, route, o)
    if t == "codex_cli":
        return _codex(prompt, route, o)
    if t == "gemini_cli":
        return _gemini(prompt, route, o)
    if t == "openai_compat":
        return _openai_compat(prompt, route, o, name)
    if t == "anthropic_api":
        return _anthropic_api(prompt, route, o)
    if t == "cli_template":
        return _template(prompt, route, o, name)
    raise LLMError(f"unsupported provider type {t}")


# ------------------------------------------------------------------ web augmentation for providers without native web tools
def _web_augment(prompt, route, o, kind):
    import web
    plan_prompt = ("You are planning web research for the task below. Output ONLY JSON: {\"queries\":[up to 6 precise web search queries],\"urls\":[up to 4 URLs that appear in the task and should be read]}.\n\nTASK:\n" + prompt[:9000])
    try:
        t, u = call_provider(route["provider"], route, plan_prompt, {**o, "allowed_tools": "", "mcp_config": None, "timeout": 180})
        _log_usage(kind + "-plan", route["provider"], route.get("model"), u, 0)
        plan = common.extract_json(t)
    except Exception as e:
        log(f"web plan failed ({e}); continuing without notes")
        plan = {"queries": [], "urls": []}
    notes = web.research_notes(plan.get("queries", [])[:6], plan.get("urls", [])[:4])
    if not notes.strip():  # search engine unreachable / nothing fetched: let a provider with native web tools do this call instead
        raise LLMError("web retrieval returned nothing (search engine unreachable); falling back to a provider with native web search")
    return prompt + ("\n\nNOTE: you have no browsing tools in this run. Use ONLY the research notes below (fetched by the pipeline). If something is not in them, say it is unverified.\n"
                     "=== RESEARCH NOTES ===\n" + (notes or "(nothing could be retrieved)"))


# ------------------------------------------------------------------ main entry
def run(prompt, *, kind="misc", allowed_tools="", model=None, timeout=300, extra_args=None, cwd=None, max_turns=None, mcp_config=None, persist=False, provider=None, **_ignored):
    """Returns (text, raw). 'model' is only a legacy hint; routing decides. provider= forces one (used by bench / tests)."""
    need_browser = bool(mcp_config)
    need_web = any(t in allowed_tools for t in WEB_TOOLS)
    o = dict(allowed_tools=allowed_tools, timeout=timeout, extra_args=extra_args, cwd=cwd, max_turns=max_turns, mcp_config=mcp_config, persist=persist)
    primary = route_for(kind)
    chain = [primary] if not provider else [dict(primary, provider=provider, model=model or "")]
    # Quality work in the background waits for its own provider (next run) instead of being done badly or timing out on a fallback.
    defer = common.BACKGROUND and not provider and kind in common.SETTINGS["llm"].get("defer_in_background", [])
    for fb in common.SETTINGS["llm"].get("fallbacks", []):
        if not provider and not defer and fb != primary["provider"]:
            chain.append({"provider": fb, "model": "", "effort": primary.get("effort", "medium")})
    cacheable = not mcp_config and not need_web
    if cacheable:
        hit = cache_get(kind, primary, prompt)
        if hit is not None:
            return hit, {"provider": primary["provider"], "usage": {}, "cached": True}
    errors = []
    for route in chain:
        name = route["provider"]
        ok, why = available(name)
        caps = registry().get(name, {}).get("capabilities", {})
        if not ok:
            errors.append(f"{name}: {why}")
            continue
        if need_browser and not caps.get("browser"):
            errors.append(f"{name}: cannot drive a browser")
            continue
        if over_budget(name):
            errors.append(f"{name}: daily budget reached")
            continue
        bgb = common.SETTINGS["llm"].get("budgets", {}).get(name, {}).get("background_calls_per_5h")
        if common.BACKGROUND and bgb and bg_calls_5h(name) >= bgb:
            errors.append(f"{name}: background allowance for the last 5 hours used (kept free for you)")
            continue
        p = prompt
        oo = dict(o)
        t0 = time.time()
        try:
            if need_web and not caps.get("web"):
                p = _web_augment(prompt, route, o, kind)
                oo["allowed_tools"] = ""
            text, usage = call_provider(name, route, p, oo)
        except LLMError as e:
            errors.append(f"{name}: {e}")
            log(f"llm {kind} via {name} failed: {e}")
            if QUOTA_RX.search(str(e)):
                _set_cooldown(name, str(e))
            continue
        _log_usage(kind, name, route.get("model"), usage, time.time() - t0)
        if cacheable and name == primary["provider"]:
            cache_put(kind, primary, prompt, text)
        if name != primary["provider"] and not provider:
            log(f"llm {kind}: fell back to {name}")
        return text, {"provider": name, "usage": usage}
    if defer:
        raise LLMError("deferred to the next run (its provider is busy or out of quota; background quality work does not fall back): " + "; ".join(errors))
    raise LLMError("all providers failed for '%s': %s" % (kind, "; ".join(errors)))


def test(name, model="", effort=""):
    """Tiny round-trip to confirm a provider works. Returns dict(ok, seconds, answer|error)."""
    t0 = time.time()
    try:
        text, raw = run('Reply with exactly the word: pong', kind="test", provider=name, timeout=120)
        return {"ok": "pong" in text.lower(), "seconds": round(time.time() - t0, 1), "answer": text.strip()[:80], "provider": raw["provider"]}
    except Exception as e:
        return {"ok": False, "seconds": round(time.time() - t0, 1), "error": str(e)[:300]}
