"""Referral planning. Uses YOUR LinkedIn connections export (data/Connections.csv) -- no logged-in scraping of LinkedIn.
Per target job: rank connections at that company, have Claude research the best ones on the public web, draft the outreach."""
import csv, json, os, re
import outreach, packets
from common import db, log, now, run_claude, extract_json, DATA, ROOT, SETTINGS, PROFILE, budget_add, budget_used

ALIASES = {"google deepmind": ["deepmind", "google deepmind", "google"], "meta": ["meta", "facebook", "meta ai"], "microsoft": ["microsoft", "microsoft research"],
           "nvidia": ["nvidia"], "amazon": ["amazon", "aws", "amazon web services"], "openai": ["openai"], "anthropic": ["anthropic"],
           "cursor (anysphere)": ["anysphere", "cursor"], "together ai": ["together ai", "together.ai", "togetherai"], "inferact (vllm)": ["inferact", "vllm"],
           "radixark (sglang)": ["radixark", "sglang"], "fireworks ai": ["fireworks ai", "fireworks"], "xai": ["xai", "x.ai"], "thinking machines lab": ["thinking machines"],
           "character.ai": ["character.ai", "character ai"], "scale ai": ["scale ai", "scale"], "sarvam ai": ["sarvam", "sarvam ai"]}
NOISE = re.compile(r"\b(inc|llc|ltd|labs?|corp|corporation|technologies|technology|ai|pvt|private|limited|the|\(yc [a-z]\d+\))\b|[^a-z0-9 ]", re.I)


def norm(s):
    return re.sub(r"\s+", " ", NOISE.sub(" ", (s or "").lower())).strip()


def _find_export():
    """Newest LinkedIn export in data/: Connections*.csv, any *.csv under data/linkedin/, or a LinkedIn archive .zip (any date-versioned name)."""
    import glob, zipfile, io
    cands = glob.glob(os.path.join(DATA, "Connections*.csv")) + glob.glob(os.path.join(DATA, "linkedin", "**", "*.csv"), recursive=True) +         glob.glob(os.path.join(DATA, "*.zip")) + glob.glob(os.path.join(DATA, "linkedin", "**", "*.zip"), recursive=True)
    cands = [c for c in cands if os.path.getsize(c) > 0]
    best = []
    for c in sorted(cands, key=os.path.getmtime, reverse=True):
        if c.lower().endswith(".zip"):
            try:
                z = zipfile.ZipFile(c)
                name = next((n for n in z.namelist() if n.lower().endswith("connections.csv")), None)
                if name:
                    return z.read(name).decode("utf-8-sig"), c
            except zipfile.BadZipFile:
                continue
        else:
            txt = open(c, encoding="utf-8-sig", newline="").read()
            if "first name" in txt.lower()[:2000]:
                return txt, c
    return None, None


def load_connections():
    txt, src = _find_export()
    if not txt:
        return []
    lines = txt.splitlines()
    start = next((i for i, l in enumerate(lines) if l.lower().startswith("first name")), 0)
    rows = list(csv.DictReader(lines[start:]))
    out = []
    for r in rows:
        out.append({"name": f"{r.get('First Name','')} {r.get('Last Name','')}".strip(), "url": r.get("URL", ""), "company": r.get("Company", ""),
                    "position": r.get("Position", ""), "connected_on": r.get("Connected On", ""), "email": r.get("Email Address", "")})
    return out


def match_connections(company, conns):
    keys = {norm(company)} | {norm(a) for a in ALIASES.get(company.lower(), [])}
    keys.discard("")
    hits = []
    for c in conns:
        n = norm(c["company"])
        if n and any(k == n or (len(k) > 3 and (k in n or n in k)) for k in keys):
            hits.append(c)
    return hits


def rank(hits, family):
    def s(c):
        p = c["position"].lower()
        v = 0
        if re.search(r"research|scientist|engineer|member of technical staff|mts|developer|fellow", p):
            v += 3
        if re.search(r"recruit|talent|sourcer|university|campus|early career", p):
            v += 2.5
        if re.search(r"manager|lead|head|director|principal|staff", p):
            v += 1.5
        if family in ("mlsys",) and re.search(r"kernel|inference|gpu|cuda|systems|compiler|infra", p):
            v += 3
        if family in ("safety", "ai_research") and re.search(r"safety|alignment|interpret|research", p):
            v += 3
        if re.search(r"intern", p):
            v -= 1
        if re.search(r"ceo|cto|founder|vp|president|chief", p):
            v -= 1
        return v
    return sorted(hits, key=s, reverse=True)


