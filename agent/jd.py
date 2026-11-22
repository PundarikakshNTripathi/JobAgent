"""Full job descriptions and the eligibility gate.

Nothing gets a packet, a referral plan or a form fill until the FULL posting has been read and its requirements checked against the candidate:
years of experience, required degree (MS/PhD), seniority in the title, citizenship/clearance, graduation window. Everything here is deterministic
(no model tokens): ATS APIs first (Greenhouse, Lever, Ashby), then the page itself (JSON-LD JobPosting or HTML), then the logged-in agent browser for
JavaScript-only pages. Results are stored on the job row (`jd_full`, `req`) so each posting is read once."""
import urllib.error, json, re, time
import common
from common import PROFILE
from common import db, log, now, PROFILE, http_json
import scoring, web

MIN_JD = 700
MIN_NONSTD = 300  # a program page, blog post or your own notes: no ATS posting exists  # characters of real posting text before we trust it


class GateError(Exception):
    """The candidate does not meet a hard requirement (or the posting could not be read). str() is the user-facing reason."""


class PostingGone(Exception):
    """The board's own API says the posting no longer exists (Greenhouse 404, missing from the Ashby/Lever board)."""


# ------------------------------------------------------------------ fetching
def _html_to_text(h):
    return web.to_text(h or "") if hasattr(web, "to_text") else re.sub(r"<[^>]+>", " ", h or "")


def _from_ats(job):
    """Full text from the ATS's public API when the job id tells us which board it is. Returns '' when not applicable."""
    jid, url = job["id"] or "", job["url"] or ""
    parts = jid.split(":")
    try:
        if parts[0] == "greenhouse" and len(parts) >= 3:
            try:
                j = http_json(f"https://boards-api.greenhouse.io/v1/boards/{parts[1]}/jobs/{parts[-1]}?content=true", timeout=20)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise PostingGone("the posting was removed from the company's Greenhouse board")
                raise
            j = j or http_json(f"https://boards-api.greenhouse.io/v1/boards/{parts[1]}/jobs/{parts[-1]}?content=true", timeout=20)
            import html as _h
            return _html_to_text(_h.unescape(j.get("content", "")))
        if parts[0] == "lever" and len(parts) >= 3:
            try:
                j = http_json(f"https://api.lever.co/v0/postings/{parts[1]}/{parts[-1]}", timeout=20)
            except urllib.error.HTTPError as e:
                if e.code == 404:
                    raise PostingGone("the posting was removed from the company's Lever board")
                raise
            lists = "\n".join(f"{l.get('text','')}:\n{_html_to_text(l.get('content',''))}" for l in j.get("lists", []))
            return "\n".join([j.get("descriptionPlain", ""), lists, j.get("additionalPlain", "")])
        if parts[0] == "ashby" and len(parts) >= 3:
            j = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{parts[1]}", timeout=25)
            for p in j.get("jobs", []):
                if p.get("id") == parts[-1] or (p.get("jobUrl") or "") == url:
                    return p.get("descriptionPlain") or _html_to_text(p.get("descriptionHtml", ""))
            if j.get("jobs"):
                raise PostingGone("the posting is no longer on the company's Ashby board")
    except PostingGone:
        raise
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
    try:
        text = _from_ats(job)
    except PostingGone as e:
        common.set_status(c, job_id, "closed")
        raise GateError(f"this posting is closed: {e}")
    if len(text) < MIN_JD and job["url"]:
        _, body = web.fetch_text(job["url"], 16000)
        if body and not body.startswith("(fetch failed"):
            if re.search(r"no longer (accepting|available|open)|position (has been )?filled|job (is )?closed|page (was )?not found", body[:3000], re.I):
                common.set_status(c, job_id, "closed")
                raise GateError("this posting is closed or no longer accepting applications")
            text = body if len(body) > len(text) else text
    if len(text) < MIN_JD and use_browser and job["url"]:
        text = _from_browser(job["url"]) or text
    info = (job["info_url"] if "info_url" in job.keys() else "") or ""
    nonstd = False
    if len(text) < MIN_JD and info and info != job["url"]:
        _, body = web.fetch_text(info, 16000)
        body = body if body and not body.startswith("(fetch failed") else ""
        if len(body) < MIN_JD and use_browser:
            body = _from_browser(info) or body
        if len(body) >= MIN_NONSTD:
            text, nonstd = body, True
    stored = job["desc"] or ""
    if len(text) < MIN_JD and job["id"].startswith(("manual:", "prog:", "scout:")) and len(stored) >= MIN_NONSTD:
        text, nonstd = stored, True  # no standard posting: what you (or the inbox) supplied about the opportunity is the description
    ok = len(text) >= MIN_JD or (nonstd and len(text) >= MIN_NONSTD)
    if ok:
        desc = text[:20000]
        s = scoring.score({"title": job["title"], "location": job["location"], "desc": desc, "ats": job["ats"], "_tier": job["tier"]})
        c.execute("UPDATE jobs SET desc=?, jd_full=?, yoe=?, visa=? WHERE id=?", (desc, 2 if nonstd else 1, s["yoe"], s["visa"] if s["visa"] != "unknown" else job["visa"], job_id))
        c.commit()
        job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    return job, ok


