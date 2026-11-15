# Architecture, and what makes an agent actually effective

JobAgent is a **local, provider-agnostic agent** for the job search. Its core is a pipeline that costs no model tokens (polling and filtering), wrapped in an agentic layer (memory, skills, tools, feedback) that spends tokens only where judgement matters.

## 1. Core vs personal data

```
repository (public code)                 your workspace (private, local, auto-created)
  agent/      all modules                  config/profile.json     your facts and rules
  defaults/   settings, filters,           config/settings.json    only YOUR overrides
              companies, providers,        config/*.json           optional overrides (filters, companies)
              profile template, skills     data/jobs.db            jobs, triage, outcomes, chat, usage
  docs/                                    data/memory/            what the agent learned about you
                                           data/voice.md ...       your sent messages and edits
                                           data/transcripts/ linkedin/ chrome-profile/
                                           out/                    packets, referral plans, dashboards
                                           logs/  skills/          (your own skills override shipped ones)
```

The workspace is found via `$JOBAGENT_HOME`, a one-line `.jobagent_home` file next to the code (gitignored), or `~/JobAgent`. Shipped defaults and your overrides are deep-merged at load time (`common.load`), so upgrading the code never touches your data and your data never lands in the repository. `run.py init` (or the GUI setup wizard) creates the workspace and profile.

## 2. Provider layer (`agent/llm.py`)

One entry point, `llm.run(prompt, kind=...)`. Settings route each **task kind** (`triage`, `today`, `ingest`, `chat`, `reflect`, `packet`, `outreach`, `referral`, `resume`, `scout`, `fill`, `linkedin`, `yc`) to a `{provider, model, effort}`. Features:

- **Adapters**: Claude Code (`claude -p`), Antigravity (`agy -p`, Gemini and others), Codex (`codex exec`), Gemini CLI, any OpenAI-compatible endpoint (OpenAI, OpenRouter, Gemini API, Ollama, LM Studio...), the Anthropic API, and `cli_template` for anything else (Hermes Agent, oh-my-pi, a script). `claude` and `agy` are verified against the real tools; the rest follow vendor docs and are marked "untested" until you press *Test*.
- **Capabilities**: text, native web, browser (MCP). A text-only model still does research because JobAgent retrieves pages and search results itself and hands the model the notes (`web.py`); form filling and LinkedIn need a browser-capable provider.
- **Fallbacks and budgets**: if a provider is missing, errors, or hits its daily call/token budget, the next one in the chain is used.
- **Usage log**: every call records provider, model, task, tokens, seconds and cost; the GUI shows today/7/30 days.
- **Lean mode** for Claude (no user MCP servers, skills, or default system prompt) cuts per-call overhead from about 27k to under 1k input tokens; `agy` always carries a base prompt of about 8k tokens.

## 3. What strong open agents have in common, and what JobAgent adopted

Researched from the public docs of Hermes Agent (Nous Research), oh-my-pi (omp, a fork of the `pi` harness), Codex CLI, Gemini CLI and Antigravity (Nov 2026). Sources vary in quality; details were checked against `agy --help` where the tool was installed.

| Idea | Where it shows up in the tools | In JobAgent |
|---|---|---|
| **Persistent, file-based memory** that the agent curates | Hermes: `MEMORY.md`/`USER.md`, periodic "nudges" to save durable facts | `data/memory/MEMORY.md`, written by `memory.reflect()` from your outcomes, injected (trimmed) into triage, chat and the agent loop |
| **Skills**: reusable playbooks loaded on demand (progressive disclosure) | Hermes skills hub (agentskills.io format), omp/pi extensions | `defaults/skills/*.md` (human-writing, referral-playbook, resume-tailoring), overridable in `<workspace>/skills`, loaded only into the prompts that need them |
| **Closed learning loop**: outcomes change future behaviour | Hermes self-improving skills | Skips (with reasons), applies and replies are logged; `reflect()` updates memory and proposes small scoring tweaks that `scoring.py` applies |
| **Tool-using loop under a step budget** | all of them | `agentic.loop()`: ReAct-style, one tool per step (search jobs, build packet, plan referral, fetch, web search, remember, ingest...), 12 steps max, no tool can submit an application |
| **Provider independence** | Hermes (Nous/OpenRouter/OpenAI/custom), omp (many models), Codex/Gemini (own vendors) | the whole provider layer above; the agent loop works with any text model because tool execution is done by JobAgent, not the vendor CLI |
| **Human in the loop for consequential actions** | approval modes (`--mode`, sandbox) | chat only *proposes* actions; submits are impossible; LinkedIn is read-only; prestige roles never auto-apply |
| **Transcripts you can inspect** | session logs | fill/LinkedIn sessions are persisted; usage log; `research/audit_sessions.py`-style audit tools |
| **Scheduling/gateway** | Hermes cron + messaging gateway | Windows Task Scheduler (or cron), desktop toast and ntfy push |

What is deliberately *not* copied: autonomous skill authoring and open-ended shell access. For a job-search tool that touches your accounts, bounded tools and explicit confirmation matter more than autonomy.

## 4. Why a pipeline plus an agent, not only an agent

About 25,000 postings per poll cannot be read by a model economically. Deterministic code does the 99%: fetch, normalise, deduplicate, score (title family, level, years of experience, location realism, visa signals, learned tweaks). Models then spend tokens on the top few hundred postings (triage), the few dozen worth acting on (packets, referral plans, resume advice) and the things only an agent can do (browser form filling, read-only LinkedIn scouting, open-web scouting, inbox ingestion, chat). That split is what keeps a Pro-plan budget workable while still being genuinely agentic where it counts.