REF_PROMPT = """You are a networking coach helping the candidate below get a referral/intro for a specific job. Be concrete and honest. Use WebSearch/WebFetch (public web only: personal sites, GitHub, Google Scholar, arXiv, blogs, talks, company pages, X/Twitter public posts). Do NOT try to log into LinkedIn.

CANDIDATE
{summary}
Best proof points: {projects}
Site: {site}

TARGET JOB: {company} | {title} | {location} | {url}
Why it fits (triage): {reason}

{contacts_block}

TASKS
{tasks}

Do NOT write any messages: a later step writes them in the candidate's own voice. Fill the message fields with empty strings. Put the specific, checkable hook about each person (what they built/wrote/said, with URLs) in "evidence".

Return ONLY JSON:
{{"strategy":"2-4 sentences: who to contact first, in what order, and what to ask for (advice first vs referral)",
"targets":[{{"name":"","role":"","why_them":"","evidence":"what you found on the web about their work, with URLs","profile_url":"","how_to_reach":"LinkedIn connection note / email / X DM / GitHub issue etc.",
"connection_note":"<=280 chars","dm":"<=90 words","followup":"send after 4-5 days if no reply","blurb_for_referrer":"short paragraph"}}],
"before_you_message":["2-4 concrete prep actions, e.g. read paper X, star/contribute to repo Y, post Z"],
"timing":"when to ask relative to applying (before/after) per what you found about this company's referral process; mark UNVERIFIED if unsure"}}
"""


def plan_referral(job_id, manual=False, require_linkedin=False):
    """manual=True: the user asked for this one, so the daily LinkedIn cap (meant for background runs) does not apply.
    Whenever LinkedIn scouting does not run or fails, the plan says so on its first line instead of silently looking like a LinkedIn result."""
    c = db()
    li_note = ""
    job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    tri = c.execute("SELECT * FROM triage WHERE job_id=?", (job_id,)).fetchone()
    pk = c.execute("SELECT * FROM packets WHERE job_id=?", (job_id,)).fetchone()
    conns = load_connections()
    hits = rank(match_connections(job["company"], conns), job["family"])[:6]
    if hits:
        cb = "CONNECTIONS THE CANDIDATE ALREADY HAS AT THIS COMPANY (from their own LinkedIn export, ranked by my heuristic):\n" + \
             "\n".join(f"- {h['name']} | {h['position']} | connected {h['connected_on']} | {h['url']}" for h in hits)
        tasks = ("1. Research each listed connection on the public web (name + company + role) to learn their real work/team.\n"
                 "2. Pick the best 1-2 to approach (relevant team, approachable seniority) plus a recruiter/university-recruiting route if listed; explain why.\n"
                 "3. Draft the messages. These are existing connections, so a DM (not a connection request) fits; acknowledge the connection is dormant honestly and briefly.")
    else:
        cb = ("NO EXISTING CONNECTIONS FOUND" + (" (connections file is present but nothing matched this company)" if conns else " (no LinkedIn export found in data/ yet)") +
              ". Find warm paths on the public web instead: the hiring manager or team named in the posting, authors of the team's recent papers/blog posts, open-source maintainers of the relevant repo, "
              f"and alumni of the candidate's university ({PROFILE.get('education', '')}) at the company if you can find any publicly.")
        tasks = ("1. Identify 2-3 specific real people (cite where you found them) who work on the relevant team/problem.\n"
                 "2. Rank them by approachability and relevance.\n3. Draft cold-outreach messages (connection note, DM, email if a public email exists).")
    if not SETTINGS.get("linkedin_browse", True) and not require_linkedin:
        li_note = "LinkedIn browsing is switched off in settings (linkedin_browse), so this plan uses public-web research only."
    elif not (manual or require_linkedin) and budget_used(c, "linkedin") >= SETTINGS.get("max_linkedin_per_day", 1):
        li_note = f"Background LinkedIn scouting is capped at {SETTINGS.get('max_linkedin_per_day', 1)}/day and is used up, so this plan is public-web only. Run `linkedin {job_id}` (or /linkedin in the chat) to scout LinkedIn for this job now."
    else:
        try:
            import linkedin
            r = linkedin.scout_people(job, hits)
            if not (r.get("targets") or []):
                li_note = "LinkedIn was searched but no usable people were found (" + str(r.get("notes") or r.get("degree_searched") or "no detail") + "). Public-web research was added below."
            else:
                return _write(c, job, tri, pk, r, len(hits), "linkedin")
        except Exception as e:
            li_note = "LinkedIn scouting FAILED, so this is public-web research only, not a LinkedIn result: " + str(e)[:400]
            log(f"linkedin scout failed ({e}); falling back to public-web referral research")
    if li_note:
        log("referral: " + li_note)
        if require_linkedin:
            print("WARNING:", li_note)
    prompt = REF_PROMPT.format(summary=PROFILE["summary"], projects=" | ".join(PROFILE["projects"][:3]), site=PROFILE["website"], company=job["company"],
                               title=job["title"], location=job["location"], url=job["url"], reason=(tri["reason"] if tri else ""), contacts_block=cb, tasks=tasks)
    text, _ = run_claude(prompt, kind="referral", allowed_tools="WebSearch,WebFetch", timeout=SETTINGS["packet_timeout_s"], max_turns=16)
    r = extract_json(text)
    if li_note:
        r["_note"] = li_note
    return _write(c, job, tri, pk, r, len(hits), "web")


