"""Feed adapters. Each returns a list of normalized job dicts:
{id, company, title, location, url, ats, posted, desc}
All sources are public JSON endpoints that the companies themselves publish for their job boards."""
import concurrent.futures as cf, datetime as dt, html, re, traceback
from common import http_json, http_get, log, SETTINGS, COMPANIES

TAG = re.compile(r"<[^>]+>")


def clean(s, n=6000):
    s = html.unescape(TAG.sub(" ", s or ""))
    return re.sub(r"\s+", " ", s).strip()[:n]


def _j(company, ats, jid, title, loc, url, posted, desc):
    return {"id": f"{ats}:{company['slug'] or company['name']}:{jid}", "company": company["name"], "title": (title or "").strip(),
            "location": (loc or "").strip(), "url": url, "ats": ats, "posted": posted or "", "desc": desc or ""}


def greenhouse(c):
    d = http_json(f"https://boards-api.greenhouse.io/v1/boards/{c['slug']}/jobs?content=true", timeout=60)
    out = []
    for j in d.get("jobs", []):
        out.append(_j(c, "greenhouse", j["id"], j.get("title"), (j.get("location") or {}).get("name"), j.get("absolute_url"),
                      (j.get("first_published") or j.get("updated_at") or "")[:10], clean(j.get("content"))))
    return out


def lever(c):
    d = http_json(f"https://api.lever.co/v0/postings/{c['slug']}?mode=json", timeout=40)
    out = []
    for j in d:
        cat = j.get("categories") or {}
        loc = cat.get("location") or ", ".join(cat.get("allLocations") or [])
        posted = ""
        if j.get("createdAt"):
            posted = dt.datetime.fromtimestamp(j["createdAt"] / 1000, dt.timezone.utc).date().isoformat()
        out.append(_j(c, "lever", j["id"], j.get("text"), loc, j.get("hostedUrl"), posted,
                      (j.get("descriptionPlain") or "") + " " + " ".join((l.get("text", "") + " " + clean(l.get("content", ""))) for l in j.get("lists", []))))
    return out


def ashby(c):
    d = http_json(f"https://api.ashbyhq.com/posting-api/job-board/{c['slug']}?includeCompensation=false", timeout=40)
    out = []
    for j in d.get("jobs", []):
        if j.get("isListed") is False:
            continue
        loc = j.get("location") or ""
        sec = [s.get("location") for s in (j.get("secondaryLocations") or []) if isinstance(s, dict)]
        if sec:
            loc += " | " + " | ".join(x for x in sec if x)
        if j.get("isRemote"):
            loc += " | Remote"
        out.append(_j(c, "ashby", j["id"], j.get("title"), loc, j.get("jobUrl"), (j.get("publishedAt") or "")[:10],
                      (j.get("descriptionPlain") or clean(j.get("descriptionHtml")))[:6000]))
    return out


def smartrecruiters(c):
    out, off = [], 0
    while off < 300:
        d = http_json(f"https://api.smartrecruiters.com/v1/companies/{c['slug']}/postings?limit=100&offset={off}", timeout=40)
        for j in d.get("content", []):
            l = j.get("location") or {}
            loc = ", ".join(x for x in (l.get("city"), l.get("country")) if x)
            out.append(_j(c, "smartrecruiters", j["id"], j.get("name"), loc,
                          f"https://jobs.smartrecruiters.com/{c['slug']}/{j['id']}", (j.get("releasedDate") or "")[:10], ""))
        off += 100
        if off >= d.get("totalFound", 0):
            break
    return out


ADAPTERS = {"greenhouse": greenhouse, "lever": lever, "ashby": ashby, "smartrecruiters": smartrecruiters}


