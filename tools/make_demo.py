"""Build a demo workspace with FICTIONAL sample data (made-up companies, people and candidate) for screenshots and trying the GUI safely.
    python tools/make_demo.py <folder>        then   JOBAGENT_HOME=<folder> python agent/run.py gui
Nothing here touches your real workspace."""
import json, os, random, sys, datetime as dt

dest = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "demo-workspace")
os.makedirs(dest, exist_ok=True)
os.environ["JOBAGENT_HOME"] = dest
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))
import common  # noqa: E402
import setup  # noqa: E402

random.seed(7)
setup.create_profile({"name": "Alex Rivera", "email": "alex@example.com", "location": "Pune, India", "linkedin": "https://www.linkedin.com/in/example",
                      "github": "https://github.com/example", "education": "B.Tech Computer Science, Example Institute of Technology (expected 2027)", "graduation": "2027-06",
                      "summary": "Final-year CS student who builds ML systems: fused GPU kernels, a small inference server, and retrieval pipelines with measured results.",
                      "projects": ["KernelKit: fused attention kernels in Triton, 1.8x faster than the reference on an RTX 4060",
                                   "TinyServe: an LLM inference server with paged KV cache, 2.3x throughput at batch 16"],
                      "home_country": "India", "work_authorization": "Authorised to work in India"})
# Fictional companies get their own drawn logos (tools/demo_logos.py), pre-filled in the cache, so nothing is fetched and no real company's
# logo is ever shown next to a made-up posting.
common.save_override("settings.json", {"ui": {"logos": True}})
common.reload_config()
import logos as _logos  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import demo_logos  # noqa: E402
demo_logos.write(_logos.CACHE, lambda n: _logos._slug(_logos._SUFFIX.sub("", n)) or "x")
c = common.db()
now = dt.datetime.now()
ts = lambda h=0: (now - dt.timedelta(hours=h)).isoformat(timespec="seconds")
JOBS = [
    ("greenhouse:northwind:101", "Northwind AI", "A", "Inference Engineering Intern", "Bengaluru, India", "APPLY", 88, "packet", "india", "Paged-KV and kernel work matches TinyServe and KernelKit directly."),
    ("lever:contoso:202", "Contoso Labs", "A", "ML Systems Engineer, New Grad", "Remote (India)", "APPLY", 84, "ready", "remote", "Systems-heavy ML role; the inference server is strong evidence."),
    ("ashby:fabrikam:303", "Fabrikam Robotics", "B", "Applied Scientist Intern, Perception", "Hyderabad, India", "STRETCH", 71, "triaged", "india", "Perception is adjacent; retrieval and evaluation work transfers."),
    ("greenhouse:tailspin:404", "Tailspin Systems", "B", "Software Engineer I, Platform", "Pune, India", "APPLY", 79, "fill-partial", "india", "Go/Python platform work; two form fields need you."),
    ("ashby:adatum:505", "Adatum Research", "A", "Research Engineer, Efficient Inference", "London, UK (visa sponsored)", "STRETCH", 69, "watch", "relocate", "Prestige lab; referral first."),
    ("greenhouse:wingtip:606", "Wingtip Compute", "B", "GPU Kernel Engineer Intern", "Remote", "APPLY", 82, "triaged", "remote", "CUDA/Triton kernels are exactly KernelKit."),
    ("lever:litware:707", "Litware Data", "C", "Data Scientist I", "Bengaluru, India", "STRETCH", 62, "triaged", "india", "Analytics-heavy; fine as a backup."),
    ("greenhouse:proseware:808", "Proseware", "B", "Senior ML Engineer", "Remote", None, 0, "ineligible", "remote", ""),
]
for jid, co, tier, title, loc, verdict, fit, status, lc, reason in JOBS:
    desc = (f"About the role: join the {title} team at {co}. Requirements: currently pursuing a Bachelor's in CS or related field; Python; "
            f"experience with PyTorch or CUDA is a plus. ") * 6
    c.execute("""INSERT OR REPLACE INTO jobs(id,company,tier,category,title,location,url,ats,first_seen,last_seen,active,score,loc_class,family,early,desc,status,visa,jd_full,req)
                 VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?,1,?)""",
              (jid, co, tier, "demo", title, loc, f"https://example.com/jobs/{jid.split(':')[-1]}", jid.split(":")[0], ts(30), ts(1), fit or 55, lc, "mlsys", 1, desc, status,
               "yes" if lc == "relocate" else "n/a", json.dumps({"ok": status != "ineligible", "problems": ["senior-level title", "needs 5+ years of experience"] if status == "ineligible" else []})))
    if verdict:
        c.execute("INSERT OR REPLACE INTO triage(job_id,fit,verdict,resume,yoe_gap,reason,needs_referral,flags,at) VALUES(?,?,?,?,?,?,?,?,?)",
                  (jid, fit, verdict, "General", "", reason, 1, "[]", ts(20)))
