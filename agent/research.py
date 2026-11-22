"""Research that actually reads: dossiers on companies and on the people you will contact, built from pages read in full.

Who does what (your rule): the LOCAL model (task kind 'research') reads and extracts facts with their source URL; the big model (kind 'review')
only makes decisions and writes. Nothing here contacts anyone.

Where it reads (your browsing policy):
- company: its own site (home, about, team/people, product/docs/research, careers, the latest blog posts), the company's LinkedIn page, and
  web search for funding and recent news. Off-platform first; LinkedIn only for the company page.
- person (only people you will actually contact): LinkedIn profile + recent activity, X bio and recent posts, their own site or GitHub.

How it reads: a page is read until its text stops growing (scrolling, waiting for content), not opened and closed after a fixed sleep. Every
page goes through websafe (hidden text out, injections cut, wrapped as untrusted). The pages read, their size and any safety flags are
stored with the dossier, so the app can show 'What I read'. Dossiers are cached (14 days for companies, 10 for people)."""
import datetime as dt, json, re, time, urllib.parse
import common
from common import db, log, now, extract_json, SETTINGS
import web, websafe

CO_DAYS, PERSON_DAYS = 14, 10
_LINK = re.compile(r'(?is)<a\b[^>]*href=["\']([^"\'#]+)["\'][^>]*>(.*?)</a>')
_WANT = [("about", r"\b(about|company|mission|who we are|our story)\b"), ("team", r"\b(team|people|leadership|founders?)\b"),
         ("product", r"\b(product|platform|solutions?|docs|documentation|research|technology)\b"), ("careers", r"\b(careers?|jobs|join us|hiring|work with us)\b"),
         ("blog", r"\b(blog|news|updates|engineering|posts?|journal)\b")]


def _cfg():
    r = SETTINGS.get("research", {})
    return {"max_pages": r.get("max_pages", 9), "page_chars": r.get("page_chars", 9000), "linkedin_company_page": r.get("linkedin_company_page", True),
            "profile_views_per_day": r.get("profile_views_per_day", 25)}


# ------------------------------------------------------------------ reading
def read(url, max_chars=None, browser_ok=True):
    """(title, text, links) of one page, read fully. Plain HTTP first; the logged-in browser (scrolling until the text stops growing) when the
    page is script-rendered or thin. Text is already sanitized by websafe."""
    max_chars = max_chars or _cfg()["page_chars"]
    ok, why = websafe.url_ok(url)
    if not ok:
        return "", "", []
    title, text, links = "", "", []
    try:
        raw, ctype = web._get(url)
        if "html" in ctype or "<html" in raw[:500].lower():
            title = websafe.clean((re.search(r"(?is)<title[^>]*>(.*?)</title>", raw) or [None, ""])[1]).strip()
            links = [(urllib.parse.urljoin(url, h), websafe.clean(web.to_text(t))[:80]) for h, t in _LINK.findall(raw)]
            text = websafe.sanitize(web.to_text(raw), url)[0]
        else:
            text = websafe.sanitize(raw, url)[0]
    except Exception:
        pass
    if len(text) < 900 and browser_ok:
        try:
            import browse
            r = browse.read_full(url, max_chars)
            if len(r.get("text", "")) > len(text):
                title, text = r.get("title") or title, r["text"]
                links = [(l["h"], l["t"]) for l in r.get("links", [])] or links
        except Exception as e:
            log(f"research: browser read failed for {url}: {str(e)[:120]}")
    return title[:200], text[:max_chars], [(h, t) for h, t in links if h.startswith("http")]


def _domain(company, url=""):
    try:
        import logos
        for d, trusted in logos.candidates(company, url):
            if trusted or logos._plausible(d, company, url):
                return d
    except Exception:
        pass
    return ""


def _pick_links(home, links, host):
    """The pages worth reading on a company site: about, team, product/research, careers, and the newest blog posts."""
    picked, seen = {}, set()
    for h, t in links:
        u = urllib.parse.urlparse(h)
        if not u.netloc.endswith(host) or h in seen or h.rstrip("/") == home.rstrip("/"):
            continue
        seen.add(h)
        label = (t + " " + u.path.replace("-", " ").replace("/", " ")).lower()
        for k, rx in _WANT:
            if k not in picked and re.search(rx, label) and u.path.count("/") <= 2:
                picked[k] = h
                break
    return picked


