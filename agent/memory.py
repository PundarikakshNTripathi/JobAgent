"""Agent memory, skills and the learning loop (the pieces that make a pipeline behave like an agent that improves):
 - skills/*.md: reusable playbooks loaded into prompts by task (shipped in defaults/skills, overridable per user in <workspace>/skills).
 - data/memory/MEMORY.md: durable notes about the user and what works (written by reflect() and by the user via chat).
 - outcomes table: applied / skipped (with reason) / replied events recorded from the GUI/CLI.
 - reflect(): once a day, distils outcomes + memory into an updated MEMORY.md and small scoring tweaks that scoring.py applies."""
import json, os, re
from common import DEFAULTS, USER_SKILLS, DATA, db, now, log, SETTINGS, PROFILE, extract_json, meta_get, meta_set, today

MEM = os.path.join(DATA, "memory", "MEMORY.md")
TWEAKS = os.path.join(DATA, "memory", "score_tweaks.json")


def skill(name):
    for d in (USER_SKILLS, os.path.join(DEFAULTS, "skills")):
        p = os.path.join(d, name + ".md")
        if os.path.exists(p):
            return open(p, encoding="utf-8").read()
    return ""


def list_skills():
    names = set()
    for d in (USER_SKILLS, os.path.join(DEFAULTS, "skills")):
        if os.path.isdir(d):
            names |= {f[:-3] for f in os.listdir(d) if f.endswith(".md")}
    return sorted(names)


def memory_text(limit=3500):
    try:
        return open(MEM, encoding="utf-8").read()[-limit:]
    except OSError:
        return ""


def add_memory(note):
    os.makedirs(os.path.dirname(MEM), exist_ok=True)
    with open(MEM, "a", encoding="utf-8") as f:
        f.write(f"- ({today()}) {note.strip()}\n")


def score_tweaks():
    try:
        return json.load(open(TWEAKS, encoding="utf-8"))
    except (OSError, ValueError):
        return []


def recent_outcomes(n=60):
    c = db()
    return c.execute("""SELECT o.ts, o.event, o.note, j.company, j.title, j.family, j.loc_class, t.fit FROM outcomes o LEFT JOIN jobs j ON j.id=o.job_id
                        LEFT JOIN triage t ON t.job_id=o.job_id ORDER BY o.id DESC LIMIT ?""", (n,)).fetchall()


def context_pack(extra=""):
    """What the agent knows about the user right now. Used by chat, ingest and (trimmed) triage."""
    prefs = PROFILE.get("preferences", {})
    out = [f"USER: {PROFILE.get('name','')} | {PROFILE.get('summary','')}", f"EDUCATION: {PROFILE.get('education','')}",
           f"PREFERENCES: {json.dumps(prefs)[:900]}"]
    m = memory_text()
    if m:
        out.append("LEARNED MEMORY:\n" + m)
    try:
        import inbox
        out.append(inbox.summary(8))
    except Exception:
        pass
    if extra:
        out.append(extra)
    return "\n".join(out)


REFLECT_PROMPT = """You maintain the long-term memory of a job-search agent for ONE user. Update it from the new evidence.

CURRENT MEMORY
{memory}

RECENT OUTCOMES (event, note, job)
{outcomes}

Write the new memory as at most 25 short bullet lines of durable facts: the user's real preferences and dislikes (inferred from what they skip/apply to and why), which kinds of roles/companies they respond to, what messages or approaches worked, constraints. Drop stale or contradicted lines. No filler.
Also propose up to 6 small scoring tweaks for the job filter based on repeated patterns, e.g. the user keeps skipping 'forward deployed' roles.

Return ONLY JSON: {{"memory":"bullets joined with newlines","tweaks":[{{"pattern":"regex on lowercase job title","delta":-12..12,"why":""}}]}}"""


def reflect(force=False):
    if not SETTINGS.get("memory", {}).get("enabled", True):
        return None
    c = db()
    if not force and meta_get(c, "last_reflect") == today():
        return None
    rows = recent_outcomes(60)
    if not rows and not memory_text():
        return None
    from llm import run
    outs = "\n".join(f"{r['ts'][:10]} {r['event']} | {r['note'] or ''} | {r['company']} - {r['title']} (fit {r['fit']})" for r in rows) or "(none yet)"
    text, _ = run(REFLECT_PROMPT.format(memory=memory_text() or "(empty)", outcomes=outs), kind="reflect", timeout=240, max_turns=2)
    d = extract_json(text)
    os.makedirs(os.path.dirname(MEM), exist_ok=True)
    open(MEM, "w", encoding="utf-8").write(d.get("memory", "").strip() + "\n")
    tw = [t for t in d.get("tweaks", []) if t.get("pattern") and isinstance(t.get("delta"), (int, float))]
    for t in tw:
        try:
            re.compile(t["pattern"])
        except re.error:
            t["pattern"] = re.escape(t["pattern"])
        t["delta"] = max(-12, min(12, t["delta"]))
    json.dump(tw, open(TWEAKS, "w", encoding="utf-8"), indent=1)
    meta_set(c, "last_reflect", today())
    log(f"reflect: memory updated ({len(d.get('memory','').splitlines())} lines, {len(tw)} tweaks)")
    return d


def auth_note():
    fd = PROFILE.get("form_defaults", {})
    home = PROFILE.get("preferences", {}).get("home_country") or fd.get("country") or "their home country"
    return f"authorised to work in {home} only ({fd.get('work_authorization') or 'no other work authorisation stated'}); relocation {'welcome if the employer sponsors a visa' if PROFILE.get('preferences', {}).get('ok_with_relocation', True) else 'not wanted'}."


def persona():
    """Profile-derived description of the candidate and their constraints, used in prompts instead of any hard-coded person."""
    P, pr = PROFILE, PROFILE.get("preferences", {})
    home = pr.get("home_country") or P.get("form_defaults", {}).get("country") or "their home country"
    return "\n".join(filter(None, [
        f"CANDIDATE: {P.get('summary','')} Education: {P.get('education','')}. Based in {P.get('location','')}; {auth_note()}",
        ("Track priority (highest first): " + " > ".join(pr.get("track_priority", []))) if pr.get("track_priority") else "",
        f"Availability: {pr.get('availability','')}" if pr.get("availability") else "",
        f"Pay/prestige stance: {pr.get('pay_policy','')}" if pr.get("pay_policy") else "",
        f"Visa reality: roles outside {home} normally need employer sponsorship or student status there (e.g. US CPT/J-1 internships). Prefer roles open to candidates based in {home} or remote-global; "
        "mark relocation roles APPLY only if the posting or company clearly sponsors international candidates, otherwise STRETCH with flag 'visa'; skip roles that need a local work permit the candidate cannot get. "
        "Logistics clarity matters: abroad roles with explicit sponsorship/relocation/visa support (including startups, neolabs, frontier labs, research and academic institutes, big tech) outrank abroad roles with unclear logistics; home-country and remote roles outrank unclear abroad roles.",
        "Wants lenient years-of-experience bars: internships, new-grad, residencies, fellowships and early-career-friendly roles.",
    ]))
