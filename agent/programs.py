"""Research & fellowships: academic research internships, pre-docs, residencies and fellowships (frontier labs, universities, safety programs).

Where they come from: the curated list (defaults/programs.json + your config/programs.json), the web scout, inbox items that are programs, and
X searches for 'residency / fellowship / research internship' announcements (discover.py). Each program's own page is read (local model) to
fill in its current window, deadline, eligibility for you, and whether it is open, with the source; nothing about dates is guessed. Your best
model writes an application strategy on request. Programs are never auto-applied: high stakes, referral and preparation first."""
import json, os, re
import common
from common import db, log, now, extract_json, PROFILE, CORE

PAGE = """Read this program page and extract the facts a candidate needs. Use ONLY the page. {rule}

PROGRAM: {name} ({org})
CANDIDATE: {cand}

PAGE ({url}):
{page}

Return ONLY JSON: {{"status": "open|upcoming|closed|unknown", "window": "when applications open/close as the page states, else ''",
"deadline": "YYYY-MM-DD if a date is stated, else ''", "eligibility": "who can apply, as stated", "fits_candidate": "yes|no|unclear",
"why": "one sentence (citizenship, degree level, year of study, location as stated)", "stipend_or_support": "", "duration": ""}}"""

STRATEGY = """Write an application strategy for {name} for this program. Use the program facts and {name}'s real facts only. {rule}

PROGRAM: {prog} ({org}) | window: {window} | eligibility: {elig}
PAGE NOTES: {notes}
{name}'S FACTS: {facts}

Return ONLY JSON: {{"worth_it": "yes|maybe|no", "why": "", "timeline": ["dated or ordered steps from now to the deadline"],
"materials": ["what to prepare (statement angle, which projects to lead with, references)"], "people": "who to contact first and how (alumni, mentors, PIs), only kinds of people, no invented names",
"edge": "where {name}'s record stands out for this program", "risks": ["what would sink the application"]}}"""


def curated():
    out = []
    for path in (os.path.join(CORE, "defaults", "programs.json"), os.path.join(common.HOME, "config", "programs.json")):
        try:
            out += json.load(open(path, encoding="utf-8")).get("programs", [])
        except (OSError, ValueError):
            pass
    return out


def _pid(name):
    return "prog:" + re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:60]


def refresh(limit=12, force=False):
    """Read each program's page (local model extracts) and store window, deadline, eligibility and status with the date checked."""
    from llm import run
    import research, websafe
    c = db()
    for p in curated():
        c.execute("INSERT OR IGNORE INTO programs(id,name,org,kind,url,window,eligibility,notes,added,deadline,track) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                  (_pid(p["name"]), p["name"], p.get("org", ""), "program", p["url"], "", "", "", now(), "", p.get("track", "research")))
    c.commit()
    rows = c.execute("SELECT * FROM programs ORDER BY COALESCE(notes,'')='' DESC, added").fetchall()
    cand = f"{PROFILE.get('summary', '')[:300]} | {PROFILE.get('education', '')} | graduates {PROFILE.get('graduation', '')} | citizen of {(PROFILE.get('preferences') or {}).get('home_country') or 'India'}"
    n = 0
    for r in rows:
        if n >= limit:
            break
        if not force and r["notes"] and "checked " + now()[:7] in (r["notes"] or ""):
            continue
        if not r["url"]:
            continue
        _, page, _ = research.read(r["url"], 9000)
        if len(page) < 200:
            c.execute("UPDATE programs SET notes=? WHERE id=?", (f"page not readable (checked {now()[:10]}): open it yourself", r["id"]))
            c.commit()
            continue
        try:
            text, _ = run(PAGE.format(rule=websafe.RULE, name=r["name"], org=r["org"] or "", cand=cand, url=r["url"], page=websafe.wrap(r["url"], page)), kind="research", timeout=300)
            d = extract_json(text)
        except Exception as e:
            log(f"programs: {r['name']}: {str(e)[:120]}")
            continue
        notes = f"{d.get('status', 'unknown')}; fits you: {d.get('fits_candidate', 'unclear')} ({d.get('why', '')}); {d.get('stipend_or_support', '')} {d.get('duration', '')} (checked {now()[:10]} from {r['url']})"
        c.execute("UPDATE programs SET window=?, eligibility=?, deadline=?, notes=? WHERE id=?",
                  (d.get("window", "")[:300], d.get("eligibility", "")[:500], d.get("deadline", "")[:10], notes[:800], r["id"]))
        c.commit()
        n += 1
    return n


def strategy(prog_id):
    from llm import run
    import outreach, websafe
    c = db()
    r = c.execute("SELECT * FROM programs WHERE id=?", (prog_id,)).fetchone()
    if not r:
        return {"error": "no such program"}
    text, _ = run(STRATEGY.format(name=PROFILE.get("first_name") or "the candidate", rule=websafe.RULE, prog=r["name"], org=r["org"] or "", window=r["window"] or "?",
                                  elig=r["eligibility"] or "?", notes=r["notes"] or "", facts=outreach.facts(full=True)[:4500]), kind="review", timeout=300)
    try:
        s = extract_json(text)
    except ValueError:
        return {"error": "could not read the strategy"}
    c.execute("UPDATE programs SET strategy=? WHERE id=?", (json.dumps(s, ensure_ascii=False), prog_id))
    c.commit()
    return s
