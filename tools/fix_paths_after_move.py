"""After moving a workspace (or splitting it out of the repo): rewrite absolute paths stored in the database and in packet.json files.
Usage: python tools/fix_paths_after_move.py <old_out_dir> <new_out_dir>"""
import json, os, sqlite3, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "agent"))
import common
old, new = sys.argv[1].rstrip("\\/"), sys.argv[2].rstrip("\\/")
c = common.db()
n = 0
for table in ("packets", "referrals"):
    for r in c.execute(f"SELECT rowid, dir FROM {table} WHERE dir LIKE ?", (old + "%",)).fetchall():
        c.execute(f"UPDATE {table} SET dir=? WHERE rowid=?", (new + r["dir"][len(old):], r["rowid"]))
        n += 1
c.commit()
m = 0
for root, _, files in os.walk(new):
    if "packet.json" in files:
        p = os.path.join(root, "packet.json")
        s = open(p, encoding="utf-8").read()
        t = s.replace(json.dumps(old)[1:-1], json.dumps(new)[1:-1]).replace(old.replace("\\", "/"), new.replace("\\", "/"))
        if t != s:
            open(p, "w", encoding="utf-8").write(t)
            m += 1
print(f"db rows updated: {n}; packet.json files updated: {m}")
