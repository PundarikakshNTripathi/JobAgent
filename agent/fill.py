"""Browser form filling. Claude (your subscription) drives a dedicated Chrome window through Playwright MCP
over CDP. The agent can fill everything but is blocked from submitting:
  1. prompt rule: never press the final submit button;
  2. page-level guard script injected into every page: swallows submit events / submit-button clicks / application POSTs
     for `guard_minutes` after the run starts (longer than the agent's hard timeout), with a visible banner;
  3. hard subprocess timeout shorter than the guard window.
You review the filled tab and press Submit yourself."""
import json, os, re, shutil, socket, subprocess, time, urllib.request
from common import db, log, now, run_claude, extract_json, DATA, SETTINGS, PROFILE, budget_add, notify

PORT = SETTINGS["chrome_debug_port"]
ACCOUNT_WALLS = ("myworkdayjobs.com", "icims.com", "taleo.net", "successfactors", "oraclecloud.com/hcmui", "smartrecruiters.com/oneclick")
CHROME_CANDIDATES = [r"C:\Program Files\Google\Chrome\Application\chrome.exe", r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
                     os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe"), r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
                     "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "/Applications/Chromium.app/Contents/MacOS/Chromium",
                     "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"]
for _n in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "microsoft-edge"):
    _w = shutil.which(_n)
    if _w:
        CHROME_CANDIDATES.append(_w)
try:  # Playwright's own Chromium (installed by the installer when no browser is present)
    import glob as _g
    CHROME_CANDIDATES += sorted(_g.glob(os.path.expanduser("~/.cache/ms-playwright/chromium-*/chrome-linux*/chrome")) + _g.glob(os.path.expanduser("~/Library/Caches/ms-playwright/chromium-*/chrome-mac*/Chromium.app/Contents/MacOS/Chromium")) +
                                _g.glob(os.path.expandvars(r"%LOCALAPPDATA%\ms-playwright\chromium-*\chrome-win*\chrome.exe")))
except Exception:
    pass

GUARD_JS = r"""
(() => {
  const UNTIL = %d;
  const locked = () => Date.now() < UNTIL;
  const stamp = new Date(UNTIL).toLocaleTimeString();
  const banner = () => {
    if (!document.documentElement) return;
    let b = document.getElementById('__jobagent_banner');
    if (!locked()) { if (b) b.remove(); return; }
    if (!b) { b = document.createElement('div'); b.id = '__jobagent_banner';
      b.style.cssText = 'position:fixed;z-index:2147483647;left:0;right:0;top:0;background:#7c2d12;color:#fff;font:600 13px system-ui;padding:6px 12px;text-align:center;pointer-events:none';
      document.documentElement.appendChild(b); }
    b.textContent = 'JobAgent is filling this form - Submit is locked until ' + stamp + '. After that, review and submit yourself.';
  };
  setInterval(banner, 1000); document.addEventListener('DOMContentLoaded', banner);
  const FINAL = /^(submit( application)?|send( application)?|apply now|complete( application)?|finish|confirm( and submit)?)$/i;
  const SAFE = /^(next|continue|save|review|upload|add|back|attach|save and continue|save & continue)/i;
  const txtOf = el => (el && (el.innerText || el.value || el.getAttribute('aria-label') || '')).trim();
  addEventListener('submit', e => {
    if (!locked()) return;
    const t = txtOf(e.submitter);
    if (t && SAFE.test(t) && !FINAL.test(t)) return;
    e.preventDefault(); e.stopImmediatePropagation(); console.warn('[jobagent] submit blocked');
  }, true);
  addEventListener('click', e => {
    if (!locked()) return;
    const el = e.target && e.target.closest ? e.target.closest('button, input[type=submit], [role=button], a') : null;
    if (!el) return;
    const t = txtOf(el);
    const isApplyOpen = /^apply( for this job| to this job)?$/i.test(t);
    const submitType = (el.type === 'submit') && !!el.closest('form') && !SAFE.test(t);
    if (!isApplyOpen && (FINAL.test(t) || submitType || /submit/i.test(t))) {
      e.preventDefault(); e.stopImmediatePropagation(); console.warn('[jobagent] submit click blocked');
    }
  }, true);
  const BAD = /(submit[-_ ]?application|SubmitSingleApplicationForm|applicationForm\.submit|\/applications?\/?$|\/apply\/?$|candidate\/apply|\/submit\/?$|postApplication)/i;
  const of = window.fetch;
  window.fetch = function (input, init) {
    try { const u = typeof input === 'string' ? input : (input && input.url) || ''; const m = ((init && init.method) || (input && input.method) || 'GET').toUpperCase();
      if (locked() && m === 'POST' && BAD.test(u)) { console.warn('[jobagent] POST blocked', u); return Promise.reject(new Error('blocked by JobAgent guard')); } } catch (e) {}
    return of.apply(this, arguments); };
  const oo = XMLHttpRequest.prototype.open;
  XMLHttpRequest.prototype.open = function (m, u) { if (locked() && String(m).toUpperCase() === 'POST' && BAD.test(String(u))) { this.send = () => { throw new Error('blocked by JobAgent guard'); }; } return oo.apply(this, arguments); };
})();
"""