# ----------------------------------------------------------- aggregated sources
WORKDAY = [
    {"name": "NVIDIA", "tier": "S", "category": "bigco", "host": "nvidia.wd5.myworkdayjobs.com", "tenant": "nvidia", "site": "NVIDIAExternalCareerSite"},
    {"name": "Intel", "tier": "B", "category": "bigco", "host": "intel.wd1.myworkdayjobs.com", "tenant": "intel", "site": "External"},
    {"name": "Salesforce", "tier": "C", "category": "bigco", "host": "salesforce.wd12.myworkdayjobs.com", "tenant": "salesforce", "site": "External_Career_Site"},
    {"name": "Adobe", "tier": "C", "category": "bigco", "host": "adobe.wd5.myworkdayjobs.com", "tenant": "adobe", "site": "external_experienced"},
    {"name": "Micron", "tier": "B", "category": "bigco", "host": "micron.wd1.myworkdayjobs.com", "tenant": "micron", "site": "External"},
    {"name": "Cisco", "tier": "B", "category": "bigco", "host": "cisco.wd5.myworkdayjobs.com", "tenant": "cisco", "site": "Cisco_Careers"},
    {"name": "HP", "tier": "C", "category": "bigco", "host": "hp.wd5.myworkdayjobs.com", "tenant": "hp", "site": "ExternalCareerSite"},
    {"name": "Capital One", "tier": "C", "category": "bigco", "host": "capitalone.wd12.myworkdayjobs.com", "tenant": "capitalone", "site": "Capital_One"},
    {"name": "Mastercard", "tier": "C", "category": "bigco", "host": "mastercard.wd1.myworkdayjobs.com", "tenant": "mastercard", "site": "CorporateCareers"},
    {"name": "Broadcom / VMware", "tier": "B", "category": "bigco", "host": "broadcom.wd1.myworkdayjobs.com", "tenant": "broadcom", "site": "External_Career"},
    {"name": "Samsung Electronics", "tier": "B", "category": "bigco", "host": "sec.wd3.myworkdayjobs.com", "tenant": "sec", "site": "Samsung_Careers"},
    {"name": "PayPal", "tier": "C", "category": "bigco", "host": "paypal.wd1.myworkdayjobs.com", "tenant": "paypal", "site": "jobs"},
    {"name": "Workday", "tier": "C", "category": "bigco", "host": "workday.wd5.myworkdayjobs.com", "tenant": "workday", "site": "Workday"},
    {"name": "Zoom", "tier": "C", "category": "bigco", "host": "zoom.wd5.myworkdayjobs.com", "tenant": "zoom", "site": "Zoom"},
    {"name": "Cadence", "tier": "B", "category": "bigco", "host": "cadence.wd1.myworkdayjobs.com", "tenant": "cadence", "site": "External_Careers"},
]
WD_QUERIES = ["intern", "new college graduate", "university graduate", "machine learning", "deep learning inference", "cuda", "software engineer intern", "research"]


