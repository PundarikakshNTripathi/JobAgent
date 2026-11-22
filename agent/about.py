"""'About me': the structured, editable context JobAgent keeps on you, beyond what the profile and GitHub can show.

Sections (config/about.md in your workspace, plain Markdown, edit in the app or any editor): your story, goals, projects in depth, recent
wins, constraints, things never to say, and anything else. Every prompt that writes for you (applications, messages, chat, the agent) gets it.
'Things never to say' is enforced as a hard rule. Facts from GitHub sync or research never land here on their own: they arrive as
proposals you accept or reject (see proposals())."""
import json, os, re
import common
from common import db, log, now, HOME

PATH = os.path.join(HOME, "config", "about.md")
SECTIONS = [("story", "My story", "Where you come from and what you are working toward, in your own words."),
            ("goals", "Goals", "What you want next: roles, research directions, timelines (e.g. a PhD application cycle)."),
            ("projects", "Projects in depth", "What each project really involved, what was hard, what you would say about it in an interview."),
            ("wins", "Recent wins", "New results, releases, talks, rankings, people you met. Dated."),
            ("constraints", "Constraints", "Visa, location, dates, money, health, anything that limits what fits."),
            ("never", "Things never to say", "Claims, names or projects that must never appear in anything written for you."),
            ("other", "Anything else", "Preferences, tone, context the agent should know.")]


def load():
    try:
        t = open(PATH, encoding="utf-8").read()
    except OSError:
        t = ""
    out = {k: "" for k, _, _ in SECTIONS}
    titles = {title.lower(): k for k, title, _ in SECTIONS}
    cur = None
    for line in t.splitlines():
        m = re.match(r"^##\s+(.*)$", line)
        if m:
            cur = titles.get(m.group(1).strip().lower(), "other")
            continue
        if cur:
            out[cur] += line + "\n"
    return {k: v.strip() for k, v in out.items()}


def save(sections):
    cur = load()
    cur.update({k: str(v or "").strip()[:8000] for k, v in sections.items() if k in cur})
    os.makedirs(os.path.dirname(PATH), exist_ok=True)
    with open(PATH, "w", encoding="utf-8", newline="\n") as f:
        f.write("# About me\n\n" + "\n\n".join(f"## {title}\n{cur[k]}" for k, title, _ in SECTIONS) + "\n")
    return cur


def append(section, text):
    cur = load()
    cur[section] = (cur.get(section, "") + "\n- " + text.strip()).strip()
    return save(cur)


def never_say():
    return [l.strip("-* ").strip() for l in load().get("never", "").splitlines() if l.strip("-* ").strip()]


def prompt_block(max_chars=2600):
    """For prompts: your own words about yourself (most useful sections first)."""
    a = load()
    parts = [f"{title.upper()}: {a[k]}" for k, title, _ in SECTIONS if a.get(k) and k != "never"]
    nv = never_say()
    if nv:
        parts.append("NEVER SAY OR CLAIM: " + "; ".join(nv))
    return ("ABOUT THE CANDIDATE (their own words):\n" + "\n".join(parts))[:max_chars] if parts else ""


# ------------------------------------------------------------------ proposals (from GitHub sync and research): you decide
def _table(c):
    c.execute("""CREATE TABLE IF NOT EXISTS proposals(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, source TEXT, section TEXT, text TEXT, url TEXT,
                 status TEXT DEFAULT 'pending', k TEXT UNIQUE)""")
    return c


def propose(source, section, text, url="", key=None):
    c = _table(db())
    try:
        c.execute("INSERT INTO proposals(ts,source,section,text,url,k) VALUES(?,?,?,?,?,?)", (now(), source, section, text[:1200], url, key or f"{source}:{url}:{text[:60]}"))
        c.commit()
        return True
    except Exception:
        c.rollback()  # a duplicate must not leave a write lock open for the next writer
        return False
    finally:
        c.close()


def proposals(status="pending"):
    return [dict(r) for r in _table(db()).execute("SELECT * FROM proposals WHERE status=? ORDER BY id DESC", (status,)).fetchall()]


def decide(pid, accept):
    c = _table(db())
    r = c.execute("SELECT * FROM proposals WHERE id=?", (int(pid),)).fetchone()
    if not r:
        return "no such proposal"
    if accept:
        append(r["section"] if r["section"] in dict((k, 1) for k, _, _ in SECTIONS) else "projects", r["text"] + (f" ({r['url']})" if r["url"] else ""))
    c.execute("UPDATE proposals SET status=? WHERE id=?", ("accepted" if accept else "rejected", int(pid)))
    c.commit()
    return "added to About me" if accept else "dismissed"
