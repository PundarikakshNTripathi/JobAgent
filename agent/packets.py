"""Per-job application packet: fit analysis, cover letter (md + pdf), answers bank, resume choice."""
import json, os, re, shutil, subprocess, memory, humanize, common
from common import (db, log, now, run_claude, extract_json, slug, APPS, PROFILE, SETTINGS, budget_add, budget_used, http_json)

PACKET_PROMPT = """You are preparing a job application for ONE candidate. You may use WebFetch/WebSearch to read the posting and research the company. Ground every company-specific statement in what you actually read; never invent facts about the company or the candidate.

=== CANDIDATE PROFILE (the only source of truth about the candidate) ===
{profile}

=== HARD RULES (non-negotiable) ===
{rules}

=== TARGET JOB ===
company: {company}
title: {title}
location: {location}
url: {url}
source/ATS: {ats}
posting text from feed (may be partial; fetch the url for the full text): {desc}
triage note: {triage}
{questions_block}

=== TASKS ===
1. WebFetch the posting URL (and, if useful, the company's about/blog/research page; do at most 4 fetches/searches in total). Learn what the role really needs and what the team does.
2. Pick the best resume variant from: {variants}.
3. Write a cover letter, 170-230 words, plain text, no placeholders, addressed to the hiring team. Structure: one sentence on why this team/problem specifically (grounded in something you read), then the two strongest pieces of REAL evidence from the profile mapped to the posting's needs (with the measured numbers), then one honest sentence on what is still to learn / how you ramp, then a short close. No flattery, no clichés ("passionate", "excited to leverage"), no em-dash chains.
4. Draft answers for common free-text questions (60-120 words each, first person, specific): why_company, why_role, relevant_project, strengths_fit, anything_else, and compensation/start-date handling if relevant. If the posting needs something the candidate lacks, say how they would close that gap rather than hiding it.
5. For every real application-form question listed above, answer it. Where the answer depends on personal data not in the profile (salary, specific dates, demographic/EEO info, AI-tool-use disclosure, visa details beyond the profile), set needs_user=true and leave answer="".
6. Be honest about odds and gaps.

Return ONLY this JSON object:
{{"role_summary":"2-3 sentences","resume":"<variant>","strengths":["..."],"gaps":["..."],"honest_odds":"e.g. low / moderate, and why",
"cover_letter":"...","answers":{{"why_company":"","why_role":"","relevant_project":"","strengths_fit":"","anything_else":""}},
"form_questions":[{{"question":"","answer":"","needs_user":false,"confidence":"high|medium|low"}}],
"needs_user":["things only the candidate can supply"],"deadline":"if stated, else ''","visa_note":"what the posting says about sponsorship/location, else ''",
"company_notes":"hiring process / what they value, from what you read (2-4 sentences)","referral_priority":"high|medium|low",
"eligibility":"ok|uncertain|blocked","eligibility_reason":"blocked ONLY for a hard stop stated in the posting/form: requires existing work authorization in a country where the candidate has none and says it does not sponsor; US citizenship/clearance; PhD-only; wrong level; must be enrolled in a program the candidate is not in. uncertain if sponsorship is unstated for an on-site role outside the candidate's home country. Candidate: {auth_note}"}}
"""


def _greenhouse_questions(job):
    m = re.match(r"greenhouse:([^:]+):(\d+)$", job["id"])
    if not m:
        return ""
    try:
        d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{m.group(1)}/jobs/{m.group(2)}?questions=true", timeout=30)
    except Exception:
        return ""
    qs = []
    for q in d.get("questions", []):
        label = q.get("label", "")
        kinds = [f.get("type") for f in q.get("fields", [])]
        if label and not any(k in ("input_file",) for k in kinds) and label.lower() not in ("first name", "last name", "email", "phone"):
            opts = [v.get("label") for f in q.get("fields", []) for v in (f.get("values") or [])][:8]
            qs.append(f"- {label}" + (f"  options: {opts}" if opts else ""))
    return "Actual application-form questions (from the Greenhouse API):\n" + "\n".join(qs) if qs else ""


def _latex_escape(s):
    rep = {"\\": r"\textbackslash{}", "&": r"\&", "%": r"\%", "$": r"\$", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}", "~": r"\textasciitilde{}", "^": r"\textasciicircum{}"}
    return "".join(rep.get(ch, ch) for ch in s)


