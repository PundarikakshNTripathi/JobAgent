# JobAgent

A local, provider-agnostic agent for the job search. It watches hundreds of company career pages, judges what fits you, prepares the application (cover letter, answers, resume advice), plans who to contact for a referral and writes the messages in your voice, fills the application form in a separate browser, and tells you every morning what to do to raise your odds.

**It never presses Submit. You do.** Your data stays on your machine.

Works with the AI tools you already have: **Claude Code, Google Antigravity (Gemini), Codex, Gemini CLI, any OpenAI-compatible API (OpenAI, OpenRouter, Ollama, LM Studio, the Gemini API), the Anthropic API, Hermes Agent, oh-my-pi, or any command-line tool.** You choose which model does which job.

Standard library Python only; no `pip install`. Windows is the primary target (toasts, scheduler); the core runs anywhere.

---

## 1. Quick start

```
python agent/run.py init        # creates your private workspace and profile (or use the GUI wizard)
python agent/run.py doctor      # shows which model providers, browser and scheduler are ready
python agent/run.py gui         # opens http://127.0.0.1:8777 and starts a guided tour
python agent/run.py cycle       # one full run from the terminal (the GUI has a button too)
python agent/run.py install-schedule   # optional: run automatically (Windows Task Scheduler)
```

You need at least one model provider: install a CLI (Claude Code, Antigravity `agy`, Codex, Gemini CLI) or set an API key (`OPENAI_API_KEY`, `OPENROUTER_API_KEY`, `GEMINI_API_KEY`, `ANTHROPIC_API_KEY`) or run Ollama. Form filling and LinkedIn scouting need a browser-capable provider (Claude Code today) plus Chrome or Edge.

### Your workspace (private)

All personal data lives in a **workspace**, never in the code folder: profile, settings, database, applications, referral plans, voice samples, memory, browser profile, logs. It is created on first run at `~/JobAgent`, or wherever `$JOBAGENT_HOME` or a one-line `.jobagent_home` file points. The repository ships only defaults, templates and skills, so you can update the code freely and publish it without leaking anything. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## 2. What it does

`python agent/run.py cycle` (the scheduler runs exactly this):

| # | Step | Uses a model? | What happens |
|---|---|---|---|
| 1 | **Inbox** | yes | Processes anything you pasted (see section 5). |
| 2 | **Poll** | no | About 150 company boards (Greenhouse, Lever, Ashby, SmartRecruiters, Workable), official career sites (Google, Microsoft, NVIDIA, Qualcomm, Micron, Netflix, Atlassian, Amazon, 15 Workday tenants), YC role pages, Hacker News, VC portfolio boards, GitHub internship lists, remote boards. About 25k postings per poll, deduplicated. |
| 3 | **Filter + score** | no | Title family, level, years of experience, early-career markers, location realism (home country / remote / relocate; drops places that need a work permit you cannot get), visa-sponsorship signals, your learned preferences. Strong new matches trigger an instant alert. |
| 4 | **Watchlist** | no | Prestige roles (fellowships, residencies, pre-docs, safety programs, frontier-lab programs) are tracked, never auto-applied. |
| 5 | **Triage** | yes | Batches get APPLY / STRETCH / SKIP, a fit score, the resume to use and the exact gap. |
| 6 | **Scout** | yes | Every few days: open-web search for X/Twitter hiring posts, bot-blocking career pages, pre-docs/residencies/fellowships, newly funded companies. |
| 7 | **Packets** | yes | Reads the real posting, writes the cover letter (md + PDF if `tectonic` is configured), answers, real form questions, honest odds and gaps, eligibility check. Strong fits also get resume advice. |
| 8 | **Referral plans** | yes | For **every** APPLY/STRETCH job: who to contact (1st-degree, 2nd-degree via mutual connections, or public-web paths), what they work on, and messages in your voice. |
| 9 | **Form fill** | yes | Opens the form in a dedicated browser, fills every section, uploads your files, declines EEO questions, leaves unknowns blank, reports READY / PARTIAL / BLOCKED. Account-wall systems (Workday...) are detected up front and skipped. |
| 10 | **Reflect, plan, dashboard** | yes (small) | Updates the agent's memory from your outcomes, builds today's plan, a morning toast, and `out/dashboard.html`. |

---

## 3. Choosing models (provider and agent agnostic)

**GUI: Settings → Which model does which job.** Each task has its own provider, model and effort. Providers are detected automatically; press *Test* to check one. If a provider is missing, fails or hits its daily budget, the next in the fallback chain is used.

| Task | What it does | Good fit |
|---|---|---|
| triage, ingest, chat | fast judgement and conversation | a fast, well-calibrated model |
| packet, outreach, resume | writing in your voice | your best writer |
| referral, scout | research and synthesis | any model; non-browsing models get JobAgent's own web retrieval |
| today, reflect | plans and memory | cheapest capable model |
| fill, linkedin, yc | drive a browser | browser-capable provider (Claude Code) |

