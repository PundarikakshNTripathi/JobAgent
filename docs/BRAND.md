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

| Token | Light | Dark | Use |
|---|---|---|---|
| Ink | `#14201B` | `#E4ECE8` | text, primary surfaces in the chat |
| Paper | `#F5F7F6` | `#0D1412` | page background |
| Panel | `#FFFFFF` | `#131C19` | cards and lists |
| Pine (accent) | `#1E6B58` | `#5FC2A5` | the one action colour: primary buttons, progress, fit gauges |
| Amber | `#B7791F` / logo `#F2B84B` | `#E0A94A` | needs-you states and the logo's dot only |
| Slate | `#5B6A64` | `#8B9B95` | secondary text |

There's one accent per screen. Colour on tags means status, never decoration.

## Type
- **Bricolage Grotesque** (600/700) for page titles, the Today headline and the wordmark: characterful, set tight (−0.035em).
- **Schibsted Grotesk** (400–700) for everything else. Tabular figures only where numbers line up (scores, counts, tables).
- Fallbacks: Segoe UI Variable, system UI.

## Product shape
- **Today leads with the next action**, not a greeting.
- **One primary button per view.** In the top bar it's "Run pipeline now".
- **Ctrl K** reaches everything.
- **GIFs and screenshots** come from the fictional demo workspace (`tools/make_demo.py`). Fictional companies have their own drawn logos (`tools/demo_logos.py`); a real company's logo never appears next to a made-up posting.
