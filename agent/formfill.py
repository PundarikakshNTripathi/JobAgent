"""Deterministic application-form filling (cheap): read the form's fields with a small script, let ONE model call map the candidate's facts and prepared
answers onto those fields, then fill them with Playwright. The model never sees the page DOM and never drives the browser.

Safety is unchanged from fill.py: the submit guard (blocks final buttons, submit events and application POSTs for guard_minutes, with a banner) is
injected before the page loads, nothing here ever clicks a submit button, legal attestations are left to the user, and the tab stays open for review."""
import json, os, re, time, urllib.parse
from common import log, PROFILE, SETTINGS, extract_json, run_claude
import browse

FIELDS_JS = r"""() => {
  const vis = el => { const r = el.getBoundingClientRect(); const s = getComputedStyle(el); return (r.width > 0 || r.height > 0 || el.type === 'file') && s.visibility !== 'hidden' && s.display !== 'none'; };
  const clean = t => (t || '').replace(/\s+/g, ' ').trim().slice(0, 300);
  const labelOf = el => {
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l) return clean(l.innerText); }
    if (el.getAttribute('aria-label')) return clean(el.getAttribute('aria-label'));
    const lb = el.getAttribute('aria-labelledby'); if (lb) return clean(lb.split(' ').map(i => (document.getElementById(i) || {}).innerText || '').join(' '));
    const wrap = el.closest('label'); if (wrap) return clean(wrap.innerText);
    const fs = el.closest('fieldset'); if (fs && fs.querySelector('legend')) return clean(fs.querySelector('legend').innerText);
    let p = el.parentElement; for (let i = 0; i < 4 && p; i++, p = p.parentElement) { const t = clean(p.innerText); if (t && t.length < 300) return t; }
    return clean(el.name || el.placeholder || '');
  };
  const out = []; let n = 0; const seenRadio = new Set();
  for (const el of document.querySelectorAll('input, select, textarea')) {
    const type = (el.tagName === 'SELECT') ? 'select' : (el.tagName === 'TEXTAREA' ? 'textarea' : (el.type || 'text').toLowerCase());
    if (['hidden', 'submit', 'button', 'reset', 'image', 'search'].includes(type) || !vis(el) || el.disabled) continue;
    if (type !== 'file' && (el.tabIndex === -1 || el.getAttribute('aria-hidden') === 'true') && !['radio', 'checkbox'].includes(type)) continue;  // dummy inputs of custom selects
    if (type === 'radio' || type === 'checkbox') {
      const key = type + ':' + (el.name || labelOf(el));
      if (type === 'radio' && seenRadio.has(key)) continue;
      seenRadio.add(key);
      const group = type === 'radio' && el.name ? [...document.querySelectorAll(`input[type=radio][name="${CSS.escape(el.name)}"]`)] : [el];
      const id = 'f' + (n++); group.forEach((g, i) => g.setAttribute('data-ja', id + '_' + i));
      const fs = el.closest('fieldset'); const q = fs && fs.querySelector('legend') ? clean(fs.querySelector('legend').innerText) : labelOf(el.closest('div') || el);
      out.push({id, type, label: q, required: !!el.required || /\*/.test(q), options: group.map(g => labelOf(g)).slice(0, 30)});
      continue;
    }
    const id = 'f' + (n++); el.setAttribute('data-ja', id);
    const f = {id, type, label: labelOf(el), required: !!el.required || el.getAttribute('aria-required') === 'true' || /\*\s*$/.test(labelOf(el)), value: type === 'file' ? '' : (el.value || '').slice(0, 80)};
    if (type === 'select') f.options = [...el.options].map(o => clean(o.text)).filter(Boolean).slice(0, 80);
    if (el.getAttribute('role') === 'combobox' || el.getAttribute('aria-autocomplete')) f.type = 'combobox';
    if (el.maxLength > 0) f.maxlength = el.maxLength;
    out.push(f);
  }
  return out;
}"""

MAP_PROMPT = """Map the candidate's information onto this job application form. You only decide VALUES; a script types them in.

RULES
- Use only facts from CANDIDATE and PREPARED ANSWERS. Never invent. If a required field needs something not given (salary, exact dates, references, a question you
  cannot answer truthfully), leave it out and list it in needs_user with the field's label.
- Legal attestations and declarations (certify/consent/acknowledge, export control, citizenship statements, background checks, relatives, AI-policy) are NEVER filled
  by you unless the exact answer is literally in the candidate data: list them in needs_user. EEO / demographic / veteran / disability: choose the decline option if present.
- Work authorization: authorised to work in India = yes; needs visa sponsorship to work outside India = yes.
- For select/radio fields, the value MUST be one of the listed options, copied exactly. For checkbox use true/false. For file fields use "@resume", "@cover" or "@transcript".
- Respect maxlength. Use the prepared answers for open questions, adapted lightly to the exact wording.
- Phone: if the form has a separate country / dial-code field, give the national number only (no +91, no leading 0) and set the country field to the country.
- A dropdown whose options do not include the right answer (e.g. a school list without the candidate's university): leave it out, put the answer in any
  'not listed / other' text box the form offers, and list the dropdown in needs_user.

JOB: {company} - {title}
CANDIDATE: {profile}
PREPARED ANSWERS: {answers}
PREPARED FORM QUESTIONS: {formq}
COVER LETTER (for a cover-letter text box): {cover}

FORM FIELDS (id, type, label, required, options):
{fields}

Return ONLY JSON: {{"values": {{"<field id>": "<value>"}}, "needs_user": ["label: why"]}}"""