`python agent/run.py bench` compares providers **on your own data** (triage agreement, speed, tokens; whether research URLs are alive and mention the company). Use it instead of trusting claims. Details, adapter status and measured numbers: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

Adding a tool JobAgent doesn't know: choose provider `custom` (or Hermes / omp presets) and set its command in the settings file, e.g. `"llm": {"custom": {"custom": {"cmd": ["mytool", "--print"], "prompt_via": "stdin"}}}`.

---

## 4. The GUI

`python agent/run.py gui` (or `start_gui.bat`). Localhost only. First run: a setup wizard, then a guided tour (re-run it from **More → Take the tour**). Light/dark themes, works on a phone-width window, `/` focuses search.

| Group | View | What you do there |
|---|---|---|
| Work | **Today** | headline numbers, forms waiting for you, the day's improvement plan (tick items off), outreach to do |
| | **All jobs** | a job-board style browser over *every* tracked posting, analysed or not. Every row links straight to the employer's posting. Filters: keywords, role family, level (internship / new grad / early career / experienced), location type, visa-sponsorship signal, company tier, AI-analysed or not, company, source, first seen, minimum match, has referral plan / packet; sort and page size. Jobs the AI hasn't reviewed yet show the posting excerpt and an *Analyse & prepare* button |
| | **My picks** | everything *you* chose to work on (pressed Prepare, Referral plan, Resume advice or Fill, or added through the Inbox), with a progress tracker per job (application, referral plan, resume advice, form filled) and a *Ready / In progress* split. Automatic work from the scheduler stays in the other lists |
| | **Ready to submit** / **Needs you** | review filled forms and press Submit yourself; see which fields are waiting |
| | **Prepared** / **Best matches** / **New** | one-click *Prepare application*, *Fill the form*, *Referral plan*; skip with a reason (teaches the agent) |
| | **Applied** | what you marked applied |
| Network | **Watchlist** / **Programs** | prestige roles and fellowships: referral first, deadlines, eligibility |
| Tools | **Inbox** | paste links, job text or messages you found anywhere, plus your context |
| | **Ask the agent** | chat about your pipeline; it proposes actions, you confirm each |
| | **My voice** | paste messages you really sent and your draft→final edits; see what the agent has learned |
| Setup | **Usage & cost** | tokens, calls, seconds and cost per provider and task, last 14 days |
| | **Settings** | providers, model routing, schedule frequency, limits, budgets per provider, notifications, privacy-relevant toggles |
| | **Help**, **Activity** | short docs, live log |

Click a card title for the **detail panel**: Application (odds, strengths, gaps, answers), Cover letter, Referral & outreach (hover any paragraph to copy), Resume suggestions, Form fill report.

---

## 5. Things you find yourself: Inbox, chat, agent loop

- **Inbox** (GUI) or `run.py inbox add "<text or link>" "<your context>"`: the agent extracts the opportunities, fetches the pages, judges fit (your items jump the queue), and for promising ones prepares the packet and referral plan.
- **Ask the agent** (GUI) or `run.py chat "..."`: knows your profile, memory, pipeline, inbox and picks. Write what you want in plain words, or use **slash commands** (below). Whatever it starts runs as a hidden background job and the **result is posted back into the chat** when it finishes: the people found (names, profile links, how to reach each) or the honest reason it failed. Prep actions (packet, referral, LinkedIn scouting, resume, scout, today, poll, yc-profile, inbox retry, remember, add) start right away because you asked for them; `fill`, `mark` and `agent` run from plain words only when your words call for them, otherwise you get a confirm button. Nothing ever submits an application.

### Chat slash commands

Type `/` in the chat box for a menu; `/help` lists everything; the *Commands* panel under the chat box is a clickable tour. They run instantly and need no model, so they work even when every provider is out of quota. `<id or words>` is a job id (from `/top` or `/jobs`) or company/title words, e.g. `/referral datadog applied science`; if the words match several jobs, they are listed and you repeat with an id.

| Group | Commands |
|---|---|
| Basics | `/help [cmd]` `/status` `/usage` |
| Find | `/top [n]` `/jobs <words>` `/picks` `/watch` |
| Prepare | `/packet <job>` `/referral <job>` `/linkedin <job>` `/resume <job>` `/fill <job>` |
| Track | `/mark <job> applied\|skip\|dismissed [reason]` `/add <link or text> [-- context]` `/retry` `/inbox` `/remember <fact>` |
| Run | `/scout` `/today` `/poll` `/cycle` `/yc-profile` `/reflect` `/agent <goal>` |

