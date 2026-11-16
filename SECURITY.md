# Security policy

JobAgent runs on your own machine with your logged-in browser sessions, so security reports matter a lot.

## Reporting a vulnerability

Please **do not open a public issue**. Use GitHub's private reporting instead: **Security → Report a vulnerability** on this repository. Include what an attacker could do, how to reproduce it, and the version (commit) you tested.

You'll get an acknowledgement within 7 days. Fixes for confirmed issues are released as soon as they're ready, and reporters are credited unless they prefer otherwise.

## In scope

- Anything that could make JobAgent submit an application, send a message or email, or click Connect/Follow/Like/Apply without the user doing it.
- Ways for a web page, job posting, email or chat message to make the agent run commands, read or upload local files, or leak data (prompt injection that crosses a safety boundary).
- The local GUI server (it must only be reachable from `127.0.0.1` and must reject cross-site requests).
- Leaks of personal data from the workspace into logs, the repository or third parties.

## Out of scope

- A model giving a poor answer, unless it crosses one of the boundaries above.
- Rate limits or account actions by LinkedIn, X or job boards caused by heavy use against their terms.