def make_cover_pdf(text, company, title, out_pdf):
    tectonic = SETTINGS.get("tectonic")
    if not tectonic or not os.path.exists(tectonic):
        return False
    body = "\n\n".join(_latex_escape(p.strip()) for p in text.split("\n\n") if p.strip())
    tex = (r"\documentclass[11pt]{article}\usepackage[margin=1in]{geometry}\usepackage[T1]{fontenc}\usepackage{lmodern}\usepackage[hidelinks]{hyperref}"
           r"\setlength{\parindent}{0pt}\setlength{\parskip}{9pt}\pagestyle{empty}\begin{document}"
           + r"{\large\textbf{" + _latex_escape(PROFILE["name"]) + r"}}\\ " + _latex_escape(PROFILE["email"] + " | " + PROFILE["phone"] + " | " + PROFILE["location"])
           + r"\\ \href{" + PROFILE["website"] + r"}{" + PROFILE["website"].replace("https://", "") + r"}" + "\n\n"
           + r"\textbf{Re: " + _latex_escape(title) + " at " + _latex_escape(company) + "}\n\n" + body + "\n\nSincerely,\\\\\n" + _latex_escape(PROFILE["name"]) + r"\end{document}")
    d = os.path.dirname(out_pdf)
    tex_path = os.path.join(d, "cover_letter.tex")
    open(tex_path, "w", encoding="utf-8").write(tex)
    try:
        p = subprocess.run([tectonic, "cover_letter.tex"], cwd=d, capture_output=True, text=True, timeout=180)
        return p.returncode == 0 and os.path.exists(out_pdf)
    except Exception as e:
        log(f"tectonic failed: {e}")
        return False


def packet_dir(job):
    d = os.path.join(APPS, f"{job['first_seen'][:10]}_{slug(job['company'], 24)}_{slug(job['title'], 40)}_{slug(job['id'].split(':')[-1], 12)}")
    os.makedirs(d, exist_ok=True)
    return d


def build_packet(job_id, force=False):
    import jd
    jd.gate(job_id, force=force)  # full posting read + requirements met, or GateError with the reason
    c = db()
    job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    tri = c.execute("SELECT * FROM triage WHERE job_id=?", (job_id,)).fetchone()
    if not job:
        raise ValueError("unknown job")
    d = packet_dir(job)
    triage_note = f"fit={tri['fit']:.0f} verdict={tri['verdict']} resume={tri['resume']} yoe_gap={tri['yoe_gap']} reason={tri['reason']}" if tri else "(none)"
    qb = _greenhouse_questions(job)
    prof = {k: PROFILE[k] for k in ("name", "email", "phone", "location", "linkedin", "github", "org_github", "website", "education", "graduation", "summary",
                                    "experience", "projects", "achievements", "skills_defensible", "form_defaults")}
    prompt = PACKET_PROMPT.format(profile=json.dumps(prof, indent=1), rules="\n".join("- " + r for r in PROFILE["hard_rules"]),
                                  company=job["company"], title=job["title"], location=job["location"], url=job["url"], ats=job["ats"],
                                  desc=(job["desc"] or "")[:3500], triage=triage_note, questions_block=qb,
                                  variants=json.dumps(PROFILE["resume_variant_guidance"]), auth_note=memory.auth_note())
    prompt += common.job_context(job_id, "packet")
    text, _ = run_claude(prompt, kind="packet", allowed_tools="WebFetch,WebSearch",
                         timeout=SETTINGS["packet_timeout_s"], max_turns=14)
    p = extract_json(text)
    style_warn = []
    fields = {"cover_letter": p.get("cover_letter") or ""} | {f"a:{k}": v for k, v in (p.get("answers") or {}).items() if isinstance(v, str) and v}
    import grounding
    src = [json.dumps(PROFILE, ensure_ascii=False), job["desc"] or "", job["title"], job["company"], job["location"] or ""]
    extra = {k: [f"unsupported figure '{n}': remove it or use a number from the profile" for n in grounding.unsupported_numbers(v, src)] for k, v in fields.items()}
    for key, (txt, w) in humanize.polish_batch(fields, ctx="A cover letter and job-application answers written by a student.", extra=extra).items():  # one rewrite call at most
        if key == "cover_letter":
            p["cover_letter"] = txt
        else:
            p["answers"][key[2:]] = txt
        style_warn += [f"{key.replace('a:', '')}: {x}" for x in w]
    p["style_warnings"] = style_warn
    if p.get("resume") not in PROFILE["resume_variants"]:
        p["resume"] = (tri["resume"] if tri else "AI_Engineer")
    # files
    resume_src = PROFILE["resume_variants"][p["resume"]]
    resume_dst = os.path.join(d, os.path.basename(resume_src))
    if os.path.exists(resume_src):
        shutil.copy2(resume_src, resume_dst)
    p["resume_path"] = resume_dst if os.path.exists(resume_dst) else resume_src
    open(os.path.join(d, "cover_letter.txt"), "w", encoding="utf-8").write(p.get("cover_letter", ""))
    pdf = os.path.join(d, "cover_letter.pdf")
    p["cover_pdf"] = pdf if make_cover_pdf(p.get("cover_letter", ""), job["company"], job["title"], pdf) else ""
    json.dump({"job": dict(job), "triage": dict(tri) if tri else None, "packet": p}, open(os.path.join(d, "packet.json"), "w", encoding="utf-8"), indent=1)
    md = [f"# {job['company']} - {job['title']}", f"{job['location']}  |  {job['url']}", f"first seen {job['first_seen']}  |  score {job['score']:.0f}  |  visa signal: {job['visa']}", "",
          f"**Resume to use:** {p['resume']} -> `{p['resume_path']}`", f"**Eligibility:** {p.get('eligibility','?')} {p.get('eligibility_reason','')}", f"**Honest odds:** {p.get('honest_odds','')}", f"**Referral priority:** {p.get('referral_priority','')}",
          f"**Deadline:** {p.get('deadline') or 'not stated'}  |  **Visa/location:** {p.get('visa_note') or 'not stated'}", "", "## Role", p.get("role_summary", ""), "",
          "## Strengths", *[f"- {x}" for x in p.get("strengths", [])], "", "## Gaps (be ready to address)", *[f"- {x}" for x in p.get("gaps", [])], "",
          "## Company notes", p.get("company_notes", ""), "", "## Cover letter", p.get("cover_letter", ""), "", "## Answers"]
    for k, v in (p.get("answers") or {}).items():
        md += [f"**{k}**", v, ""]
    md += ["## Form questions"]
    for q in p.get("form_questions", []):
        md += [f"- **{q.get('question')}** ({q.get('confidence','')}{', NEEDS YOU' if q.get('needs_user') else ''})", f"  {q.get('answer','')}"]
    md += ["", "## Needs you"] + ([f"- {x}" for x in p.get("needs_user", [])] or ["- nothing flagged"])
    open(os.path.join(d, "README.md"), "w", encoding="utf-8").write("\n".join(md))
    c.execute("INSERT OR REPLACE INTO packets(job_id,dir,at) VALUES(?,?,?)", (job_id, d, now()))
    cur = job["status"]
    common.set_status(c, job_id, "ineligible" if p.get("eligibility") == "blocked" else (cur if cur in ("watch",) + common.USER_STATUS else "packet"))
    budget_add(c, "packets")
    try:
        if p.get("eligibility") != "blocked" and ((tri and (tri["fit"] or 0) >= 60) or cur == "watch"):
            import resume
            resume.advise(job_id)
    except Exception as e:
        log(f"resume advice skipped: {e}")
    log(f"packet ready: {job['company']} | {job['title']} -> {d}")
    return d


