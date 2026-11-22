"""Tailored one-page PDF resume per job, with a hard fact gate.

It uses YOUR resume generator (profile key `resume_generator`, e.g. Resume/master-resume/_gen.py: a verified bullet bank plus build()), so no
sentence is ever rewritten: the model (task kind 'review') only CHOOSES and ORDERS existing, verified bullets, projects and skill lines for the job.
The gate then checks, in code, that every line in the generated LaTeX comes verbatim from the bank (no new numbers, titles or employers can
appear), compiles with tectonic, and refuses anything that is not exactly one page. Your generator and its output folders are never modified:
the tailored resume is written into the job's packet folder."""
import importlib.util, json, os, re, shutil, subprocess
import common
from common import db, log, now, PROFILE, SETTINGS, extract_json

PICK = """Choose which VERIFIED resume bullets to show for this job. You may only use the ids listed; you cannot write or edit text.
Goal: the strongest one-page match to what the posting asks for, most relevant first. Keep the base variant's size: about the same number of
projects (usually 4) and bullets (3-5 per project), so it still fits one page.

JOB: {company} | {title}
POSTING (excerpt):
{jd}

BASE VARIANT ({base}): {base_spec}

BANK:
experience bullets: {exp}
projects (id: title | bullet ids with a short preview):
{projects}
achievements: {ach}
skill lines: {skills}

Return ONLY JSON: {{"exp": ["exp ids in order"], "projects": [["project id", ["bullet ids in order"]]], "ach": ["ids"], "skills": ["ids"], "why": "one sentence"}}"""


class CVError(Exception):
    pass


