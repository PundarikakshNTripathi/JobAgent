"""Company logos for the GUI. The local server resolves a company's domain, fetches its icon ONCE through a favicon service, and caches it in the workspace
(data/logos). The browser only ever talks to localhost; only company domain names leave the machine. Disable with settings ui.logos=false."""
import hashlib, os, re, threading, time, urllib.parse, urllib.request
from common import DATA, SETTINGS

CACHE = os.path.join(DATA, "logos")
os.makedirs(CACHE, exist_ok=True)
_lock = threading.Lock()
_default_hash = {}
ATS_HOSTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "myworkdayjobs.com", "smartrecruiters.com", "workable.com", "icims.com", "eightfold.ai", "getro.com", "ycombinator.com",
             "workatastartup.com", "github.com", "linkedin.com", "recruitee.com", "jobvite.com", "taleo.net", "bamboohr.com", "teamtailor.com", "tally.so", "notion.site", "google.com",
             "metacareers.com", "amazon.jobs", "jobs.apple.com", "careers.microsoft.com", "apply.workable.com", "adobe.com", "careers-apac-atlassian.icims.com")
KNOWN = {
    "google": "google.com", "google deepmind / google": "deepmind.google", "google deepmind": "deepmind.google", "microsoft": "microsoft.com", "microsoft research": "microsoft.com",
    "microsoft research india": "microsoft.com", "meta": "meta.com", "amazon": "amazon.com", "nvidia": "nvidia.com", "intel": "intel.com", "qualcomm": "qualcomm.com", "micron": "micron.com",
    "samsung electronics": "samsung.com", "broadcom / vmware": "broadcom.com", "netflix": "netflix.com", "atlassian": "atlassian.com", "cisco": "cisco.com", "hp": "hp.com",
    "capital one": "capitalone.com", "mastercard": "mastercard.com", "paypal": "paypal.com", "workday": "workday.com", "salesforce": "salesforce.com", "adobe": "adobe.com",
    "openai": "openai.com", "anthropic": "anthropic.com", "xai": "x.ai", "mistral ai": "mistral.ai", "cohere": "cohere.com", "perplexity": "perplexity.ai", "together ai": "together.ai",
    "thinking machines lab": "thinkingmachines.ai", "character.ai": "character.ai", "cursor (anysphere)": "cursor.com", "cognition": "cognition.ai", "scale ai": "scale.com",
    "fireworks ai": "fireworks.ai", "baseten": "baseten.co", "modal": "modal.com", "etched": "etched.com", "inferact (vllm)": "inferact.ai", "radixark (sglang)": "radixark.ai",
    "lambda": "lambda.ai", "coreweave": "coreweave.com", "nebius": "nebius.com", "crusoe": "crusoe.ai", "cerebras": "cerebras.ai", "tenstorrent": "tenstorrent.com",
    "d-matrix": "d-matrix.ai", "sambanova": "sambanova.ai", "matx": "matx.com", "goodfire": "goodfire.ai", "apollo research": "apolloresearch.ai", "metr": "metr.org",
    "redwood research": "redwoodresearch.org", "center for ai safety": "safe.ai", "far.ai": "far.ai", "uk ai security institute": "aisi.gov.uk", "epoch ai": "epoch.ai",
    "hugging face": "huggingface.co", "jane street": "janestreet.com", "imc trading": "imc.com", "jump trading": "jumptrading.com", "sarvam ai": "sarvam.ai", "cred": "cred.club",
    "meesho": "meesho.io", "freshworks": "freshworks.com", "databricks": "databricks.com", "stripe": "stripe.com", "snowflake": "snowflake.com", "mats": "matsprogram.org",
    "spar / kairos": "sparai.org", "pivotal": "pivotal-research.org", "centre for the governance of ai": "governance.ai", "era": "erafellowship.org", "hackerrank": "hackerrank.com",
    "elevenlabs": "elevenlabs.io", "black forest labs": "blackforestlabs.ai", "reflection ai": "reflection.ai", "periodic labs": "periodic.com", "prime intellect": "primeintellect.ai",
    "physical intelligence": "physicalintelligence.company", "liquid ai": "liquid.ai", "isomorphic labs": "isomorphiclabs.com", "palantir": "palantir.com", "figure ai": "figure.ai",
}
_SUFFIX = re.compile(r"\s*\((yc [a-z]\d+|.*?)\)|\b(inc|llc|ltd|corp|corporation|limited|pvt|gmbh|co)\b\.?", re.I)