FILL_PROMPT = """You are filling out ONE job application form in a browser using the Playwright MCP tools, on behalf of the candidate. You are NOT allowed to submit it.

ABSOLUTE RULES
- NEVER click the final "Submit", "Submit application", "Send", "Finish" or equivalent button. Stop with the form fully filled and the final button untouched. A page guard also blocks submission; do not try to bypass or probe it.
- Do not create accounts, set passwords, solve CAPTCHAs, or complete email/phone verification. If the form requires any of that before it can be filled, stop and report BLOCKED with the URL and exact reason.
- Only enter facts that appear below. Never invent. Legal attestations and eligibility declarations (export-control/citizenship statements, "I certify", background-check consent, prior-employer or relative disclosures, AI-policy acknowledgements, "do you meet the requirements") must NOT be inferred or ticked by you: leave them unanswered and list them in needs_user, unless the exact answer is literally stated in the candidate data (work authorization in India = yes; needs sponsorship outside India = yes). If a required field needs information not given (salary, exact dates, EEO/demographics other than 'decline', AI-tool-use disclosure, references, etc.), leave it blank and list it in needs_user. For EEO/demographic/veteran/disability questions choose the decline-to-answer option.
- Open the application in a NEW tab (browser_tabs action=new) so earlier tabs stay open. Leave this tab open at the end.
- Dropdown/autocomplete tips: for a School/University list that lacks the candidate's school, choose "Other"/"Not listed" if offered, else leave blank and report. For location autocompletes, type only the city name from the candidate data, wait for suggestions, then CLICK the matching suggestion (do not just press Enter); re-snapshot to confirm the field committed. Education start year is in the candidate data; if a month is required and unknown, leave it and report.
- Stay efficient: use browser_snapshot, browser_fill_form, browser_type, browser_select_option, browser_click, browser_file_upload. Budget about 40 tool calls. Re-snapshot at the end and verify required fields are filled and nothing shows a validation error.

JOB: {company} - {title}
APPLICATION URL: {url}
(If the page is a posting with an "Apply" button, click Apply to reach the form. If it redirects to a third-party ATS, continue there.)

CANDIDATE DATA
{profile}

FILES TO UPLOAD (use browser_file_upload with these exact absolute paths)
- Resume (PDF): {resume}
- Transcript (PDF, upload ONLY if a transcript/academic-record upload is requested): {transcript}
- Cover letter (PDF, upload only if there is a cover letter upload field; otherwise paste the text): {cover_pdf}

COVER LETTER TEXT
{cover}

PREPARED ANSWERS (adapt lightly to the exact question wording and character limits)
{answers}

PREPARED FORM-QUESTION ANSWERS
{formq}

When done return ONLY this JSON:
{{"status":"READY_TO_SUBMIT|PARTIAL|BLOCKED","filled":["short list of sections filled"],"needs_user":["every field you left blank or unsure about, with the exact question text"],
"blocker":"reason if BLOCKED/PARTIAL, else ''","submit_button_label":"label of the final button you did NOT press","page_url":"current URL"}}
"""


def _port_open():
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex(("127.0.0.1", PORT)) == 0


