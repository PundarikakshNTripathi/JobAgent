"""One switch to stop JobAgent from using AI: the local model (and the GPU), API keys, and subscriptions.
Pause state lives in <workspace>/data/paused.json, so it survives restarts and reboots. While a scope is paused, no call
reaches a provider of that billing kind, and while everything is paused, scheduled cycles exit at once. Nothing resumes
by itself: only resume() (GUI button, `run.py resume`, `/resume`) clears it."""
import json, os
import common
from common import DATA, log, now

PATH = os.path.join(DATA, "paused.json")
SCOPES = ("local", "api", "subscription")
LABEL = {"local": "local model (GPU)", "api": "API keys", "subscription": "subscriptions"}


def billing_of(name):
    """Which bill a provider runs on: 'local' (this computer), 'api' (per-token key) or 'subscription' (a logged-in CLI)."""
    p = common.PROVIDERS.get("providers", {}).get(name, {})
    if p.get("billing") in SCOPES:
        return p["billing"]
    t = p.get("type", "")
    if t == "openai_compat":
        url = p.get("base_url", "")
        return "local" if ("localhost" in url or "127.0.0.1" in url) else "api"
    if t == "anthropic_api":
        return "api"
    return "subscription"  # claude_cli, agy_cli, codex_cli, gemini_cli, cli_template


def state():
    try:
        with open(PATH, encoding="utf-8") as f:
            s = json.load(f)
    except (OSError, ValueError):
        s = {}
    out = {k: bool(s.get(k)) for k in SCOPES}
    out["since"] = s.get("since", "")
    out["reason"] = s.get("reason", "")
    out["any"] = any(out[k] for k in SCOPES)
    out["all"] = all(out[k] for k in SCOPES)
    return out


def is_paused(provider):
    return state().get(billing_of(provider), False)


def _write(s):
    os.makedirs(DATA, exist_ok=True)
    tmp = PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump({k: s.get(k, False) for k in SCOPES} | {"since": s.get("since", ""), "reason": s.get("reason", "")}, f)
    os.replace(tmp, PATH)


def pause(scopes=("all",), reason=""):
    """Pause the given scopes ('all' or any of local/api/subscription). Pausing local also stops the local model server."""
    scopes = SCOPES if "all" in scopes else tuple(x for x in scopes if x in SCOPES)
    if not scopes:
        raise ValueError("pause what? all, local, api or subscription")
    s = state()
    for k in scopes:
        s[k] = True
    s["since"] = s.get("since") or now()
    s["reason"] = reason or s.get("reason", "")
    _write(s)
    msg = "paused: " + ", ".join(LABEL[k] for k in SCOPES if s[k])
    if "local" in scopes:
        try:
            import local_ai
            msg += "; local model " + local_ai.stop(force=True)
        except Exception as e:
            msg += f"; could not stop the local model: {e}"
    log(msg)
    return msg


def resume(scopes=("all",)):
    scopes = SCOPES if "all" in scopes else tuple(x for x in scopes if x in SCOPES)
    s = state()
    for k in scopes:
        s[k] = False
    if not any(s[k] for k in SCOPES):
        s["since"] = s["reason"] = ""
    _write(s)
    msg = "resumed: " + ", ".join(LABEL[k] for k in scopes) + ("" if not any(s[k] for k in SCOPES) else "; still paused: " + ", ".join(LABEL[k] for k in SCOPES if s[k]))
    log(msg)
    return msg


def describe():
    s = state()
    if not s["any"]:
        return "running normally"
    return "paused since " + s["since"][:16].replace("T", " ") + ": " + ", ".join(LABEL[k] for k in SCOPES if s[k])