def _blog_posts(blog_url, links, host, n=3):
    base = urllib.parse.urlparse(blog_url).path.rstrip("/")
    posts = []
    for h, t in links:
        u = urllib.parse.urlparse(h)
        if u.netloc.endswith(host) and u.path.startswith(base + "/") and len(u.path) > len(base) + 6 and h not in posts and len(t) > 12:
            posts.append(h)
    return posts[:n]


# ------------------------------------------------------------------ extraction (local model)
CO_PROMPT = """You extract facts about a company from pages JobAgent read. Use ONLY these pages. Every fact must name the page number it came from.
{rule}

COMPANY: {company}

PAGES:
{pages}

Return ONLY JSON:
{{"what_they_do": {{"text": "", "src": 0}}, "product": [{{"text": "", "src": 0}}], "customers_or_users": [{{"text": "", "src": 0}}],
 "stage_and_funding": [{{"text": "", "src": 0}}], "team_and_size": [{{"text": "", "src": 0}}], "tech_and_research": [{{"text": "", "src": 0}}],
 "recent_news": [{{"text": "", "date": "", "src": 0}}], "hiring_and_culture": [{{"text": "", "src": 0}}], "visa_or_relocation": [{{"text": "", "src": 0}}],
 "open_problems": [{{"text": "what they are visibly trying to solve or build next", "src": 0}}], "people": [{{"name": "", "role": "", "src": 0}}]}}
Leave a list empty when the pages do not say it. Never guess."""

PERSON_PROMPT = """You extract facts about one person from pages JobAgent read, so the candidate can write to them like a human who did their homework.
Use ONLY these pages. Every fact must name the page number it came from.
{rule}

PERSON: {name} ({company})
CANDIDATE (for shared ground only): {candidate}

PAGES:
{pages}

Return ONLY JSON:
{{"role_now": {{"text": "", "src": 0}}, "background": [{{"text": "", "src": 0}}], "what_they_post_or_write_about": [{{"text": "", "src": 0}}],
 "recent_specific_thing": [{{"text": "a specific recent post, talk, paper, launch or project of theirs", "date": "", "src": 0}}],
 "shared_ground": [{{"text": "a real overlap with the candidate (school, field, tool, project area)", "src": 0}}],
 "how_they_hire_or_prefer_contact": [{{"text": "", "src": 0}}]}}
Leave a list empty when the pages do not say it. Never guess."""


def _pages_block(pages):
    return "\n\n".join(f"[PAGE {i}] {p['url']}\n" + websafe.wrap(p["url"], p["text"], 6000) for i, p in enumerate(pages))


def _clean_facts(d, pages):
    """Keep only facts that cite a page that was actually read; attach that page's URL."""
    out = {}
    for k, v in (d or {}).items():
        items = v if isinstance(v, list) else [v]
        keep = []
        for it in items:
            if not isinstance(it, dict):
                continue
            try:
                i = int(it.get("src", -1))
            except (TypeError, ValueError):
                i = -1
            txt = str(it.get("text") or it.get("name") or "").strip()
            if 0 <= i < len(pages) and txt and not websafe.scan(txt):
                keep.append(dict(it, url=pages[i]["url"]))
        if keep:
            out[k] = keep
    return out


def _extract(prompt_tpl, pages, **kw):
    from llm import run
    if not pages:
        return {}
    text, _ = run(prompt_tpl.format(rule=websafe.RULE, pages=_pages_block(pages), **kw), kind="research", timeout=420, max_turns=1)
    try:
        return _clean_facts(extract_json(text), pages)
    except ValueError:
        return {}


# ------------------------------------------------------------------ cache
def _get_cached(k, days):
    r = db().execute("SELECT * FROM dossiers WHERE k=? AND at>=?", (k, (dt.datetime.now() - dt.timedelta(days=days)).isoformat(timespec="seconds"))).fetchone()
    if not r:
        return None
    return {"k": r["k"], "kind": r["kind"], "name": r["name"], "facts": json.loads(r["facts"] or "{}"), "pages": json.loads(r["pages"] or "[]"),
            "flags": json.loads(r["flags"] or "[]"), "at": r["at"]}


def _store(k, kind, name, facts, pages, flags):
    c = db()
    slim = [{"url": p["url"], "title": p.get("title", ""), "chars": len(p.get("text", "")), "why": p.get("why", ""), "flags": p.get("flags", [])} for p in pages]
    c.execute("INSERT OR REPLACE INTO dossiers(k,kind,name,facts,pages,flags,at) VALUES(?,?,?,?,?,?,?)",
              (k, kind, name, json.dumps(facts, ensure_ascii=False), json.dumps(slim, ensure_ascii=False), json.dumps(sorted(set(flags))), now()))
    c.commit()
    return {"k": k, "kind": kind, "name": name, "facts": facts, "pages": slim, "flags": sorted(set(flags)), "at": now()}


