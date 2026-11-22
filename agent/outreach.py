"""Writes the actual outreach messages in the user's voice. Research (who to contact and why) is done elsewhere; this step only writes,
with the style playbook, the user's own sent messages and how they edited past drafts, then lints and fixes the result."""
import json
from common import PROFILE, extract_json, log
import humanize, memory
from llm import run

PROMPT = """{style}

TASK: write the messages {name} will send personally. They must read like {name} typed them: plain, specific, a little informal, and they must
FLOW: one line on who {name} is, then why THIS person (something specific they built, wrote or posted, from the research), then one clear ask.
No warm-up sentences, no compliments without a fact behind them, no jumping straight to a topic without saying who is writing.
Use "I" for {name}; never use pronouns for {name} that aren't given.

TRUE FACTS ABOUT {name} (the only claims allowed): {facts}
HARD RULES: {rules}

TARGET JOB: {company} | {title} | {location}
WHY THIS ROLE FITS (from earlier analysis): {fit}
{dossier}

PEOPLE (research already done; use only what is written here as the hook, never invent details about them):
{people}

For EACH person return one object:
- "name"
- "channel": "linkedin" or "email", and "channel_why": one short reason (network contacts: LinkedIn; founders/hiring-post authors with a found email: email)
- "connect_note": max 250 characters, for 2nd-degree or cold contacts on LinkedIn; "" if they are already a 1st-degree connection
- "dm": 50-90 words, the main message (LinkedIn DM or the email body): who {name} is -> why them specifically -> one ask
- "followup": 20-35 words, to send once after 5 to 7 days, plain, no guilt-tripping
- "ask_mutual": 25-45 words to the mutual connection asking for an intro (only for 2nd-degree, else "")
- "blurb": 2-3 sentences a referrer could paste to a recruiter, in third person about {name}, factual
- "make_it_yours": 2-3 short, concrete suggestions for what {name} should add or change by hand (a personal detail only they know, a line to make rougher, a question only they would ask)
The first message asks a question or a pointer; it does not ask for a referral unless the person is a 1st-degree connection on the exact team.
Return ONLY a JSON array of these objects."""


def facts(full=False):
    """The candidate's true facts. full=True: every project, role and achievement (so the proof can be matched to what THIS company needs,
    instead of always leading with the same project)."""
    P = PROFILE
    n = None if full else 4
    cut = 600 if full else 230
    proofs = [p.split(":", 1)[0] + ": " + p.split(":", 1)[1].strip()[:cut] if ":" in p else p[:cut] for p in P.get("projects", [])[:n]]
    return " | ".join([P.get("summary", ""), P.get("education", "")] + P.get("experience", [])[: (None if full else 1)] + proofs + P.get("achievements", [])[: (None if full else 2)])


def draft(job, targets, fit="", dossier=""):
    """targets: list of dicts from referral research (name, headline/role, degree, evidence, why_likely_to_respond, mutual). Returns list of message dicts."""
    if not targets:
        return []
    people = "\n".join(f"- {t.get('name')} | {t.get('role') or t.get('headline','')} | degree: {t.get('degree','cold')} | mutual: {t.get('mutual','')} | what they work on / evidence: {str(t.get('evidence') or t.get('why_them') or '')[:400]}" for t in targets)
    prompt = PROMPT.format(style=humanize.style_pack(), name=PROFILE.get("first_name") or PROFILE.get("name", "the student"), facts=facts(full=True)[:5000],
                           rules=(" ".join(PROFILE.get("hard_rules", [])) + " " + " ".join("Never say: " + x for x in __import__("about").never_say()))[:1600], company=job["company"], title=job["title"], location=job["location"], fit=fit[:400],
                           people=people, dossier=("COMPANY RESEARCH (pages JobAgent read; use for 'why them'):\n" + dossier) if dossier else "")
    import common
    prompt += common.job_context(job["id"], "outreach") if "id" in job.keys() else ""
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
    for it in items:  # older callers read dm_short / dm_long
        it.setdefault("dm_short", it.get("dm", ""))
        it.setdefault("dm_long", "")
    FIELDS = ("connect_note", "dm_short", "followup", "ask_mutual", "blurb")
    import grounding
    src = [facts(full=True), people, job["company"], job["title"], job["location"] or "", fit, dossier]
    extra = {f"{i}:{f}": [f"unsupported figure '{n}': remove it or use a number from the profile" for n in grounding.unsupported_numbers(it.get(f, ""), src)]
             for i, it in enumerate(items) for f in FIELDS}
    fixed = humanize.polish_batch({f"{i}:{f}": it.get(f, "") for i, it in enumerate(items) for f in FIELDS}, ctx="These are LinkedIn messages and emails from a student. Keep the order: who they are, why this person, one ask.", extra=extra)
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
