"""A browser any model can drive: Claude, GPT/Codex, Gemini/Antigravity, a local model, or an API.

Each step, JobAgent shows the model the page as compact text plus a numbered list of the controls on it (links, buttons, inputs with their current
values, selects with their options) and the model replies with ONE JSON action. JobAgent carries it out with Playwright on the agent Chrome (where
you are logged in). The browser never depends on the provider having its own browser tool (MCP), so browsing works the same on every provider
and falls back like every other task.

Safety lives in code, not in the prompt:
- mode "read":  navigation only (open links, scroll, back). On LinkedIn the read-only guard also blocks Connect/Message/Follow/... in the page.
- mode "form":  may type/select/check/upload, but never clicks Submit / Apply / Send / Finish / Pay (and fill's page guard blocks submits too).
- mode "save":  like "form", and may press Save / Update on a profile; never Apply / Send / Message / Connect.
Every step is logged; the run stops at a step limit, a login wall or a security check."""
import json, os, re, time
import common
from common import log, extract_json
import browse

OBSERVE_JS = r"""(maxText) => {
  const vis = e => { const r = e.getBoundingClientRect(); if (r.width < 1 || r.height < 1) return false; const s = getComputedStyle(e);
    return s.visibility !== 'hidden' && s.display !== 'none' && s.opacity !== '0'; };
  document.querySelectorAll('[data-ja]').forEach(e => e.removeAttribute('data-ja'));
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, [role=button], [role=link], [role=checkbox], [role=radio], [role=combobox], [role=option], [role=tab], [role=switch], [contenteditable=true]';
  const els = [...document.querySelectorAll(sel)].filter(vis).slice(0, 160);
  const lab = e => {
    let t = e.getAttribute('aria-label') || '';
    if (!t && e.labels && e.labels.length) t = e.labels[0].innerText;
    if (!t && e.id) { const l = document.querySelector('label[for="' + CSS.escape(e.id) + '"]'); if (l) t = l.innerText; }
    if (!t) t = e.placeholder || e.innerText || e.value || e.name || e.title || '';
    return String(t).trim().replace(/\s+/g, ' ').slice(0, 90);
  };
  const out = els.map((e, i) => {
    e.setAttribute('data-ja', String(i));
    const tag = e.tagName.toLowerCase(), type = (e.getAttribute('type') || e.getAttribute('role') || '').toLowerCase();
    let val = '';
    if (tag === 'input' || tag === 'textarea') val = (type === 'checkbox' || type === 'radio') ? (e.checked ? 'checked' : 'unchecked') : (type === 'password' ? '***' : (e.value || ''));
    else if (tag === 'select') val = (e.options[e.selectedIndex] || {}).text || '';
    else if (e.isContentEditable) val = e.innerText;
    else if (e.getAttribute('aria-checked')) val = e.getAttribute('aria-checked') === 'true' ? 'checked' : 'unchecked';
    const o = {i, tag, type, label: lab(e), value: String(val).trim().slice(0, 160)};
    if (tag === 'a') o.href = e.href.split('#')[0].slice(0, 160);
    if (tag === 'select') o.options = [...e.options].slice(0, 30).map(x => x.text.trim()).filter(Boolean);
    if (e.required || e.getAttribute('aria-required') === 'true') o.required = true;
    if (e.disabled) o.disabled = true;
    return o;
  });
  const main = document.querySelector('main') || document.body;
  return {url: location.href, title: document.title, text: main.innerText.replace(/\n{3,}/g, '\n\n').slice(0, maxText), els: out};
}"""

SUBMIT = re.compile(r"\b(submit|apply|send|finish|complete (my )?application|place order|pay|purchase|publish|confirm and|sign up|register|create account)\b", re.I)
CONTACT = re.compile(r"\b(connect|message|follow|invite|endorse|like|comment|repost|send (a )?(message|inmail|request))\b", re.I)
OPEN_FORM = re.compile(r"^\s*(apply|apply now|apply for (this|the) (job|role|position)|i'?m interested|start (your )?application)\s*$", re.I)  # opens the form; final submit stays blocked by the page guard
SAVE_OK = re.compile(r"^\s*(save|save changes|update|update profile|done|next|continue|add|edit|ok)\b", re.I)
NAV_OK = re.compile(r"\b(next|more|see all|show (all|more)|load more|view|people|jobs|about|profile|search|page \d+|\d+)\b", re.I)

