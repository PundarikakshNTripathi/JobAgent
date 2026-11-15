"""Read-only LinkedIn scouting through the logged-in agent Chrome. You authorised browsing connections and profiles; you send every message yourself.

How it works now (cheap and exact):
 1. browse.scan_company reads LinkedIn people search deterministically (1st degree first, 2nd degree with mutual connections when 1st is thin) and opens
    the top profiles for evidence. No model tokens; results cached for 7 days in the `people` table.
 2. ONE small model call ranks those real people and writes the approach from compact JSON (a few thousand tokens instead of a 300k-token browsing session).
 3. Only if the page layout defeats the extractor (results found but nothing parsed) does it fall back to a Claude + Playwright-MCP session.
Action buttons (Connect/Message/Follow/...) are blocked by an always-on page guard either way."""
import json
from common import db, log, run_claude, extract_json, PROFILE, budget_add
import browse, browser

RANK = """You are helping a final-year student get a referral or intro for this job. Below are REAL people seen just now on LinkedIn through the student's own
logged-in account (degree = connection degree to the student). Use only these people and only facts shown; do not invent anyone or anything.

STUDENT: {summary}
Proof points: {projects}
JOB: {company} - {title} ({location})  {url}

PEOPLE (JSON):
{people}

Pick the best 1-4 to approach, best first. Prefer: people on the relevant team or doing related work; approachable seniority (engineers, researchers, team leads,
university recruiters over executives); 1st degree over 2nd; 2nd degree only with a named mutual connection who can introduce. For each, give the specific hook from
THEIR headline/profile that the student can mention.
Return ONLY JSON:
{{"degree_searched":"1st|2nd|both","strategy":"2-4 sentences: who first, in what order, ask for advice vs referral, and when (apply and ask in parallel)",
"targets":[{{"name":"","degree":"1st|2nd","headline":"","profile_url":"","mutual":"names or ''","why_likely_to_respond":"","evidence":"what their profile shows, concretely",
"how_to_reach":"LinkedIn message (1st degree) | intro via <mutual> (2nd degree)"}}],
"before_you_message":["1-3 prep actions tied to these people's work"],"timing":"apply and message in parallel; one follow-up after 5-7 days"}}"""

MCP_PROMPT = """You are scouting LinkedIn (READ-ONLY, in a browser where the user is already logged in) to find the best people to approach for a referral. NEVER click Connect, Message, Follow, Like, Comment, Send or anything that changes state; a guard blocks them anyway. At most 10 page loads, waiting 3-6 s between them. Use browser_evaluate with short JS returning compact JSON (names, headlines, profile URLs, degree, mutual-connection text) rather than full snapshots.
TARGET: {company} - {title}. 1st degree: https://www.linkedin.com/search/results/people/?keywords={kw}&network=%5B%22F%22%5D ; if none, 2nd degree: network=%5B%22S%22%5D.
Report only people you actually saw, with their profile URL. Return ONLY JSON:
{{"degree_searched":"1st|2nd|none-found","strategy":"","targets":[{{"name":"","degree":"","headline":"","profile_url":"","mutual":"","why_likely_to_respond":"","evidence":"","how_to_reach":""}}],"before_you_message":[],"timing":"","notes":""}}"""


def _compact(people):
    return json.dumps([{k: (p.get(k) or "")[:700 if k == "about" else 200] for k in ("name", "degree", "headline", "current", "location", "mutual", "url", "about")}
                       for p in people], ensure_ascii=False)


def scout_people(job, csv_hits=None):
    """Returns the referral-plan JSON with real targets, or raises browse.BrowseError (login wall etc.) so the caller can say exactly what went wrong."""
    c = db()
    company = job["company"]
    people = browse.scan_company(company)
    budget_add(c, "linkedin")
    if csv_hits:  # people from the user's own LinkedIn export who were not in search results
        seen = {p["url"] for p in people}
        people += [{"name": h["name"], "degree": "1st", "headline": h["position"], "url": h["url"], "mutual": "", "location": "", "current": f"{h['position']} at {company}"}
                   for h in csv_hits if h.get("url") and h["url"] not in seen][:4]
    if not people:
        return {"degree_searched": "both", "strategy": "", "targets": [], "notes": f"No 1st- or 2nd-degree connections currently at {company} showed up in LinkedIn search."}
    text, _ = run_claude(RANK.format(summary=PROFILE["summary"], projects=" | ".join(PROFILE["projects"][:3]), company=company, title=job["title"],
                                     location=job["location"], url=job["url"], people=_compact(people)), kind="rank", timeout=240, max_turns=1)
    r = extract_json(text)
    known = {p["url"].rstrip("/"): p for p in people}
    r["targets"] = [t for t in r.get("targets", []) if (t.get("profile_url") or "").rstrip("/") in known]  # never let the model add someone we did not see
    log(f"linkedin scout for {company}: {len(people)} people seen, {len(r['targets'])} recommended")
    return r


def scout_people_mcp(job):
    """Fallback: Claude drives the browser through Playwright MCP (expensive). Used only when the deterministic extractor cannot parse the page."""
    mp = browser.mcp_config("readonly", browser.READONLY_GUARD)
    text, _ = run_claude(MCP_PROMPT.format(company=job["company"], title=job["title"], kw=job["company"].replace(" ", "%20").split("(")[0]),
                         kind="linkedin", allowed_tools="mcp__fillbrowser", timeout=600, max_turns=30, mcp_config=mp, persist=True)
    return extract_json(text)


def scan_list(companies, limit=15):
    """Batch: who you know at each company (1st degree, then 2nd with mutuals). Returns {company: [people]} and never calls a model."""
    out = {}
    for co in companies[:limit]:
        try:
            out[co] = browse.scan_company(co, want=6, profiles=0)
        except browse.BrowseError as e:
            out[co] = {"error": str(e)}
            if "log in" in str(e):
                break
    browse.close()
    return out
