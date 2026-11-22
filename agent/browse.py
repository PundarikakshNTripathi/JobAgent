"""Deterministic browser layer: Playwright attached (CDP) to the dedicated agent Chrome, where you are logged in to LinkedIn, YC Work at a Startup and job
boards. It reads pages with small JavaScript extractors instead of handing raw DOM dumps to a model, so browsing costs no model tokens and works with any
provider, including small local models.

Safety: everything here is READ-ONLY. LinkedIn pages get the always-on guard (no Connect/Message/Follow/... clicks), loads are paced like a human,
pages per run are capped, and login walls / security checkpoints stop the run with a clear message. Form filling lives in fill.py (never submits)."""
import contextlib, json, os, random, re, threading, time, urllib.parse
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


def _xacquire(timeout=300):
    """One JobAgent process drives the agent Chrome at a time. Two processes navigating the same Chrome at once made pages hang until the
    45 s timeout (a Baseten and a Google scout ran together and the Google one failed). Re-entrant within a process."""
    if _state.get("xdepth"):
        _state["xdepth"] += 1
        return
    fh = open(os.path.join(common.DATA, "browser.lock"), "a+")
    t0 = time.time()
    while True:
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if time.time() - t0 > timeout:
                fh.close()
                raise BrowseError("another JobAgent task has been using the browser for 5 minutes; try again shortly")
            time.sleep(1.0)
    _state["xfh"], _state["xdepth"] = fh, 1


def _xrelease():
    _state["xdepth"] = max(0, _state.get("xdepth", 0) - 1)
    if _state["xdepth"] == 0 and _state.get("xfh"):
        fh = _state.pop("xfh")
        try:
            if os.name == "nt":
                import msvcrt
                fh.seek(0)
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_UN)
        except OSError:
            pass
        fh.close()


@contextlib.contextmanager
def session():
    """Exclusive use of the agent browser (this thread and this process)."""
    with _lock:
        _xacquire()
        try:
            yield
        finally:
            _xrelease()


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
        # one retry: LinkedIn's single-page app sometimes never fires DOMContentLoaded for a search; the first response is enough there
        try:
            page.goto(url, wait_until="commit", timeout=30000)
            page.wait_for_selector("main", timeout=20000)
        except Exception:
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
    with session():
        page = _open(url)
        try:
            import websafe
            for _ in range(3):  # let SPAs render
                try:
                    page.evaluate(websafe.HIDE_JS)  # invisible text never reaches a model
                except Exception:
                    pass
                r = page.evaluate(TEXT_JS, max_chars)
                if len(r["text"]) > 800:
                    break
                time.sleep(2)
            return websafe.sanitize(r["text"], url)[0]
        finally:
            page.close()


def open_page(url, max_chars=6000, links=40):
    """Title, text and the first links of a page (for the agent)."""
    with session():
        page = _open(url)
        try:
            import websafe
            try:
                page.evaluate(websafe.HIDE_JS)
            except Exception:
                pass
            r = page.evaluate(TEXT_JS, max_chars)
            r["text"] = websafe.sanitize(r["text"], url)[0]
            r["title"] = websafe.clean(r.get("title", ""))[:200]
            r["links"] = [l for l in page.evaluate(LINKS_JS, links) if websafe.url_ok(l["h"])[0]]
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
    with session():
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


def company_aliases(company):
    """'Google / Google DeepMind' -> ['Google', 'Google DeepMind']; 'Hook (YC S21)' -> ['Hook']. Searching LinkedIn for the literal compound name
    matched nobody (the Google Student Researcher plan said "no connections" while there are many)."""
    base = re.sub(r"\([^)]*\)", " ", company or "")
    parts = [x.strip(" -\u2013\u2014") for x in re.split(r"\s+/\s+|\s*\|\s*|\s*;\s*|\s+&\s+(?=[A-Z])", base)]
    out = []
    for x in parts:
        x = re.sub(r"\s+", " ", x).strip()
        if x and x.lower() not in [o.lower() for o in out]:
            out.append(x)
    return out or [(company or "").strip()]


def _keys(company):
    out = set()
    for al in company_aliases(company):
        base = re.sub(r"[^a-z0-9 ]", " ", al.lower())
        base = re.sub(r"\s+", " ", base).strip()
        out |= {base, re.sub(r"\s+", " ", re.sub(r"\b(ai|labs?|inc|technologies|research|india|corp|corporation|ltd|llc)\b", "", base)).strip()}
    return {k for k in out if len(k) >= 2}


