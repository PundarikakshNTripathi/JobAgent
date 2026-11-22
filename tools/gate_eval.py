"""Measure the eligibility gate on labelled real postings (false drops hide good roles silently; false passes waste referrals).

    python tools/gate_eval.py                 # run tests/fixtures/jd/cases.json through the gate, print mismatches
    python tools/gate_eval.py --export ID ... # (maintainer) copy postings from your workspace into the fixture file, to label them by hand

Each case: {"id", "company", "title", "location", "desc", "expect": "pass" | "drop" | "visa-unclear", "why"}."""
import json, os, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
FIX = os.path.join(ROOT, "tests", "fixtures", "jd", "cases.json")


def verdict(case):
    import jd, common
    # the fixtures are labelled for a candidate based in India who needs visa sponsorship elsewhere and has no full-time experience
    common.PROFILE.setdefault("form_defaults", {})["requires_visa_sponsorship_outside_home_country"] = "Yes"
    common.PROFILE.setdefault("preferences", {})["home_country"] = "India"
    row = {"id": case["id"], "title": case["title"], "location": case.get("location", ""), "desc": case["desc"], "visa": case.get("visa", "unknown"),
           "jd_full": 1}

    class R(dict):
        def keys(self):
            return super().keys()
    r = R(row)
    req = jd.requirements(r)
    probs = jd.problems(req)
    vg, why = jd.visa_gate(r, case["desc"])
    if probs or vg == "problem":
        return "drop", "; ".join(probs + ([why] if vg == "problem" else []))
    if vg == "unclear":
        return "visa-unclear", why
    return "pass", ""


def evaluate(cases):
    bad = []
    for c in cases:
        got, why = verdict(c)
        if got != c["expect"]:
            bad.append((c, got, why))
    return bad


def main():
    if "--export" in sys.argv:
        import sqlite3, common
        con = sqlite3.connect(os.path.join(common.DATA, "jobs.db"))
        con.row_factory = sqlite3.Row
        cases = json.load(open(FIX, encoding="utf-8")) if os.path.exists(FIX) else []
        for jid in sys.argv[sys.argv.index("--export") + 1:]:
            r = con.execute("SELECT id, company, title, location, desc FROM jobs WHERE id=?", (jid,)).fetchone()
            if r:
                cases.append({"id": r["id"], "company": r["company"], "title": r["title"], "location": r["location"], "desc": r["desc"], "expect": "?", "why": ""})
        os.makedirs(os.path.dirname(FIX), exist_ok=True)
        json.dump(cases, open(FIX, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        print(f"{len(cases)} cases in {FIX}; set 'expect' by hand")
        return
    cases = [c for c in json.load(open(FIX, encoding="utf-8")) if c.get("expect") in ("pass", "drop", "visa-unclear")]
    bad = evaluate(cases)
    for c, got, why in bad:
        print(f"MISMATCH {c['company']} | {c['title'][:50]}: expected {c['expect']}, got {got} ({why}) - {c.get('why', '')}")
    print(f"{len(cases) - len(bad)}/{len(cases)} correct")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
