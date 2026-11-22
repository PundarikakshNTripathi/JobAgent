"""Keeps generated messages from reading as machine-written: a deterministic linter + auto-fixer, a style pack for prompts
(human-writing skill + the user's own sent messages + how they edited past drafts), and one optional rewrite pass."""
import json, os, re
from common import DATA, SETTINGS, log

VOICE = os.path.join(DATA, "voice.md")
EDITS = os.path.join(DATA, "voice_edits.jsonl")

TELLS = [
    r"hope (this|you)[^.]{0,40}(well|finds)", r"came across", r"i am writing to", r"i wanted to reach out", r"reach(ing)? out", r"\bimpressive\b", r"\bfascinating\b", r"\binspiring\b",
    r"\bremarkable\b", r"\bpassionate\b", r"\bthrilled\b", r"\bexcited\b", r"\beager\b", r"\bkeen\b", r"\bresonat", r"huge fan", r"\bleverag", r"\bdelve", r"\bshowcas", r"\baligns?\b",
    r"intersection of", r"cutting[- ]edge", r"state[- ]of[- ]the[- ]art", r"game[- ]chang", r"\brobust\b", r"\bseamless", r"\bjourney\b", r"\btapestry\b", r"\btestament\b",
    r"pick your brain", r"quick (chat|call)", r"would love to", r"i'd love to", r"i would love", r"\bkindly\b", r"in today's", r"\badditionally,", r"\bfurthermore", r"\bmoreover",
    r"thanks in advance", r"looking forward to hearing", r"best regards", r"great fit", r"perfect fit", r"\bthrive\b", r"\bpivotal\b", r"\bunderscore", r"sorry to bother",
]
_TELL_RX = [re.compile(t, re.I) for t in TELLS]


def _voice_text():
    return _read(VOICE, 20000).lower()


def lint(text):
    """Return a list of human-readable issues. Empty list = reads fine. Anything that appears in messages you actually sent (data/voice.md) is
    your voice, not an AI tell, and is never flagged."""
    issues = []
    voice = _voice_text()
    for rx in _TELL_RX:
        m = rx.search(text)
        if m and m.group(0).lower() not in voice:
            issues.append(f"tell: '{m.group(0)}'")
    if "—" in text or "–" in text or " -- " in text:
        issues.append("em/en dash used as punctuation")
    if "!" in text:
        issues.append("exclamation mark")
    if ";" in text:
        issues.append("semicolon")
    if re.search(r"(?m)^\s*([-*•]|\d+\.)\s", text) and not re.search(r"(?m)^\s*([-*•]|\d+\.)\s", voice):
        issues.append("bullet list")
    if re.search(r"\b\w+, \w+(?: \w+)?, and \w+", text):
        issues.append("three-item list")
    sents = [s.strip() for s in re.split(r"(?<=[.?!])\s+", text) if s.strip()]
    if len(sents) >= 4 and sum(1 for s in sents if s.startswith("I ")) > len(sents) * 0.6:
        issues.append("most sentences start with 'I'")
    return issues


def autofix(text):
    t = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    t = re.sub(r"\s*[—–]\s*", " - ", t)  # a plain hyphen reads human; turning dashes into commas made comma splices
    t = t.replace(" -- ", " - ")
    t = re.sub(r";\s*", ". ", t)
    t = re.sub(r"!{2,}", "!", t)
    t = re.sub(r"(?i)\b(thanks in advance|looking forward to hearing from you|best regards|kind regards),?\.?", "", t)
    t = re.sub(r"[ \t]{2,}", " ", t)
    t = re.sub(r",\s*\.", ".", t)
    t = re.sub(r"\s+([,.?])", r"\1", t)
    return t.strip()


def _read(path, limit):
    try:
        return open(path, encoding="utf-8").read()[-limit:]
    except OSError:
        return ""


def add_voice_sample(text):
    os.makedirs(DATA, exist_ok=True)
    with open(VOICE, "a", encoding="utf-8") as f:
        f.write("\n---\n" + text.strip() + "\n")