def _slug(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _registrable(host):
    parts = host.lower().split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "gov", "ac") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


# job-board hosts: their favicon is the board's, never the employer's
ATS_HOSTS = ATS_HOSTS + ("applytojob.com", "jazzhr.com", "breezy.hr", "recruitee.com", "personio.de", "personio.com", "zohorecruit.com", "freshteam.com",
                         "ashbyhq.com", "dover.com", "wellfound.com", "angel.co", "rippling.com", "paylocity.com", "ultipro.com", "ukg.com", "oraclecloud.com",
                         "successfactors.com", "successfactors.eu", "jobvite.com", "workable.com", "pinpointhq.com", "trakstar.com", "comeet.com", "hire.trakstar.com",
                         "lifeattiktok.com", "metacareers.com", "careers.google.com", "naukri.com", "instahyre.com", "cutshort.io", "talent.com", "glassdoor.com",
                         "indeed.com", "simplify.jobs", "otta.com", "builtin.com", "remoteok.com", "remotive.com", "weworkremotely.com")
_BOARD = [  # (host regex, path regex): where the employer's own slug lives in a job-board URL
    (r"(?:job-)?boards(?:-api)?\.greenhouse\.io$", r"^/(?:embed/job_app\?for=)?([\w-]+)"), (r"jobs\.ashbyhq\.com$", r"^/([\w.-]+)"), (r"jobs\.(?:eu\.)?lever\.co$", r"^/([\w-]+)"),
    (r"apply\.workable\.com$", r"^/([\w-]+)"), (r"jobs\.smartrecruiters\.com$", r"^/([\w-]+)"), (r"^([\w-]+)\.wd\d+\.myworkdayjobs\.com$", None),
    (r"^([\w-]+)\.(?:applytojob|breezy|recruitee|bamboohr|teamtailor|jobs\.personio)\.(?:com|hr|de)$", None), (r"^careers-([\w-]+)\.icims\.com$", None),
]


def board_slug(url):
    """The employer's slug on a job board ('hookmusic' in jobs.ashbyhq.com/hookmusic/...), or ''."""
    u = urllib.parse.urlparse(url or "")
    host = u.netloc.lower().split(":")[0]
    for hrx, prx in _BOARD:
        m = re.search(hrx, host)
        if m:
            if prx is None:
                return m.group(1).lower()
            m2 = re.search(prx, u.path + ("?" + u.query if u.query else ""))
            return m2.group(1).lower() if m2 else ""
    return ""


def _known(company):
    """KNOWN by full name, by the name without suffixes, or by each part of a 'Parent / Unit' style name."""
    base = _SUFFIX.sub("", company or "").strip().lower()
    for k in [(company or "").lower().strip(), base] + [x.strip() for x in re.split(r"[/|,]", base) if x.strip()]:
        if k in KNOWN:
            return KNOWN[k]
    return ""


def candidates(company, url=""):
    """[(domain, trusted)] best first. Trusted: stated by a feed, known, or the employer's own job-page host. Untrusted guesses (slug.com ...)
    are only used if the site's own title names the company (see _plausible)."""
    base = _SUFFIX.sub("", company or "").strip()
    out = []
    dom, _ = _meta(company)
    if dom:
        out.append((_registrable(dom), True))
    k = _known(company)
    if k:
        out.append((k, True))
    host = urllib.parse.urlparse(url or "").netloc.lower().split(":")[0]
    if host and not any(host == h or host.endswith("." + h) for h in ATS_HOSTS):
        out.append((_registrable(host), True))
    s, b = _slug(base), _slug(board_slug(url))
    guesses = []
    if b and b != s:
        guesses += [b + t for t in (".com", ".ai", ".co", ".io")]
    if s and not (b and b != s and not b.startswith(s)):  # 'hookmusic' on the board: don't guess plain hook.com
        guesses += [s + t for t in (".com", ".ai", ".io", ".co", ".org")]
    out += [(g, False) for g in guesses]
    seen, res = set(), []
    for d, t in out:
        if d not in seen:
            seen.add(d)
            res.append((d, t))
    return res[:8]


_VERIFY = {}


