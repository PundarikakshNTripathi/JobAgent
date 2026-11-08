"""Extra / underrated sources beyond company ATS boards:
YC (role pages, include visa + min-experience), Hacker News (job stories + monthly Who's Hiring),
VC portfolio job boards (Getro-powered), Remotive, RemoteOK, speedyapply GitHub lists (AI + SWE college jobs)."""
import datetime as dt, hashlib, html, json, re
from common import http_get, http_json, log

TAGS = re.compile(r"<[^>]+>")


def _text(s, n=4000):
    return re.sub(r"\s+", " ", html.unescape(TAGS.sub(" ", s or ""))).strip()[:n]


# ------------------------------------------------------------------ YC
YC_ROLES = ["machine-learning-engineer", "ai-engineer", "research-engineer", "research-scientist", "data-scientist",
            "software-engineer", "backend-engineer", "full-stack-engineer", "intern", "founding-engineer", "robotics-engineer", "devops-engineer", "data-engineer"]
YC_LOCS = ["", "/remote", "/india", "/san-francisco", "/new-york", "/seattle", "/boston", "/austin", "/los-angeles", "/london"]


def yc_jobs():
    from common import db, meta_get, meta_set, now
    c = db()
    last = meta_get(c, "last_yc")
    if last and (dt.datetime.now() - dt.datetime.fromisoformat(last)).total_seconds() < 6 * 3600:
        return []  # YC pages are large; refresh every 6 hours
    meta_set(c, "last_yc", now())
    out, seen = [], set()
    for role in YC_ROLES:
        for loc in YC_LOCS:
            try:
                b = http_get(f"https://www.ycombinator.com/jobs/role/{role}{loc}", timeout=30)
            except Exception as e:
                log(f"yc {role}{loc}: {e}")
                continue
            m = re.search(r'data-page="([^"]+)"', b)
            if not m:
                continue
            try:
                posts = json.loads(html.unescape(m.group(1)))["props"].get("jobPostings") or []
            except (json.JSONDecodeError, KeyError):
                continue
            for p in posts:
                if p["id"] in seen:
                    continue
                seen.add(p["id"])
                desc = (f"YC {p.get('companyBatchName','')} startup: {p.get('companyOneLiner','')}. Type: {p.get('type','')}. "
                        f"Min experience: {p.get('minExperience')}. Visa: {p.get('visa')}. Skills: {', '.join(p.get('skills') or [])}. "
                        f"Salary: {p.get('salaryRange')}. Equity: {p.get('equityRange')}.")
                out.append({"id": f"yc:{p['id']}", "company": f"{p.get('companyName','?')} (YC {p.get('companyBatchName','')})",
                            "title": p.get("title", ""), "location": p.get("location", "") + (" | Remote" if "/remote" in loc else ""),
                            "url": "https://www.ycombinator.com" + p.get("url", ""), "ats": "yc", "posted": "", "desc": desc,
                            "_remote_ok": "/remote" in loc})
    return out


# ------------------------------------------------------------------ Hacker News
def hn_jobs():
    """YC 'jobs' front-page posts + comments of the latest 'Ask HN: Who is hiring?' thread."""
    out = []
    ids = http_json("https://hacker-news.firebaseio.com/v0/jobstories.json", timeout=30)[:40]
    for i in ids:
        try:
            it = http_json(f"https://hacker-news.firebaseio.com/v0/item/{i}.json", timeout=20)
        except Exception:
            continue
        if not it or it.get("dead") or it.get("deleted"):
            continue
        out.append({"id": f"hnjob:{i}", "company": (it.get("title", "").split(" is hiring")[0].split(" (YC")[0])[:60] or "HN job",
                    "title": it.get("title", ""), "location": "", "url": it.get("url") or f"https://news.ycombinator.com/item?id={i}",
                    "ats": "hn-jobs", "posted": dt.datetime.fromtimestamp(it.get("time", 0)).date().isoformat(), "desc": _text(it.get("text", ""))})
    # monthly thread
    s = http_json("https://hn.algolia.com/api/v1/search_by_date?tags=story,author_whoishiring&hitsPerPage=6", timeout=30)
    thread = next((h for h in s.get("hits", []) if "who is hiring" in (h.get("title") or "").lower()), None)
    if thread:
        page = 0
        while page < 6:
            d = http_json(f"https://hn.algolia.com/api/v1/search?tags=comment,story_{thread['objectID']}&hitsPerPage=500&page={page}", timeout=40)
            for h in d.get("hits", []):
                if h.get("parent_id") != int(thread["objectID"]):
                    continue
                txt = _text((h.get("comment_text") or "").replace("<p>", " \n "), 3000)
                head = txt.split(" | ")
                company = head[0][:60]
                title = " | ".join(head[:4])[:160]
                out.append({"id": f"hnwh:{h['objectID']}", "company": company, "title": title,
                            "location": " | ".join(head[1:4])[:100], "url": f"https://news.ycombinator.com/item?id={h['objectID']}",
                            "ats": "hn-whoishiring", "posted": (h.get("created_at") or "")[:10], "desc": txt})
            page += 1
            if page >= d.get("nbPages", 1):
                break
    return out