for i in range(160):
    co = random.choice(["Northwind AI", "Contoso Labs", "Fabrikam Robotics", "Tailspin Systems", "Wingtip Compute", "Litware Data", "Woodgrove Analytics", "Alpine Kernels"])
    t = random.choice(["Machine Learning Engineer Intern", "Software Engineer, New Grad", "Data Scientist Intern", "Inference Engineer", "Research Intern", "Backend Engineer I"])
    loc, lc = random.choice([("Bengaluru, India", "india"), ("Remote", "remote"), ("Pune, India", "india"), ("Singapore", "relocate"), ("Berlin, Germany", "relocate")])
    c.execute("""INSERT OR REPLACE INTO jobs(id,company,tier,category,title,location,url,ats,first_seen,last_seen,active,score,loc_class,family,early,desc,status,visa)
                 VALUES(?,?,?,?,?,?,?,?,?,?,1,?,?,?,?,?,?,?)""",
              (f"demo:{i}", co, "B", "demo", t, loc, f"https://example.com/jobs/d{i}",
               "greenhouse", ts(random.randint(1, 200)), ts(1), random.randint(48, 80), lc, "ml_eng", 1, "Sample posting.", "new", "unknown"))
for jid, acts in (("greenhouse:northwind:101", "packet,referral"), ("lever:contoso:202", "packet,referral,fill"), ("greenhouse:tailspin:404", "packet,fill"), ("ashby:adatum:505", "referral")):
    c.execute("INSERT OR REPLACE INTO picks(job_id,ts,actions) VALUES(?,?,?)", (jid, ts(5), acts))
    d = os.path.join(common.OUT, "applications", jid.replace(":", "_"))
    os.makedirs(d, exist_ok=True)
    open(os.path.join(d, "README.md"), "w").write("# Application packet (sample)\n\n## Fit\nStrong: paged KV cache and fused kernels match the role.\n\n## Gaps\nNo production on-call experience yet.\n")
    open(os.path.join(d, "cover_letter.txt"), "w").write("Hi team,\n\nI build inference systems for fun and for coursework. TinyServe, my small LLM server with a paged KV cache, "
                                                          "reached 2.3x throughput at batch 16 on one GPU, and KernelKit's fused attention runs 1.8x faster than the reference.\n\n"
                                                          "I'd like to bring that to the inference team.\n\nAlex")
    if "referral" in acts:
        open(os.path.join(d, "REFERRAL.md"), "w").write(
            "# Referral plan (sample)\n\n## Strategy\nPriya is a 1st-degree connection on the serving team: ask her for ten minutes of advice first. "
            "Daniel leads ML platform; reach him through Sam Lee, who knows you both.\n\n## Target 1: Priya Sharma (Inference Engineer)\n"
            "Profile: https://www.linkedin.com/in/demo-priya-sharma/\nReach via: LinkedIn message (1st degree)\n\n**Message (short)**\n"
            "Hi Priya, I saw your post on paging the KV cache. I built something similar in TinyServe (2.3x at batch 16). "
            "Could I ask how the inference intern team picks projects?\n\n## Target 2: Daniel Okafor (ML Platform Lead)\n"
            "Profile: https://www.linkedin.com/in/demo-daniel-okafor/\nReach via: intro via Sam Lee (2nd degree)\n")
    if "packet" in acts:
        c.execute("INSERT OR REPLACE INTO packets(job_id,dir,at,referral_at,filled_at) VALUES(?,?,?,?,?)", (jid, d, ts(5), ts(4) if "referral" in acts else None, ts(3) if "fill" in acts else None))
    if "referral" in acts:
        c.execute("INSERT OR REPLACE INTO referrals(job_id,dir,at,how) VALUES(?,?,?,?)", (jid, d, ts(4), "linkedin"))
