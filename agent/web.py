"""Provider-agnostic web access: fetch a page as clean text, run a web search. Lets ANY model (even text-only CLIs and APIs) do research,
because JobAgent retrieves the pages itself and hands the model the notes. Search backends: DuckDuckGo HTML (no key) or Brave Search API (key in env)."""
import html, json, os, re, urllib.parse, urllib.request
from common import SETTINGS, DATA, log

_UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36", "Accept-Language": "en"}
_STRIP = [(re.compile(r"(?is)<(script|style|noscript|svg|template|header|footer|nav)[^>]*>.*?</\1>"), " "), (re.compile(r"(?s)<!--.*?-->"), " ")]


def _get(url, timeout=25, data=None, headers=None):
    rq = urllib.request.Request(url, data=data, headers={**_UA, **(headers or {})})
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        raw = r.read(2_500_000)
        enc = r.headers.get_content_charset() or "utf-8"
        return raw.decode(enc, "replace"), r.headers.get("Content-Type", "")


def to_text(h):
    import websafe
    h = websafe.strip_hidden_html(h)  # hidden text is a classic place for instructions aimed at AI readers
    for rx, rep in _STRIP:
        h = rx.sub(rep, h)
    h = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d|tr)>", "\n", h)
    t = html.unescape(re.sub(r"<[^>]+>", " ", h))
    t = re.sub(r"[ \t\r\f\v]+", " ", t)
    return re.sub(r"\n\s*\n+", "\n", t).strip()


def fetch_text(url, max_chars=None):
    """Return (title, text) for a URL. JSON-LD JobPosting blocks are preferred when present (best signal on JS-heavy career pages)."""
    max_chars = max_chars or SETTINGS["web"].get("page_chars", 3500)
    try:
        body, ctype = _get(url)
    except Exception as e:
        return "", f"(fetch failed: {e})"
    title = (re.search(r"(?is)<title[^>]*>(.*?)</title>", body) or [None, ""])[1]
    extra = ""
    for m in re.finditer(r'(?is)<script[^>]+application/ld\+json[^>]*>(.*?)</script>', body):
        try:
            j = json.loads(m.group(1))
        except ValueError:
            continue
        for node in (j if isinstance(j, list) else [j]):
            if isinstance(node, dict) and "JobPosting" in str(node.get("@type")):
                extra += f"\n[JobPosting] {node.get('title','')} | {node.get('hiringOrganization',{}).get('name','') if isinstance(node.get('hiringOrganization'), dict) else ''} | " \
                         f"{json.dumps(node.get('jobLocation'))[:300]} | {to_text(str(node.get('description','')))[:2500]}"
    text = (extra + "\n" + to_text(body)) if "html" in ctype or "<html" in body[:500].lower() else body
    import websafe
    text, _ = websafe.sanitize(text, url)
    return websafe.clean(html.unescape(title)).strip()[:300], text.strip()[:max_chars]


def search(query, n=5):
    """List of {title, url, snippet}. Never raises; returns [] on failure."""
    cfg = SETTINGS["web"]
    try:
        key = os.environ.get(cfg.get("brave_api_key_env", ""), "")
        if cfg.get("search_backend") == "brave" and key:
            body, _ = _get("https://api.search.brave.com/res/v1/web/search?q=" + urllib.parse.quote(query) + f"&count={n}", headers={"X-Subscription-Token": key, "Accept": "application/json"})
            return [{"title": r["title"], "url": r["url"], "snippet": r.get("description", "")} for r in json.loads(body).get("web", {}).get("results", [])[:n]]
        body, _ = _get("https://html.duckduckgo.com/html/", data=urllib.parse.urlencode({"q": query}).encode())
        out = []
        for m in re.finditer(r'(?is)<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>.*?(?:class="result__snippet"[^>]*>(.*?)</(?:a|td|div)>)?', body):
            href = html.unescape(m.group(1))
            q = urllib.parse.parse_qs(urllib.parse.urlparse(href).query)
            url = q["uddg"][0] if "uddg" in q else href
            if url.startswith("//"):
                url = "https:" + url
            out.append({"title": to_text(m.group(2)), "url": url, "snippet": to_text(m.group(3) or "")})
            if len(out) >= n:
                break
        return out
    except Exception as e:
        log(f"search failed for {query!r}: {e}")
        return []


def research_notes(queries=(), urls=(), max_pages=None):
    """Run searches + fetches and return one text block of notes (with source URLs) for a model that has no browsing tools."""
    max_pages = max_pages or SETTINGS["web"].get("max_pages", 6)
    seen, blocks = set(), []
    targets = [(u, "") for u in urls]
    for q in queries:
        for r in search(q, 4):
            blocks.append(f"SEARCH '{q}': {r['title']} - {r['url']} - {r['snippet'][:200]}")
            targets.append((r["url"], r["title"]))
    pages = 0
    for url, _ in targets:
        if url in seen or pages >= max_pages:
            continue
        seen.add(url)
        title, text = fetch_text(url)
        if text and not text.startswith("(fetch failed"):
            import websafe
            blocks.append(f"PAGE {url}\nTITLE: {title}\n" + websafe.wrap(url, text))
            pages += 1
    return "\n\n".join(blocks)[:60000]