`/linkedin <job>` opens your logged-in agent Chrome **read-only** and looks at 1st-degree, then 2nd-degree, people at the company; it reports real names, profile links and how to approach each. If LinkedIn cannot be used (browsing switched off, the Claude quota is out, you are logged out), the plan says so on its first line instead of quietly handing you generic web advice. `/referral` uses LinkedIn too when allowed. Jobs you start by hand ignore the daily LinkedIn cap, which only limits scheduled background scouting.
- **Agent loop** `run.py agent "<goal>"`: a bounded ReAct loop (default 12 steps) over tools such as search jobs, get job, packet, referral, resume, mark, remember, web search, fetch, inbox. It works with any provider. It cannot submit applications, change settings or touch the schedule.

---

## 6. Messages that sound like you

Outreach is written in a separate step from research, using a style playbook of machine "tells" to avoid, **your own sent messages** (`My voice`), and **how you edited past drafts**. Output is linted (stock phrases, dashes as punctuation, lists, exclamation marks, repetitive "I" openings), auto-fixed, and rewritten once if needed. Cover letters and form answers get the same treatment. The more you use *My voice* and mark edits, the less you need to change. Playbooks live in `defaults/skills/`; put your own in `<workspace>/skills/` to override them.

---

## 7. Referrals, LinkedIn, YC

- **Referral plans** are made for every APPLY/STRETCH job: 1st-degree connections first, 2nd-degree with a mutual connection who can forward a two-line ask, then public-web paths (team members, paper authors, maintainers). Output is `REFERRAL.md` in the job's folder, with a short and a longer DM, a connection note for cold contacts, one follow-up, and a blurb a referrer can forward.
- **LinkedIn scouting** (off by default; enable in Settings): drives your logged-in browser **read-only** (an always-on guard blocks Connect/Message/Follow/Like/Comment/Send), about 10 page loads per company, paced, capped per day. **You send every message yourself.** This is automated use of a logged-in account, which LinkedIn's terms discourage; keep the cap low. You can also drop LinkedIn's data export (zip or `Connections.csv`) into `<workspace>/data/` and it will be used to prioritise people you already know.
- **YC Work at a Startup**: `run.py yc-profile` rewrites your profile from your own facts only (never invents; never touches privacy or account settings).

---

## 8. Submitting safely

1. The fill prompt forbids the final button, account creation, CAPTCHAs and verification.
2. A page guard blocks submit events, final-button clicks and application POSTs for `guard_minutes`, with a visible banner; Next/Continue/Save still work.
3. A watchdog reports any confirmation page that appears in the agent browser.
4. Legal attestations and eligibility declarations are left for you unless the exact answer is in your profile.
5. Prestige roles are never auto-applied; chat and the agent loop cannot submit.

---

## 9. CLI reference

`python agent/run.py <command>`; job ids appear in the GUI, `top` and `status`.

| Command | What it does |
|---|---|
| `init` / `doctor` | create workspace + profile / check providers, browser, scheduler |
| `cycle` | full pipeline |
| `poll` / `triage` / `scout` | single stages (`poll` costs no tokens) |
| `top [n]` / `watch` / `status` / `usage` | what to look at / watchlist / counts / model usage per provider and task |
| `packet <id>` / `referral <id>` / `resume <id>` / `fill <id>` | prepare / plan contacts / tailor resume advice / fill form (never submits) |
| `linkedin <id>` | read-only LinkedIn scouting for that job (people, profile links, approach); warns if it could not run |
| `picks` | add earlier hand-prepared jobs to *My picks* (safe to repeat) |
| `mark <id> applied\|skip\|dismissed [reason]` | record your decision; reasons teach the agent |
| `inbox [add "<text>" "<context>"]` / `chat "<message>"` / `agent "<goal>"` | bring in outside opportunities / talk / run a goal |
| `today [--force]` / `dashboard` / `reflect` | daily plan / dashboard / update memory now |
| `providers [test <name>]` / `bench [n]` | list or test providers / benchmark on your data |
| `gui [--no-open]` / `yc-profile` | web GUI / YC profile |
| `install-schedule` / `remove-schedule` / `enable-schedule` / `disable-schedule` / `set-interval <min>` | scheduling |

---

## 10. Configuration

