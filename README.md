# JobAgent

A local, Claude-powered job-search and application-prep agent. It finds roles within about 30 minutes of them going live, judges fit, writes cover letters and answers, picks the right resume, fills the application form in a dedicated Chrome window, scouts LinkedIn (read-only) for the best people to ask for a referral, tracks prestigious roles you should not cold-apply to, and tells you every morning what to do to improve your odds.

**It never presses Submit. You do.**

It runs on your Claude subscription through the headless `claude -p` CLI (model: Sonnet 5.5). Everything else is Python standard library; no `pip install`.

---

## 1. Quick start

Prerequisites (already set up on this machine): Windows, Python 3.12+, the `claude` CLI signed in to your subscription, Node.js (Playwright MCP is fetched by `npx`), Google Chrome.

```
cd C:\path\to\JobAgent
python agent\run.py gui            # open the GUI (http://127.0.0.1:8777), or double-click start_gui.bat
python agent\run.py cycle          # or run one full cycle from the terminal
```

First time only, in the **JobAgent Chrome** (it opens by itself on the first fill/LinkedIn/YC step, own profile in `data/chrome-profile`): log in to LinkedIn and Work at a Startup once. The sessions persist.

Always-on: `python agent\run.py install-schedule` creates the Windows task `JobAgentCycle`, which runs a cycle every `poll_every_minutes` (30). `run.py disable-schedule` / `enable-schedule` pause and resume it.

---

## 2. What a cycle does

`python agent\run.py cycle` (the scheduler runs exactly this; a lock file prevents overlap):

| # | Step | Claude? | What happens |
|---|---|---|---|
| 1 | **Poll** | no | ~150 company boards on public ATS feeds (Greenhouse, Lever, Ashby, SmartRecruiters, Workable) plus **official career sites** of big companies: Microsoft, NVIDIA, Qualcomm, Micron, Netflix (Eightfold), Atlassian, Amazon (India, Singapore, UK, Germany, Canada, US), and Workday tenants (NVIDIA, Intel, Salesforce, Adobe, Micron, Cisco, HP, Capital One, Mastercard, Broadcom/VMware, Samsung, PayPal, Workday, Zoom, Cadence). Also YC role pages, Hacker News (jobs + Who is hiring), VC portfolio boards (Getro), SimplifyJobs + speedyapply GitHub lists, Remotive, RemoteOK. About 25k postings per poll; duplicates across sources are removed. Google, Apple, Meta, Uber and Oracle publish no usable feed: the daily web scout covers them and the dashboard lists their search links. |
| 2 | **Filter + score** | no | `config/filters.json`: title family (ML systems, ML/AI engineer, data science, SWE, low-level, research, safety), drops senior / PhD-only / non-engineering titles, reads required years of experience, early-career markers, location (India / remote / relocate) and visa-sponsorship signals. Track priority weights your order: MLSys > AI/ML eng, DS, applied > SWE > research > safety. |
| 3 | **Instant alert** | no | A brand-new posting scoring at least `instant_alert_score` triggers a desktop toast immediately. |
| 4 | **Watchlist** | no | Prestige roles (frontier-lab fellowships/residencies, pre-docs, safety programs) move to the high-stakes watchlist (section 6). |
| 5 | **Triage** | yes | Batches of 15 postings get APPLY / STRETCH / SKIP, a fit score, the resume variant, and the exact YOE or visa gap. Tuned for a numbers game (generous). |
| 6 | **Scout** | yes | Once a day: open-web search for X/Twitter hiring posts, bot-blocking career pages (Google, DeepMind, Meta, Microsoft, Apple, Mistral, IBM...), pre-docs / residencies / fellowships (corporate, neolab, academic, research orgs, safety programs), and new companies worth tracking (`out/new_companies_to_review.md`). |
| 7 | **Packets** | yes | For APPLY/STRETCH jobs: reads the real posting and company pages and writes `out/applications/<date>_<company>_<role>/` with the cover letter (md + PDF), answers, real form questions (Greenhouse API), resume copy, honest odds, gaps and an eligibility check. A hard stop (no sponsorship where you need it, PhD-only...) marks the job `ineligible`. |
| 8 | **Referral plans** | yes | Watchlist first, then packets. LinkedIn scouting (section 7), else public-web research. Output: `REFERRAL.md` in the packet folder. |
| 9 | **Form fill** | yes | APPLY packets: opens the form in the JobAgent Chrome, fills every field, uploads resume / cover letter / transcript, declines EEO questions, leaves unknowns blank, reports `READY_TO_SUBMIT` / `PARTIAL` / `BLOCKED`, then notifies you. |
| 10 | **Today page + dashboard** | yes (small) | `out/today.md`, `out/dashboard.html`, `out/digest-<date>.md`, and a morning toast. |

