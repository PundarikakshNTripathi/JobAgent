"""Official career pages of big tech / large companies that have no Greenhouse/Lever/Ashby board:
Eightfold PCSX (Microsoft, NVIDIA, Qualcomm, Micron, Netflix), Atlassian listings JSON, Workable v3 (Hugging Face).
Google, Apple, Meta, Uber and Oracle expose no stable public feed; the web scout and the dashboard's manual links cover them."""
import datetime as dt
from common import http_json
from sources_extra import _text

EIGHTFOLD = [  # (name, tier, host, domain, api)
    ("Microsoft", "A", "apply.careers.microsoft.com", "microsoft.com", "pcsx"),
    ("NVIDIA", "S", "nvidia.eightfold.ai", "nvidia.com", "pcsx"),
    ("Qualcomm", "A", "qualcomm.eightfold.ai", "qualcomm.com", "pcsx"),
    ("Micron", "B", "micron.eightfold.ai", "micron.com", "pcsx"),
    ("Netflix", "B", "explore.jobs.netflix.net", "netflix.com", "apply_v2"),
]
EF_QUERIES = ["machine learning", "intern", "new graduate", "applied scientist", "cuda gpu", "research", "software engineer", "data scientist"]


def eightfold(name, tier, host, domain, api):
    out, seen = [], set()
    for q in EF_QUERIES:
        for loc in ("", "India"):
            for start in (0, 10, 20):
                qs = f"domain={domain}&query={q.replace(' ', '%20')}&start={start}" + (f"&location={loc}" if loc else "")
                url = (f"https://{host}/api/pcsx/search?{qs}&sort_by=relevance" if api == "pcsx" else f"https://{host}/api/apply/v2/jobs?{qs}&num=10")
                try:
                    j = http_json(url, timeout=30)
                except Exception:
                    break
                d = j.get("data", j)
                pos = d.get("positions") or []
                if not pos:
                    break
                for p in pos:
                    pid = str(p.get("id"))
                    if pid in seen:
                        continue
                    seen.add(pid)
                    locs = p.get("locations") or ([p["location"]] if p.get("location") else [])
                    ts = p.get("postedTs") or p.get("t_create") or 0
                    out.append({"id": f"eightfold:{domain}:{pid}", "company": name, "title": p.get("name", ""),
                                "location": " | ".join(locs)[:200] + (" | Remote" if p.get("workLocationOption") == "remote" else ""),
                                "url": f"https://{host}/careers/job/{pid}", "ats": "eightfold",
                                "posted": dt.datetime.fromtimestamp(ts).date().isoformat() if ts else "",
                                "desc": f"{p.get('department', '')}. {p.get('workLocationOption', '')}"})
    return out


def atlassian():
    j = http_json("https://www.atlassian.com/endpoint/careers/listings", timeout=60)
    out = []
    for p in j:
        pp = p.get("portalJobPost") or {}
        out.append({"id": f"atlassian:{p['id']}", "company": "Atlassian", "title": p.get("title", ""), "location": " | ".join(p.get("locations") or [])[:200],
                    "url": pp.get("portalUrl") or f"https://www.atlassian.com/company/careers/details/{p['id']}", "ats": "atlassian",
                    "posted": (pp.get("updatedDate") or "")[:10],
                    "desc": _text(p.get("overview"), 1500) + " " + _text(p.get("responsibilities"), 1000) + " " + _text(p.get("qualifications"), 1500)})
    return out


def workable(name, slug):
    d = http_json(f"https://apply.workable.com/api/v3/accounts/{slug}/jobs", data={"query": "", "location": [], "department": [], "worktype": [], "remote": []}, timeout=40)
    out = []
    for j in d.get("results", []):
        loc = j.get("location") or {}
        out.append({"id": f"workable:{slug}:{j['shortcode']}", "company": name, "title": j.get("title", ""),
                    "location": ", ".join(x for x in (loc.get("city"), loc.get("country")) if x) + (" | Remote" if j.get("remote") else ""),
                    "url": f"https://apply.workable.com/{slug}/j/{j['shortcode']}/", "ats": "workable", "posted": (j.get("published") or "")[:10], "desc": ""})
    return out


# ------------------------------------------------------------------ Google Careers (data embedded in the results page)
GOOGLE_QUERIES = {"India": ["machine learning", "software engineer", "research", "systems infrastructure"],
                  "Singapore": ["machine learning"], "United Kingdom": ["machine learning"], "Switzerland": ["machine learning"], "Germany": ["machine learning"],
                  "Canada": ["machine learning"], "United States": ["machine learning intern"]}
GOOGLE_LEVELS = ["INTERN_AND_APPRENTICE", "EARLY"]


def google_careers():
    import json as _json, re as _re, urllib.parse as _up
    from common import http_get
    out, seen = [], set()
    from common import SETTINGS
    for loc, qs in SETTINGS.get("google_locations") or GOOGLE_QUERIES.items():
        for q in qs:
            for lvl in GOOGLE_LEVELS:
                for page in (1, 2):
                    url = f"https://www.google.com/about/careers/applications/jobs/results/?q={_up.quote(q)}&location={_up.quote(loc)}&target_level={lvl}&page={page}"
                    try:
                        h = http_get(url, timeout=40, headers={"Accept-Language": "en", "Accept": "text/html"})
                    except Exception:
                        break
                    m = _re.search(r"AF_initDataCallback\(\{key: 'ds:1'.*?data:(.*?), sideChannel", h, _re.S)
                    if not m:
                        break
                    try:
                        d = _json.loads(m.group(1))
                    except ValueError:
                        break
                    jobs = d[0] or []
                    for j in jobs:
                        jid = str(j[0])
                        if jid in seen:
                            continue
                        seen.add(jid)
                        locs = [x[0] for x in (j[9] or []) if x]
                        slug = _re.sub(r"[^a-z0-9]+", "-", (j[1] or "").lower()).strip("-")
                        dates = _text((j[15] or [None, ""])[1], 200) if len(j) > 15 and j[15] else ""
                        out.append({"id": f"google:{jid}", "company": "Google", "title": j[1], "location": " | ".join(locs)[:200],
                                    "url": f"https://www.google.com/about/careers/applications/jobs/results/{jid}-{slug}", "ats": "google",
                                    "posted": dt.datetime.fromtimestamp(j[12][0]).date().isoformat() if len(j) > 12 and j[12] else "",
                                    "desc": dates + " " + _text((j[3] or [None, ""])[1], 1200) + " " + _text((j[4] or [None, ""])[1], 1800)})
                    if len(jobs) < (d[3] or 20):
                        break
    return out
