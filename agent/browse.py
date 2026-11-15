"""Deterministic browser layer: Playwright attached (CDP) to the dedicated agent Chrome, where you are logged in to LinkedIn, YC Work at a Startup and job
boards. It reads pages with small JavaScript extractors instead of handing raw DOM dumps to a model, so browsing costs no model tokens and works with any
provider, including small local models.

Safety: everything here is READ-ONLY. LinkedIn pages get the always-on guard (no Connect/Message/Follow/... clicks), loads are paced like a human,
pages per run are capped, and login walls / security checkpoints stop the run with a clear message. Form filling lives in fill.py (never submits)."""
import json, os, random, re, threading, time, urllib.parse
import common
from common import db, log, now, SETTINGS

MAX_PAGES_PER_RUN = 40
_lock = threading.RLock()
_state = {"pw": None, "browser": None, "pages": 0}


class BrowseError(Exception):
    pass


# ------------------------------------------------------------------ connection
def available():
    """(ok, why). Browser use is required; doctor reports this."""
    try:
        import playwright  # noqa: F401
    except ImportError:
        return False, "Python package 'playwright' is missing: run the installer, or `python -m pip install playwright`"
    try:
        import fill
        fill.ensure_chrome()
    except Exception as e:
        return False, f"agent Chrome could not start: {e}"
    return True, f"agent Chrome on port {SETTINGS['chrome_debug_port']}"


def _context():
    with _lock:
        if _state["browser"] is None or not _state["browser"].is_connected():
            try:
                from playwright.sync_api import sync_playwright
            except ImportError as e:
                raise BrowseError("Playwright is not installed: run the installer (or `python -m pip install playwright`)") from e
            import fill
            fill.ensure_chrome()
            _state["pw"] = _state["pw"] or sync_playwright().start()
            _state["browser"] = _state["pw"].chromium.connect_over_cdp(f"http://127.0.0.1:{SETTINGS['chrome_debug_port']}", timeout=30000)
        b = _state["browser"]
        return b.contexts[0] if b.contexts else b.new_context()


def close():
    with _lock:
        try:
            if _state["browser"]:
                _state["browser"].close()  # disconnects; the agent Chrome keeps running
            if _state["pw"]:
                _state["pw"].stop()
        except Exception:
            pass
        _state.update(pw=None, browser=None)


def _pace(lo=3.0, hi=6.0):
    time.sleep(random.uniform(lo, hi))


WALL = re.compile(r"/(login|signin|uas/login|checkpoint|authwall)|captcha|challenge", re.I)


def _open(url, guard=False, wait=2.5):
    if _state["pages"] >= MAX_PAGES_PER_RUN:
        raise BrowseError(f"page budget for this run reached ({MAX_PAGES_PER_RUN}); stopping to stay human-paced")
    ctx = _context()
    page = ctx.new_page()
    if guard or "linkedin.com" in url:
        import browser
        page.add_init_script(browser.READONLY_GUARD)
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        page.close()
        raise BrowseError(f"could not open {url}: {str(e)[:160]}")
    _state["pages"] += 1
    time.sleep(wait)
    if WALL.search(page.url):
        page.close()
        site = urllib.parse.urlparse(url).netloc
        raise BrowseError(f"{site} wants you to log in (or shows a security check). Open the agent browser (GUI: Settings -> Open agent browser), log in once, then retry.")
    return page


# ------------------------------------------------------------------ generic pages
TEXT_JS = r"""(maxc) => {
  let ld = '';
  for (const s of document.querySelectorAll('script[type="application/ld+json"]')) {
    try { const j = JSON.parse(s.textContent); for (const n of [].concat(j)) if (String(n['@type']).includes('JobPosting'))
      ld += `[JobPosting] ${n.title||''} | ${(n.hiringOrganization||{}).name||''}\n` + (n.description||'').replace(/<[^>]+>/g,' ') + '\n'; } catch(e) {}
  }
  const main = document.querySelector('main, [role=main], article, #content') || document.body;
  const t = (ld + '\n' + (main ? main.innerText : document.body.innerText)).replace(/\n{3,}/g, '\n\n');
  return {title: document.title, url: location.href, text: t.slice(0, maxc)};
}"""

LINKS_JS = r"""(n) => [...document.querySelectorAll('a[href]')].map(a => ({t: (a.innerText||a.getAttribute('aria-label')||'').trim().replace(/\s+/g,' ').slice(0,90), h: a.href}))
  .filter(x => x.t && x.h.startsWith('http')).slice(0, n)"""


def page_text(url, max_chars=12000):
    """Readable text of any page, through your logged-in browser (JS-rendered career pages, YC, LinkedIn...)."""
    with _lock:
        page = _open(url)
        try:
            for _ in range(3):  # let SPAs render
                r = page.evaluate(TEXT_JS, max_chars)
                if len(r["text"]) > 800:
                    break
                time.sleep(2)
            return r["text"]
        finally:
            page.close()