Operating stance: a **numbers game on normal-stakes roles** (apply broadly, fast), **referral-first on high-stakes roles**, and a **daily improvement plan** sized to your 5 hours.

---

## 3. The GUI

`python agent\run.py gui` (or double-click `start_gui.bat`) opens `http://127.0.0.1:8777`. It listens on localhost only; every state-changing request needs a custom header and only whitelisted commands can run. Light and dark themes (More > Toggle light / dark), mobile-friendly, keyboard: `/` focuses search, `Esc` closes the detail panel.

**Layout.** A sidebar with live counts, a search bar, `Run full cycle`, and a `More` menu (Poll, Triage, Web scout, Rebuild today's plan, Rebuild dashboard, Refresh YC profile, theme).

**Views (sidebar)**

| View | What you see |
|---|---|
| **Today** (landing) | Greeting, headline numbers (ready to submit, need fields, best matches, watchlist, new strong), the forms waiting for you, today's improvement plan as a tick-off checklist (ticks are remembered for the day), outreach to do, and the top watchlist items. |
| **Ready to submit** | Forms filled in the JobAgent Chrome. Open the tab, review, press Submit yourself, then `Mark applied`. |
| **Needs you** | Partial or blocked fills. Open the card's Details > Form fill report to see exactly which fields are waiting. |
| **Watchlist** | Prestige roles (fellowships, residencies, pre-docs, safety programs): tracked, not applied to. Main action: `Plan referral`. |
| **Prepared** | Applications with a packet but no filled form yet. Main action: `Fill the form`. |
| **Best matches** | Triaged APPLY/STRETCH jobs without a packet yet. Main action: `Prepare application`. |
| **New** | New postings with a high heuristic score, not triaged yet. |
| **Applied** | What you marked as applied. |
| **Programs** | Pre-docs, residencies, fellowships with windows and eligibility, with links. |
| **Activity** | The live agent log. |

**Job cards.** Company avatar, title (click for details), chips for location type (India / Remote / Relocate), visa-sponsorship signal, verdict (APPLY / STRETCH), the resume variant chosen, a fit ring (0-100), and the one-paragraph reason with the YOE/visa gap. Buttons: the contextual main action, `Details`, `Referral plan`, `Mark applied`, `Skip`. Filter chips (location, Tier S/A) and a sort (best fit / newest) sit above the list.

**Detail panel (click a title or `Details`).** Tabs: **Application** (the packet: odds, strengths, gaps, company notes, answers), **Cover letter** (copy button), **Referral & outreach** (strategy, targets, connection notes, DMs, follow-ups; hover any paragraph for a `Copy` button), **Form fill report**. If something does not exist yet, the tab offers a `Generate` button.

A click on any action runs in the background (toast confirms it); the sidebar footer shows today's Claude calls and what is running.

Typical flow: open **Today**, work through **Ready to submit**, skim **Best matches** and press `Prepare application`/`Fill the form` on the ones you like, copy messages from **Referral & outreach** and send them yourself, tick off the plan.

---

## 4. The CLI

Run from `C:\path\to\JobAgent` as `python agent\run.py <command>`. Job ids are shown in `top`, the dashboard and the GUI.

| Command | What it does |
|---|---|
| `cycle` | Full pipeline (section 2). What the scheduler runs. |
| `poll` | Fetch + score + store + instant alerts. No Claude. |
| `triage` | Claude judges the best untriaged postings (respects the daily cap). |
| `scout` | Force the web scout now (jobs, programs, new companies). |
| `packet <id>` | Build the application packet for one job. |
| `referral <id>` | Referral plan for one job (LinkedIn scouting if the daily LinkedIn cap allows, else public-web research). Alias: `linkedin <id>`. |
| `fill <id>` | Fill the application form in the JobAgent Chrome. Never submits. |
| `today [--force]` | Build `out/today.md`: forms waiting, improvement tasks, outreach, watchlist, programs. |
| `top [n]` | Print the best current matches with ids. |
| `watch` | List the high-stakes watchlist. |
| `status` | Counts by status and today's usage per step. |
| `usage` | Claude tokens and list-price-equivalent cost by step (today / 7 days). |
| `dashboard` | Rebuild `out/dashboard.html` and the markdown digest. |
| `mark <id> applied\|skip\|dismissed` | Record your decision (applied jobs leave the queues). |
| `yc-profile` | Rewrite your Work at a Startup profile from `config/profile.json` facts only; before/after in `out/yc_profile_before_after.md`. |
| `gui [--no-open]` | Local web GUI. |
| `notify-test` | Send a test notification. |
| `install-schedule` / `remove-schedule` | Create / delete the Windows scheduled task. |
| `enable-schedule` / `disable-schedule` | Pause / resume it without deleting. |

Examples:

```
python agent\run.py top 30                        # what is best right now
python agent\run.py packet "<id>"                 # prepare one application
python agent\run.py referral "<id>"               # who to message and what to say
python agent\run.py fill "<id>"                   # fill the form, then review and submit it yourself
python agent\run.py mark "<id>" applied
```

---

## 5. Submitting safely (the one rule)

The agent fills forms but cannot submit:

1. The fill prompt forbids the final button and forbids creating accounts, solving CAPTCHAs and email verification (it reports `BLOCKED` instead). Legal attestations and eligibility declarations are left for you unless the exact answer is in your profile (the export-control "not a U.S. person" answer is).
2. A guard script injected into every page of the agent browser swallows `submit` events, final-button clicks ("Submit application", "Send", "Finish"...) and application-POST requests for `guard_minutes` (9), with a red banner showing the unlock time. "Next / Continue / Save" still work, so multi-step forms can be filled.
3. The fill process has a hard timeout shorter than the guard window. After every fill a watchdog notifies you if a confirmation page appears in the browser, so a submission can never go unnoticed.

After the banner disappears, press Submit yourself in that tab. Each fill session's transcript is kept so you can audit what the agent clicked (`research/audit_sessions.py`).

---

## 6. High-stakes watchlist (prestige roles)

Frontier-lab fellowships/residencies, pre-docs (Google DeepMind, Meta FAIR, Microsoft Research, neolabs, academic institutes, research orgs) and AI-safety programs are **never auto-applied**. They are tracked with their window and eligibility, a draft packet is prepared (never filled), the referral plan comes first, and `out/today.md` pushes daily tasks and outreach to raise your odds. You decide when to apply. The rules live in `settings.json -> high_stakes` (title patterns, categories, companies). To drop an entry, `mark <id> skip`.

---

## 7. Referrals, LinkedIn and YC

**LinkedIn (read-only).** `agent/linkedin.py` drives the JobAgent Chrome, logged in as you:

1. Searches your **1st-degree** connections at the target company; if none, **2nd-degree** (mutual connections give an intro path).
2. Opens at most 4 profiles (about 10 page loads per company, with paced waits) and judges who is most likely to respond: relevant team, approachable seniority, activity, shared background.
3. Writes connection notes, DMs, follow-ups, an ask a mutual connection could forward, and a blurb a referrer can paste. **You send every message yourself.**

An always-on page guard blocks Connect / Message / Follow / Like / Comment / Send / Endorse. Cap: `max_linkedin_per_day` (5). This is automated use of a logged-in session, which LinkedIn's terms discourage; keep the cap modest. If you also drop LinkedIn's data export (zip or `Connections.csv`, any date-versioned filename) into `data/` or `data/linkedin/`, the newest one is used to prioritise people you already know.

**YC Work at a Startup.** `run.py yc-profile` rewrites your profile with truthful facts only (on the first run it removed unsupported claims) and never touches privacy, visibility or account settings. The YC role pages are also a job source, with visa-sponsorship and minimum-experience fields.

---

## 8. Configuration

Edit files in `config/` (no code changes needed). Restart the GUI after changes; scheduled cycles pick them up on their next run.

**`settings.json`**

| Key | Meaning |
|---|---|
| `claude_model`, `claude_model_packets`, `claude_model_fill`, `today_model` | Model per stage (all `claude-sonnet-5-5`). |
| `poll_every_minutes` | Scheduler interval (re-run `install-schedule` after changing). |
| `min_heuristic_score_for_llm`, `instant_alert_score` | Funnel width and alert threshold. |
| `max_triage_calls_per_day`, `triage_batch_size`, `max_triage_per_cycle`, `triage_desc_chars` | Triage volume (calls x batch = postings judged per day). |
| `max_packets_per_day`, `packet_min_fit` | How many applications get prepared, and the lowest fit that qualifies. |
| `max_referrals_per_day`, `max_linkedin_per_day`, `linkedin_browse` | Referral plans and LinkedIn scouting volume. |
| `max_fills_per_day`, `auto_fill`, `fill_window_local_hours` | Form fills per day, on/off, and the hours when the browser may open. |
| `scout_every_days` | Web scout cadence (1 = daily). |
| `guard_minutes`, `chrome_debug_port`, `gui_port` | Submit-lock window, agent Chrome port, GUI port. |
| `notify.windows_toast`, `notify.ntfy_topic` | Desktop toasts; optional phone push via ntfy.sh (third-party service; titles only). |
| `high_stakes` | Watchlist rules. |

**`filters.json`**: title families with base weights (your track order), exclusion patterns, early-career markers, niche keywords (CUDA, Triton, inference...), India / remote markers, YOE thresholds.
**`companies.json`**: boards to poll (`tier` S/A/B/C, `category`, `ats`, `slug`). Add a company by copying a row; new-company suggestions land in `out/new_companies_to_review.md`. Regenerate the research table with `python research\make_report.py` (`research/TARGET_COMPANIES.md`).
**`profile.json`** (gitignored; `profile.example.json` is the template): your facts, resume variants, hard rules, form defaults, preferences, Quiet Intelligence details, academics, transcript path. Anything empty in `form_defaults` is never guessed: it appears under "Needs you". After your re-exam, update the CGPA/backlog text and replace the file in `data/transcripts/`.

---

## 9. Token economy and limits

- Headless calls run in **lean mode**: no user MCP servers, no skills, no default system prompt, only the built-in tools needed. A trivial call costs under 1k input tokens instead of about 27k.
- Polling, filtering, dedup and the watchlist rules cost nothing.
- Browser-driven steps (form fill, LinkedIn, YC) are the expensive ones (hundreds of thousands of cached input tokens per run). Lower `max_fills_per_day` / `max_linkedin_per_day` if your plan runs short.
- `run.py usage` (and the GUI status line) show consumption. Costs are list-price equivalents; a subscription is rate-limited, not billed per token.
- Current limits are set for a numbers game: 16 triage calls (up to about 240 postings judged), 12 packets, 6 referral plans, 5 LinkedIn companies, 8 form fills, daily scouting.

---

## 10. Files

```
agent/      run.py (CLI)  gui.py + gui_static/index.html (GUI)  feeds.py  sources_extra.py  sources_big.py  scoring.py  pipeline.py  stakes.py  packets.py  referrals.py
            linkedin.py  fill.py  browser.py  scout.py  today.py  yc_profile.py  dashboard.py  common.py
config/     settings.json  filters.json  companies.json  profile.json (local)  profile.example.json
data/       jobs.db (SQLite state)  chrome-profile/  transcripts/  linkedin/   (local, gitignored)
out/        applications/<date>_<company>_<role>/  (README.md packet, cover_letter.pdf, REFERRAL.md, FILL_REPORT.md, resume)
            dashboard.html  today.md  digest-<date>.md  notifications.log  new_companies_to_review.md  yc_profile_before_after.md
logs/       agent.log  gui_runs.log
research/   TARGET_COMPANIES.md and the probes used to discover job-board feeds
```

---

## 11. Troubleshooting

- **A feed is missing or empty:** the poll log (`logs/agent.log`) lists failing sources; ATS boards occasionally rename slugs. Fix the slug in `companies.json`.
- **A fill stops at BLOCKED:** the site wants an account (Workday-style) or a CAPTCHA/verification. Create the account yourself, or use the packet to apply manually.
- **A required dropdown does not list your school:** the agent picks "Other" when offered, otherwise leaves it and lists it under "Needs you".
- **Chrome did not open:** the agent starts Chrome with `--remote-debugging-port=9333` using `data/chrome-profile`; if the port is busy, close that Chrome window and retry.
- **Cycle "already running":** `data/cycle.lock` expires after 100 minutes; delete it if a run crashed.
- **Too many / too few results:** adjust `min_heuristic_score_for_llm`, `packet_min_fit`, the title patterns in `filters.json` and the daily caps.
- **Want it quiet:** `run.py disable-schedule`, or set `auto_fill` to false so forms are only filled when you press `Fill form`.

---

## 12. Limits worth knowing

- X/Twitter has no free API and scraping it is against its terms: it is covered by the daily web scout plus a saved X search link on the dashboard.
- Cover letters are drafts: skim each once. The honesty rules in `profile.json -> hard_rules` (no overclaiming TernixEngine, Amazon placement or Aegis scope; CGPA and backlog stated truthfully) are enforced in the prompts, not by code.
- Salary, start-date details beyond the profile, AI-tool-use disclosures and legal attestations that are not in the profile are always left for you.
