"""Static HTML dashboard + markdown digest. Open out/dashboard.html."""
import html, json, os
from common import db, OUT, now, today, meta_get

MANUAL = [
    ("Google Careers (student/intern/new grad)", "https://www.google.com/about/careers/applications/jobs/results/?q=machine%20learning&target_level=INTERN_AND_APPRENTICE"),
    ("Google DeepMind student researcher", "https://deepmind.google/about/careers/"),
    ("Mistral AI careers", "https://mistral.ai/careers"),
    ("Meta Careers (internships)", "https://www.metacareers.com/jobs?roles[0]=Internship%20-%20Engineering%20and%20Research"),
    ("Microsoft Research India", "https://www.microsoft.com/en-us/research/lab/microsoft-research-india/"),
    ("Apple Jobs", "https://jobs.apple.com/en-in/search?team=machine-learning-and-ai-MLAI"),
    ("Transluce", "https://transluce.org/jobs"),
    ("80,000 Hours job board (AI safety)", "https://jobs.80000hours.org/"),
    ("aisafety.com jobs", "https://aisafety.com/jobs"),
    ("YC Work at a Startup", "https://www.workatastartup.com/jobs"),
    ("X search: hiring posts", "https://x.com/search?q=%28%22we%27re%20hiring%22%29%20%28%22research%20engineer%22%20OR%20inference%20OR%20kernel%29%20%28intern%20OR%20%22new%20grad%22%29&f=live"),
]


def e(x):
    return html.escape(str(x if x is not None else ""))


def rows(c, where, args=(), limit=60):
    return c.execute(f"""SELECT j.*, t.fit, t.verdict, t.reason, t.resume, t.yoe_gap, p.dir, p.filled_at, p.referral_at, p.fill_report
                         FROM jobs j LEFT JOIN triage t ON t.job_id=j.id LEFT JOIN packets p ON p.job_id=j.id
                         WHERE {where} ORDER BY COALESCE(t.fit, j.score) DESC LIMIT {limit}""", args).fetchall()


def card(r):
    link = lambda path, label: f' <a href="file:///{e(path.replace(chr(92), "/"))}">{label}</a>' if path else ""
    d = r["dir"]
    files = ""
    if d:
        files = link(os.path.join(d, "README.md"), "packet") + link(os.path.join(d, "cover_letter.pdf"), "cover letter") + \
                (link(os.path.join(d, "REFERRAL.md"), "referral plan") if r["referral_at"] else "") + (link(os.path.join(d, "FILL_REPORT.md"), "fill report") if r["filled_at"] else "")
    return (f'<div class="card"><div class="h"><b>{e(r["company"])}</b> <span class="t">tier {e(r["tier"])}</span> '
            f'<span class="pill {e((r["verdict"] or "").lower())}">{e(r["verdict"] or "untriaged")} {int(r["fit"]) if r["fit"] is not None else int(r["score"])}</span> '
            f'<span class="pill loc">{e(r["loc_class"])}</span> <span class="pill visa-{e(r["visa"])}">visa:{e(r["visa"])}</span></div>'
            f'<div><a href="{e(r["url"])}">{e(r["title"])}</a> <span class="m">{e((r["location"] or "")[:80])} | {e(r["ats"])} | seen {e(r["first_seen"][:10])}</span></div>'
            f'<div class="m">{e(r["reason"] or "")} {("| gap: " + e(r["yoe_gap"])) if r["yoe_gap"] else ""} {("| resume: " + e(r["resume"])) if r["resume"] else ""}</div>'
            f'<div class="m">id <code>{e(r["id"])}</code> {files}</div></div>')