def add_edit(draft, final):
    """Learn from how the user edits drafts."""
    with open(EDITS, "a", encoding="utf-8") as f:
        f.write(json.dumps({"draft": draft, "final": final}) + "\n")


def style_pack():
    """Prompt block: the style skill + the user's real messages + their recent edits."""
    import memory
    parts = [memory.skill("human-writing")]
    v = _read(VOICE, 3500)
    if v.strip():
        parts.append("MESSAGES THE USER ACTUALLY SENT (imitate this voice: length, punctuation, vocabulary, how direct they are):\n" + v)
    ed = _read(EDITS, 4000).strip().splitlines()[-3:]
    if ed:
        pairs = []
        for ln in ed:
            try:
                d = json.loads(ln)
                pairs.append(f"DRAFT: {d['draft']}\nUSER'S FINAL: {d['final']}")
            except (ValueError, KeyError):
                pass
        if pairs:
            parts.append("HOW THE USER EDITED PAST DRAFTS (they changed the draft into the final; apply the same instincts):\n" + "\n\n".join(pairs))
    return "\n\n".join(p for p in parts if p)


def polish_batch(fields, ctx="", extra=None):
    """fields: {key: text}. Deterministic autofix for all; then ONE model rewrite for every field still flagged (instead of one call per field).
    Returns {key: (text, remaining_issues)}."""
    out = {k: (autofix(v), []) for k, v in fields.items() if v}
    out = {k: (t, lint(t) + list((extra or {}).get(k, []))) for k, (t, _) in out.items()}
    flagged = {k: v for k, v in out.items() if v[1]}
    cfg = SETTINGS.get("humanize", {})
    if flagged and cfg.get("enabled", True) and cfg.get("max_rewrites", 1) > 0:
        from llm import run
        import json as _j
        prompt = ("Rewrite each text below so it reads like a real person wrote it. Keep every fact and the same length or shorter. Fix ONLY the listed problems. "
                  "Rules: plain words, contractions, no flattery, no dashes as punctuation, no lists, no exclamation marks, no stock phrases.\n" + ctx +
                  "\nReturn ONLY a JSON object mapping each key to its rewritten text.\n\n" +
                  _j.dumps({k: {"problems": iss, "text": t} for k, (t, iss) in flagged.items()}, ensure_ascii=False))
        try:
            new, _ = run(prompt, kind="review", timeout=180, max_turns=1)
            from common import extract_json
            d = extract_json(new)
            for k, v in (d.items() if isinstance(d, dict) else []):
                if k in flagged and isinstance(v, str):
                    t2 = autofix(v.strip().strip('"'))
                    left = lint(t2) + [x for x in (extra or {}).get(k, []) if x.split("'")[1:2] and x.split("'")[1] in t2]
                    if len(left) < len(flagged[k][1]):
                        out[k] = (t2, left)
        except Exception as e:
            log(f"humanize batch rewrite skipped: {e}")
    return out


def polish(text, kind="outreach", ctx="", rewrite=True):
    """Autofix, then (if still flagged, enabled and rewrite=True) one rewrite pass by the model. Returns (text, remaining_issues)."""
    t = autofix(text)
    issues = lint(t)
    cfg = SETTINGS.get("humanize", {})
    if rewrite and issues and cfg.get("enabled", True) and cfg.get("max_rewrites", 1) > 0:
        from llm import run
        prompt = ("Rewrite the text below so it reads like a real person wrote it. Keep every fact and the same length or shorter. Fix ONLY these problems: " + "; ".join(issues) +
                  ".\nRules: plain words, contractions, no flattery, no dashes as punctuation, no lists, no exclamation marks, no stock phrases. Output ONLY the rewritten text.\n"
                  f"{ctx}\n\nTEXT:\n{t}")
        try:
            new, _ = run(prompt, kind="review", timeout=120, max_turns=1)
            new = autofix(new.strip().strip('"'))
            if len(lint(new)) < len(issues):
                t, issues = new, lint(new)
        except Exception as e:
            log(f"humanize rewrite failed: {e}")
    return t, issues