def open_page(url, max_chars=6000, links=40):
    """Title, text and the first links of a page (for the agent)."""
    with _lock:
        page = _open(url)
        try:
            r = page.evaluate(TEXT_JS, max_chars)
            r["links"] = page.evaluate(LINKS_JS, links)
            return r
        finally:
            page.close()


def find_on_page(url, query, window=300):
    text = page_text(url, 60000)
    words = [w for w in re.findall(r"\w+", query.lower()) if len(w) > 2]
    hits = []
    for m in re.finditer("|".join(map(re.escape, words)) or "$^", text, re.I):
        s = text[max(0, m.start() - window // 2): m.end() + window // 2].replace("\n", " ")
        if not hits or s[:60] not in hits[-1]:
            hits.append(s)
        if len(hits) >= 8:
            break
    return hits


def screenshot(url, path):
    with _lock:
        page = _open(url)
        try:
            page.screenshot(path=path, full_page=False)
            return path
        finally:
            page.close()


# ------------------------------------------------------------------ LinkedIn (read-only)
# Layout-independent: LinkedIn A/B-tests its markup (list items in one layout, one big link per card in another). From each profile link, climb to the
# LARGEST ancestor that still contains exactly one connection-degree marker: that is one person's result card.
PEOPLE_JS = r"""() => {
  const DEG = /(?:^|[\s•·])(1st|2nd|3rd\+?)(?=\s|$)/g;
  const out = [], seen = new Set();
  for (const a of document.querySelectorAll('main a[href*="/in/"]')) {
    let card = null;
    for (let el = a, i = 0; el && i < 12; el = el.parentElement, i++) {
      const t = el.innerText || '';
      const n = (t.match(DEG) || []).length;
      if (n > 1 || t.length > 1500) break;
      if (n === 1) card = el;
    }
    if (!card || seen.has(card)) continue;
    seen.add(card);
    const prof = card.matches('a[href*="/in/"]') ? card : card.querySelector('a[href*="/in/"]');
    out.push({url: prof.href.split('?')[0], text: card.innerText.trim().slice(0, 700)});
  }
  return out;
}"""


def _parse_card(c):
    lines = [l.strip(" •·•") for l in c["text"].splitlines() if l.strip(" •·•")]
    name = re.sub(r"\s*(View .*profile|\b(1st|2nd|3rd\+?)\b).*$", "", lines[0]).strip() if lines else ""
    deg = (re.search(r"\b(1st|2nd|3rd)\b", c["text"]) or [None, ""])[1]
    rest = [l for l in lines[1:] if not re.fullmatch(r"(1st|2nd|3rd\+?|Message|Connect|Follow|Pending|Status is \w+)", l)]
    headline = rest[0] if rest else ""
    loc = rest[1] if len(rest) > 1 and not re.search(r"^(Current|Past|Skills|Summary):|mutual", rest[1], re.I) else ""
    cur = next((l[8:].strip() for l in rest if l.lower().startswith("current:")), "")
    mutual = next((l for l in rest if "mutual" in l.lower()), "")
    return {"name": name, "degree": deg, "headline": headline, "location": loc, "current": cur, "mutual": mutual, "url": c["url"]}


def _company_match(person, company):
    base = re.sub(r"\(.*?\)", "", company.lower())
    base = re.sub(r"[^a-z0-9 ]", " ", base).strip()
    keys = {base, re.sub(r"\b(ai|labs?|inc|technologies|research|india)\b", "", base).strip()}
    hay = (person["headline"] + " | " + person["current"]).lower()
    # current employer only, stated as an employer ("at Stripe", "@ Stripe"); "ex-Stripe", "former ...", or a keyword snippet like
    # "built Stripe-based payments" do not count
    return any(k and re.search(r"(?<!ex)(?<!former)(?<!previously)(?:\bat\b|@)\s*" + re.escape(k) + r"(?![\w-])", hay) for k in keys)


def cached_people(company, days=7):
    rows = db().execute("SELECT * FROM people WHERE lower(company)=lower(?) AND seen_at>=datetime('now', ?) ORDER BY degree, name",
                        (company, f"-{days} day")).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        head, _, cur = d["headline"].partition(" | Current: ")
        d.update(headline=head, current=cur)
        if _company_match(d, company):  # re-check: matching rules improve over time
            out.append(d)
    return out


def _save_people(company, people, source):
    c = db()
    for p in people:
        c.execute("""INSERT INTO people(url,company,name,headline,degree,mutual,location,about,source,seen_at) VALUES(?,?,?,?,?,?,?,COALESCE((SELECT about FROM people WHERE url=?),''),?,datetime('now'))
                     ON CONFLICT(url) DO UPDATE SET company=excluded.company,name=excluded.name,headline=excluded.headline,degree=excluded.degree,mutual=excluded.mutual,
                     location=excluded.location,source=excluded.source,seen_at=excluded.seen_at""",
                  (p["url"], company, p["name"], p["headline"] + (f" | Current: {p['current']}" if p.get("current") else ""), p["degree"], p["mutual"], p["location"], p["url"], source))
    c.commit()


def linkedin_people(company, degree="1st", role_words="", limit=12, use_cache=True):
    """People at `company` among your 1st- or 2nd-degree network, from LinkedIn search (read-only). Cached for 7 days."""
    if use_cache and not role_words:
        hit = [p for p in cached_people(company) if p["degree"] == degree]
        if hit:
            return hit[:limit]
    net = {"1st": "F", "2nd": "S"}[degree]
    kw = (company + (" " + role_words if role_words else "")).strip()
    url = "https://www.linkedin.com/search/results/people/?keywords=" + urllib.parse.quote(kw) + "&network=%5B%22" + net + "%22%5D&origin=FACETED_SEARCH"
    with _lock:
        page = _open(url, guard=True, wait=4)
        try:
            cards = page.evaluate(PEOPLE_JS)
        finally:
            page.close()
    people = [p for p in (_parse_card(c) for c in cards) if p["name"] and _company_match(p, company)][:limit]
    _save_people(company, people, f"linkedin-{degree}")
    log(f"browse: linkedin {degree}-degree at {company}: {len(people)} people (of {len(cards)} results)")
    _pace()
    return [dict(p, company=company) for p in people]


PROFILE_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  const sec = (re) => { for (const s of main.querySelectorAll('section')) { const h = s.querySelector('h2'); if (h && re.test(h.innerText)) return s.innerText.slice(0, 1600); } return ''; };
  const top = main.querySelector('section') ? main.querySelector('section').innerText.slice(0, 700) : '';
  return {top, about: sec(/^\s*About/i), experience: sec(/^\s*Experience/i), activity: sec(/^\s*Activity/i).slice(0, 700)};
}"""


def linkedin_profile(url):
    """Top card, About, Experience and recent activity of one profile (truncated, read-only). Stored with the person."""
    with _lock:
        page = _open(url.split("?")[0], guard=True, wait=4)
        try:
            r = page.evaluate(PROFILE_JS)
        finally:
            page.close()
    about = "\n".join(f"{k.upper()}: {v}" for k, v in r.items() if v)[:3500]
    c = db()
    c.execute("UPDATE people SET about=? WHERE url=?", (about, url.split("?")[0]))
    c.commit()
    _pace()
    return about


def scan_company(company, want=6, profiles=2):
    """1st degree first; 2nd degree (with mutuals) when 1st is thin. Opens the top profiles for evidence. Returns people dicts."""
    people = linkedin_people(company, "1st")
    if len(people) < 3:
        people += linkedin_people(company, "2nd")
    people = people[:want]
    for p in people[:profiles]:
        if not p.get("about"):
            try:
                p["about"] = linkedin_profile(p["url"])
            except BrowseError as e:
                log(f"browse: profile skipped: {e}")
                break
    return people


# ------------------------------------------------------------------ open-ended browsing for the agent (read-only)
TASK_PROMPT = """You are browsing the web READ-ONLY for this goal: {goal}
You are on: {url}  ({title})
PAGE TEXT (truncated):
{text}

LINKS (index: text -> url):
{links}

NOTES SO FAR:
{notes}

Reply with JSON only, one of:
{{"open": <link index>, "note": "what you learned on this page that matters for the goal"}}
{{"goto": "<absolute url>", "note": "..."}}
{{"answer": "the final answer for the goal, specific (names, titles, links)", "note": "..."}}
Never log in, submit, apply, message or click buttons; only read and follow links. Pages left: {left}."""


def browser_task(goal, start_url, max_pages=6):
    """Multi-step read-only navigation driven by a cheap model call per page (compact page text + link list, not the DOM)."""
    from llm import run
    url, notes = start_url, []
    for left in range(max_pages, 0, -1):
        r = open_page(url, 3500, 60)
        links = "\n".join(f"{i}: {l['t']} -> {l['h']}" for i, l in enumerate(r.get("links", [])))
        text, _ = run(TASK_PROMPT.format(goal=goal, url=r["url"], title=r["title"], text=r["text"], links=links, notes="\n".join(notes) or "(none)", left=left - 1),
                      kind="browse", timeout=180, max_turns=1)
        try:
            d = common.extract_json(text)
        except ValueError:
            break
        if d.get("note"):
            notes.append(f"- {r['url']}: {d['note'][:300]}")
        if d.get("answer"):
            return d["answer"] + ("\n\nSeen:\n" + "\n".join(notes[-6:]) if notes else "")
        if "open" in d and str(d["open"]).isdigit() and int(d["open"]) < len(r.get("links", [])):
            url = r["links"][int(d["open"])]["h"]
        elif d.get("goto", "").startswith("http"):
            url = d["goto"]
        else:
            break
        _pace(1.5, 3)
    return "Stopped before finding a definite answer. Notes:\n" + "\n".join(notes)
