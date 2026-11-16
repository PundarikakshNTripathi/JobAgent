"""Model routing presets (defaults/presets.json): "recommended", "low-cost", "hosted", "local-only".

    jobagent routing                 show the current routing and which preset it matches
    jobagent routing list            the presets and whether their providers are ready here
    jobagent routing use <name>      switch to a preset (e.g. `routing use recommended` to go back to the recommended mix)

A preset sets which provider/model does each task and the fallback order. It is written to your workspace's config/settings.json like any other
setting, so Settings in the app shows it and you can still change single tasks afterwards."""
import json, os
import common
from common import DEFAULTS


def load():
    with open(os.path.join(DEFAULTS, "presets.json"), encoding="utf-8") as f:
        return {k: v for k, v in json.load(f).items() if not k.startswith("_")}


def _norm(r):
    return {k: (v.get("provider", ""), v.get("model", "") or "") for k, v in (r or {}).items()}


def current():
    """Name of the preset the current routing matches exactly, else 'custom'."""
    llm = common.SETTINGS["llm"]
    cur = _norm(llm.get("routing"))
    for name, p in load().items():
        want = _norm(p["routing"])
        if all(cur.get(k) == v for k, v in want.items()) and list(llm.get("fallbacks", [])) == p["fallbacks"]:
            return name
    return "custom"


def info():
    import llm
    out = []
    for name, p in load().items():
        missing = [n for n in p.get("needs", []) if not llm.available(n)[0]]
        out.append({"name": name, "label": p["label"], "needs": p.get("needs", []), "missing": missing, "active": name == current()})
    return out


def apply(name):
    ps = load()
    if name not in ps:
        raise ValueError(f"unknown preset {name!r}; choose one of: {', '.join(ps)}")
    p = ps[name]
    common.save_override("settings.json", {"llm": {"routing": p["routing"], "fallbacks": p["fallbacks"]}})
    common.reload_config()
    import llm
    missing = [n for n in p.get("needs", []) if not llm.available(n)[0]]
    common.log(f"routing preset applied: {name}" + (f" (not ready here: {', '.join(missing)}; their tasks fall back)" if missing else ""))
    return missing


def text():
    lines = [f"current routing: {current()}"]
    for i in info():
        lines.append(f"{'*' if i['active'] else ' '} {i['name']:12} {i['label']}" + (f"   [not ready here: {', '.join(i['missing'])}]" if i["missing"] else ""))
    return "\n".join(lines)
