"""Keep JobAgent's picture of your work current from GitHub (weekly, public API; a token in $GITHUB_TOKEN only raises the rate limit).

It looks at your repositories and your organisations' (profile: github, org_github), ranks them by recent activity and care (recent commits,
stars, README, not a fork), reads the README of the top few, and the local model turns each into short factual notes: what it is, the stack,
results the README itself states. These arrive as PROPOSALS in About me: nothing is added to what JobAgent says about you until you accept it.
README text is untrusted web content (websafe)."""
import datetime as dt, json, os, re, urllib.request
import common
from common import db, log, now, PROFILE, extract_json

API = "https://api.github.com"
NOTE = """Summarise this GitHub repository for a resume/context file, using ONLY its README and metadata. {rule}

REPO: {full} | {desc} | language {lang} | stars {stars} | commits in the last 30 days: {commits} | last push {pushed}
README:
{readme}

Return ONLY JSON: {{"what": "one sentence: what it is", "stack": "main tools/languages", "results": ["measured results exactly as the README states them, with units"],
"status": "active / finished / early R&D (as the README or activity shows)", "highlight": "the single most impressive TRUE thing, one sentence"}}"""


def _get(url, raw=False):
    h = {"User-Agent": "JobAgent", "Accept": "application/vnd.github.raw" if raw else "application/vnd.github+json"}
    tok = os.environ.get("GITHUB_TOKEN", "")
    if tok:
        h["Authorization"] = "Bearer " + tok
    with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=25) as r:
        b = r.read(400000).decode("utf-8", "replace")
    return b if raw else json.loads(b)


def _owners():
    out = []
    for k in ("github", "org_github"):
        v = PROFILE.get(k) or ""
        for part in (v if isinstance(v, list) else [v]):
            m = re.search(r"github\.com/([A-Za-z0-9-]+)", str(part)) or re.match(r"^([A-Za-z0-9-]+)$", str(part).strip())
            if m:
                out.append(m.group(1))
    return list(dict.fromkeys(out))


def repos():
    out = []
    for owner in _owners():
        for kind in ("users", "orgs"):
            try:
                out += _get(f"{API}/{kind}/{owner}/repos?per_page=100&sort=pushed")
                break
            except Exception:
                continue
    return [r for r in out if isinstance(r, dict) and not r.get("fork") and not r.get("archived")]


def _commits_30d(full):
    since = (dt.datetime.utcnow() - dt.timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        return len(_get(f"{API}/repos/{full}/commits?since={since}&per_page=100"))
    except Exception:
        return 0


def sync(top=6, force=False):
    from llm import run
    import about, websafe
    c = db()
    if not force and (common.meta_get(c, "github_sync") or "")[:10] >= (dt.date.today() - dt.timedelta(days=7)).isoformat():
        return "GitHub was synced this week"
    rs = repos()
    if not rs:
        return "no repositories found (set profile.github to your GitHub URL)"
    for r in rs:
        r["_commits"] = _commits_30d(r["full_name"]) if r.get("pushed_at", "") >= (dt.date.today() - dt.timedelta(days=90)).isoformat() else 0
        r["_score"] = r["_commits"] * 3 + (r.get("stargazers_count") or 0) * 2 + (10 if r.get("description") else 0) + (8 if r.get("pushed_at", "") >= (dt.date.today() - dt.timedelta(days=30)).isoformat() else 0)
    rs.sort(key=lambda r: -r["_score"])
    n = 0
    for r in rs[:top]:
        try:
            readme = _get(f"{API}/repos/{r['full_name']}/readme", raw=True)
        except Exception:
            readme = ""
        if len(readme) < 200:
            continue
        try:
            text, _ = run(NOTE.format(rule=websafe.RULE, full=r["full_name"], desc=r.get("description") or "", lang=r.get("language") or "", stars=r.get("stargazers_count", 0),
                                      commits=r["_commits"], pushed=(r.get("pushed_at") or "")[:10], readme=websafe.wrap(r["html_url"], readme[:6000])), kind="research", timeout=240)
            d = extract_json(text)
        except Exception as e:
            log(f"github: {r['full_name']}: {str(e)[:120]}")
            continue
        line = f"{r['name']}: {d.get('what', '')} Stack: {d.get('stack', '')}. " + (("Results (README): " + "; ".join(d.get("results") or []) + ". ") if d.get("results") else "") + f"Status: {d.get('status', '')}."
        if about.propose("github", "projects", line, r["html_url"], key=f"github:{r['full_name']}:{(r.get('pushed_at') or '')[:10]}"):
            n += 1
    common.meta_set(c, "github_sync", now())
    log(f"github: {n} new project notes proposed (About me)")
    return f"{n} new proposals from {min(top, len(rs))} active repositories"