Edit in the GUI (Settings) or in `<workspace>/config/settings.json` (only your overrides; shipped defaults are in `defaults/settings.json`). Highlights: `llm.routing` (provider/model/effort per task), `llm.fallbacks`, `llm.budgets` (daily calls/tokens per provider), `poll_every_minutes`, `cycle_minutes_budget` (time box for heavy model work per run, so polling stays fresh; the rest resumes next run) with `packets_per_cycle` and `referrals_per_cycle`, daily caps (`max_triage_calls_per_day`, `max_packets_per_day`, `max_referrals_per_day`, `max_linkedin_per_day`, `max_fills_per_day`), `auto_fill`, `fill_window_local_hours`, `web.search_backend` (`ddg` or `brave` with `$BRAVE_API_KEY`), `humanize`, `memory`, `notify` (desktop toast, ntfy push), `high_stakes` rules. Other overrides: `filters.json` (title families, weights, exclusions), `companies.json` (boards to poll; same name/slug replaces a default), `profile.json` (your facts, resume variants, hard rules, form defaults, preferences).

Anything empty in `profile.form_defaults` is never guessed: it appears under "Needs you".

---

## 11. Costs and limits

Polling, filtering, de-duplication and the watchlist rules use no tokens. Headless Claude calls run in lean mode (about 0.7k input tokens overhead instead of about 27k). Browser steps (form fill, LinkedIn, YC) are the expensive ones. Every call is logged by provider and task (**Usage & cost**); set per-provider daily budgets and the agent falls back to another provider when one is exhausted. Costs shown are list-price equivalents; subscriptions are rate-limited rather than billed per token.

---

### Balanced defaults

Out of the box a cycle runs every 3 hours and does a modest amount of model work: at most 2 packets and 1 referral plan per cycle, 10 packets, 8 referral plans, 3 LinkedIn scouts (background only) and 3 form fills per day, one scout every 2 days. Form filling and LinkedIn are the heavy steps, so they are the tightest. The recommended mix when you have both Claude and Antigravity: **Claude** for triage, chat, inbox, packets, outreach, resume, form filling, LinkedIn and YC (quality, and the only provider here that drives the browser); **agy (Gemini)** for referral research, scouting, the daily plan and reflection (slow, text-only work that spares your Claude window). Each falls back to the other, and a provider that reports its limit is paused until the reset. Raise the caps in Settings when you have headroom; `run.py usage` or `/usage` shows what each provider has done today.

### Quota-aware, and protecting your own budget

If a provider reports that its usage limit is reached (for example Claude's 5-hour window), JobAgent pauses that provider until the stated reset time, shows a banner in the GUI, and falls back to another provider where it can. Inbox items are stored and wait; they retry on their own (or press *Retry*). Scheduled background work is also held to a rolling allowance (`llm.budgets.<provider>.background_calls_per_5h`) so it cannot use up the window you need for your own requests (inbox, chat, manual clicks).

### Reading the full job description

Job-board feeds often carry only a short intro. JobAgent fetches the full posting for promising jobs (plain HTTP, no tokens), reads the **years of experience** (digits, words, ranges: "3+", "two years", "5-7 years") and the **visa sponsorship** wording, and re-scores. Roles that need 3+ years are dismissed (4+ for "fellowships"), 2 years is penalised hard, and the model that triages jobs is told to read the requirements section. The *All jobs* view has an "experience required" filter and shows a "N+ yrs required" tag.

### Abroad roles: logistics clarity ranks first

For jobs outside your home country, a clear sponsorship/relocation statement ranks above an unclear one (`visa_weights` in `filters.json`); home-country and remote roles rank above abroad roles with unclear logistics. YC's explicit visa field is parsed ("Will sponsor" and "US citizenship/visa not required" are good signs, "US citizen/visa only" is not).

### Silent background operation (Windows)

The scheduled task runs `pythonw` and every child process (model CLIs, PowerShell toasts, `schtasks`) is created with no console window, so nothing flashes on screen while you work. The agent's Chrome starts minimized and without taking focus.

### Company logos

Logos are fetched by the local server (DuckDuckGo's icon service, Google's as backup) and cached in `<workspace>/data/logos`. Your browser only talks to localhost; only company domain names leave the machine. Turn off with `"ui": {"logos": false}` in settings.

## 12. Troubleshooting

- `doctor` shows what is missing. A provider "not set up" needs its CLI on PATH or its API key in the environment variable shown.
- Fill says BLOCKED: the employer's system wants an account (Workday-style) or a CAPTCHA. Use the packet to apply manually.
- Headless `agy` sometimes tries to use tools it can't get permission for; JobAgent tells it not to and falls back to the next provider.
- Chrome profile is in use and you want to move the workspace: close the agent's Chrome window first (`chrome_profile_dir` in settings points to it).
- Cycle "already running": `<workspace>/data/cycle.lock` expires after 100 minutes.
- `tools/probe_ats.py` finds which company slugs have a public job board; `tools/audit_sessions.py` lists what a browser session clicked.

---

## 13. Publishing your own copy

The repository contains no personal data, but its **git history may** if you developed it with your own profile checked in. Before making a copy public, create a fresh history (for example a new repository from the current tree) and add a licence of your choice. Never commit your workspace.
