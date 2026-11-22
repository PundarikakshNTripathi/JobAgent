"""Referral planning. Uses YOUR LinkedIn connections export (data/Connections.csv) -- no logged-in scraping of LinkedIn.
Per target job: rank connections at that company, have Claude research the best ones on the public web, draft the outreach."""
import csv, json, os, re
import common, outreach, packets
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
"timing":"apply and ask in parallel; one follow-up after 5-7 days (add a company-specific note only if a source you read states it)"}}
Only include people you found on a page you actually read, with that page's URL as profile_url; leave anything you could not confirm out entirely (no guesses, no 'unverified' entries).
"""


def plan_referral(job_id, manual=False, require_linkedin=False, force=False):
    """manual=True: the user asked for this one, so the daily LinkedIn cap (meant for background runs) does not apply.
    Whenever LinkedIn scouting does not run or fails, the plan says so on its first line instead of silently looking like a LinkedIn result."""
    import jd
    jd.gate(job_id, force=force)
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
                 "3. Note for each what makes a natural opening (their team, a recent post or project) in 'evidence'. These are existing connections, so a DM (not a connection request) fits later.")
    else:
        cb = ("NO EXISTING CONNECTIONS FOUND" + (" (connections file is present but nothing matched this company)" if conns else " (no LinkedIn export found in data/ yet)") +
              ". Find warm paths on the public web instead: the hiring manager or team named in the posting, authors of the team's recent papers/blog posts, open-source maintainers of the relevant repo, "
              f"and alumni of the candidate's university ({PROFILE.get('education', '')}) at the company if you can find any publicly.")
        tasks = ("1. Identify 2-3 specific real people (cite where you found them) who work on the relevant team/problem.\n"
                 "2. Rank them by approachability and relevance.\n3. For each, put a specific, checkable hook (what they built/wrote/said, with the URL) in 'evidence', and a public email in how_to_reach only if a page you read shows it.")
    if not SETTINGS.get("linkedin_browse", True) and not require_linkedin:
        li_note = "LinkedIn browsing is switched off in settings (linkedin_browse), so this plan uses public-web research only."
    elif not (manual or require_linkedin) and budget_used(c, "linkedin") >= SETTINGS.get("max_linkedin_per_day", 1):
        li_note = f"Background LinkedIn scouting is capped at {SETTINGS.get('max_linkedin_per_day', 1)}/day and is used up, so this plan is public-web only. Run `linkedin {job_id}` (or /linkedin in the chat) to scout LinkedIn for this job now."
    else:
        try:
            import linkedin
            r = linkedin.scout_people(job, hits)
            if not (r.get("targets") or []):
                li_note = "LinkedIn was searched: " + str(r.get("notes") or "nobody suitable among your 1st/2nd-degree connections there") + ". Below are people found on public pages instead (each one checked)."
            else:
                return _write(c, job, tri, pk, r, len(hits), "linkedin")
        except Exception as e:
            li_note = "LinkedIn scouting could not run (" + str(e)[:300] + "). Below are people found on public pages instead (each one checked)."
            log(f"linkedin scout failed ({e}); falling back to public-web referral research")
    if li_note:
        log("referral: " + li_note)
        if require_linkedin:
            print("WARNING:", li_note)
    prompt = REF_PROMPT.format(summary=PROFILE["summary"], projects=" | ".join(PROFILE["projects"][:3]), site=PROFILE["website"], company=job["company"],
                               title=job["title"], location=job["location"], url=job["url"], reason=(tri["reason"] if tri else ""), contacts_block=cb, tasks=tasks)
    prompt += common.job_context(job_id, "referral")
    text, _ = run_claude(prompt, kind="referral", allowed_tools="WebSearch,WebFetch", timeout=SETTINGS["packet_timeout_s"], max_turns=16)
    r = extract_json(text)
    if li_note:
        r["_note"] = li_note
    return _write(c, job, tri, pk, r, len(hits), "web")


def _verify(job, r, how):
    """Keep only targets that can be reached and were actually seen: a profile URL is required, and people found on the public web must appear (name and
    company) on that page when it is read. Scrubs 'unverified' wording. Returns r with r['_dropped'] = how many were removed."""
    import web
    keep, dropped = [], 0
    comp = re.sub(r"\(.*?\)", "", job["company"]).strip().split()[0].lower()
    for t in r.get("targets", []) or []:
        url = (t.get("profile_url") or "").strip()
        name = (t.get("name") or "").strip()
        if not url.startswith("http") or not name or re.search(r"unverified|unknown|n/?a", name, re.I):
            dropped += 1
            continue
        if how == "web":
            try:
                if "linkedin.com/" in url:
                    import browse
                    page = browse.page_text(url, 6000)
                else:
                    page = web.fetch_text(url, 12000)[1]
            except Exception:
                page = ""
            last = name.split()[-1].lower()
            if not page or last not in page.lower() or comp not in page.lower():
                dropped += 1
                continue
        for k, v in list(t.items()):
            if isinstance(v, str):
                t[k] = re.sub(r"\s*\(?\bunverified\b\)?:?", "", v, flags=re.I).strip()
        keep.append(t)
    r["targets"] = keep[:4]
    r["timing"] = re.sub(r"\s*\(?\bunverified\b\)?:?", "", r.get("timing", "") or "", flags=re.I).strip() or "Apply and message in parallel; one follow-up after 5-7 days."
    r["_dropped"] = dropped
    return r


def _write(c, job, tri, pk, r, nhits, how):
    r = _verify(job, r, how)
    if not r["targets"]:
        r["_note"] = ((r.get("_note") or "") + " No reachable person could be confirmed for this job. Run /connections " + job["company"] +
                      " to look again through LinkedIn, or drop your LinkedIn connections export into the workspace data folder.").strip()
    try:  # messages first, so they are in the file (they were drafted after the file text was built before)
        msgs = outreach.draft(job, r.get("targets", []), fit=(tri["reason"] if tri else ""))
        key = lambda n: re.sub(r"[^a-z ]", "", (n or "").lower()).split()

        def find(name, i):  # tolerant: exact, then same first+last word, then same position
            k = key(name)
            for m in msgs:
                mk = key(m.get("name"))
                if mk == k or (k and mk and k[0] == mk[0] and k[-1] == mk[-1]):
                    return m
            return msgs[i] if len(msgs) == len(r.get("targets", [])) else None
        for i, t in enumerate(r.get("targets", [])):
            m = find(t.get("name"), i)
            if m:
                t.update(connection_note=m.get("connect_note", ""), dm=m.get("dm_short", ""), dm_long=m.get("dm_long", ""), followup=m.get("followup", ""),
                         intro_ask_for_mutual=m.get("ask_mutual", ""), blurb_for_referrer=m.get("blurb", ""), style_warnings=m.get("_style_warnings", []))
    except Exception as e:
        log(f"outreach drafting failed: {e}")
    out = [f"# Referral plan: {job['company']} - {job['title']}", job["url"]]
    if r.get("_note"):
        out += ["", "> NOTE: " + r["_note"]]
    out += ["", "## Strategy", r.get("strategy", ""), "", "## Timing", r.get("timing", ""), "", "## Do first"]
    tim = str(r.get("timing", "")).strip().lower()
    # small models sometimes spill neighbouring keys into this list ("timing", then its value): keep real prep actions only
    out += [f"- {x}" for x in r.get("before_you_message", []) if isinstance(x, str) and len(x.strip()) >= 15 and x.strip().lower() != tim]
    sec = lambda title, body: ["", f"**{title}**", body] if (body or "").strip() else []
    for i, t in enumerate(r.get("targets", []), 1):
        why = t.get("why_them") or t.get("why_likely_to_respond") or ""
        out += ["", f"## Target {i}: {t.get('name')} ({t.get('role') or t.get('headline') or ''})", f"Profile: {t.get('profile_url')}",
                f"Reach via: {t.get('how_to_reach') or ('LinkedIn (' + str(t.get('degree', '')) + ' degree)')}"]
        out += [f"Mutual: {t['mutual']}"] if t.get("mutual") else []
        out += [f"Why them: {why}", f"Evidence: {t.get('evidence') or ''}"]
        out += sec("Connection note (only if you are not connected yet)", t.get("connection_note"))
        out += sec("Message (short)", t.get("dm")) + sec("Message (longer option)", t.get("dm_long"))
        out += sec("Ask the mutual connection for an intro", t.get("intro_ask_for_mutual"))
        out += sec("Follow-up (once, after 5-7 days)", t.get("followup")) + sec("Blurb they can forward", t.get("blurb_for_referrer"))
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
