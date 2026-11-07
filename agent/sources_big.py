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