def _add(pages, url, why, browser_ok=True, max_chars=None):
    if any(p["url"].rstrip("/") == url.rstrip("/") for p in pages):
        return None
    title, text, links = read(url, max_chars, browser_ok)
    if len(text) < 200:
        return None
    flags = [w for w, _ in websafe.scan(text)]
    pages.append({"url": url, "title": title, "text": text, "why": why, "flags": flags})
    return links


# ------------------------------------------------------------------ dossiers
def company(name, url="", force=False):
    """Dossier of a company (cached CO_DAYS). url = a job or company URL that helps find its website."""
    k = "co:" + common.company_key(name)
    if not force:
        hit = _get_cached(k, CO_DAYS)
        if hit:
            return hit
    cfg = _cfg()
    pages = []
    dom = _domain(name, url)
    if dom:
        home = "https://" + dom
        links = _add(pages, home, "home page") or []
        picked = _pick_links(home, links, dom)
        for key in ("about", "team", "product", "careers"):
            if key in picked and len(pages) < cfg["max_pages"]:
                _add(pages, picked[key], key)
        if "blog" in picked and len(pages) < cfg["max_pages"]:
            blinks = _add(pages, picked["blog"], "blog") or []
            for post in _blog_posts(picked["blog"], blinks, dom):
                if len(pages) >= cfg["max_pages"]:
                    break
                _add(pages, post, "recent blog post")
    if cfg["linkedin_company_page"] and SETTINGS.get("linkedin_browse", True):
        try:
            import browse
            li = browse.linkedin_company(name)
            if li.get("slug") and len(pages) < cfg["max_pages"] + 1:
                _add(pages, f"https://www.linkedin.com/company/{li['slug']}/about/", "LinkedIn company page", max_chars=5000)
        except Exception as e:
            log(f"research: LinkedIn company page for {name} skipped: {str(e)[:120]}")
    for q in (f'"{name}" funding raised', f'"{name}" news'):
        for r in web.search(q, 4):
            host = urllib.parse.urlparse(r["url"]).netloc
            if dom and host.endswith(dom):
                continue
            if len(pages) >= cfg["max_pages"] + 3:
                break
            if name.lower().split()[0] in (r["title"] + r["snippet"]).lower():
                _add(pages, r["url"], f"search: {q}", browser_ok=False, max_chars=6000)
                break
    facts = _extract(CO_PROMPT, pages, company=name)
    d = _store(k, "company", name, facts, pages, [f for p in pages for f in p["flags"]])
    log(f"research: {name}: read {len(pages)} pages ({sum(len(p['text']) for p in pages)} chars), {sum(len(v) for v in facts.values())} facts")
    return d


def _profile_budget_ok():
    c = db()
    return common.budget_used(c, "profile_views") < _cfg()["profile_views_per_day"]


def person(name, company_name="", linkedin_url="", x_handle="", site="", force=False):
    """Dossier of one person you will contact (cached PERSON_DAYS)."""
    k = "p:" + common.company_key((linkedin_url or x_handle or name) + "|" + company_name)
    if not force:
        hit = _get_cached(k, PERSON_DAYS)
        if hit:
            return hit
    pages = []
    if linkedin_url and _profile_budget_ok():
        try:
            import browse
            about = browse.linkedin_profile(linkedin_url)
            common.budget_add(db(), "profile_views")
            if about:
                pages.append({"url": linkedin_url.split("?")[0], "title": "LinkedIn profile", "text": websafe.sanitize(about, linkedin_url)[0], "why": "LinkedIn profile", "flags": []})
            act = linkedin_url.split("?")[0].rstrip("/") + "/recent-activity/all/"
            _add(pages, act, "LinkedIn recent activity", max_chars=6000)
            common.budget_add(db(), "profile_views")
        except Exception as e:
            log(f"research: LinkedIn profile of {name} skipped: {str(e)[:120]}")
    if x_handle:
        _add(pages, f"https://x.com/{x_handle.lstrip('@')}", "X profile and recent posts", max_chars=6000)
    if site:
        _add(pages, site, "their own site")
    cand = (common.PROFILE.get("summary") or "")[:400] + " | " + (common.PROFILE.get("education") or "")[:200]
    facts = _extract(PERSON_PROMPT, pages, name=name, company=company_name, candidate=cand)
    return _store(k, "person", name, facts, pages, [f for p in pages for f in p["flags"]])


