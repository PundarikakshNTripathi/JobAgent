"""Full job descriptions and the eligibility gate.

Nothing gets a packet, a referral plan or a form fill until the FULL posting has been read and its requirements checked against the candidate:
years of experience, required degree (MS/PhD), seniority in the title, citizenship/clearance, graduation window. Everything here is deterministic
(no model tokens): ATS APIs first (Greenhouse, Lever, Ashby), then the page itself (JSON-LD JobPosting or HTML), then the logged-in agent browser for
JavaScript-only pages. Results are stored on the job row (`jd_full`, `req`) so each posting is read once."""
import json, re, time
import common
from common import db, log, now, PROFILE, http_json
import scoring, web

MIN_JD = 700  # characters of real posting text before we trust it


class GateError(Exception):
    """The candidate does not meet a hard requirement (or the posting could not be read). str() is the user-facing reason."""


# ------------------------------------------------------------------ fetching
def _html_to_text(h):
    return web.to_text(h or "") if hasattr(web, "to_text") else re.sub(r"<[^>]+>", " ", h or "")


def _from_ats(job):
    """Full text from the ATS's public API when the job id tells us which board it is. Returns '' when not applicable."""
    jid, url = job["id"] or "", job["url"] or ""
    parts = jid.split(":")
    try:
        if parts[0] == "greenhouse" and len(parts) >= 3:
            j = http_json(f"https://boards-api.greenhouse.io/v1/boards/{parts[1]}/jobs/{parts[-1]}?content=true", timeout=20)
            import html as _h
            return _html_to_text(_h.unescape(j.get("content", "")))
        if parts[0] == "lever" and len(parts) >= 3:
            j = http_json(f"https://api.lever.co/v0/postings/{parts[1]}/{parts[-1]}", timeout=20)
            lists = "\n".join(f"{l.get('text','')}:\n{_html_to_text(l.get('content',''))}" for l in j.get("lists", []))
            return "\n".join([j.get("descriptionPlain", ""), lists, j.get("additionalPlain", "")])
        if parts[0] == "ashby" and len(parts) >= 3:
            j = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{parts[1]}", timeout=25)
            for p in j.get("jobs", []):
                if p.get("id") == parts[-1] or (p.get("jobUrl") or "") == url:
                    return p.get("descriptionPlain") or _html_to_text(p.get("descriptionHtml", ""))
    except Exception as e:
        log(f"jd: ATS api failed for {jid}: {e}")
    return ""


def _from_browser(url):
    try:
        import browse
        return browse.page_text(url, max_chars=16000)
    except Exception as e:
        log(f"jd: browser read failed for {url}: {e}")
        return ""


def ensure_full_jd(job_id, use_browser=True, refresh=False):
    """Make sure the stored description is the full posting. refresh=True reads it again (closed postings, edited requirements). Returns (row, ok)."""
    c = db()
    job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        raise GateError("unknown job")
    if job["jd_full"] and not refresh:
        return job, True
    text = _from_ats(job)
    if len(text) < MIN_JD and job["url"]:
        _, body = web.fetch_text(job["url"], 16000)
        if body and not body.startswith("(fetch failed"):
            if re.search(r"no longer (accepting|available|open)|position (has been )?filled|job (is )?closed|page (was )?not found", body[:3000], re.I):
                c.execute("UPDATE jobs SET active=0, status='closed' WHERE id=?", (job_id,))
                c.commit()
                raise GateError("this posting is closed or no longer accepting applications")
            text = body if len(body) > len(text) else text
    if len(text) < MIN_JD and use_browser and job["url"]:
        text = _from_browser(job["url"]) or text
    ok = len(text) >= MIN_JD
    if ok:
        desc = text[:20000]
        s = scoring.score({"title": job["title"], "location": job["location"], "desc": desc, "ats": job["ats"], "_tier": job["tier"]})
        c.execute("UPDATE jobs SET desc=?, jd_full=1, yoe=?, visa=? WHERE id=?", (desc, s["yoe"], s["visa"] if s["visa"] != "unknown" else job["visa"], job_id))
        c.commit()
        job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return job, ok


