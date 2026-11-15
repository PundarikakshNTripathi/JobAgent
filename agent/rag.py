"""Retrieval over everything the agent knows (SQLite FTS5 / BM25, stdlib only; no embeddings needed).

Instead of pasting whole snapshots into prompts, chat and the agent loop retrieve the few most relevant items for the question: jobs (with their full
JD requirements and triage reasons), referral plans, packets, people seen on LinkedIn, the user's profile facts, long-term memory and past chat.
The index updates incrementally (a signature per document), so refreshing it is cheap and can run before every chat turn."""
import hashlib, json, os, re
from common import db, log, PROFILE

SCHEMA = """CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(kind, ref UNINDEXED, title, body, tokenize='porter unicode61');
CREATE TABLE IF NOT EXISTS docs_state(ref TEXT PRIMARY KEY, sig TEXT, rowid_ INTEGER);"""


def _conn():
    c = db()
    c.executescript(SCHEMA)
    return c


def _sig(*parts):
    return hashlib.md5("\x1f".join(str(p) for p in parts).encode("utf-8", "replace")).hexdigest()


def _upsert(c, kind, ref, title, body):
    sig = _sig(title, body)
    st = c.execute("SELECT sig, rowid_ FROM docs_state WHERE ref=?", (ref,)).fetchone()
    if st and st["sig"] == sig:
        return 0
    if st:
        c.execute("DELETE FROM docs WHERE rowid=?", (st["rowid_"],))
    cur = c.execute("INSERT INTO docs(kind,ref,title,body) VALUES(?,?,?,?)", (kind, ref, title, body[:8000]))
    c.execute("INSERT OR REPLACE INTO docs_state(ref,sig,rowid_) VALUES(?,?,?)", (ref, sig, cur.lastrowid))
    return 1


def _read(path, n=6000):
    try:
        return open(path, encoding="utf-8").read()[:n]
    except OSError:
        return ""


def refresh():
    """Index new or changed documents. Returns how many were (re)indexed."""
    c = _conn()
    n = 0
    for r in c.execute("""SELECT j.id,j.company,j.title,j.location,j.url,j.status,j.desc,j.req,t.verdict,t.fit,t.reason FROM jobs j LEFT JOIN triage t ON t.job_id=j.id
                          WHERE j.active=1 AND (t.job_id IS NOT NULL OR j.jd_full=1 OR j.status NOT IN ('new','dismissed','skipped'))""").fetchall():
        req = ""
        try:
            q = json.loads(r["req"] or "{}")
            req = "Eligibility: " + ("OK" if q.get("ok") else "; ".join(q.get("problems", []))) if q else ""
        except ValueError:
            pass
        body = f"{r['location']} | status {r['status']} | {r['verdict'] or ''} {int(r['fit'] or 0) or ''} | {r['reason'] or ''} | {req} | {r['url']}\n{(r['desc'] or '')[:3000]}"
        n += _upsert(c, "job", f"job:{r['id']}", f"{r['company']} - {r['title']}", body)
    for tbl, fname, kind in (("referrals", "REFERRAL.md", "referral"), ("packets", "README.md", "packet")):
        for r in c.execute(f"SELECT t.job_id, t.dir, j.company, j.title FROM {tbl} t JOIN jobs j ON j.id=t.job_id").fetchall():
            txt = _read(os.path.join(r["dir"] or "", fname))
            if txt:
                n += _upsert(c, kind, f"{kind}:{r['job_id']}", f"{kind} for {r['company']} - {r['title']}", txt)
    for r in c.execute("SELECT url, company, name, headline, degree, mutual FROM people").fetchall():
        n += _upsert(c, "person", f"person:{r['url']}", f"{r['name']} ({r['degree']}) at {r['company']}", f"{r['headline']} | mutual: {r['mutual']} | {r['url']}")
    facts = [("summary", PROFILE.get("summary", "")), ("education", PROFILE.get("education", ""))]
    facts += [(f"project {i}", p) for i, p in enumerate(PROFILE.get("projects", []))] + [(f"experience {i}", p) for i, p in enumerate(PROFILE.get("experience", []))]
    facts += [(f"achievement {i}", p) for i, p in enumerate(PROFILE.get("achievements", []))]
    for k, v in facts:
        if v:
            n += _upsert(c, "fact", f"fact:{k}", k, v if isinstance(v, str) else json.dumps(v))
    try:
        import memory
        for i, line in enumerate(l for l in memory.memory_text(20000).splitlines() if l.strip()):
            n += _upsert(c, "memory", f"memory:{i}", "memory", line)
    except Exception:
        pass
    for r in c.execute("SELECT id, role, text FROM chat WHERE role IN ('user','agent') ORDER BY id DESC LIMIT 400").fetchall():
        n += _upsert(c, "chat", f"chat:{r['id']}", r["role"], r["text"][:2000])
    c.commit()
    if n:
        log(f"rag: indexed {n} new/changed documents")
    return n


STOP = set("the a an and or of to for in on at is are was be with my me i you your it this that what which who how do does can please about any all from".split())


def _match(query):
    words = [w for w in re.findall(r"[A-Za-z0-9+#.]{2,}", query.lower()) if w not in STOP][:12]
    return " OR ".join(f'"{w}"' for w in words)


def retrieve(query, k=8, kinds=None):
    """[{kind, ref, title, snippet}] best first (BM25; titles weigh more)."""
    m = _match(query)
    if not m:
        return []
    c = _conn()
    sql = "SELECT kind, ref, title, snippet(docs, 3, '', '', ' … ', 24) snip FROM docs WHERE docs MATCH ?"
    args = [m]
    if kinds:
        sql += " AND kind IN (%s)" % ",".join("?" * len(kinds))
        args += list(kinds)
    sql += " ORDER BY bm25(docs, 0.0, 0.0, 4.0, 1.0) LIMIT ?"
    try:
        return [{"kind": r["kind"], "ref": r["ref"], "title": r["title"], "snippet": r["snip"]} for r in c.execute(sql, args + [k]).fetchall()]
    except Exception as e:
        log(f"rag: query failed ({e})")
        return []


def context(query, k=8, kinds=None, max_chars=3500):
    """Compact text block for a prompt."""
    out, used = [], 0
    for d in retrieve(query, k, kinds):
        line = f"[{d['kind']}] {d['title']} ({d['ref'].split(':', 1)[1][:80]}): {d['snippet']}"
        if used + len(line) > max_chars:
            break
        out.append(line)
        used += len(line)
    return "\n".join(out)