def build():
    c = db()
    sections = [
        ("READY TO SUBMIT (form filled, review and press Submit)", "j.status='ready'"),
        ("HIGH-STAKES WATCHLIST: tracked, not applying yet (referral/relationship first)", "j.status='watch'"),
        ("Needs your help (partial / blocked fill)", "j.status LIKE 'fill-%'"),
        ("Packets prepared (cover letter, answers, resume chosen)", "j.status='packet'"),
        ("Looked good but ruled out by the packet (visa / level / eligibility)", "j.status='ineligible'"),
        ("Triaged: APPLY / STRETCH, no packet yet", "j.status='triaged' AND t.verdict IN ('APPLY','STRETCH')"),
        ("New since last look, high heuristic score, not yet triaged", "j.status='new' AND j.baseline=0 AND j.score>=65 AND t.job_id IS NULL"),
    ]
    out = [f"<!doctype html><meta charset=utf-8><title>JobAgent</title><style>body{{font:14px system-ui;max-width:1050px;margin:20px auto;padding:0 14px;background:#0f1115;color:#e6e6e6}}"
           f"a{{color:#7cc4ff}}h2{{margin-top:28px;border-bottom:1px solid #333;padding-bottom:4px}}.card{{background:#171a21;border:1px solid #262b36;border-radius:8px;padding:9px 12px;margin:8px 0}}"
           f".m{{color:#9aa3b2;font-size:12px;margin-top:3px}}.t{{color:#9aa3b2;font-size:12px}}.pill{{font-size:11px;padding:1px 7px;border-radius:9px;background:#2a3040;margin-left:4px}}"
           f".apply{{background:#14532d}}.stretch{{background:#713f12}}.visa-no{{background:#7f1d1d}}.visa-yes{{background:#14532d}}code{{font-size:11px}}</style>",
           f"<h1>JobAgent</h1><div class=m>generated {now()} | last poll {e(meta_get(c, 'last_poll', '-'))} | "
           f"{c.execute('SELECT COUNT(*) FROM jobs WHERE active=1').fetchone()[0]} active postings tracked, {c.execute('SELECT COUNT(*) FROM jobs WHERE score>=58').fetchone()[0]} pass the filter</div>"]
    for title, where in sections:
        rs = rows(c, where)
        out.append(f"<h2>{e(title)} ({len(rs)})</h2>" + ("".join(card(r) for r in rs) or '<div class=m>nothing here</div>'))
    progs = c.execute("SELECT name, org, kind, window, eligibility, url FROM programs ORDER BY added DESC LIMIT 30").fetchall()
    out.append(f"<h2>Pre-doc / residency / fellowship programs tracked ({len(progs)})</h2>" + "".join(
        f'<div class="card"><b>{e(p["name"])}</b> <span class="t">{e(p["org"])} | {e(p["kind"])}</span><div class="m">window: {e(p["window"])} | eligible: {e(p["eligibility"])} | <a href="{e(p["url"])}">link</a></div></div>' for p in progs))
    us = c.execute("SELECT COUNT(*) n, COALESCE(SUM(inp+cache_create+cache_read),0) tin, COALESCE(SUM(outp),0) tout FROM llm_log WHERE ts>=date('now','localtime')").fetchone()
    out.append(f'<div class=m style="margin-top:18px">Claude today: {us["n"]} calls, {us["tin"]:,} input / {us["tout"]:,} output tokens. Daily plan: out/today.md</div>')
    out.append("<h2>Mark a job</h2><div class=m>python agent/run.py mark &lt;id&gt; applied|skip &nbsp;|&nbsp; python agent/run.py fill &lt;id&gt; &nbsp;|&nbsp; python agent/run.py referral &lt;id&gt;</div>")
    out.append("<h2>Check by hand (sites that block bots or have no API)</h2><ul>" + "".join(f'<li><a href="{e(u)}">{e(n)}</a></li>' for n, u in MANUAL) + "</ul>")
    path = os.path.join(OUT, "dashboard.html")
    open(path, "w", encoding="utf-8").write("\n".join(out))
    # markdown digest
    md = [f"# JobAgent digest {today()}", ""]
    for title, where in sections[:4]:
        rs = rows(c, where, limit=25)
        md.append(f"## {title} ({len(rs)})")
        for r in rs:
            md.append(f"- **{r['company']}** | [{r['title']}]({r['url']}) | {r['location'][:50]} | {r['verdict'] or ''} {int(r['fit'] or r['score'])} | `{r['id']}`")
        md.append("")
    open(os.path.join(OUT, f"digest-{today()}.md"), "w", encoding="utf-8").write("\n".join(md))
    return path
