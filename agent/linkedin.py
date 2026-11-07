"""Read-only LinkedIn scouting through the logged-in agent Chrome. You authorised browsing connections and profiles; you send every message yourself.
Safeguards: action buttons (Connect/Message/Follow/Like/Comment/Send...) are blocked by an always-on page guard; page budget and human-paced waits are in the prompt;
page text is extracted with small JS snippets instead of full snapshots to save tokens. Capped per day (default 1 company)."""
import json, os
from common import db, log, now, run_claude, extract_json, SETTINGS, PROFILE, budget_add, DATA
import browser

PROMPT = """You are scouting LinkedIn (READ-ONLY, in a browser where the user is already logged in) to find the best people to approach for a referral/intro. NEVER click Connect, Message, Follow, Like, Comment, Send, Endorse or anything that changes state; a guard blocks them anyway. Do not open more than 10 pages in total and wait 3-6 seconds (browser_wait_for) between page loads, like a human. To keep it cheap, avoid full snapshots: use browser_evaluate with short JS that returns compact JSON (names, headlines, profile URLs, degree, mutual-connection text, first ~1500 chars of the profile's About/Experience), and browser_navigate for pages.

CANDIDATE (the user): {summary}
Proof points: {projects}
TARGET: {company} - {title} ({location})  {url}
{csv_block}

STEPS
1. FIRST-DEGREE: open https://www.linkedin.com/search/results/people/?keywords={kw}&network=%5B%22F%22%5D (1st-degree connections). Keep people whose headline/current company is {company}.
2. If none: SECOND-DEGREE: network=%5B%22S%22%5D with keywords "{kw}" plus a role word (research engineer, kernel, inference, software engineer, recruiter, university recruiter). Prefer people with mutual connections (an intro path) and ones at relevant teams.
3. Open at most 4 best profiles (evaluate their innerText, truncated). Judge likelihood to respond: relevant team, seniority not too high, recently active/posting, open-source/research visibility, India or shared-background links, 'open to' signals, mutual connections with the user.
4. Write outreach that the USER will send. Specific hook from THEIR profile/work, one low-friction ask, honest about being a final-year undergrad from a non-top university; never overclaim (TernixEngine is kernels in R&D; Aegis one-week sprint; no publications). Connection note <=280 chars; DM <=90 words; follow-up; for 2nd-degree also a short ask a mutual connection could forward.

Return ONLY JSON:
{{"degree_searched":"1st|2nd|none-found","strategy":"2-4 sentences: who first, in what order, ask for advice vs referral","targets":[{{"name":"","degree":"1st|2nd","headline":"","profile_url":"","mutual":"names/count or ''","why_likely_to_respond":"","evidence":"what you saw on their profile","connection_note":"","dm":"","followup":"","intro_ask_for_mutual":"",
"blurb_for_referrer":""}}],"before_you_message":["1-3 prep actions"],"timing":"","notes":"any page that blocked you"}}
"""


def scout_people(job, csv_hits=None):
    c = db()
    prompt = PROMPT.format(summary=PROFILE["summary"], projects=" | ".join(PROFILE["projects"][:3]), company=job["company"], title=job["title"], location=job["location"],
                           url=job["url"], kw=job["company"].replace(" ", "%20").split("(")[0],
                           csv_block=("The user's own LinkedIn export lists these 1st-degree connections at this company (prioritise them, verify on their profile): " +
                                      "; ".join(f"{h['name']} ({h['position']}) {h['url']}" for h in csv_hits)) if csv_hits else "")
    mp = browser.mcp_config("readonly", browser.READONLY_GUARD)
    text, _ = run_claude(prompt, kind="linkedin", allowed_tools="mcp__fillbrowser,WebSearch", model=SETTINGS["claude_model_packets"], timeout=600, max_turns=45, mcp_config=mp, persist=True)
    budget_add(c, "linkedin")
    log(f"linkedin scout done for {job['company']}")
    return extract_json(text)