def ensure_chrome():
    if _port_open():
        return True
    exe = next((p for p in CHROME_CANDIDATES if os.path.exists(p)), None)
    if not exe:
        raise RuntimeError("Chrome/Edge not found")
    prof = SETTINGS.get("chrome_profile_dir") or os.path.join(DATA, "chrome-profile")
    os.makedirs(prof, exist_ok=True)
    kw = {}
    if os.name == "nt":  # start minimized and without activating, so a background fill never steals focus
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 7  # SW_SHOWMINNOACTIVE
        kw["startupinfo"] = si
    subprocess.Popen([exe, f"--remote-debugging-port={PORT}", f"--user-data-dir={prof}", "--no-first-run", "--no-default-browser-check", "--start-minimized", "about:blank"],
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, **kw)
    for _ in range(40):
        if _port_open():
            return True
        time.sleep(0.5)
    raise RuntimeError("Chrome debug port did not open")


SUBMITTED_HINT = ("confirmation", "thank-you", "thankyou", "submitted", "application-complete", "/success")


def submission_watchdog():
    """Return (title,url) of any agent-browser tab that looks like a post-submission page. Run after every fill."""
    try:
        tabs = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=10).read())
    except Exception:
        return []
    return [(t.get("title", ""), t.get("url", "")) for t in tabs if t.get("type") == "page" and any(h in t.get("url", "").lower() for h in SUBMITTED_HINT)]


def fill_job(job_id, force=False):
    import jd
    jd.gate(job_id, force=force)
    c = db()
    job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    pk = c.execute("SELECT * FROM packets WHERE job_id=?", (job_id,)).fetchone()
    if not (job and pk):
        raise ValueError("no packet for this job; run `packet` first")
    data = json.load(open(os.path.join(pk["dir"], "packet.json"), encoding="utf-8"))["packet"]
    if any(h in (job["url"] or "").lower() for h in ACCOUNT_WALLS):
        rep = {"status": "BLOCKED", "filled": [], "needs_user": ["This employer's application system requires you to create an account or sign in first, which the agent never does. Apply manually: the packet has your cover letter, answers and resume."],
               "blocker": "account required (Workday/iCIMS/Taleo-style system)", "submit_button_label": "", "page_url": job["url"]}
        c.execute("UPDATE packets SET filled_at=?, fill_report=? WHERE job_id=?", (now(), json.dumps(rep), job_id))
        c.execute("UPDATE jobs SET status='fill-blocked' WHERE id=?", (job_id,))
        c.commit()
        with open(os.path.join(pk["dir"], "FILL_REPORT.md"), "w", encoding="utf-8") as f:
            f.write(f"# Fill report ({now()})\nstatus: **BLOCKED**\n\n" + rep["needs_user"][0] + "\n")
        log(f"fill skipped (account wall): {job['company']} | {job['title']}")
        return rep
    ensure_chrome()
    until = int((time.time() + SETTINGS["guard_minutes"] * 60) * 1000)
    guard = os.path.join(DATA, "guard.js")
    open(guard, "w", encoding="utf-8").write(GUARD_JS % until)
    log(f"filling {job['company']} | {job['title']} ...")
    rep = None
    try:  # cheap path first: script reads the form, one model call maps values, Playwright types them
        import formfill
        rep = formfill.fill(job, data, GUARD_JS % until)
    except Exception as e:
        log(f"formfill failed ({str(e)[:200]}); using the model-driven fill")
    if rep:
        return _finish(c, job, pk, job_id, rep)
    prof = {k: PROFILE[k] for k in ("name", "first_name", "last_name", "email", "phone", "location", "linkedin", "github", "org_github", "website", "twitter", "education",
                                    "graduation", "education_start_year", "education_notes", "summary", "experience", "projects", "achievements", "skills_defensible", "form_defaults") if k in PROFILE}
    try:  # odd forms: JobAgent's own browser loop, driven by whichever provider is available (no provider-side browser needed)
        import webagent
        goal = FILL_PROMPT.split("CANDIDATE DATA")[0].replace("using the Playwright MCP tools", "").replace("- Open the application in a NEW tab (browser_tabs action=new) so earlier tabs stay open. Leave this tab open at the end.\n", "")
        goal = re.sub(r"- Stay efficient:.*\n", "", goal).format(company=job["company"], title=job["title"], url=job["url"])
        goal += ("CANDIDATE DATA\n" + json.dumps(prof, indent=1) + f"\n\nFILES: resume {data.get('resume_path', '')}; transcript {PROFILE.get('transcript_pdf', '(none)')}; "
                 f"cover letter PDF {data.get('cover_pdf') or '(none: paste the text)'}\n\nCOVER LETTER TEXT\n{data.get('cover_letter', '')}\n\nPREPARED ANSWERS\n"
                 + json.dumps(data.get("answers", {}), indent=1) + "\n\nFORM QUESTIONS\n" + json.dumps(data.get("form_questions", []), indent=1))
        w = webagent.run(goal, job["url"], mode="form", kind="fill", max_steps=60, init_script=GUARD_JS % until, max_text=2500,
                         result_hint='{"status":"READY_TO_SUBMIT|PARTIAL|BLOCKED","filled":[],"needs_user":[],"blocker":"","submit_button_label":"","page_url":""}')
        if isinstance(w["result"], dict) and w["result"].get("status"):
            rep = dict(w["result"], how="browser loop")
            return _finish(c, job, pk, job_id, rep)
        log(f"fill browser loop did not finish: {str(w['result'])[:200]}")
    except Exception as e:
        log(f"fill browser loop failed ({str(e)[:200]})")
    import browser
    mcp = {"mcpServers": {"fillbrowser": browser.npx_server(["@playwright/mcp@0.0.84", "--cdp-endpoint", f"http://127.0.0.1:{PORT}", "--init-script", guard, "--allow-unrestricted-file-access"])}}
    mcp_path = os.path.join(DATA, "mcp-fill.json")
    json.dump(mcp, open(mcp_path, "w"), indent=1)
    prof = {k: PROFILE[k] for k in ("name", "first_name", "last_name", "email", "phone", "location", "linkedin", "github", "org_github", "website", "twitter", "education",
                                    "graduation", "education_start_year", "education_notes", "summary", "experience", "projects", "achievements", "skills_defensible", "form_defaults")}
    prompt = FILL_PROMPT.format(company=job["company"], title=job["title"], url=job["url"], profile=json.dumps(prof, indent=1),
                                resume=data.get("resume_path", ""), transcript=PROFILE.get("transcript_pdf", "(none)"), cover_pdf=data.get("cover_pdf") or "(none generated; paste text)", cover=data.get("cover_letter", ""),
                                answers=json.dumps(data.get("answers", {}), indent=1), formq=json.dumps(data.get("form_questions", []), indent=1))
    timeout = min(SETTINGS["fill_timeout_s"], SETTINGS["guard_minutes"] * 60 - 60)
    text, _ = run_claude(prompt, kind="fill", allowed_tools="mcp__fillbrowser", timeout=timeout, max_turns=70,
                         mcp_config=mcp_path, persist=True)
    try:
        rep = extract_json(text)
    except Exception:
        rep = {"status": "PARTIAL", "filled": [], "needs_user": [], "blocker": "could not parse fill report: " + text[:300]}
    rep.setdefault("how", "model-driven")
    return _finish(c, job, pk, job_id, rep)