PROMPT = """You operate a web browser for JobAgent, one action at a time.
GOAL:
{goal}

RULES
- {mode_rules}
- Use only facts given in the goal. Never invent names, dates, numbers or links.
- Stop at login walls, CAPTCHAs, email or phone verification: finish with "done" and say so in the result.
- Prefer the fewest steps. When you have what the goal asks for, or you are stuck, finish with "done".

Reply with ONE JSON object and nothing else:
{{"thought": "one short sentence", "action": "goto|click|type|select|check|upload|scroll|back|done", "i": <element number>, "text": "", "url": "", "value": "", "on": true, "path": "", "result": {{}}}}
- click i / type i text (replaces the field) / select i value (an option's visible text) / check i on=true|false / upload i path / goto url / scroll / back
- done: put the final answer in "result" exactly as the goal asks.

STEPS LEFT: {left}
WHAT YOU DID SO FAR:
{hist}

PAGE: {title} | {url}
VISIBLE TEXT (truncated):
{text}

ELEMENTS (number, kind, label, current value):
{els}
Return ONLY JSON."""

MODE_RULES = {
    "read": "READ-ONLY: you may open links, scroll and go back. Do not type into forms, and never click Connect, Message, Follow, Like, Apply or Send.",
    "form": ("You may type, select, tick and upload, but NEVER press the final Submit / Apply / Send / Finish button (it is blocked anyway). "
             "Leave legal attestations and anything not in the facts blank and report them."),
    "save": ("You may edit fields and press Save / Update to store the user's own profile. Never press Apply, Send, Message, Connect or anything that "
             "contacts someone or submits an application, and never change account, privacy, email or password settings."),
}


def _fmt(els):
    out = []
    for e in els:
        s = f"[{e['i']}] {e['tag']}{('/' + e['type']) if e['type'] else ''} \"{e['label']}\""
        if e.get("value"):
            s += f" = \"{e['value']}\""
        if e.get("options"):
            s += " options: " + " | ".join(e["options"][:20])
        if e.get("href") and e["tag"] == "a":
            s += f" -> {e['href']}"
        if e.get("required"):
            s += " (required)"
        if e.get("disabled"):
            s += " (disabled)"
        out.append(s)
    return "\n".join(out) or "(no interactive elements)"


def _allowed(mode, act, el):
    """None when allowed, else the reason (fed back to the model)."""
    label = (el or {}).get("label", "") if el else ""
    if act in ("type", "select", "check", "upload") and mode == "read":
        return "this is a read-only task: do not change form fields"
    if act == "click" and el:
        is_link = el["tag"] == "a" or el.get("type") in ("link", "tab")
        if CONTACT.search(label):
            return f'"{label}" would contact someone or change your network; you do that yourself'
        if mode == "read":
            if not is_link and not NAV_OK.search(label):
                return f'"{label}" is not a navigation control; in a read-only task only links, tabs and next/more buttons may be clicked'
        elif mode == "form":
            if SUBMIT.search(label) and not OPEN_FORM.search(label):
                return f'"{label}" would submit the application; the user presses that button'
        elif mode == "save":
            if SUBMIT.search(label) and not SAVE_OK.search(label):
                return f'"{label}" would submit or publish something; only Save/Update is allowed here'
    return None