def workday(w):
    seen, out = set(), []
    for q in WD_QUERIES:
        try:
            d = http_json(f"https://{w['host']}/wday/cxs/{w['tenant']}/{w['site']}/jobs",
                          data={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": q}, timeout=40)
        except Exception as e:
            log(f"workday {w['name']} q={q}: {e}")
            continue
        for j in d.get("jobPostings", []):
            p = j.get("externalPath", "")
            if p in seen:
                continue
            seen.add(p)
            jid = p.rsplit("_", 1)[-1] if "_" in p else p
            out.append({"id": f"workday:{w['tenant']}:{jid}", "company": w["name"], "title": j.get("title", ""),
                        "location": j.get("locationsText", ""), "url": f"https://{w['host']}/{w['site']}{p}",
                        "ats": "workday", "posted": j.get("postedOn", ""), "desc": ""})
    return out


def amazon():
    out, seen = [], set()
    import common as _c
    homeq = ["machine learning intern", "applied scientist intern", "software development engineer intern", "software development engineer university graduate",
                                    "applied scientist", "research scientist", "deep learning", "inference", "data scientist"]
    cs = _c.SETTINGS.get("amazon_countries") or ["USA"]
    combos = [(cs[0], q) for q in homeq] + [(c, q) for c in cs[1:] for q in ["machine learning intern", "applied scientist intern", "software development engineer intern", "research scientist intern"]]
    for cc, q in combos:
        try:
            d = http_json(f"https://www.amazon.jobs/en/search.json?result_limit=25&country%5B%5D={cc}&base_query=" + q.replace(" ", "+"), timeout=40)
        except Exception as e:
            log(f"amazon q={q}: {e}")
            continue
        for j in d.get("jobs", []):
            if j["id_icims"] in seen:
                continue
            seen.add(j["id_icims"])
            desc = clean((j.get("description") or "") + " " + (j.get("basic_qualifications") or "") + " " + (j.get("preferred_qualifications") or ""))
            out.append({"id": f"amazon:{j['id_icims']}", "company": "Amazon", "title": j.get("title", ""), "location": j.get("normalized_location") or j.get("location", ""),
                        "url": "https://www.amazon.jobs" + j.get("job_path", ""), "ats": "amazon", "posted": j.get("posted_date", ""), "desc": desc})
    return out


def github_list(url, label):
    """SimplifyJobs community lists: big-tech / quant / startup internships and new-grad roles."""
    days = SETTINGS.get("recent_days_github_lists", 30)
    cutoff = (dt.datetime.now() - dt.timedelta(days=days)).timestamp()
    d = http_json(url, timeout=120)
    out = []
    for j in d:
        if not j.get("active", True) or j.get("is_visible") is False:
            continue
        if (j.get("date_posted") or 0) < cutoff:
            continue
        locs = j.get("locations") or []
        out.append({"id": f"{label}:{j.get('id') or j.get('url')}", "company": j.get("company_name", ""), "title": j.get("title", ""),
                    "location": " | ".join(locs) if isinstance(locs, list) else str(locs), "url": j.get("url", ""), "ats": label,
                    "posted": dt.datetime.fromtimestamp(j["date_posted"]).date().isoformat() if j.get("date_posted") else "",
                    "desc": f"Sponsorship: {j.get('sponsorship','?')}. Terms: {j.get('terms','')}"})
    return out


def fetch_all():
    """Returns (jobs, health) where health maps source->(count or error)."""
    jobs, health = [], {}

    def one(c):
        try:
            return c, ADAPTERS[c["ats"]](c), None
        except Exception as e:
            return c, [], f"{type(e).__name__}: {e}"

    ats_companies = [c for c in COMPANIES if c.get("ats") in ADAPTERS and c.get("slug")]
    with cf.ThreadPoolExecutor(8) as ex:
        for c, js, err in ex.map(one, ats_companies):
            for j in js:
                j["_tier"], j["_cat"] = c["tier"], c["category"]
            jobs += js
            health[c["name"]] = err or len(js)
    tiers = {c["name"].lower(): (c["tier"], c["category"]) for c in COMPANIES}

    import sources_big as _B
    extra = [(w["name"], (lambda w=w: workday(w))) for w in WORKDAY]
    extra += [(f"{e[0]} (careers site)", (lambda e=e: _B.eightfold(*e))) for e in _B.EIGHTFOLD]
    extra += [("Google (careers site)", _B.google_careers), ("Atlassian", _B.atlassian), ("Hugging Face", lambda: _B.workable("Hugging Face", "huggingface"))]
    extra += [("Amazon", amazon),
             ("simplify-intern", lambda: github_list("https://raw.githubusercontent.com/SimplifyJobs/Summer2027-Internships/dev/.github/scripts/listings.json", "simplify-intern"))]
    if SETTINGS.get("include_new_grad_list"):
        extra.append(("simplify-newgrad", lambda: github_list("https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json", "simplify-newgrad")))
    import sources_extra as X
    extra += [("yc", X.yc_jobs), ("hn", X.hn_jobs), ("vc-boards", X.getro), ("remotive", X.remotive), ("remoteok", X.remoteok), ("speedyapply", X.speedyapply)]
    src_tier = {"yc": ("B", "startup"), "hn-jobs": ("B", "startup"), "hn-whoishiring": ("B", "startup"), "vc-board": ("B", "startup"),
                "remotive": ("C", "remote-board"), "remoteok": ("C", "remote-board")}
    wd_meta = {w["name"]: (w["tier"], w["category"]) for w in WORKDAY}
    wd_meta["Amazon"] = ("A", "bigco")
    wd_meta.update({e[0]: (e[1], "bigco") for e in _B.EIGHTFOLD})
    wd_meta.update({"Google": ("S", "bigco"), "Atlassian": ("B", "bigco"), "Hugging Face": ("A", "neolab")})
    for name, fn in extra:
        try:
            js = fn()
            for j in js:
                t = wd_meta.get(j["company"]) or tiers.get(j["company"].lower()) or src_tier.get(j["ats"]) or ("C", "bigco")
                j["_tier"], j["_cat"] = t
            jobs += js
            health[name] = len(js)
        except Exception as e:
            health[name] = f"{type(e).__name__}: {e}"
            log(f"source {name} failed: {e}")
    return jobs, health