def _finish(c, job, pk, job_id, rep):
    c.execute("UPDATE packets SET filled_at=?, fill_report=? WHERE job_id=?", (now(), json.dumps(rep), job_id))
    status = rep.get("status", "PARTIAL")
    c.execute("UPDATE jobs SET status=? WHERE id=?", ("ready" if status == "READY_TO_SUBMIT" else "fill-" + status.lower(), job_id))
    c.commit()
    budget_add(c, "fills")
    with open(os.path.join(pk["dir"], "FILL_REPORT.md"), "w", encoding="utf-8") as f:
        f.write(f"# Fill report ({now()})\nstatus: **{status}**\n\nfilled: {rep.get('filled')}\n\nneeds you:\n" + "\n".join(f"- {x}" for x in rep.get("needs_user", [])) +
                f"\n\nblocker: {rep.get('blocker','')}\n\nfinal button NOT pressed: {rep.get('submit_button_label','')}\nlast page: {rep.get('page_url','')}\n")
    sub_tabs = submission_watchdog()
    if sub_tabs:
        notify("CHECK: a confirmation page is open", "; ".join(f"{t[:40]} {u[:60]}" for t, u in sub_tabs)[:300] + " (agent never submits; verify it was you)")
        rep.setdefault("needs_user", []).append(f"Browser has post-submission pages open: {sub_tabs}")
    if status == "READY_TO_SUBMIT":
        notify(f"READY - hit Submit: {job['company']}", f"{job['title']}. Form filled in the JobAgent Chrome window (tab left open). Review, then submit. Needs you: {len(rep.get('needs_user', []))} field(s).")
    else:
        notify(f"Needs your help: {job['company']}", f"{job['title']}: {status}. {rep.get('blocker','')[:160]}")
    return rep
