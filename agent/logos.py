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


def candidates(company, url=""):
    base = _SUFFIX.sub("", company or "").strip()
    out = []
    k = KNOWN.get((company or "").lower().strip()) or KNOWN.get(base.lower())
    if k:
        out.append(k)
    if url:
        host = urllib.parse.urlparse(url).netloc.split(":")[0]
        if host and not any(host.endswith(h) for h in ATS_HOSTS):
            out.append(_registrable(host))
    s = _slug(base)
    if s:
        out += [s + ".com", s + ".ai", s + ".io", s + ".co", s + ".org"]
    seen, res = set(), []
    for d in out:
        if d not in seen:
            seen.add(d)
            res.append(d)
    return res[:6]


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


def logo_for(company, url=""):
    """Return (bytes, content_type) or None. Cached on disk (positive and negative)."""
    if not SETTINGS.get("ui", {}).get("logos", True) or not company:
        return None
    key = _slug(_SUFFIX.sub("", company)) or "x"
    img, none = os.path.join(CACHE, key + ".img"), os.path.join(CACHE, key + ".miss")
    if os.path.exists(img):
        b = open(img, "rb").read()
        return b, sniff(b)
    if os.path.exists(none) and time.time() - os.path.getmtime(none) < 7 * 86400:
        return None
    with _lock:
        if os.path.exists(img):
            b = open(img, "rb").read()
            return b, sniff(b)
        for d in candidates(company, url):
            b = _icon(d)
            if b:
                open(img, "wb").write(b)
                return b, sniff(b)
        open(none, "w").write("none")
    return None