# ------------------------------------------------------------------ VC boards (Getro)
GETRO_BOARDS = ["jobs.generalcatalyst.com", "jobs.accel.com", "jobs.khoslaventures.com", "jobs.thrivecap.com", "jobs.insightpartners.com", "jobs.nfx.com"]
GETRO_QUERIES = ["machine learning", "research engineer", "research scientist", "intern", "new grad", "inference", "gpu", "ai safety", "software engineer intern"]


def getro():
    out, seen = [], set()
    for host in GETRO_BOARDS:
        for q in GETRO_QUERIES:
            try:
                b = http_get(f"https://{host}/jobs?q={q.replace(' ', '+')}", timeout=30)
            except Exception as e:
                log(f"getro {host}: {e}")
                break
            m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', b, re.S)
            if not m:
                break
            try:
                found = json.loads(m.group(1))["props"]["pageProps"]["initialState"]["jobs"].get("found") or []
            except (json.JSONDecodeError, KeyError):
                continue
            for j in found:
                if j["id"] in seen:
                    continue
                seen.add(j["id"])
                org = j.get("organization") or {}
                locs = ", ".join(j.get("searchableLocations") or [])
                out.append({"id": f"getro:{j['id']}", "company": org.get("name", "?"), "title": j.get("title", ""),
                            "location": locs + (" | Remote" if j.get("workMode") == "remote" else ""), "url": j.get("url", ""),
                            "ats": "vc-board", "posted": dt.datetime.fromtimestamp(j["createdAt"]).date().isoformat() if j.get("createdAt") else "",
                            "desc": f"Portfolio company of {host.split('.',1)[1]}. Skills: {', '.join(j.get('skills') or [])}. Work mode: {j.get('workMode')}."})
    return out


# ------------------------------------------------------------------ remote boards
def remotive():
    out = []
    for q in ["machine learning", "ai", "research", "software engineer intern", "cuda"]:
        d = http_json(f"https://remotive.com/api/remote-jobs?search={q.replace(' ', '%20')}&limit=40", timeout=40)
        for j in d.get("jobs", []):
            out.append({"id": f"remotive:{j['id']}", "company": j.get("company_name", ""), "title": j.get("title", ""),
                        "location": "Remote | " + (j.get("candidate_required_location") or ""), "url": j.get("url", ""), "ats": "remotive",
                        "posted": (j.get("publication_date") or "")[:10], "desc": _text(j.get("description"), 3000)})
    return list({o["id"]: o for o in out}.values())


def remoteok():
    d = http_json("https://remoteok.com/api", timeout=40)
    out = []
    for j in d[1:]:
        out.append({"id": f"remoteok:{j.get('id')}", "company": j.get("company", ""), "title": j.get("position", ""),
                    "location": "Remote | " + (j.get("location") or ""), "url": j.get("url", ""), "ats": "remoteok",
                    "posted": (j.get("date") or "")[:10], "desc": _text(j.get("description"), 3000)})
    return out


# ------------------------------------------------------------------ GitHub curated lists (speedyapply)
ROW = re.compile(r'^\|\s*<a href="[^"]*"><strong>(?P<co>[^<]+)</strong></a>\s*\|\s*(?P<pos>[^|]+?)\s*\|\s*(?P<loc>[^|]*?)\s*\|(?:[^|]*\|)?\s*<a href="(?P<url>[^"]+)"', re.M)


def speedyapply():
    out, seen = [], set()
    for repo, label in (("2026-AI-College-Jobs", "ai"), ("2026-SWE-College-Jobs", "swe")):
        for fn in ("README.md", "INTERN_INTL.md", "NEW_GRAD_USA.md", "NEW_GRAD_INTL.md"):
            try:
                md = http_get(f"https://raw.githubusercontent.com/speedyapply/{repo}/main/{fn}", timeout=40)
            except Exception:
                continue
            for m in ROW.finditer(md):
                url = html.unescape(m.group("url"))
                if url in seen:
                    continue
                seen.add(url)
                out.append({"id": f"speedyapply-{label}:{hashlib.md5(url.encode()).hexdigest()[:14]}", "company": html.unescape(m.group("co")), "title": html.unescape(m.group("pos")),
                            "location": html.unescape(m.group("loc")), "url": url, "ats": "simplify-intern" if "INTERN" in fn or fn == "README.md" else "simplify-newgrad",
                            "posted": "", "desc": f"Curated {label.upper()} college list ({fn}). Sponsorship: unknown."})
    return out
