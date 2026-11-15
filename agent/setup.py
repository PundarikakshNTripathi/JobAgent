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


def recommended_routing(ready):
    """A sensible provider mix for whatever is installed. Claude where quality or the browser matters; Antigravity/Gemini for research and synthesis."""
    hosted = [p for p in ("claude", "agy", "codex", "gemini", "anthropic", "openai", "openrouter", "gemini_api") if ready.get(p)]
    if not hosted:
        return None, []
    main = hosted[0]
    research = "agy" if ready.get("agy") and main != "agy" else ("gemini" if ready.get("gemini") and main != "gemini" else main)
    browser = "claude" if ready.get("claude") else main
    r = {k: {"provider": main, "model": "", "effort": e} for k, e in (("triage", "low"), ("ingest", "low"), ("chat", "low"), ("packet", "high"), ("outreach", "high"),
                                                                       ("resume", "high"), ("rank", "low"), ("browse", "low"))}
    r.update({k: {"provider": research, "model": "", "effort": ""} for k in ("referral", "scout", "today", "reflect")})
    r.update({k: {"provider": browser, "model": "", "effort": "medium"} for k in ("fill", "linkedin", "yc")})
    return r, [p for p in hosted if p != main][:2] + [main]


def wizard(defaults=False, reconfigure=False):
    """Interactive first-run setup. defaults=True accepts every default (unattended installs, CI). Only asks where your choice matters."""
    print(f"JobAgent setup. Your personal data will live in: {HOME}\n(nothing personal is ever stored in the code folder; press Enter to accept the [default])\n")

    def ask(q, d=""):
        if defaults:
            return d
        return input(f"{q}{' [' + d + ']' if d else ''}: ").strip() or d

    def yes(q, d=True):
        return d if defaults else (ask(q + (" [Y/n]" if d else " [y/N]")).lower() in (("", "y", "yes") if d else ("y", "yes")))

    if common.needs_setup() or (reconfigure and yes("Re-enter your profile?", False)):
        if defaults:
            print("profile: skipped in --defaults mode (fill it in the GUI's setup screen)")
        else:
            profile_questions(ask)
    # 1. AI providers
    print("\n1) AI providers found on this computer:")
    det = detect_providers()
    ready = {n: ok for n, (ok, _) in det.items()}
    for n, (ok, why) in det.items():
        if ok or n in ("claude", "agy", "codex", "gemini", "local"):
            print(f"   {n:10} {'ready' if ok else '-'}  {'' if ok else why}")
    routing, fallbacks = recommended_routing(ready)
    if routing:
        summary = sorted({v['provider'] for v in routing.values()})
        if yes(f"   Use the recommended mix ({' + '.join(summary)}; Claude handles the browser and the writing when available)?"):
            save_override("settings.json", {"llm": {"routing": routing, "fallbacks": fallbacks}})
    else:
        print("   No hosted AI found. Install Claude Code, Antigravity, Codex or Gemini CLI, or set an API key, then run `init --reconfigure`.")
    # 2. Local AI
    print("\n2) Local AI (runs the cheap, high-volume steps on your own GPU for free)")
    try:
        import local_ai
        rec = local_ai.recommend()
        if rec and yes(f"   Set up {rec['label']} (about {rec['size_gb']} GB download)?", False):
            import run as _run
            _run.local_setup(local_ai, assume_yes=True)
        elif not rec:
            print("   Not recommended on this hardware; hosted models will be used.")
    except Exception as e:
        print(f"   skipped: {e}")
    # 3. Browser logins
    print("\n3) Browser: JobAgent reads LinkedIn, YC Work at a Startup and job boards through its own Chrome window, where you log in once.")
    try:
        import browse
        ok, why = browse.available()
        print("   " + ("ready: " if ok else "NOT READY: ") + why)
        if ok and yes("   Open it now so you can log in to LinkedIn and YC Work at a Startup?"):
            ctx = browse._context()
            for u in ("https://www.linkedin.com/login", "https://www.workatastartup.com/"):
                ctx.new_page().goto(u, wait_until="domcontentloaded", timeout=45000)
            print("   Log in in the window that opened (it stays open). Your sessions stay in JobAgent's own browser profile.")
    except Exception as e:
        print(f"   browser check failed: {e}")
    # 4. Schedule
    print("\n4) When should it run by itself? (once or twice a day keeps AI usage low; you can always press 'Run pipeline now')")
    ch = ask("   twice (09:00 and 18:00) / once (09:00) / custom / none", "twice")
    import schedule as sch
    if ch.startswith("t"):
        print("   " + sch.install(["09:00", "18:00"]).replace("\n", "\n   "))
    elif ch.startswith("o"):
        print("   " + sch.install(["09:00"]))
    elif ch.startswith("c"):
        ts = [t.zfill(5) for t in ask("   times, e.g. 08:30 20:00", "09:00 18:00").replace(",", " ").split()]
        print("   " + sch.install(ts))
    else:
        print("   no schedule; run `python agent/run.py install-schedule` later")
    # 5. Notifications
    print("\n5) Notifications: desktop notifications are on. For phone pushes, install the free ntfy app and pick a private topic name.")
    topic = ask("   ntfy topic (Enter to skip)", "")
    if topic:
        save_override("settings.json", {"notify": {"ntfy_topic": topic}})
    common.reload_config()
    print("\nDone. `python agent/run.py doctor` checks everything; `python agent/run.py gui` opens the app (with a short tour).")
    if not defaults and yes("Open the GUI now?"):
        import gui
        gui.serve(open_browser=True)


def profile_questions(ask):
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
    print("Profile saved.")