def run(goal, start_url, *, mode="read", kind="browse", max_steps=25, init_script=None, provider=None, max_text=3000, result_hint=None):
    """Drive the browser toward `goal`. Returns {"result": <the model's result object or text>, "steps": n, "url": last url, "log": [...]}."""
    from llm import run as llm_run
    hist, steps_log, seen = [], [], set()
    with browse.session():
        ctx = browse._context()
        page = ctx.new_page()
        try:
            if init_script:
                page.add_init_script(init_script)
            if "linkedin.com" in start_url or mode == "read":
                import browser
                page.add_init_script(browser.READONLY_GUARD)
            page.goto(start_url, wait_until="domcontentloaded", timeout=45000)
            time.sleep(2.5)
            for left in range(max_steps, 0, -1):
                if browse.WALL.search(page.url):
                    return {"result": {"error": f"login wall or security check at {page.url}; log in once in the agent browser and retry"}, "steps": max_steps - left, "url": page.url, "log": steps_log, "seen": sorted(seen)}
                obs = page.evaluate(OBSERVE_JS, max_text)
                seen.update(e["href"] for e in obs["els"] if e.get("href"))
                seen.add(obs["url"])
                prompt = PROMPT.format(goal=goal + (f"\n\nRESULT FORMAT: {result_hint}" if result_hint else ""), mode_rules=MODE_RULES[mode], left=left,
                                       hist="\n".join(hist[-10:]) or "(nothing yet)", title=obs["title"][:120], url=obs["url"], text=obs["text"], els=_fmt(obs["els"]))
                out, _ = llm_run(prompt, kind=kind, timeout=240, max_turns=1, provider=provider)
                try:
                    d = extract_json(out)
                    if not isinstance(d, dict):
                        raise ValueError
                except ValueError:
                    hist.append("(your last reply was not one JSON object; reply with exactly one)")
                    continue
                act = str(d.get("action", "")).lower().strip()
                el = None
                if "i" in d and str(d.get("i", "")).strip().lstrip("-").isdigit():
                    idx = int(d["i"])
                    el = next((e for e in obs["els"] if e["i"] == idx), None)
                steps_log.append({"url": obs["url"], "action": act, "i": d.get("i"), "label": (el or {}).get("label", ""), "thought": str(d.get("thought", ""))[:200]})
                log(f"webagent {kind} step {max_steps - left + 1}: {act} {('#' + str(d.get('i'))) if el else ''} {(el or {}).get('label', '')[:50]} {d.get('url', '')[:80]}")
                if act == "done":
                    return {"result": d.get("result", d.get("text", "")), "steps": max_steps - left + 1, "url": page.url, "log": steps_log, "seen": sorted(seen)}
                why = _allowed(mode, act, el)
                if act in ("click", "type", "select", "check", "upload") and el is None:
                    why = "that element number is not on the page; use a number from ELEMENTS"
                if why:
                    hist.append(f"- {act} #{d.get('i')}: REFUSED: {why}")
                    continue
                loc = page.locator(f'[data-ja="{el["i"]}"]') if el else None
                try:
                    if act == "goto":
                        u = str(d.get("url", ""))
                        if not u.startswith("http"):
                            raise ValueError("goto needs a full http(s) URL")
                        page.goto(u, wait_until="domcontentloaded", timeout=45000)
                        res = f"opened {u}"
                    elif act == "click":
                        before = page.url
                        try:
                            loc.first.click(timeout=6000)
                        except Exception:  # covered by an overlay or off-screen: follow the link directly, else a script click
                            if el.get("href", "").startswith("http"):
                                page.goto(el["href"], wait_until="domcontentloaded", timeout=45000)
                            else:
                                loc.first.evaluate("e => e.click()")
                        page.wait_for_timeout(1800)
                        res = f'clicked "{el["label"]}"' + (f" -> now at {page.url}" if page.url != before else "")
                    elif act == "type":
                        txt = str(d.get("text", ""))
                        try:
                            loc.first.fill(txt, timeout=8000)
                        except Exception:
                            loc.first.click(timeout=5000)
                            page.keyboard.press("Control+A")
                            page.keyboard.type(txt, delay=15)
                        page.wait_for_timeout(600)
                        res = f'typed into "{el["label"]}"'
                    elif act == "select":
                        v = str(d.get("value", ""))
                        try:
                            loc.first.select_option(label=v, timeout=6000)
                        except Exception:
                            loc.first.select_option(value=v, timeout=6000)
                        res = f'selected "{v}" in "{el["label"]}"'
                    elif act == "check":
                        loc.first.set_checked(bool(d.get("on", True)), timeout=6000)
                        res = f'{"ticked" if d.get("on", True) else "unticked"} "{el["label"]}"'
                    elif act == "upload":
                        pth = str(d.get("path", ""))
                        if not os.path.isfile(pth):
                            raise ValueError(f"file not found: {pth}")
                        loc.first.set_input_files(pth, timeout=8000)
                        res = f'uploaded {os.path.basename(pth)} to "{el["label"]}"'
                    elif act == "scroll":
                        page.mouse.wheel(0, 1400)
                        page.wait_for_timeout(900)
                        res = "scrolled down"
                    elif act == "back":
                        page.go_back(timeout=20000)
                        res = f"went back to {page.url}"
                    else:
                        res = f"unknown action {act!r}; use one of goto, click, type, select, check, upload, scroll, back, done"
                except Exception as e:
                    res = f"FAILED: {str(e).splitlines()[0][:200]}"
                line = f"- {act} {('#' + str(d.get('i')) + ' ') if el else ''}{(el or {}).get('label', '')[:60]}: {res}"
                if hist and hist[-1] == line:
                    line += "  (same as the step before: do something different, or finish with done and report what you found)"
                hist.append(line)
                browse._pace(0.8, 2.0)
            return {"result": {"error": "step limit reached", "last_url": page.url, "progress": hist[-6:]}, "steps": max_steps, "url": page.url, "log": steps_log, "seen": sorted(seen)}
        finally:
            if mode == "read":
                try:
                    page.close()
                except Exception:
                    pass