def brief(d, max_chars=2600):
    """Dossier as compact text for a writing prompt: facts with their source URLs (nothing else may be claimed about them)."""
    if not d or not d.get("facts"):
        return ""
    lines = [f"DOSSIER: {d['name']} (read {len(d['pages'])} pages on {d['at'][:10]}; use only these facts)"]
    for k, items in d["facts"].items():
        for it in items[:4]:
            lines.append(f"- {k.replace('_', ' ')}: {it.get('text') or it.get('name', '')}" + (f" ({it['role']})" if it.get("role") else "")
                         + (f" [{it['date']}]" if it.get("date") else "") + f"  <{it['url']}>")
    return "\n".join(lines)[:max_chars]


def what_i_read(d):
    """For the app: the pages behind a dossier."""
    return [{"url": p["url"], "title": p.get("title", ""), "chars": p.get("chars", 0), "why": p.get("why", ""), "flags": p.get("flags", [])} for p in (d or {}).get("pages", [])]


# ------------------------------------------------------------------ visa sponsorship check (Visa unclear tab)
SPONSOR_PROMPT = """Decide whether this employer sponsors work visas for a role like this one, for a candidate from {home} with no existing work permit.
Use ONLY the evidence below (facts read from the web, each with its source). Strong evidence: the employer's own careers/FAQ page, the posting,
official filings (H-1B/LCA records, UK sponsor register). Weak: third-party blog claims. Relocation help alone is NOT visa sponsorship.

ROLE: {title} at {company} ({location})

EVIDENCE:
{evidence}

Return ONLY JSON: {{"sponsors": "yes|no|unknown", "confidence": "high|medium|low", "reason": "one sentence citing the source", "source": "url"}}"""


def sponsorship(job_id):
    """Research the employer's visa sponsorship for a 'Visa unclear' job and settle it: yes -> back to normal, no -> ineligible, unknown -> stays."""
    from llm import run
    c = db()
    j = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not j:
        return {"error": "no such job"}
    pages = []
    co = j["company"]
    for q in (f'"{co}" visa sponsorship', f'"{co}" H-1B sponsor', f'"{co}" careers relocation visa international candidates'):
        for r in web.search(q, 3)[:2]:
            if len(pages) < 6:
                _add(pages, r["url"], f"search: {q}", browser_ok=False, max_chars=5000)
    d = company(co, j["url"] or "")
    ev = [f"{it.get('text')} <{it['url']}>" for it in d.get("facts", {}).get("visa_or_relocation", [])]
    facts = _extract("""Extract every statement about visa sponsorship, work permits, H-1B/LCA filings, relocation or international hiring by {company}.
{rule}

PAGES:
{pages}

Return ONLY JSON: {{"statements": [{{"text": "", "src": 0}}]}}""", pages, company=co)
    ev += [f"{it['text']} <{it['url']}>" for it in facts.get("statements", [])]
    if not ev:
        res = {"sponsors": "unknown", "confidence": "low", "reason": "nothing about visas found on the pages read", "source": ""}
    else:
        home = (common.PROFILE.get("preferences") or {}).get("home_country") or "India"
        text, _ = run(SPONSOR_PROMPT.format(home=home, title=j["title"], company=co, location=j["location"], evidence="\n".join(ev)[:6000]), kind="review", timeout=240, max_turns=1)
        try:
            res = extract_json(text)
        except ValueError:
            res = {"sponsors": "unknown", "reason": "could not read the decision"}
    s = str(res.get("sponsors", "unknown")).lower()
    note = f"Sponsorship check ({now()[:10]}): {s} - {res.get('reason', '')} {res.get('source', '')}".strip()
    common.add_job_note(job_id, note)
    if s == "yes":
        c.execute("UPDATE jobs SET gate=NULL, visa='yes' WHERE id=?", (job_id,))
    elif s == "no":
        c.execute("UPDATE jobs SET gate='visa-blocked', visa='no' WHERE id=?", (job_id,))
        common.set_status(c, job_id, "ineligible", commit=False)
    c.commit()
    log(f"sponsorship {co}: {s} ({res.get('reason', '')[:120]})")
    return dict(res, pages=[p["url"] for p in pages])