def _company_match(person, company):
    """Current employer only. The 'Current:' line LinkedIn shows (or the current-company search filter) counts directly; in a headline the
    company must be stated as an employer ("at X", "@ X", "X |", "- X", ", X"). "ex-X", "former X" and "previously at X" do not count."""
    keys = _keys(company)
    cur = re.sub(r"\s+", " ", (person.get("current") or "").lower()).strip()
    # LinkedIn's "Current:" line can be a keyword snippet ("implemented Stripe-based payments"): the company must be stated as the employer
    if any(re.search(r"(?:^|\bat\b|@)\s*" + re.escape(k) + r"(?![\w-])", cur) for k in keys):
        return True
    hay = (person.get("headline") or "").lower()
    for k in keys:
        e = re.escape(k)
        if re.search(r"(?<!ex)(?<!ex-)(?<!former)(?<!formerly)(?<!previously)(?:\bat\b|@)\s*" + e + r"(?![\w-])", hay):
            return True
        if re.search(r"(?:^|[|,\-\u2013\u2014\u00b7]\s*)" + e + r"\s*(?:$|[|,\u00b7(])", hay) and not re.search(r"\b(ex|former|formerly|previously)[\s-]*" + e, hay):
            return True
    return False


def cached_people(company, days=7):
    names = [company] + company_aliases(company)
    q = ",".join("?" * len(names))
    rows = db().execute(f"SELECT * FROM people WHERE lower(company) IN ({q}) AND seen_at>=datetime('now', ?) ORDER BY degree, name",
                        [n.lower() for n in names] + [f"-{days} day"]).fetchall()
    out, seen = [], set()
    for r in rows:
        if r["url"] in seen:
            continue
        seen.add(r["url"])
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


COMPANY_LINKS_JS = r"""() => [...document.querySelectorAll('main a[href*="/company/"]')].map(a => [a.href.split('?')[0], ((a.innerText || '').trim().split('\n')[0] || '').trim()]).filter(x => x[1])"""
CANNED_JS = r"""() => [...document.querySelectorAll('a[href*="currentCompany"]')].map(a => a.href)"""


def _li_company_table(c):
    c.execute("CREATE TABLE IF NOT EXISTS li_company(k TEXT PRIMARY KEY, name TEXT, slug TEXT, id TEXT, at TEXT)")


def linkedin_company(name):
    """{'name', 'slug', 'id'} of a company on LinkedIn: the company search picks the page, and the page's own "people who work here" link
    carries LinkedIn's company id. Cached for 60 days. The id lets people search filter by CURRENT company exactly, instead of guessing
    from keywords and headlines."""
    c = db()
    _li_company_table(c)
    k = common.company_key(name)
    r = c.execute("SELECT * FROM li_company WHERE k=? AND at>=datetime('now','-60 day')", (k,)).fetchone()
    if r and r["id"]:
        return dict(r)
    with session():
        page = _open("https://www.linkedin.com/search/results/companies/?keywords=" + urllib.parse.quote(name), guard=True, wait=3)
        try:
            links = page.evaluate(COMPANY_LINKS_JS)
        finally:
            page.close()
        cands = [(u, t) for u, t in links if re.search(r"/company/[^/]+/?$", u)]
        if not cands:
            return {}
        want = name.lower().strip()
        url, title = next(((u, t) for u, t in cands if t.lower().strip() == want), cands[0])
        page = _open(url, guard=True, wait=3)
        try:
            hrefs = page.evaluate(CANNED_JS)
        finally:
            page.close()
    ids = []
    for h in hrefs:
        m = re.search(r"currentCompany=%5B%22(\d+)%22|currentCompany=\[\"(\d+)\"", h)
        if m:
            ids.append((("network" in h), m.group(1) or m.group(2)))
    cid = next((i for net, i in ids if not net), ids[0][1] if ids else "")
    slug = url.rstrip("/").rsplit("/", 1)[-1]
    c.execute("INSERT OR REPLACE INTO li_company(k,name,slug,id,at) VALUES(?,?,?,?,datetime('now'))", (k, title, slug, cid))
    c.commit()
    log(f"browse: linkedin company {name} -> {title} ({slug}, id {cid or 'unknown'})")
    return {"k": k, "name": title, "slug": slug, "id": cid}


