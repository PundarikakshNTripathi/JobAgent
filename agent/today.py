"""Daily 'Today' page + one morning toast: improvement tasks for the 5h/day budget, high-stakes watchlist nudges, deadlines, what's ready.
One cheap Haiku call per day; everything else is deterministic."""
import collections, json, os
from common import db, now, today, OUT, PROFILE, SETTINGS, run_claude, extract_json, notify, meta_get, meta_set, log

PROMPT = """You are a blunt, practical career coach for ONE student. Produce today's plan.
Student: {summary}
Track priority: {tracks}. Research interests: {interests}. Time available today for improvement work: {hours} hours. Available from late Dec 2026.
Recurring gaps found across the packets I prepared (gap: count): {gaps}
High-stakes roles I am NOT applying to yet (company - role - state): {watch}
Already-completed tasks (do not repeat): {done}

Return ONLY JSON:
{{"tasks":[{{"title":"concrete, 1 line, verb first","minutes":int,"why":"which gaps/roles it unlocks","proof":"what artifact to produce (repo commit, write-up, benchmark table, PR)"}}],
"outreach":[{{"who_or_where":"role/company to approach","action":"specific low-friction action (e.g. read X, comment on Y, ask Z one question)"}}],
"thought":"one sentence of strategic advice"}}
Rules: 4-6 tasks whose minutes sum to AT MOST {minutes} (hard limit); operate like an ambitious researcher-engineer who needs an offer ASAP: ship public proof every day, apply widely to normal-stakes roles, and work referral relationships for the high-stakes ones; at least one task producing public proof (benchmark, write-up, PR to vLLM/SGLang/Triton/llama.cpp or similar) tied to the top track; 3-5 outreach items (people to message today, communities, maintainers, programs to ask about); no generic advice; do not suggest anything that needs credentials you don't have."""


def gaps_counter(c):
    cnt = collections.Counter()
    for (pk,) in c.execute("SELECT dir FROM packets").fetchall():
        try:
            p = json.load(open(os.path.join(pk, "packet.json"), encoding="utf-8"))["packet"]
        except Exception:
            continue
        for g in p.get("gaps", []):
            cnt[g[:110]] += 1
    return cnt.most_common(8)


def build(force=False):
    c = db()
    day = today()
    path = os.path.join(OUT, "today.md")
    plan = None
    if not force and meta_get(c, "today_date") == day and os.path.exists(os.path.join(OUT, "today.json")):
        plan = json.load(open(os.path.join(OUT, "today.json"), encoding="utf-8"))
    watch = c.execute("""SELECT j.company, j.title, w.referral_state, w.deadline, w.next_step FROM watch w JOIN jobs j ON j.id=w.job_id WHERE j.status='watch'
                         ORDER BY w.added DESC LIMIT 12""").fetchall()
    if plan is None:
        prompt = PROMPT.format(summary=PROFILE["summary"], tracks=" > ".join(PROFILE["preferences"]["track_priority"]), interests="; ".join(PROFILE["preferences"]["research_interests"]),
                               hours=PROFILE["preferences"]["daily_improvement_hours"], minutes=PROFILE["preferences"]["daily_improvement_hours"] * 60, gaps="; ".join(f"{g} ({n})" for g, n in gaps_counter(c)) or "none yet",
                               watch="; ".join(f"{w['company']} - {w['title'][:50]} ({w['referral_state']})" for w in watch) or "none",
                               done=meta_get(c, "done_tasks", "none")[:400])
        text, _ = run_claude(prompt, kind="today", model=SETTINGS.get("today_model", "haiku"), timeout=180, max_turns=2)
        plan = extract_json(text)
        json.dump(plan, open(os.path.join(OUT, "today.json"), "w", encoding="utf-8"), indent=1)
        meta_set(c, "today_date", day)
    ready = c.execute("SELECT j.company, j.title FROM jobs j WHERE j.status='ready'").fetchall()
    partial = c.execute("SELECT j.company, j.title FROM jobs j WHERE j.status LIKE 'fill-%'").fetchall()
    progs = c.execute("SELECT name, org, kind, window, eligibility, url FROM programs ORDER BY added DESC LIMIT 12").fetchall()
    newc = c.execute("SELECT COUNT(*) FROM jobs WHERE baseline=0 AND first_seen>=? AND score>=70", (day,)).fetchone()[0]
    md = [f"# Today ({day})", ""]
    if ready or partial:
        md += ["## Forms waiting for you", *[f"- READY, review and submit: **{r['company']}** - {r['title']}" for r in ready], *[f"- needs a few fields: **{r['company']}** - {r['title']}" for r in partial], ""]
    md += [f"## Improve your odds (~{sum(t.get('minutes', 0) for t in plan.get('tasks', []))} min)"]
    for t in plan.get("tasks", []):
        md += [f"- [ ] **{t['title']}** ({t.get('minutes')} min) - {t.get('why')}  \n  proof: {t.get('proof')}"]
    md += ["", "## Outreach", *[f"- **{o.get('who_or_where')}**: {o.get('action')}" for o in plan.get("outreach", [])], "", f"_{plan.get('thought','')}_", "",
           "## High-stakes watchlist (not applying yet; referral/relationship first)"]
    md += [f"- **{w['company']}** - {w['title'][:70]} | referral: {w['referral_state']} | {('deadline/window: ' + w['deadline']) if w['deadline'] else ''}" for w in watch] or ["- nothing yet"]
    md += ["", "## Programs tracked (pre-doc / residency / fellowship)"]
    md += [f"- **{p['name']}** ({p['org']}, {p['kind']}) window: {p['window']} | eligible: {p['eligibility']} | {p['url']}" for p in progs] or ["- none yet"]
    md += ["", f"{newc} new strong matches since this morning. Open out/dashboard.html for everything else."]
    open(path, "w", encoding="utf-8").write("\n".join(md))
    return path, plan, len(ready), len(watch)


def morning_toast(force=False):
    c = db()
    if not force and meta_get(c, "toast_date") == today():
        return
    path, plan, nready, nwatch = build()
    t = plan.get("tasks", [])
    notify("JobAgent: today's plan", f"{len(t)} improvement tasks ({sum(x.get('minutes', 0) for x in t)} min), {len(plan.get('outreach', []))} outreach, {nready} form(s) ready to submit, {nwatch} high-stakes on watch. Open out/today.md")
    meta_set(c, "toast_date", today())
    log(f"today page: {path}")
