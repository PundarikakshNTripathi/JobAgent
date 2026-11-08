"""First-run setup. Creates the user's workspace (all personal data, local only) and a profile from answers.
Used by `run.py init` (interactive) and by the GUI setup wizard (create_profile)."""
import json, os, shutil
import common
from common import HOME, DEFAULTS, CFG, DATA, save_override


def create_profile(a):
    """a: dict of answers (name, email, phone, location, linkedin, github, website, education, graduation, summary, projects[], experience[], resume_pdf, ...)."""
    t = json.load(open(os.path.join(DEFAULTS, "profile.template.json"), encoding="utf-8"))
    name = a.get("name", "").strip() or "Your Name"
    parts = name.split()
    t.update(name=name, first_name=parts[0], last_name=parts[-1] if len(parts) > 1 else "", email=a.get("email", ""), phone=a.get("phone", ""), location=a.get("location", ""),
             linkedin=a.get("linkedin", ""), github=a.get("github", ""), website=a.get("website", ""), education=a.get("education", ""), graduation=a.get("graduation", ""),
             summary=a.get("summary", t["summary"]))
    for k in ("projects", "experience", "achievements"):
        v = a.get(k)
        if v:
            t[k] = [x.strip() for x in (v if isinstance(v, list) else str(v).splitlines()) if x.strip()]
    if a.get("skills"):
        t["skills_defensible"] = a["skills"]
    if a.get("resume_pdf"):
        t["resume_variants"] = {"General": a["resume_pdf"]}
    if a.get("home_country"):
        t["preferences"]["home_country"] = a["home_country"]
        t["form_defaults"]["country"] = a["home_country"]
    if a.get("work_authorization"):
        t["form_defaults"]["work_authorization"] = a["work_authorization"]
    os.makedirs(CFG, exist_ok=True)
    with open(os.path.join(CFG, "profile.json"), "w", encoding="utf-8") as f:
        json.dump(t, f, indent=2)
    common.reload_config()
    return t


def detect_providers():
    import llm
    return {n: llm.available(n) for n in llm.registry()}


def wizard():
    print(f"JobAgent setup. Your personal data will live in: {HOME}\n(nothing personal is ever stored in the code folder; press Enter to skip optional questions)\n")
    ask = lambda q, d="": (input(f"{q}{' [' + d + ']' if d else ''}: ").strip() or d)
    a = {"name": ask("Full name"), "email": ask("Email"), "phone": ask("Phone (optional)"), "location": ask("City, country"), "linkedin": ask("LinkedIn URL (optional)"),
         "github": ask("GitHub URL (optional)"), "website": ask("Website/portfolio (optional)"), "education": ask("Education (degree, school, expected graduation)"),
         "graduation": ask("Graduation YYYY-MM"), "summary": ask("2-3 sentence summary of you and what you want"),
         "home_country": ask("Country you are authorised to work in"), "resume_pdf": ask("Path to your resume PDF (optional)")}
    print("Add your strongest projects/experience, one per line, format 'Name: what it is + one measured result'. Empty line to finish.")
    items = []
    while True:
        x = input("  - ").strip()
        if not x:
            break
        items.append(x)
    a["projects"] = items
    create_profile(a)
    print("\nProfile saved. Detected providers:")
    for n, (ok, why) in detect_providers().items():
        print(f"  {n:10} {'ready' if ok else '-'}  {'' if ok else why}")
    print("\nNext: `python agent/run.py doctor`, then `python agent/run.py gui` (it starts a guided tour).")