# ------------------------------------------------------------------ requirements
_REQ_HDR = re.compile(r"(basic|minimum|required|must[- ]have)\s+qualifications?|requirements|what you.ll need|what we.re looking for|you (have|bring|are)|who you are|qualifications", re.I)
_PREF_HDR = re.compile(r"preferred\s+qualifications?|nice[- ]to[- ]have|bonus( points)?|pluses|preferred|great if you", re.I)
_SENIOR = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|architect|distinguished|vp)\b|\b(ii|iii|iv)\b\s*$|\bl[5-9]\b", re.I)
_EARLY_OK = re.compile(r"\b(intern(ship)?|new grad|graduate|entry|junior|associate|fellow(ship)?|residen(t|cy)|apprentice|trainee|student|early career|university|campus|i)\b", re.I)
_PHD = r"\b(ph\.?\s?d|doctora(l|te))"
_MS = r"\b(master|m\.\s?s\.?(?=\W)|ms(?=[\s/,)])|msc\b|m\.?tech\b|m\.?eng\b|graduate degree|advanced degree)"
_BS = r"\b(bachelor|b\.\s?s\.?(?=\W)|bs(?=[\s/,)])|bsc\b|b\.?tech\b|b\.e\.(?=\W)|undergraduate|four[- ]year degree)"
_CITIZEN = re.compile(r"(must be|requires?|required to be)[^.\n]{0,40}(u\.?s\.? citizen|citizenship)|security clearance|active (secret|ts/sci) clearance|\bitar\b|export control[^.\n]{0,60}(u\.?s\.? person)", re.I)
_YOE_BY_DEGREE = re.compile(r"(\d{1,2})\s*\+?\s*(?:years?|yrs?)[^()\n]{0,80}\(\s*(bachelor|bs\b|b\.s|undergrad)", re.I)


def _sections(text):
    """(required_text, preferred_text). Without headers everything counts as required context."""
    t = text or ""
    pref = _PREF_HDR.search(t)
    req = _REQ_HDR.search(t)
    if pref and (not req or pref.start() > req.start()):
        return t[:pref.start()], t[pref.start():]
    return t, ""


def _sentences(t):
    return [s for s in re.split(r"(?<=[.;\n•·])\s+|\s+[-–•·*]\s+(?=[A-Z0-9])", t) if s.strip()]


def _degree_need(req_text):
    """'phd' / 'ms' / '' : the lowest degree a REQUIRED sentence accepts, if it rules out a bachelor's."""
    need = ""
    for s in _sentences(req_text):
        sl = s.lower()
        has_phd, has_ms, has_bs = re.search(_PHD, sl), re.search(_MS, sl), re.search(_BS, sl)
        if not (has_phd or has_ms):
            continue
        if has_bs or re.search(r"\bor equivalent (practical )?experience\b|or related (field|experience)? ?(and|with)? ?\d", sl):
            continue  # bachelor's accepted in the same breath
        if re.search(r"prefer|plus|bonus|nice to have|ideally|desirable", sl):
            continue
        # mentions that are not requirements: alumni stories, "open to all degree levels", "including PhD students", "PhD students are not eligible"...
        if re.search(r"all degree levels|including|welcome|also |encourage|not eligible|became|joined|went on|after (completing|earning)|completed our|alumni|graduate of|"
                     r"majority|equivalent|such as|form\b|programs? at|prepare for|before (starting|applying|a )|apply(ing)? to ph", sl):
            continue
        if re.search(r"required|requires|must|minimum|you (have|hold|are)|(currently )?(are |be )?(pursuing|enrolled in|working towards)|candidates? (should|must)|"
                     r"what we need|degree in|you will graduate|hold an?\b", sl):
            lvl = "ms" if has_ms else "phd"
            if has_phd and not has_ms:
                lvl = "phd"
            if not need or (need == "phd" and lvl == "ms"):
                need = lvl
    return need


def _grad_years(text):
    ys = set()
    for m in re.finditer(r"graduat\w*[^.\n]{0,60}?((?:20\d\d)(?:\s*(?:-|–|to|or|and|/|,)\s*20\d\d)*)", text, re.I):
        ys |= {int(y) for y in re.findall(r"20\d\d", m.group(1))}
    return sorted(ys)


def requirements(job):
    """Structured requirements of a posting (deterministic)."""
    text = job["desc"] or ""
    req_text, _ = _sections(text)
    title = job["title"] or ""
    yoe = scoring.min_yoe(req_text) if req_text else scoring.min_yoe(text)
    m = _YOE_BY_DEGREE.search(req_text)  # "5+ yrs (Bachelor's), or 3+ (Master's), or 1+ (PhD)": the bachelor's figure is what applies to the candidate
    if m:
        yoe = max(yoe or 0, int(m.group(1)))
    return {"min_yoe": yoe, "degree": _degree_need(req_text),
            "senior_title": bool(_SENIOR.search(title)) and not _EARLY_OK.search(title), "citizenship": bool(_CITIZEN.search(text)),
            "grad_years": _grad_years(text), "chars": len(text)}