## 5. Writing in your voice (`humanize.py`, `outreach.py`)

Research and writing are separate steps. Research finds who to contact and the checkable hook (what they built or wrote). A dedicated writing step then drafts messages using: the *human-writing* skill (explicit list of machine tells and what to do instead), your own sent messages (`data/voice.md`), and your recent draft-to-final edits. Output passes a deterministic linter (stock phrases, dashes as punctuation, lists, exclamation marks, "I"-heavy sentences), an auto-fixer, and at most one model rewrite pass. Cover letters and form answers get the same treatment.

## 6. Does a bigger or "Google-native" model search better?

Short answer: it is an empirical question, and JobAgent includes a benchmark (`run.py bench`) so you can answer it on your own data instead of trusting claims.

What is known: Google has sued third-party SERP scrapers (SerpApi, Dec 2025) over circumvention of its anti-bot protections, so tools that scrape Google result pages are unreliable; first-party Gemini has native Google Search grounding, which no other vendor can replicate. Claude Code and Codex use their own vendors' search backends. Whether that translates into better *job and people research* depends on the task. JobAgent therefore (a) lets each provider use native web tools where they exist, (b) gives text-only providers the same research notes through its own retrieval, and (c) benchmarks them: the research benchmark checks that returned URLs are alive and actually mention the entity. Triage benchmark: agreement between providers on pursue-vs-skip for the same postings, plus tokens and seconds.

### Measured (Nov 2026, 8 real postings, one maintainer's data; your numbers will differ, run `bench`)

| | seconds | agreement with Claude Sonnet (pursue vs skip) | mean fit | notes |
|---|---|---|---|---|
| Claude Sonnet 5.5 | 12 | reference | 40 | about 4.4k tokens in, 1.6k out |
| Claude Haiku | 16 | 3/8 | 24 | far too conservative for triage |
| Gemini 3.8 Flash (high) via Antigravity | 235 | 7/8 | 67 | generous scores, about 60k output tokens (thinking) |
| Gemini 3.8 Flash (medium) | 56 | 5/8 | 47 | about 10.6k output tokens |
| Gemini 3.8 Flash (low) | error | - | - | headless run failed |

Research task (4 facts with URLs about a team): Claude with native web search returned 4 live URLs, 3 mentioning the company; Gemini Flash with JobAgent's own retrieval returned 4 live URLs, 4 mentioning the company (49 s vs 15 s). No sign that Google-side blocking hurts Gemini here, and no sign that it helps beyond Claude on this task. Conclusion used for routing: latency-sensitive judgement (triage, ingest, chat) and writing stay on Claude; slow, retrieval-heavy synthesis (referral research, scouting, daily plan, reflection) goes to Gemini. Headless `agy` sometimes tries to call tools it cannot get permission for; JobAgent tells it not to and falls back to the next provider on failure.

## 7. Deterministic where possible, a model only where judgement is needed

The biggest cost and reliability lesson: a model that drives a browser by reading raw page snapshots is slow, expensive (300k–500k input tokens per LinkedIn
scout or form fill) and only as good as its patience. JobAgent now splits each browser job into code and judgement:

```
            code (no tokens)                                   model (small, one call)                 code
LinkedIn   people search -> parse cards -> employer filter -> rank real people + write approach (~3k) -> REFERRAL.md, people cache
Forms      read fields in all frames (labels/options)       -> map facts/answers to field ids (~6k)  -> Playwright fills, verifies, guard
JDs        ATS API / page / browser text -> requirement rules (years, degree, seniority, citizenship, grad year) -> gate (no model at all)
```

- `browse.py`: Playwright over CDP to the agent's own Chrome (your logins), with small JavaScript extractors and pacing. LinkedIn pages always get the read-only guard.
- `jd.py`: full posting first, rules second; ineligible roles never reach triage. `audit` re-checks existing work.
- `formfill.py`: the model only sees a field list and returns values; the model-driven MCP fill remains the fallback.
- `rag.py`: SQLite FTS5 (BM25) over jobs + eligibility, plans, packets, people, profile facts, memory and chat; refreshed incrementally before each chat or
  agent run, so prompts carry the 8–10 relevant items instead of snapshots.
- `agentic.py`: plan step, notes, **context folding** (only the latest tool result stays verbatim), stable prompt prefix first for provider-side caching,
  tools for recall, LinkedIn, any page and multi-step read-only browsing.
- `llm.py`: routing per task, fallbacks, quota cooldowns, background allowance, **deferral** of quality work in background runs, response cache for
  deterministic tasks, heavy-call logging.
- `local_ai.py`: llama.cpp server with a model sized to the machine; cheap tasks are routed to it only after an agreement benchmark on the user's own jobs.

## 8. Safety model

Forms are filled but never submitted (prompt rule + injected page guard that blocks submit events, final-button clicks and application POSTs for a time window, plus a watchdog that reports confirmation pages). LinkedIn access is read-only (action buttons blocked, small page budget, paced). Chat and agent tools cannot submit, change settings, or touch the schedule. Prestige roles (fellowships, residencies, pre-docs, frontier-lab programs) are tracked, drafted and prepared for referral, never applied to automatically.
