"""Writes the actual outreach messages in the user's voice. Research (who to contact and why) is done elsewhere; this step only writes,
with the style playbook, the user's own sent messages and how they edited past drafts, then lints and fixes the result."""
import json
from common import PROFILE, extract_json, log
import humanize, memory
from llm import run

PROMPT = """{style}

TASK: write the messages {name} will send personally on LinkedIn/email. They must sound like {name} typing quickly, not like an assistant. Never use pronouns for {name} that aren't given; use "I" in the messages.

TRUE FACTS ABOUT {name} (the only claims allowed): {facts}
HARD RULES: {rules}

TARGET JOB: {company} | {title} | {location}
WHY THIS ROLE FITS (from earlier analysis): {fit}

PEOPLE (research already done; use only what is written here as the hook, never invent details about them):
{people}

For EACH person return one object:
- "name"
- "connect_note": max 250 characters, for 2nd-degree or cold contacts; "" if they are already a 1st-degree connection
- "dm_short": 35-55 words
- "dm_long": 60-90 words, same hook, one more concrete fact about the student
- "followup": 20-35 words, to send once after 5 to 7 days, plain, no guilt-tripping
- "ask_mutual": 25-45 words to the mutual connection asking for an intro (only for 2nd-degree, else "")
- "blurb": 2-3 sentences a referrer could paste to a recruiter, in third person about {name}, factual

Remember the rules in the style guide: one specific hook, one concrete fact, one easy question, no stock phrases, no dashes as punctuation, no lists, no exclamation marks. The first message asks a question or a pointer; it does not ask for a referral unless the person is a 1st-degree connection on the exact team.
Return ONLY a JSON array of these objects."""


def facts():
    P = PROFILE
    proofs = [p.split(":", 1)[0] + ": " + p.split(":", 1)[1].strip()[:230] if ":" in p else p[:230] for p in P.get("projects", [])[:4]]
    return " | ".join([P.get("summary", ""), P.get("education", "")] + P.get("experience", [])[:1] + proofs + P.get("achievements", [])[:2])


def draft(job, targets, fit=""):
    """targets: list of dicts from referral research (name, headline/role, degree, evidence, why_likely_to_respond, mutual). Returns list of message dicts."""
    if not targets:
        return []
    people = "\n".join(f"- {t.get('name')} | {t.get('role') or t.get('headline','')} | degree: {t.get('degree','cold')} | mutual: {t.get('mutual','')} | what they work on / evidence: {str(t.get('evidence') or t.get('why_them') or '')[:400]}" for t in targets)
    prompt = PROMPT.format(style=humanize.style_pack(), name=PROFILE.get("first_name") or PROFILE.get("name", "the student"), facts=facts(),
                           rules=" ".join(PROFILE.get("hard_rules", []))[:1200], company=job["company"], title=job["title"], location=job["location"], fit=fit[:400], people=people)
    text, _ = run(prompt, kind="outreach", timeout=300, max_turns=2)
    try:
        items = extract_json(text)
    except ValueError:
        log("outreach: could not parse model output")
        return []
    if isinstance(items, dict):  # a single message object, {"messages": [...]}, or {"Name": {...}}
        lst = next((v for v in items.values() if isinstance(v, list)), None)
        items = lst if lst is not None else ([items] if "name" in items else [dict(v, name=k) for k, v in items.items() if isinstance(v, dict)])
    items = [it for it in (items if isinstance(items, list) else []) if isinstance(it, dict)]
    FIELDS = ("connect_note", "dm_short", "dm_long", "followup", "ask_mutual", "blurb")
    import grounding
    src = [facts(), people, job["company"], job["title"], job["location"] or "", fit]
    extra = {f"{i}:{f}": [f"unsupported figure '{n}': remove it or use a number from the profile" for n in grounding.unsupported_numbers(it.get(f, ""), src)]
             for i, it in enumerate(items) for f in FIELDS}
    fixed = humanize.polish_batch({f"{i}:{f}": it.get(f, "") for i, it in enumerate(items) for f in FIELDS}, ctx="These are LinkedIn messages from a student.", extra=extra)
    out = []
    for i, it in enumerate(items):
        warn = []
        for f in FIELDS:
            if f"{i}:{f}" in fixed:
                it[f], issues = fixed[f"{i}:{f}"]
                warn += [f"{f}: {x}" for x in issues]
        if len(it.get("connect_note", "")) > 280:
            it["connect_note"] = it["connect_note"][:277].rsplit(" ", 1)[0] + "..."
        it["_style_warnings"] = warn
        out.append(it)
    if not out:
        log(f"outreach: no messages parsed (model returned {len(text)} chars)")
    return out