APPLY_RX = re.compile(r"^\s*(apply( now| for this job| to this job)?|i'?m interested)\s*$", re.I)


def _scan(page):
    """(frame, fields) for the frame holding the application form; embedded ATS forms (Greenhouse on company sites) live in iframes."""
    best = (page.main_frame, [])
    for fr in page.frames:
        try:
            f = fr.evaluate(FIELDS_JS)
        except Exception:
            continue
        if len(f) > len(best[1]):
            best = (fr, f)
    return best


def _open_form(page, url, job_id=""):
    """Get from a posting to its form: known ATS URL patterns first, then an 'Apply' link/button (never a submit)."""
    parts = (job_id or "").split(":")
    if parts[0] == "greenhouse" and len(parts) >= 3:  # Greenhouse's own embeddable form works for every company site that wraps it
        page.goto(f"https://job-boards.greenhouse.io/embed/job_app?for={parts[1]}&token={parts[-1]}", wait_until="domcontentloaded", timeout=45000)
        for _ in range(4):
            time.sleep(2)
            if len(_scan(page)[1]) >= 3:
                return
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    if "jobs.lever.co" in url and not url.rstrip("/").endswith("/apply"):
        page.goto(url.rstrip("/") + "/apply", wait_until="domcontentloaded", timeout=45000)
    elif "jobs.ashbyhq.com" in url and "/application" not in url:
        page.goto(url.rstrip("/") + "/application", wait_until="domcontentloaded", timeout=45000)
    for _ in range(6):  # embedded forms (iframes) can take several seconds to appear
        time.sleep(2.5)
        if len(_scan(page)[1]) >= 3:
            return
    # Follow the Apply LINK by navigating (the submit guard, rightly, blocks clicks on buttons labelled like 'Apply now'); click only real <button>s
    for el in page.query_selector_all("a, button"):
        try:
            if APPLY_RX.match(el.inner_text() or "") and el.is_visible():
                href = el.get_attribute("href")
                if href and not href.startswith("#") and not href.lower().startswith("javascript"):
                    page.goto(urllib.parse.urljoin(page.url, href), wait_until="domcontentloaded", timeout=45000)
                else:
                    el.click()
                break
        except Exception:
            continue
    for _ in range(8):  # the form (often an iframe) loads after the Apply click
        time.sleep(2.5)
        if len(_scan(page)[1]) >= 3:
            return


def _fill_one(page, fr, f, val, files):
    sel = f"[data-ja='{f['id']}']"
    t = f["type"]
    if t == "file":
        path = files.get(str(val).lstrip("@"))
        if path and os.path.exists(path):
            fr.set_input_files(sel, path)
            return True
        return False
    if t == "select":
        fr.select_option(sel, label=str(val))
        return True
    if t == "radio":
        for i, opt in enumerate(f.get("options", [])):
            if opt.strip().lower() == str(val).strip().lower():
                fr.check(f"[data-ja='{f['id']}_{i}']", force=True)
                return True
        return False
    if t == "checkbox":
        if str(val).lower() in ("true", "yes", "1"):
            fr.check(f"[data-ja='{f['id']}_0']", force=True)
        return True
    loc = fr.locator(sel)
    if t != "combobox":
        loc.fill(str(val)[: f.get("maxlength") or 20000], timeout=8000)
        return True
    # type-ahead lists (React-select etc.): type like a person, then pick the matching suggestion; keyboard if the option can't be clicked
    loc.click(timeout=5000)
    loc.fill("", timeout=5000)
    loc.press_sequentially(str(val)[:40], delay=40, timeout=8000)
    time.sleep(1.2)
    opts = fr.locator("[role=option]")
    exact = opts.filter(has_text=re.compile(r"^\s*" + re.escape(str(val)) + r"\s*$", re.I))
    cand = exact.first if exact.count() else opts.filter(has_text=re.compile(re.escape(str(val)[:20]), re.I)).first
    try:
        if cand.count():
            cand.click(timeout=3000)
            return True
    except Exception:
        pass
    loc.press("ArrowDown")
    loc.press("Enter")
    return "check"  # picked by keyboard: probably right, but the user should glance at it


