"""Rewrite the user's Work at a Startup (YC) profile to be stronger, truthful only. You authorised this; a before/after record is written to out/yc_profile_before_after.md."""
import json, os
from common import log, run_claude, extract_json, SETTINGS, PROFILE, OUT
import browser

PROMPT = """You are editing the user's own Work at a Startup (Y Combinator) candidate profile in a browser where they are already logged in. Goal: make it as strong and hireable as possible using ONLY the facts below. Never invent employers, titles, dates, metrics, skills or links; never change account/privacy/visibility/notification/email/password settings; never apply to jobs, message founders or click 'connect'/'apply'. Saving the profile form is allowed and expected.

FACTS (single source of truth)
Name: {name}. Location: {location}. Education: {education}.
Summary: {summary}
Experience: {experience}
Projects: {projects}
Achievements: {achievements}
Skills (defensible): {skills}
Links: site {website}; GitHub {github}; org GitHub {org_github}; LinkedIn {linkedin}; X {twitter}; organisation/project: {org_site} ({org_status}).
What the user wants: ML systems/inference/kernels first, then AI/ML engineer / data scientist / applied scientist, then software engineering, then research and AI safety; internships and full-time; available from late Dec 2026/Jan 2027; on-site, hybrid or remote; relocation OK if visa is sponsored; authorized to work in India.
Tone: concrete, measured numbers, no hype, no 'passionate'. Lead with the strongest verifiable proof from the facts above, with their real numbers. Respect the hard rules: {rules}

PROCEDURE (keep it cheap; use browser_evaluate with short JS to read form fields instead of full snapshots when possible)
1. Open https://www.workatastartup.com/ , find the edit-profile page (usually via the profile/avatar link or /candidate/...), and read every editable field's current value.
2. Rewrite: headline/bio/about, role preferences, experience entries (from the facts above only), projects with measured results, skills, education, links, location/work-authorization/visa fields (authorized in India; needs sponsorship elsewhere), availability. Fill empty sections that have a matching fact.
3. Save. Re-read to confirm it persisted.
Return ONLY JSON: {{"before":"short summary of what was there","changes":["each field changed and the new value (short)"],"left_unchanged":["fields not touched and why"],"saved":true|false,"notes":""}}
"""


def run():
    mp = browser.mcp_config("yc")  # no submit guard: saving the profile is the task
    prompt = PROMPT.format(name=PROFILE["name"], location=PROFILE["location"], education=PROFILE["education"], summary=PROFILE["summary"],
                           experience=" | ".join(PROFILE["experience"]), projects=" | ".join(PROFILE["projects"]), achievements=" | ".join(PROFILE["achievements"]),
                           skills=PROFILE["skills_defensible"], website=PROFILE["website"], github=PROFILE.get("github", ""), org_github=PROFILE.get("org_github", ""), linkedin=PROFILE.get("linkedin", ""), rules=" ".join(PROFILE.get("hard_rules", [])),
                           twitter=PROFILE.get("twitter", ""), org_site=(PROFILE.get("organization") or {}).get("site", ""), org_status=(PROFILE.get("organization") or {}).get("status", ""))
    text, _ = run_claude(prompt, kind="yc", allowed_tools="mcp__fillbrowser", timeout=900, max_turns=60, mcp_config=mp, persist=True)
    r = extract_json(text)
    md = ["# YC Work at a Startup profile: before / after", "", f"Saved: {r.get('saved')}", "", "## Before", r.get("before", ""), "", "## Changes", *[f"- {c}" for c in r.get("changes", [])], "",
          "## Left unchanged", *[f"- {c}" for c in r.get("left_unchanged", [])], "", r.get("notes", "")]
    open(os.path.join(OUT, "yc_profile_before_after.md"), "w", encoding="utf-8").write("\n".join(md))
    log("YC profile edit finished")
    return r