c.execute("INSERT OR REPLACE INTO watch(job_id,added,reason,next_step,referral_state) VALUES(?,?,?,?,?)", ("ashby:adatum:505", ts(10), "top lab", "get an intro first", "plan ready"))
PEOPLE = [("Northwind AI", "Priya Sharma", "1st", "Inference Engineer @ Northwind AI", ""), ("Northwind AI", "Daniel Okafor", "2nd", "ML Platform Lead at Northwind AI", "Sam Lee & 4 other mutual connections"),
          ("Contoso Labs", "Mei Chen", "2nd", "Software Engineer @ Contoso Labs", "Ravi Kumar is a mutual connection"), ("Adatum Research", "Tom Becker", "2nd", "Research Engineer at Adatum Research", "Ana Silva & 2 other mutual connections")]
for co, name, deg, head, mut in PEOPLE:
    c.execute("INSERT OR REPLACE INTO people(url,company,name,headline,degree,mutual,location,about,source,seen_at) VALUES(?,?,?,?,?,?,?,?,?,datetime('now'))",
              (f"https://www.linkedin.com/in/demo-{name.lower().replace(' ', '-')}/", co, name, head, deg, mut, "", "", "linkedin-" + deg))
CHAT = [("user", "/top 3"), ("agent", "88 APPLY   greenhouse:northwind:101  Northwind AI - Inference Engineering Intern (Bengaluru, India)\n     https://example.com/jobs/101\n"
                                     " 84 APPLY   lever:contoso:202  Contoso Labs - ML Systems Engineer, New Grad (Remote (India))\n     https://example.com/jobs/202\n"
                                     " 82 APPLY   greenhouse:wingtip:606  Wingtip Compute - GPU Kernel Engineer Intern (Remote)\n     https://example.com/jobs/606"),
        ("user", "who do I know at Northwind AI? then plan a referral for the inference intern role"),
        ("agent", "Checking LinkedIn through your logged-in browser, then planning the referral.\n\nStarted:\n- connections Northwind AI -> started: connections Northwind AI\n- linkedin greenhouse:northwind:101 -> started: linkedin greenhouse:northwind:101"),
        ("agent", "LinkedIn scouting for Northwind AI - Inference Engineering Intern is ready (source: linkedin).\n1. Priya Sharma (Inference Engineer @ Northwind AI)\n   https://www.linkedin.com/in/demo-priya-sharma/\n   reach: LinkedIn message (1st degree)\n   why: works on the serving stack the role joins; posted about KV-cache paging last week\n"
                  "2. Daniel Okafor (ML Platform Lead at Northwind AI)\n   https://www.linkedin.com/in/demo-daniel-okafor/\n   reach: intro via Sam Lee (2nd degree)\nFull plan with drafted messages: My picks -> this job -> Referral.")]
for i, (role, text) in enumerate(CHAT):
    c.execute("INSERT INTO chat(ts,role,text) VALUES(?,?,?)", (ts(2 - i * 0.1), role, text))
c.execute("INSERT INTO inbox(ts,text,context,status,result) VALUES(?,?,?,?,?)", (ts(6), "https://example.com/jobs/909 (shared by a friend)", "from a friend", "done",
                                                                                   json.dumps({"added": 1, "jobs": [{"company": "Alpine Kernels", "title": "Compiler Intern", "verdict": "APPLY"}]})))
for k, p, n, i_, o in (("triage", "claude", 6, 7000, 3000), ("packet", "claude", 4, 20000, 5000), ("rank", "claude", 5, 3400, 1300), ("referral", "agy", 3, 9000, 2700),
                       ("outreach", "claude", 4, 4200, 6800), ("fill", "claude", 2, 6100, 1200), ("today", "agy", 1, 2000, 900), ("triage", "local", 12, 7000, 2500)):
    for j in range(n):
        c.execute("INSERT INTO llm_log(ts,kind,model,inp,cache_create,cache_read,outp,cost,provider,secs) VALUES(?,?,?,?,?,?,?,?,?,?)", (ts(j), k, "", i_, 0, 0, o, 0, p, 20))
common.meta_set(c, "last_poll", ts(1))
common.meta_set(c, "last_cycle", ts(1))
c.commit()
print("demo workspace ready:", dest)
