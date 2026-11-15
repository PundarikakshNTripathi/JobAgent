# JobAgent brand

<img src="img/logo.svg" width="56" alt="JobAgent logo">

**Name:** JobAgent. One word, capital J and A.

**Tagline:** Find the role. Reach the people. You press Submit.

**One-line description (repo, app stores, link previews):** A local job-search agent that reads every posting, finds the people who can refer you, writes in your voice, and fills the form. You press Submit.

**Elevator pitch (30 seconds):** Job hunting is three jobs at once: finding roles you can actually get, getting a real person to vouch for you, and filling the same forms again and again. JobAgent does the first two thoroughly and the third almost entirely, on your own computer, with the AI you already use. It reads every job description in full and drops the ones you don't qualify for. It finds people you really know, or can reach through someone you know, at each company. It drafts messages that sound like you and fills applications up to the last click. The last click is always yours.

## What we promise (and the product enforces)
- **You press Submit.** It never submits, sends a message, connects or follows. Guards in the code make that impossible, not just discouraged.
- **Real people only.** Referral plans name people actually seen in your network or on a page that was read. There is no "unverified" filler.
- **Nothing invented.** Every figure in a letter or message must come from your profile or the job. Links must come from a real source.
- **Your data stays yours.** Everything personal lives in your workspace on your computer.
- **Honest about fit.** If a role needs three years or a PhD, it says so and moves on.

## Voice
Plain, specific and calm, like a sharp friend who has read the job description. It says what happened and what to do next.

| Do | Don't |
|---|---|
| "2 forms are waiting for your Submit." | "🚀 You're crushing it! Let's land that dream job!" |
| "Needs 3+ years; you have one internship." | "This might not be the perfect fit, but who knows!" |
| "LinkedIn wants you to log in. Open the agent browser and log in once." | "Oops! Something went wrong 😅" |
| Sentence case, active verbs, numbers as digits | ALL CAPS labels, exclamation marks, filler adjectives |

Messages written *for* you follow your own voice (My voice); this section is how JobAgent itself speaks.

## Logo
A pine tile with a white **J** drawn as a path: it starts low at the left, sweeps up the stem and stops short of an **amber dot**, the role you're heading for. The path is the work JobAgent does; the gap before the dot is the step you take yourself.

- Files: `docs/img/logo.svg`, `agent/gui_static/favicon.svg` (the same artwork).
- Minimum size 16 px; keep clear space of a quarter of the tile around it.
- Don't recolour the dot, rotate the mark, or put the tile on a busy photo.

## Colour

Warm paper and ink, one accent. The page should read like a well-set document, not a dashboard template.

| Token | Light | Dark | Use |
|---|---|---|---|
| Ink | `#1B1A16` | `#ECEBE5` | text, and the primary button (one per view) |
| Paper | `#F3F1EC` | `#121110` | page background, with a faint grain |
| Panel / Raise | `#FBFAF7` / `#FFFFFF` | `#191816` / `#1F1E1B` | lists, cards, popovers (hairline borders, no drop shadows on resting surfaces) |
| Pine (accent) | `#2A6352` | `#7CBCA3` | progress, fit meters, the active item, "fresh" dates, positive status |
| Marigold | `#9A5B09` text / `#D9A23E` dot | `#E3AC52` | needs-you and outdated states, and the logo's dot |
| Stone | `#76726A` / faint `#A29E94` | `#8F8B81` / `#66635B` | secondary text, labels |

There's one accent per screen. Colour on tags means status, never decoration; neutral tags are hairline outlines with small radii.

## Type
- **Instrument Serif** for page titles, the Today headline, big numbers (fit, tiles) and the wordmark: the human voice of the app. Italic in the accent for the key phrase.
- **Geist** (400–700) for the interface.
- **Geist Mono** for times, ids, dates, counts in tables and small uppercase labels. Tabular figures wherever numbers line up.
- Fallbacks: Iowan Old Style/Georgia, Segoe UI Variable, system UI, Cascadia Mono.

## Motion
- 150–350 ms with an ease-out curve (`cubic-bezier(.2,.8,.2,1)`); rows rise in with a short stagger; buttons press down 1 px.
- A pulsing dot means "working right now" (the activity chip). Nothing else animates on its own.
- `prefers-reduced-motion` turns all of it off.

## Product shape
- **Today leads with the next action**, not a greeting.
- **One primary button per view.** In the top bar it's "Run pipeline now".
- **Ctrl K** reaches everything.
- **The activity chip** says what the agent is doing without a toast or a modal; details are one click away.
- **A desktop app**, not a tab: its own window and icon (`jobagent app`), icons rendered from the logo by `tools/make_icons.py`.
- **GIFs and screenshots** come from the fictional demo workspace (`tools/make_demo.py`). Fictional companies have their own drawn logos (`tools/demo_logos.py`); a real company's logo never appears next to a made-up posting.