def _gen():
    path = PROFILE.get("resume_generator") or SETTINGS.get("resume", {}).get("generator") or ""
    if not path or not os.path.exists(path):
        raise CVError("no resume generator configured: set profile.resume_generator to your _gen.py (a bullet bank + build(spec, outdir))")
    spec = importlib.util.spec_from_file_location("user_resume_gen", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for name in ("B", "ROLES", "build"):
        if not hasattr(mod, name):
            raise CVError(f"{path} has no {name}; JobAgent needs B (bullet bank), ROLES (variants) and build(spec, outdir)")
    return mod, path


def _tectonic():
    t = PROFILE.get("tectonic") or SETTINGS.get("tectonic") or shutil.which("tectonic") or ""
    if not t or not (os.path.exists(t) or shutil.which(t)):
        raise CVError("tectonic (LaTeX compiler) not found: set settings.tectonic")
    return t


def _pages(pdf):
    """Page count without extra tools: page objects are usually inside compressed object streams, so inflate those too."""
    import zlib
    b = open(pdf, "rb").read()
    n = len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", b))
    for m in re.finditer(rb"stream\r?\n(.*?)\r?\nendstream", b, re.S):
        try:
            n += len(re.findall(rb"/Type\s*/Page(?![a-zA-Z])", zlib.decompress(m.group(1))))
        except zlib.error:
            continue
    return n


def _bank_lines(g):
    """Every piece of text the generator could emit (the verified bank)."""
    out = set()
    for p in g.B.values():
        out |= {v for v in p.get("b", {}).values() if v}
        out |= {p.get("title", ""), p.get("tags", ""), p.get("path", "")}
    for name in ("FLY", "ACH"):
        obj = getattr(g, name, {})
        vals = obj.get("b", {}).values() if name == "FLY" else obj.values()
        out |= {v for v in vals if isinstance(v, str)}
    for v in getattr(g, "SKILL_SETS", {}).values():
        out |= {v[1]}
    return {x for x in out if x}


def fact_gate(outdir, g):
    """Every \\resumeItem / achievement line in the generated files must be verbatim from the bank. Returns the offending lines."""
    bank = _bank_lines(g)
    bad = []
    for f in ("src/experience.tex", "src/projects.tex", "src/achievements.tex"):
        try:
            t = open(os.path.join(outdir, f), encoding="utf-8").read()
        except OSError:
            continue
        for m in re.finditer(r"\\resumeItem\{(.*)\}\s*$|\\item\\small\{(.*)\}\s*$", t, re.M):
            line = m.group(1) or m.group(2)
            if line not in bank:
                bad.append(line[:140])
    return bad


def _base_for(job):
    c = db()
    t = c.execute("SELECT resume FROM triage WHERE job_id=?", (job["id"],)).fetchone()
    return (t["resume"] if t and t["resume"] else "") or ""


def build(job_id, max_tries=4):
    from llm import run
    import packets, websafe
    g, gen_path = _gen()
    c = db()
    job = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    pk = c.execute("SELECT dir FROM packets WHERE job_id=?", (job_id,)).fetchone()
    if not job:
        raise CVError("no such job")
    if not pk or not pk["dir"]:
        raise CVError("prepare the application packet first (the tailored resume goes into its folder)")
    base = _base_for(job)
    base = base if base in g.ROLES else next(iter(g.ROLES))
    bspec = g.ROLES[base]
    exp = getattr(g, "FLY", {}).get("b", {})
    proj = "\n".join(f"- {k}: {v.get('title', '')} | " + "; ".join(f"{bk}: {re.sub(r'[^A-Za-z0-9 .,%-]', '', bv or '')[:70]}" for bk, bv in v.get("b", {}).items() if bv) for k, v in g.B.items())
    text, _ = run(PICK.format(company=job["company"], title=job["title"], jd=websafe.wrap(job["url"] or "posting", (job["desc"] or "")[:3500]), base=base,
                              base_spec=json.dumps({k: bspec[k] for k in ("exp", "projects", "ach", "skills") if k in bspec}),
                              exp=", ".join(f"{k} ({re.sub(r'[^A-Za-z0-9 .,%-]', '', v)[:60]})" for k, v in exp.items()), projects=proj,
                              ach=", ".join(getattr(g, "ACH", {}).keys()), skills=", ".join(getattr(g, "SKILL_SETS", {}).keys())), kind="review", timeout=240)
    pick = extract_json(text)
    spec = dict(bspec)
    spec["file"] = f"{bspec['file']}_{common.slug(job['company'], 24)}"
    ex = [k for k in (pick.get("exp") or []) if k in exp] or bspec["exp"]
    pj = []
    for item in pick.get("projects") or []:
        if isinstance(item, list) and len(item) == 2 and item[0] in g.B:
            bl = [b for b in item[1] if b in g.B[item[0]].get("b", {}) and g.B[item[0]]["b"][b]]
            if bl:
                pj.append((item[0], bl))
    spec["exp"] = ex
    spec["projects"] = pj or bspec["projects"]
    spec["ach"] = [k for k in (pick.get("ach") or []) if k in getattr(g, "ACH", {})] or bspec["ach"]
    spec["skills"] = [k for k in (pick.get("skills") or []) if k in getattr(g, "SKILL_SETS", {})] or bspec["skills"]
    outdir = os.path.join(pk["dir"], "cv")
    tect = _tectonic()
    for attempt in range(max_tries):
        shutil.rmtree(outdir, ignore_errors=True)
        g.build(spec, outdir)
        bad = fact_gate(outdir, g)
        if bad:
            raise CVError("blocked: these lines are not in your verified bank: " + " | ".join(bad[:3]))
        r = subprocess.run([tect, spec["file"] + ".tex"], cwd=outdir, capture_output=True, text=True, timeout=300)
        pdf = os.path.join(outdir, spec["file"] + ".pdf")
        if r.returncode != 0 or not os.path.exists(pdf):
            raise CVError("LaTeX failed: " + (r.stderr or r.stdout)[-400:])
        pages = _pages(pdf)
        if pages == 1:
            break
        # too long: drop the last bullet of the project with the most bullets, then retry
        longest = max(range(len(spec["projects"])), key=lambda i: len(spec["projects"][i][1]))
        k, bl = spec["projects"][longest]
        if len(bl) <= 2:
            spec["projects"] = spec["projects"][:-1]
        else:
            spec["projects"][longest] = (k, bl[:-1])
    else:
        raise CVError(f"could not fit one page after {max_tries} tries")
    # use it for this application
    pj_path = os.path.join(pk["dir"], "packet.json")
    try:
        d = json.load(open(pj_path, encoding="utf-8"))
        d.setdefault("packet", {})["resume_path"] = pdf
        d["packet"]["resume_tailored"] = {"base": base, "why": pick.get("why", ""), "spec": {k: spec[k] for k in ("exp", "projects", "ach", "skills")}, "at": now()}
        json.dump(d, open(pj_path, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    except (OSError, ValueError):
        pass
    log(f"cv: tailored one-page resume for {job['company']} | {job['title']} -> {pdf} (from {base}; every line verified against {os.path.basename(gen_path)})")
    return {"pdf": pdf, "base": base, "why": pick.get("why", ""), "projects": [k for k, _ in spec["projects"]]}