def pending_for_packets(limit):
    c = db()
    return c.execute("""SELECT j.id FROM jobs j JOIN triage t ON t.job_id=j.id
                        WHERE j.active=1 AND t.verdict IN ('APPLY','STRETCH') AND t.fit>=45
                          AND NOT EXISTS (SELECT 1 FROM packets p WHERE p.job_id=j.id)
                          AND j.status NOT IN ('applied','skipped','dismissed','ineligible','closed') AND j.ghost IS NULL AND j.closed_at IS NULL
                          AND COALESCE(j.gate,'') NOT IN ('visa-unclear','jd-unread')
                        ORDER BY (t.verdict='APPLY') DESC, t.fit DESC LIMIT ?""", (limit,)).fetchall()


def _mark_ineligible(job_id, why):
    c = db()
    if (c.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone() or {"status": ""})["status"] != "watch":
        common.set_status(c, job_id, "ineligible")
    log(f"jd gate: {job_id}: {why}")


def run_packets(per_cycle=None):
    c = db()
    left = SETTINGS["max_packets_per_day"] - budget_used(c, "packets")
    if per_cycle:
        left = min(left, per_cycle)
    if left <= 0:
        return []
    built = []
    import jd
    for r in pending_for_packets(left * 3):
        if len(built) >= left:
            break
        try:
            built.append(build_packet(r["id"]))
        except jd.GateError as e:
            _mark_ineligible(r["id"], str(e))
        except Exception as e:
            log(f"packet failed for {r['id']}: {e}")
    return built


def run_watch_drafts(limit=1):
    """Prepare (never fill) a draft packet for the top watchlist item that has none."""
    c = db()
    if budget_used(c, "packets") >= SETTINGS["max_packets_per_day"]:
        return []
    out = []
    for r in c.execute("""SELECT w.job_id FROM watch w JOIN jobs j ON j.id=w.job_id LEFT JOIN triage t ON t.job_id=w.job_id
                          WHERE NOT EXISTS (SELECT 1 FROM packets p WHERE p.job_id=w.job_id) AND j.status='watch'
                          ORDER BY COALESCE(t.fit,0) DESC LIMIT ?""", (limit,)).fetchall():
        try:
            out.append(build_packet(r["job_id"]))
        except Exception as e:
            log(f"watch draft skipped {r['job_id']}: {e}")
    return out
