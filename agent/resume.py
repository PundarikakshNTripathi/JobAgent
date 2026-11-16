"""Resume tailoring where it matters: for strong fits, high-stakes targets, or on request. Produces concrete, truthful edit suggestions plus a tailored
Markdown resume in the packet folder. The user's own resume source (LaTeX, Markdown, Word export...) stays the source of truth: JobAgent never edits it."""
import json, os
from common import db, log, now, extract_json, PROFILE
import memory, packets
from llm import run

PROMPT = """{skill}

CANDIDATE FACTS (the only source of truth; never add anything not here):
{facts}

CANDIDATE'S CURRENT RESUME SOURCE (excerpts):
{resume}

TARGET: {company} | {title} | {location}
POSTING / ROLE NOTES:
{desc}

TRIAGE NOTE: {triage}

Return ONLY JSON:
{{"worth_tailoring": true|false, "why": "one sentence",
"keywords_to_mirror": ["only words the candidate can truthfully claim"],
"changes": [{{"section":"summary|experience|projects|skills|achievements","action":"promote|demote|rewrite|add|remove","target":"which line/project","new_text":"the new wording or ''","reason":"what the posting wants","supported_by":"the profile fact that makes it true"}}],
"tailored_resume_md": "a one-page Markdown resume for this role built only from the facts above (empty string if worth_tailoring is false)"}}"""


def resume_text(limit=7000):
    out = []
    for p in PROFILE.get("resume_sources", []):
        try:
            out.append(f"## {os.path.basename(p)}\n" + open(p, encoding="utf-8", errors="replace").read()[:2500])
        except OSError:
            pass
    return "\n".join(out)[:limit] or "(none provided; use the profile facts)"


def advise(job_id):
    c = db()
    job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    tri = c.execute("SELECT * FROM triage WHERE job_id=?", (job_id,)).fetchone()
    if not job:
        raise ValueError("unknown job")
    import web
    desc = job["desc"] or ""
    if len(desc) < 400 and job["url"]:
        t, body = web.fetch_text(job["url"], 3500)
        desc = f"{t}\n{body}"
    import outreach
    prompt = PROMPT.format(skill=memory.skill("resume-tailoring"), facts=outreach.facts() + " | HARD RULES: " + " ".join(PROFILE.get("hard_rules", []))[:900], resume=resume_text(),
                           company=job["company"], title=job["title"], location=job["location"], desc=desc[:3500],
                           triage=(f"fit {tri['fit']}, resume {tri['resume']}, gap: {tri['yoe_gap']}, reason: {tri['reason']}" if tri else "none"))
    import common
    prompt += common.job_context(job_id, "resume")
    text, _ = run(prompt, kind="resume", timeout=400, max_turns=2)
    d = extract_json(text)
    pk = c.execute("SELECT dir FROM packets WHERE job_id=?", (job_id,)).fetchone()
    folder = pk["dir"] if pk else packets.packet_dir(job)
    os.makedirs(folder, exist_ok=True)
    md = [f"# Resume suggestions: {job['company']} - {job['title']}", f"Worth tailoring: **{d.get('worth_tailoring')}**. {d.get('why','')}", "",
          "## Keywords to mirror (only if true)", ", ".join(d.get("keywords_to_mirror", [])), "", "## Edits to make in your resume source"]
    for ch in d.get("changes", []):
        md += [f"- **{ch.get('action','').upper()} {ch.get('section','')}**: {ch.get('target','')}", f"  new: {ch.get('new_text','')}", f"  why: {ch.get('reason','')} | true because: {ch.get('supported_by','')}"]
    if d.get("tailored_resume_md"):
        md += ["", "## Tailored resume (Markdown)", "", d["tailored_resume_md"]]
    open(os.path.join(folder, "RESUME.md"), "w", encoding="utf-8").write("\n".join(md))
    c.execute("INSERT OR IGNORE INTO referrals(job_id,dir,at,how,state) VALUES(?,?,?,?,?)", (job_id, folder, now(), "resume-only", "none")) if not pk else None
    c.commit()
    log(f"resume advice written for {job['company']} | {job['title']}")
    return os.path.join(folder, "RESUME.md")