def _write(c, job, tri, pk, r, nhits, how):
    out = [f"# Referral plan: {job['company']} - {job['title']}", job["url"]]
    if r.get("_note"):
        out += ["", "> NOTE: " + r["_note"]]
    out += ["", "## Strategy", r.get("strategy", ""), "", "## Timing", r.get("timing", ""), "", "## Do first"]
    out += [f"- {x}" for x in r.get("before_you_message", [])]
    for i, t in enumerate(r.get("targets", []), 1):
        out += ["", f"## Target {i}: {t.get('name')} ({t.get('role') or t.get('headline')})", f"Why: {t.get('why_them') or t.get('why_likely_to_respond')}", f"Evidence: {t.get('evidence')}", f"Profile: {t.get('profile_url')}",
                f"Reach via: {t.get('how_to_reach') or ('LinkedIn (' + str(t.get('degree','')) + ' degree)')}", f"Likely to respond because: {t.get('why_likely_to_respond','')}", f"Mutual: {t.get('mutual','')}", "", "**Connection note**", t.get("connection_note", ""), "", "**DM (short)**", t.get("dm", ""), "", "**DM (longer option)**", t.get("dm_long", ""), "", "**Follow-up (+5-7 days, only once)**",
                t.get("followup", ""), "", "**If 2nd degree: ask a mutual connection to introduce you**", t.get("intro_ask_for_mutual", ""), "", "**Blurb they can forward**", t.get("blurb_for_referrer", "")]
    try:
        msgs = outreach.draft(job, r.get("targets", []), fit=(tri["reason"] if tri else ""))
        by = {(m.get("name") or "").strip().lower(): m for m in msgs}
        for t in r.get("targets", []):
            m = by.get((t.get("name") or "").strip().lower())
            if m:
                t.update(connection_note=m.get("connect_note", ""), dm=m.get("dm_short", ""), dm_long=m.get("dm_long", ""), followup=m.get("followup", ""),
                         intro_ask_for_mutual=m.get("ask_mutual", ""), blurb_for_referrer=m.get("blurb", ""), style_warnings=m.get("_style_warnings", []))
    except Exception as e:
        log(f"outreach drafting failed: {e}")
    d = pk["dir"] if pk else packets.packet_dir(job)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, "REFERRAL.md")
    open(path, "w", encoding="utf-8").write("\n".join(out))
    c.execute("UPDATE packets SET referral_at=? WHERE job_id=?", (now(), job["id"]))
    c.execute("UPDATE watch SET referral_state='plan ready' WHERE job_id=?", (job["id"],))
    c.execute("INSERT OR REPLACE INTO referrals(job_id,dir,at,how) VALUES(?,?,?,?)", (job["id"], d, now(), how))
    c.commit()
    budget_add(c, "referrals")
    log(f"referral plan written via {how}: {path} ({nhits} connections from export)")
    return path, nhits