def _fkey(f):
    """Stable key for a form field across page loads (the data-ja ids are assigned fresh on every scan)."""
    return f["type"] + "|" + re.sub(r"[^a-z0-9]+", " ", (f.get("label") or "").lower()).strip()[:120]


def fill(job, data, guard_js, saved=None):
    """Returns a fill report dict, or None if the form could not be read (caller falls back to the model-driven fill).
    saved = values from an earlier fill ({field key: value}): a refill replays them with no model call; only fields it cannot match are mapped again."""
    ctx = browse._context()
    token = (job["id"] or "").split(":")[-1]
    for old in list(ctx.pages):  # a re-fill replaces the earlier tab for the same job instead of piling up tabs
        try:
            if token and len(token) > 5 and token in old.url and ("apply" in old.url or "job_app" in old.url or "application" in old.url):
                old.close()
        except Exception:
            pass
    page = ctx.new_page()
    page.add_init_script(guard_js)  # before any page script runs
    page.goto(job["url"], wait_until="domcontentloaded", timeout=45000)
    _open_form(page, job["url"], job["id"])
    fr, fields = _scan(page)
    if len(fields) < 3:
        log(f"formfill: only {len(fields)} fields readable on {page.url}; handing over to the model-driven fill")
        page.close()
        return None
    prof = {k: PROFILE.get(k) for k in ("name", "first_name", "last_name", "email", "phone", "location", "linkedin", "github", "website", "twitter", "education", "graduation",
                                        "education_start_year", "education_notes", "experience", "skills_defensible", "form_defaults")}
    vals, needs = {}, []
    todo = fields
    if saved:
        for f in fields:
            if _fkey(f) in saved:
                vals[f["id"]] = saved[_fkey(f)]
        todo = [f for f in fields if f["id"] not in vals and f.get("required") and f["type"] != "file"]  # only what a refill cannot replay
        log(f"formfill: refill replays {len(vals)} saved values; {len(todo)} fields need mapping")
    if todo:
        text, _ = run_claude(MAP_PROMPT.format(company=job["company"], title=job["title"], profile=json.dumps(prof, ensure_ascii=False)[:6000],
                                               answers=json.dumps(data.get("answers", {}), ensure_ascii=False)[:5000], formq=json.dumps(data.get("form_questions", []), ensure_ascii=False)[:3000],
                                               cover=(data.get("cover_letter") or "")[:3000], fields=json.dumps(todo, ensure_ascii=False)[:14000]),
                             kind="fill", timeout=300, max_turns=1)
        m = extract_json(text)
        vals.update(m.get("values", {}) or {})
        needs += list(m.get("needs_user", []) or [])
    files = {"resume": data.get("resume_path", ""), "cover": data.get("cover_pdf", ""), "transcript": PROFILE.get("transcript_pdf", "")}
    by = {f["id"]: f for f in fields}
    filled = 0
    for fid, val in vals.items():
        f = by.get(fid)
        if not f or val in (None, ""):
            continue
        try:
            ok = _fill_one(page, fr, f, val, files)
            if ok == "check":
                needs.append(f"{f['label'][:120]}: chosen from the suggestion list, please check it says '{str(val)[:60]}'")
            if ok:
                filled += 1
            else:
                needs.append(f"{f['label'][:120]}: could not set '{str(val)[:60]}'")
        except Exception as e:
            needs.append(f"{f['label'][:120]}: {str(e).splitlines()[0][:100]}")
        time.sleep(0.15)
    time.sleep(1)
    after = {f["id"]: f for f in fr.evaluate(FIELDS_JS)}  # verify what actually stuck
    empty_req = [f["label"][:120] for f in after.values() if f.get("required") and f["type"] not in ("radio", "checkbox", "file", "combobox") and not f.get("value") and f["id"] not in vals]
    empty_req += [by[i]["label"][:120] for i in by if by[i]["type"] == "combobox" and by[i].get("required") and i not in vals]
    needs += [f"{l}: required, left for you" for l in empty_req if not any(l[:40] in n for n in needs)]
    status = "READY_TO_SUBMIT" if not needs else "PARTIAL"
    page.bring_to_front()
    keep = {_fkey(by[i]): v for i, v in vals.items() if i in by and v not in (None, "") and by[i]["type"] != "file"}
    return {"status": status, "filled": [f"{filled} of {len(fields)} fields"], "needs_user": needs, "blocker": "" if status == "READY_TO_SUBMIT" else "some fields need you",
            "submit_button_label": "(not pressed)", "page_url": page.url, "how": "refill" if saved else "deterministic", "values": keep}