def _cand_grad_year():
    g = str(PROFILE.get("graduation") or "")
    m = re.search(r"20\d\d", g)
    return int(m.group(0)) if m else None


def problems(req):
    """Reasons the candidate does not meet the posting (empty list = eligible)."""
    out = []
    gate_yoe = common.SETTINGS.get("jd_gate", {}).get("max_yoe", 1)  # 2+ required years = not for a final-year student
    if req.get("min_yoe") and req["min_yoe"] > gate_yoe:
        out.append(f"needs {req['min_yoe']}+ years of experience")
    if req.get("degree") == "phd":
        out.append("requires a PhD (or being enrolled in one)")
    elif req.get("degree") == "ms":
        out.append("requires a Master's degree (or being enrolled in one)")
    if req.get("senior_title"):
        out.append("senior-level title")
    if req.get("citizenship"):
        out.append("requires citizenship or a security clearance")
    gy, cy = req.get("grad_years") or [], _cand_grad_year()
    if gy and cy and cy not in gy and max(gy) < cy:
        out.append(f"for {'/'.join(map(str, gy))} graduates (you graduate {cy})")
    return out


def check(job_id, use_browser=True, refresh=False):
    """Read the full posting and evaluate it. Returns dict(ok, jd_read, problems, req). Stores the result on the job."""
    try:
        job, read = ensure_full_jd(job_id, use_browser=use_browser, refresh=refresh)
    except GateError as e:
        return {"ok": False, "jd_read": False, "problems": [str(e)], "req": {}}
    req = requirements(job)
    probs = problems(req) if read else []
    if not read:
        probs = ["could not read the full job description (the page needs a login, or blocks automated reading)"]
    res = {"ok": not probs, "jd_read": read, "problems": probs, "req": req, "checked": now()}
    c = db()
    c.execute("UPDATE jobs SET req=?, checked=? WHERE id=?", (json.dumps(res), now(), job_id))
    c.commit()
    return res


def gate(job_id, force=False, what="prepare"):
    """Raise GateError unless the job is eligible. force=True (the user insisted) only logs the problems."""
    res = check(job_id)
    if res["ok"]:
        return res
    msg = "; ".join(res["problems"])
    if force:
        log(f"jd gate overridden by you for {job_id}: {msg}")
        return res
    raise GateError(f"Not preparing this one: {msg}. If you still want it, run it again with --force (CLI) or add 'force' (chat).")


def prefetch(limit=30, use_browser=False):
    """Read full postings for jobs about to be triaged, best first (cheap HTTP; browser only when asked). Dismisses ineligible ones before any model call."""
    c = db()
    rows = c.execute("""SELECT id FROM jobs j WHERE active=1 AND COALESCE(jd_full,0)=0 AND score>=? AND status IN ('new','triaged','watch')
                        AND NOT EXISTS (SELECT 1 FROM triage t WHERE t.job_id=j.id) ORDER BY score DESC LIMIT ?""",
                     (common.SETTINGS["min_heuristic_score_for_llm"], limit)).fetchall()
    read = dropped = 0
    t0 = time.time()
    for r in rows:
        res = check(r["id"], use_browser=use_browser)  # writes through its own connection
        read += res["jd_read"]
        if res["jd_read"] and not res["ok"]:
            c.execute("UPDATE jobs SET status='dismissed' WHERE id=?", (r["id"],))
            c.execute("DELETE FROM watch WHERE job_id=?", (r["id"],))
            c.commit()  # never hold a write transaction open while check() writes on another connection (that deadlocks SQLite)
            dropped += 1
        if time.time() - t0 > 600:
            break
    if rows:
        log(f"jd prefetch: read {read}/{len(rows)} full postings, dismissed {dropped} you do not qualify for")
    return read, dropped


def audit():
    """Re-check every pick, packet, referral and watchlist job. Marks problems on the job (shown as a badge); deletes nothing. Returns rows."""
    c = db()
    ids = [r[0] for r in c.execute("""SELECT job_id FROM picks UNION SELECT job_id FROM packets UNION SELECT job_id FROM referrals UNION SELECT job_id FROM watch""")]
    out = []
    for jid in ids:
        res = check(jid)
        if not res["ok"]:
            j = c.execute("SELECT company,title FROM jobs WHERE id=?", (jid,)).fetchone()
            out.append((jid, j["company"] if j else "?", j["title"] if j else "?", res["problems"]))
    log(f"jd audit: {len(ids)} jobs checked, {len(out)} you may not qualify for")
    return out
