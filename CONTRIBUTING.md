# Contributing to JobAgent

Thanks for helping. JobAgent finds roles a student or early-career engineer can actually get, finds the people who can refer them, and prepares everything up to the last click. You press Submit. Contributions of every size are welcome: a new job source, a provider adapter, a sharper prompt, a bug report with a log, a typo.

## Ground rules (non-negotiable)

These are what make JobAgent safe to run on someone's real accounts. A PR that weakens one won't be merged.

1. **Never submit, send or contact.** Forms are filled and left for the user's Submit. The agent never sends a message or email, never clicks Connect, Follow, Like, Comment, Apply or Send. These rules are enforced in code (page guards, `webagent._allowed`), not just in prompts. Keep them in code.
2. **Never invent.** No made-up people, links, emails, numbers or facts. Everything shown must come from the user's profile or from a page or tool result the agent actually read (see `grounding.py`). Unverified guesses are labelled as such.
3. **Read-only on LinkedIn and X**, paced like a person, capped per run.
4. **Provider-agnostic.** Every feature must work with any configured model: Claude, Gemini/Antigravity, GPT/Codex, an API, or the local model. Don't rely on one vendor's tool (web search, MCP browser) without a path that works everywhere (`web.py` research notes, `webagent.py` browser loop).
5. **Personal data stays local.** Nothing personal goes into the repo: no real profiles, resumes, LinkedIn data, logs or screenshots of a real workspace. Demo data comes from `tools/make_demo.py` (fictional companies and people).

## Getting set up

```bash
git clone https://github.com/PundarikakshNTripathi/JobAgent.git && cd JobAgent
python -m venv .venv
.venv/bin/python -m pip install -r requirements.txt        # Windows: .venv\Scripts\python
python tools/make_demo.py ../demo-ws                        # a fictional workspace to develop against
JOBAGENT_HOME=../demo-ws .venv/bin/python agent/run.py gui  # Windows: set JOBAGENT_HOME=..\demo-ws
```

Python 3.10+ and the standard library, plus Playwright. Please don't add dependencies without discussing it in an issue first; a small install is a feature.

## Before you open a PR

```bash
python -m compileall -q agent tools tests        # must pass on Python 3.10 (CI runs 3.10 and 3.12 on Windows, macOS, Linux)
python -m unittest discover -s tests -v          # isolated temporary workspace; never touches real data
python agent/run.py selftest <provider>          # if you changed a prompt or the harness: every task's real prompt on one provider
```

- Add or update tests for what you changed (`tests/`; see `_env.py` for the throwaway workspace and `add_job`).
- No f-strings with backslashes inside `{}` (Python 3.10).
- Prompts: say what to return, ask for JSON when code consumes the result, and keep the truthfulness rules.
- If you change behaviour a user sees, update `README.md` (and regenerate images with `tools/screenshots.py` / `tools/record_gifs.py` against the demo workspace).
- Keep the style: plain, specific wording in UI text and docs; no hype.

## Where help is most useful

| Area | What | Where |
|---|---|---|
| Job sources | More ATS boards and official career sites, regional job boards (EU, India, LatAm, SEA) | `sources_big.py`, `sources_extra.py`, `feeds.py`, `defaults/companies.json` |
| Outreach signals | More hiring-post and seed-round sources, better founder discovery | `signals.py` |
| Providers | New CLI or API adapters (and testing the unverified ones: Codex, Gemini CLI, OpenRouter, Ollama, LM Studio) | `defaults/providers.json`, `llm.py` |
| Browser | Extractors for more ATS forms; LinkedIn and X layout changes | `browse.py`, `formfill.py`, `webagent.py` |
| Local AI | Model recommendations for more GPUs (AMD, Apple Silicon), benchmarks | `local_ai.py`, `bench.py` |
| Eligibility | Requirement parsing (years, degree, seniority, visa) in more phrasings and languages | `jd.py`, `scoring.py` |
| GUI | Accessibility, keyboard flows, translations | `gui_static/index.html` |
| Docs | Guides for other countries' job markets and visas | `docs/` |

Issues labelled `good first issue` are a gentle start.

## License and the contributor agreement

JobAgent is licensed under the **GNU AGPL v3** (see `LICENSE`): anyone may use and change it, and anyone who distributes a modified version, or runs one as a service for others, must publish its source under the same license.

Before your first pull request is merged, please agree to the [Contributor License Agreement](CLA.md) by adding this line to the PR description:

> I have read the JobAgent CLA (CLA.md) and agree to its terms for this and all my future contributions.

You keep the copyright to your work. The CLA lets the maintainer also offer JobAgent under a commercial license (that is how the project can be sustained), while the open version always stays AGPL. Please also sign off your commits (`git commit -s`), which certifies the [Developer Certificate of Origin](https://developercertificate.org/).

## Reporting bugs

Open an issue with:
- what you ran (`jobagent <command>` or the GUI view), what you expected and what happened;
- `jobagent doctor` output and your OS;
- the relevant lines of `<workspace>/logs/agent.log`. **Remove personal details** (names, emails, profile links) before posting.

Security problems: please don't open a public issue; see [SECURITY.md](SECURITY.md).

## Code of conduct

Be kind, assume good faith, and keep discussions about the work. See [CODE_OF_CONDUCT.md](CODE_OF_CONDUCT.md).
