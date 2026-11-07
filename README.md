# JobAgent

A local job-search and application-prep agent that runs on your Claude subscription (headless `claude -p`). It finds new roles within about 30 minutes of them going live, judges fit honestly, prepares the cover letter and answers, picks the resume, plans referrals, fills the application form in a dedicated Chrome window, and then **notifies you to press Submit**. It never submits.

Everything is Python standard library plus the `claude` CLI, Node (for Playwright MCP) and Chrome. No pip installs.

## What runs every 30 minutes (`agent/run.py cycle`)

1. **Poll** ~100 company job boards (Greenhouse, Lever, Ashby, SmartRecruiters), Workday (NVIDIA, Intel, Salesforce, Adobe), Amazon, YC role pages, Hacker News (job stories + Who is hiring), VC portfolio boards (Getro), SimplifyJobs and speedyapply GitHub lists, Remotive and RemoteOK. About 21k postings per poll. No Claude used here.
2. **Filter** with `config/filters.json`: title family (ML systems, safety, research, ML eng, data science, SWE, low-level), drops senior/PhD-only/non-engineering titles, reads years-of-experience, early-career markers, location (India / remote / relocate), and visa-sponsorship signals. New strong matches trigger an **instant desktop alert**.
3. **Triage** (Claude): batches of 10 get a calibrated APPLY / STRETCH / SKIP, fit score, the resume variant, and the exact YOE or visa gap. Capped at 14 calls/day.
4. **Scout** (Claude, once a day): searches the open web for what no API exposes: X/Twitter hiring posts, Google/DeepMind/Meta/Microsoft/Apple/Mistral career pages, 80,000 Hours and aisafety.com boards, fellowships, and **newly funded companies to add** (`out/new_companies_to_review.md`).
5. **Packet** (Claude, up to 6/day): reads the real posting and company pages, writes a cover letter (md + PDF via tectonic), answers, real form questions (Greenhouse API), honest odds and gaps, and lists what only you can answer. Folder: `out/applications/<date>_<company>_<role>/`.
6. **Referral plan** (Claude, up to 4/day): matches your LinkedIn connections at that company, researches the best people on the public web, drafts connection notes, DMs, follow-ups and a forwardable blurb (`REFERRAL.md`).
7. **Fill** (Claude + Playwright MCP, up to 4/day, 08:00-23:00): opens the application in the *JobAgent Chrome* (own profile, port 9333), fills every section, uploads the resume and cover letter, declines EEO questions, leaves anything it does not know blank, and reports `READY_TO_SUBMIT` / `PARTIAL` / `BLOCKED` in `FILL_REPORT.md`. Then it notifies you.
8. **Dashboard**: `out/dashboard.html` (ready-to-submit, needs-help, packets, triaged, plus links to sites to check by hand) and `out/digest-<date>.md`.

## Submit safety (three layers)

- The fill prompt forbids pressing the final button and forbids creating accounts, solving CAPTCHAs or doing email verification (it reports BLOCKED instead).
- A guard script is injected into every page the agent browser opens: it swallows `submit` events, final-button clicks ("Submit application", "Send", "Finish"...) and application-POST fetch/XHR calls for 9 minutes, with a visible red banner showing when it unlocks. Tested: "Submit application" click and POST were blocked, "Next" passed.
- The fill process has a hard timeout shorter than the guard window. After the guard expires you can press Submit yourself in that tab.

## Commands

```
python agent/run.py cycle                # everything (what the scheduler runs)
python agent/run.py top 30               # best current matches
python agent/run.py status | dashboard
python agent/run.py packet <id>          # ids are shown in the dashboard / `top`
python agent/run.py referral <id>
python agent/run.py fill <id>
python agent/run.py scout
python agent/run.py mark <id> applied|skip
python agent/run.py install-schedule     # Windows Task Scheduler, every 30 min   (remove-schedule undoes it)
```

Tune behaviour in `config/`: `settings.json` (caps, hours, notifications), `filters.json` (what counts as a target role), `companies.json` (watched boards), `profile.json` (facts the agent may use, hard rules, form defaults). Anything empty in `profile.json -> form_defaults` (salary, earliest start, pronouns) is never guessed; it is left blank and listed under "Needs you".

## One thing you need to do for referrals: export your LinkedIn connections

LinkedIn forbids automated browsing of logged-in sessions (risk: account restriction, which would hurt you most), so the agent does not scrape LinkedIn. You get the same result with LinkedIn's own export:

LinkedIn -> Settings & Privacy -> Data privacy -> **Get a copy of your data** -> tick **Connections** -> request archive. When the email arrives (minutes to hours), drop the zip (or its `Connections.csv`) anywhere in `data/` or `data/linkedin/`; date-versioned names are fine and the newest file wins. From then on every referral plan starts with the people you already know at that company, ranked by relevance, researched on the public web, with messages written. Without the file it falls back to finding warm paths on the public web (team members, paper authors, maintainers), as in the Together AI example.

## Phone notifications (optional)

Desktop toasts work out of the box. For your phone, install the free ntfy app, pick a private topic name, and set `notify.ntfy_topic` in `config/settings.json`. Note that ntfy.sh is a third-party service; messages contain company and role names only.

## Known limits

- X/Twitter has no free API and scraping it is against its terms: it is covered through Claude's public web search once a day plus a saved X search link on the dashboard.
- Workday-style multi-page applications that require an account are reported as BLOCKED; the packet is still ready to paste.
- Salary, start dates, AI-tool-use disclosure and visa specifics beyond `profile.json` are always left for you.
- Cover letters are drafts: read them once. The hard rules in `profile.json` (no overclaiming TernixEngine, Amazon placement, Aegis scope) are enforced in the prompts, not by code.
- Company list: `research/TARGET_COMPANIES.md` (regenerate with `python research/make_report.py`).