def _search_people(url, pages=2):
    cards = []
    with session():
        for n in range(1, pages + 1):
            page = _open(url + (f"&page={n}" if n > 1 else ""), guard=True, wait=4)
            try:
                got = page.evaluate(PEOPLE_JS)
            finally:
                page.close()
            cards += got
            if len(got) < 10:
                break
            _pace(2, 4)
    return cards


LAST = {}  # company -> how the last search ran ("company filter" or "keywords"), so callers word "nobody" claims honestly


def linkedin_people(company, degree="1st", role_words="", limit=12, use_cache=True):
    """People who CURRENTLY work at `company` among your 1st- or 2nd-degree network (read-only, cached 7 days).
    Compound names are split ('Google / Google DeepMind' searches both). Primary path: LinkedIn's current-company filter (exact); fallback:
    keyword search plus an employer check on each result."""
    if use_cache and not role_words:
        hit = [p for p in cached_people(company) if p["degree"] == degree]
        if hit:
            return hit[:limit]
    net = {"1st": "F", "2nd": "S", "any": ""}[degree]  # "any": everyone at the company (founders of a new startup are rarely in your network)
    per, modes = [], []
    for al in company_aliases(company):
        try:
            co = linkedin_company(al)
        except BrowseError as e:
            log(f"browse: company lookup failed for {al}: {e}")
            co = {}
        if co.get("id"):
            url = ("https://www.linkedin.com/search/results/people/?currentCompany=%5B%22" + co["id"] + "%22%5D" + ("&network=%5B%22" + net + "%22%5D" if net else "") +
                   "&origin=FACETED_SEARCH" + ("&keywords=" + urllib.parse.quote(role_words) if role_words else ""))
            people = [dict(p, current=p.get("current") or co.get("name") or al) for p in (_parse_card(c) for c in _search_people(url)) if p["name"]]
            modes.append("company filter")
        else:
            kw = (al + (" " + role_words if role_words else "")).strip()
            url = "https://www.linkedin.com/search/results/people/?keywords=" + urllib.parse.quote(kw) + ("&network=%5B%22" + net + "%22%5D" if net else "") + "&origin=FACETED_SEARCH"
            people = [p for p in (_parse_card(c) for c in _search_people(url, pages=1)) if p["name"] and _company_match(p, al)]
            modes.append("keywords")
        if degree != "any":
            people = [p for p in people if p["degree"] in ("", degree)] or people
        for p in people:
            p["degree"] = p["degree"] or ("" if degree == "any" else degree)
        _save_people(al, people, f"linkedin-{degree}")
        log(f"browse: linkedin {degree}-degree at {al}: {len(people)} people ({modes[-1]})")
        per.append([dict(p, company=al) for p in people])
        _pace()
    LAST[company] = "company filter" if modes and all(m == "company filter" for m in modes) else "keywords"
    out, seen = [], set()
    for row in zip(*[x + [None] * (max(map(len, per)) - len(x)) for x in per]) if per else []:  # interleave: every alias is represented
        for p in row:
            if p and p["url"] not in seen:
                seen.add(p["url"])
                out.append(p)
    return out[:limit]


PROFILE_JS = r"""() => {
  const main = document.querySelector('main') || document.body;
  const sec = (re) => { for (const s of main.querySelectorAll('section')) { const h = s.querySelector('h2'); if (h && re.test(h.innerText)) return s.innerText.slice(0, 1600); } return ''; };
  const top = main.querySelector('section') ? main.querySelector('section').innerText.slice(0, 700) : '';
  return {top, about: sec(/^\s*About/i), experience: sec(/^\s*Experience/i), activity: sec(/^\s*Activity/i).slice(0, 700)};
}"""


def linkedin_profile(url):
    """Top card, About, Experience and recent activity of one profile (truncated, read-only). Stored with the person."""
    with session():
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


def browser_task(goal, start_url, max_pages=8):
    """Multi-step read-only browsing for the agent tool: the provider-agnostic browser loop (webagent) in read mode. Any model can drive it."""
    import webagent
    r = webagent.run(goal + "\nAnswer from what the pages show; include the page URLs you used.", start_url or "https://duckduckgo.com/", mode="read",
                     kind="browse", max_steps=max_pages, result_hint='{"answer": "", "sources": ["url"]}')
    res = r["result"]
    if isinstance(res, dict) and res.get("answer"):
        return str(res["answer"]) + ("\nSources: " + ", ".join(res.get("sources") or []) if res.get("sources") else "")
    return "Stopped before finding a definite answer: " + (json.dumps(res)[:1500] if not isinstance(res, str) else res[:1500])