def _plausible(domain, company, url=""):
    """A guessed domain counts only if its home page's title or site name names the company (or the job board's slug for it)."""
    if domain in _VERIFY:
        return _VERIFY[domain]
    try:
        body = _get("https://" + domain)[:200000].decode("utf-8", "replace")
    except Exception:
        _VERIFY[domain] = None  # unreachable: unknown, not a verdict
        return None
    title = " ".join(re.findall(r"(?is)<title[^>]*>(.*?)</title>|property=[\"']og:site_name[\"'][^>]*content=[\"']([^\"']+)", body).__str__().split())
    t = _slug(title)
    names = {_slug(_SUFFIX.sub("", company or "")), _slug(board_slug(url))} - {""}
    words = {w for w in re.findall(r"[a-z0-9]+", _SUFFIX.sub("", company or "").lower()) if len(w) >= 4}
    ok = any(n in t for n in names) or (bool(words) and all(w in t for w in words))
    _VERIFY[domain] = ok
    return ok


def _get(u):
    with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0"}), timeout=8) as r:
        return r.read()


def sniff(b):
    """Content type from magic bytes (the icon services return PNG or ICO)."""
    if b[:4] == b"\x89PNG":
        return "image/png"
    if b[:4] == b"\x00\x00\x01\x00":
        return "image/x-icon"
    if b[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if b[:5] in (b"<?xml", b"<svg ") or b[:4] == b"<svg":
        return "image/svg+xml"
    return "image/png"


def _globe_hash():
    if "h" not in _default_hash:
        try:
            _default_hash["h"] = hashlib.sha1(_get("https://www.google.com/s2/favicons?sz=128&domain=zz-no-such-domain-jobagent-test.invalid")).hexdigest()
        except Exception:
            _default_hash["h"] = ""
    return _default_hash["h"]


def _icon(domain):
    """DuckDuckGo's icon service first (empty reply for unknown domains), Google's favicon service as a second source (generic globe filtered out)."""
    try:
        b = _get("https://icons.duckduckgo.com/ip3/" + urllib.parse.quote(domain) + ".ico")
        if len(b) > 200:
            return b
    except Exception:
        pass
    try:
        b = _get("https://www.google.com/s2/favicons?sz=128&domain=" + urllib.parse.quote(domain))
        if len(b) > 600 and hashlib.sha1(b).hexdigest() != _globe_hash():
            return b
    except Exception:
        pass
    return None


def _meta(company):
    """Website domain / official logo URL that a feed stated for this company (company_meta table), if any."""
    try:
        from common import db, company_key
        r = db().execute("SELECT domain, logo_url FROM company_meta WHERE k=?", (company_key(company),)).fetchone()
        return (r["domain"] or "", r["logo_url"] or "") if r else ("", "")
    except Exception:
        return "", ""


def _key(company):
    return _slug(_SUFFIX.sub("", company or "")) or "x"


def forget(company):
    """Drop the cached logo (and the cached miss) so the next request resolves it again."""
    for ext in (".img", ".miss"):
        try:
            os.remove(os.path.join(CACHE, _key(company) + ext))
        except OSError:
            pass


_locks = {}


def logo_for(company, url=""):
    """Return (bytes, content_type) or None. Cached on disk. A miss is cached only when every lookup answered (a network failure is not
    a 'no logo'), and for one day."""
    if not SETTINGS.get("ui", {}).get("logos", True) or not company:
        return None
    key = _key(company)
    img, none = os.path.join(CACHE, key + ".img"), os.path.join(CACHE, key + ".miss")
    if os.path.exists(img):
        b = open(img, "rb").read()
        return b, sniff(b)
    if os.path.exists(none) and time.time() - os.path.getmtime(none) < 86400:
        return None
    with _lock:
        lk = _locks.setdefault(key, threading.Lock())
    with lk:  # one resolver per company; different companies resolve in parallel
        if os.path.exists(img):
            b = open(img, "rb").read()
            return b, sniff(b)
        _, official = _meta(company)
        if official:  # the company's own logo as published by the job board (e.g. YC)
            try:
                b = _get(official)
                if len(b) > 200:
                    open(img, "wb").write(b)
                    return b, sniff(b)
            except Exception:
                pass
        unsure = False
        for d, trusted in candidates(company, url):
            if not trusted:
                v = _plausible(d, company, url)
                if v is None:
                    unsure = True
                if not v:
                    continue
            b = _icon(d)
            if b:
                open(img, "wb").write(b)
                return b, sniff(b)
        if not unsure:
            open(none, "w").write("none")
    return None