# ------------------------------------------------------------------ requirements
_REQ_HDR = re.compile(r"(basic|minimum|required|must[- ]have)\s+qualifications?|requirements|what you.ll need|what we.re looking for|you (have|bring|are)|who you are|qualifications", re.I)
_PREF_HDR = re.compile(r"preferred\s+qualifications?|nice[- ]to[- ]have|bonus( points)?|pluses|preferred|great if you", re.I)
_MTS = re.compile(r"member of (the )?technical staff|\bmts\b|technical staff", re.I)  # a flat title at labs (OpenAI, Anthropic, CynLr...), not seniority
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
        if re.search(r"\bif you are\b|looking to (further|continue|deepen)|all degree levels|including|welcome|also |encourage|not eligible|became|joined|went on|after (completing|earning)|completed our|alumni|graduate of|"
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


_UG_OK = re.compile(r"\bug\s*/\s*pg\b|you are in your (ug|undergrad)|\b(undergrad(uate)?s?|bachelor'?s?( students?)?|b\.?tech|students) (are )?(welcome|eligible|can apply|encouraged)|"
                    r"(bs|b\.s\.|bachelor'?s?)\s*/\s*(ms|m\.s\.|master'?s?)\s*/\s*(phd|ph\.d)|(pursuing|enrolled in) (a |an )?(bachelor|undergrad|b\.?tech)", re.I)


def requirements(job):
    """Structured requirements of a posting (deterministic)."""
    text = job["desc"] or ""
    req_text, _ = _sections(text)
    title = job["title"] or ""
    has_hdr = bool(_REQ_HDR.search(text))
    yoe = scoring.min_yoe(req_text if req_text else text, require_context=not has_hdr)
    m = _YOE_BY_DEGREE.search(req_text)  # "5+ yrs (Bachelor's), or 3+ (Master's), or 1+ (PhD)": the bachelor's figure is what applies to the candidate
    if m:
        yoe = max(yoe or 0, int(m.group(1)))
    degree = "" if _UG_OK.search(text) else _degree_need(req_text)
    return {"min_yoe": yoe, "degree": degree,
            "senior_title": bool(_SENIOR.search(_MTS.sub(" ", title))) and not _EARLY_OK.search(title), "citizenship": bool(_CITIZEN.search(text)),
            "grad_years": _grad_years(text), "chars": len(text)}


def _cand_grad_year():
    g = str(PROFILE.get("graduation") or "")
    m = re.search(r"20\d\d", g)
    return int(m.group(0)) if m else None


def problems(req):
    """Reasons the candidate does not meet the posting (empty list = eligible)."""
    out = []
    if req.get("min_yoe") and req["min_yoe"] >= yoe_block_at():
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


def yoe_block_at():
    """Required years at which a role is out of reach. Default 1 (you have one internship and no full-time experience: '1+', '1-3 years' are out,
    '0-2 years' and 'new grad' are in). Override with settings jd_gate.yoe_block_at."""
    g = common.SETTINGS.get("jd_gate", {})
    return int(g.get("yoe_block_at", 1))


_NEEDS_AUTH = re.compile(
    r"must (already )?(be|have)[^.\n]{0,40}(legally )?(authori[sz]ed|eligible|permitted|the right) to work|"
    r"(work|employment) authori[sz]ation (in|for) (the )?(us|u\.s\.?|united states|uk|united kingdom|canada|eu|european union|australia|germany|singapore)\b|"
    r"(right|permit|authori[sz]ation) to work in (the )?(us|u\.s\.?|united states|uk|united kingdom|canada|eu|europe|australia|germany|netherlands|singapore|switzerland)[^.\n]{0,40}(required|is a must|needed|mandatory)|"
    r"\b(us|u\.s\.|usa|uk|canada|eu)[- ](based )?(residents|citizens|candidates|persons?) only|only (open|available) to (candidates|applicants|residents) (located |based |residing )?(in|of)\b|"
    r"must (reside|live|be (based|located)) in (the )?(us|u\.s\.?|united states|uk|united kingdom|canada|eu|europe)|"
    r"(not|unable to|cannot|can.t|won.t|will not|do not|does not) (be able to )?(offer |provide |support )?(visa |immigration |work permit )?sponsor", re.I)
_REMOTE_ELSEWHERE = re.compile(r"remote\W{0,3}(\(|-|–|,|in)?\s*(the\s+)?(us|usa|u\.s\.|united states|canada|uk|united kingdom|europe|eu|emea|latam|americas|north america|germany|france|brazil|mexico)\b|"
                               r"\b(us|usa|united states|canada|uk|europe|eu|emea|latam)\W{0,3}remote\b", re.I)
_GLOBAL_OK = re.compile(r"anywhere|worldwide|global|international|apac|asia|india|any (country|location)|all countries|timezone", re.I)


def needs_sponsorship():
    fd = PROFILE.get("form_defaults", {}) or {}
    v = fd.get("requires_visa_sponsorship_outside_home_country") or fd.get("requires_visa_sponsorship_outside_india") or ""
    return str(v).strip().lower().startswith("y")


def visa_gate(job, text):
    """('ok'|'problem'|'unclear', reason). Abroad roles need sponsorship (from your profile); roles that need an existing work permit, or remote
    roles limited to another country, are out; abroad roles that say nothing about sponsorship go to 'Visa unclear', never to APPLY."""
    if not needs_sponsorship():
        return "ok", ""
    loc_txt = job["location"] or ""
    where = scoring.classify_location(loc_txt)
    if where == "india":
        return "ok", ""
    t = (text or "")[:30000]
    if where == "remote":
        if (_REMOTE_ELSEWHERE.search(loc_txt) or _REMOTE_ELSEWHERE.search(t[:4000]) or _NEEDS_AUTH.search(t)) and not _GLOBAL_OK.search(loc_txt):
            return "problem", "remote only for people already allowed to work in that country"
        return "ok", ""
    v = scoring.visa_status(t + " " + loc_txt)
    if v == "no" or _NEEDS_AUTH.search(t):
        return "problem", "abroad and needs an existing work permit (no visa sponsorship)"
    strict = [rx for rx in scoring.VISA_YES if "relocation" not in rx]  # relocation help is not a visa
    if re.search(r"visa: will sponsor", t, re.I) or any(re.search(rx, t.lower().replace("’", "'")) for rx in strict):
        return "ok", ""
    return "unclear", "abroad, and the posting does not say it sponsors visas"


def quick_gate(job_id):
    """Cheap re-evaluation from what is stored (no fetching): sets jobs.gate to 'visa-unclear' / 'visa-blocked' / 'jd-unread' / NULL. Returns it."""
    c = db()
    job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not job:
        return None
    vg, _ = visa_gate(job, job["desc"] or "")
    g = {"problem": "visa-blocked", "unclear": "visa-unclear"}.get(vg) or (None if job["jd_full"] else "jd-unread")
    c.execute("UPDATE jobs SET gate=? WHERE id=?", (g, job_id))
    c.commit()
    return g


def check(job_id, use_browser=True, refresh=False):
    """Read the full posting and evaluate it. Returns dict(ok, jd_read, problems, req). Stores the result on the job."""
    try:
        job, read = ensure_full_jd(job_id, use_browser=use_browser, refresh=refresh)
    except GateError as e:
        return {"ok": False, "jd_read": False, "problems": [str(e)], "req": {}}
    req = requirements(job)
    probs = problems(req) if read else []
    vg, vwhy = visa_gate(job, job["desc"] or "") if read else ("ok", "")
    if vg == "problem":
        probs.append(vwhy)
    if not read:
        probs = ["could not read the full job description (the page needs a login, or blocks automated reading)"]
    req["non_standard"] = bool(read and job["jd_full"] == 2)
    res = {"ok": not probs, "jd_read": read, "problems": probs, "req": req, "checked": now(), "visa": vg}
    gate_col = "jd-unread" if not read else ("visa-unclear" if vg == "unclear" else ("visa-blocked" if vg == "problem" else None))
    c = db()
    c.execute("UPDATE jobs SET req=?, checked=?, gate=? WHERE id=?", (json.dumps(res), now(), gate_col, job_id))
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
            common.set_status(c, r["id"], "dismissed", commit=False)
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
