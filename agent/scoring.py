"""Cheap heuristic pre-filter. No LLM. Decides what is worth spending Claude on."""
import re
from common import FILTERS as F

TIER_BOOST = {"S": 14, "A": 9, "B": 4, "C": 0}


def _any(patterns, text):
    return any(re.search(p, text) for p in patterns)


def _count(patterns, text):
    return sum(1 for p in patterns if re.search(p, text))


def classify_location(loc):
    t = (loc or "").lower()
    if any(re.search(r"(?<![a-z])" + w + r"(?![a-z])", t) for w in F["india_locations"]):
        return "india"
    if any(w in t for w in F["remote_markers"]):
        # "Remote - US" etc. is not really open to someone in India
        if _any(F["remote_restricted"], t) and not re.search(r"india|apac|asia|worldwide|anywhere|global", t):
            return "relocate"
        return "remote"
    return "relocate"


VISA_NO = [r"no (visa )?sponsorship", r"(not|unable to|cannot|can.t|won.t|will not) (be able to )?(offer |provide )?(visa )?sponsor", r"does not (offer|provide) (visa )?sponsorship",
           r"without (the need for )?(visa )?sponsorship", r"u\.?s\.? citizen", r"citizens? only", r"citizenship is required", r"security clearance", r"visa only",
           r"must (currently )?(be )?(legally )?(authori[sz]ed|eligible) to work in the (u\.?s|united states|uk|canada)"]
VISA_YES = [r"will sponsor", r"offers? (visa )?sponsorship", r"visa sponsorship (is )?(available|provided|offered)", r"we (can |do )?sponsor", r"sponsorship (is )?(available|provided)", r"relocation (assistance|support|package)"]


def visa_status(text):
    t = (text or "").lower().replace("’", "'")
    if _any(VISA_NO, t) and not re.search(r"will sponsor|offers? sponsorship", t):
        return "no"
    if _any(VISA_YES, t):
        return "yes"
    return "unknown"


def min_yoe(text):
    """Smallest 'N years' requirement mentioned near 'experience'. None if no mention."""
    vals = []
    ym = re.search(r"min experience: (\d{1,2})", text, re.I)
    if ym:
        vals.append(int(ym.group(1)))
    for m in re.finditer(r"(\d{1,2})\s*\+?\s*(?:[-–to]+\s*\d{1,2}\s*)?(?:years?|yrs?)(?:\s+of)?[^.]{0,60}?(?:experience|exp)", text, re.I):
        vals.append(int(m.group(1)))
    for m in re.finditer(r"(?:experience|minimum|at least)[^.]{0,40}?(\d{1,2})\s*\+?\s*(?:years?|yrs?)", text, re.I):
        vals.append(int(m.group(1)))
    return min(vals) if vals else None


def score(job):
    """Returns dict(score, family, early, yoe, loc_class, drop). score is 0..100."""
    title = job["title"].lower()
    desc = (job.get("desc") or "").lower()
    early = _any(F["early_career_markers"], title) or job["ats"] in ("simplify-intern", "simplify-newgrad")
    r = {"score": 0.0, "family": None, "early": int(early), "yoe": None,
         "loc_class": classify_location(job["location"]), "drop": None, "visa": "unknown"}

    if _any(F["exclude_title"], title):
        r["drop"] = "excluded title"
        return r

    fam, base = None, 0
    for name, spec in F["families"].items():
        if _any(spec["title"], title) and spec["base"] > base:
            fam, base = name, spec["base"]
    if fam is None:
        r["drop"] = "no target family"
        return r
    r["family"] = fam

    yoe = min_yoe(desc)
    r["yoe"] = yoe
    s = base
    if early:
        s += 22
    elif yoe is not None and yoe <= 1 and job["ats"] == "yc":
        s += 14  # YC startups asking for 0-1 years are exactly the lenient-bar targets
    elif yoe is not None:
        if yoe >= F["yoe_hard_exclude_at"]:
            r["drop"] = f"needs {yoe}+ yrs"
            return r
        if yoe >= F["yoe_penalty_at"]:
            s -= 18
    else:
        s -= 6  # unknown seniority, not marked early-career
    r["visa"] = visa_status(desc + " " + job["location"])
    s += TIER_BOOST.get(job.get("_tier", "C"), 0)
    if r["loc_class"] == "relocate":
        s += {"no": -25, "yes": 5, "unknown": 0}[r["visa"]]
    elif r["loc_class"] == "remote" and r["visa"] == "no":
        s -= 15
    s += {"india": 14, "remote": 9, "relocate": -6}[r["loc_class"]]
    s += min(14, 2 * _count(F["niche_keywords"], title + " " + desc[:3000]))
    r["score"] = max(0.0, min(100.0, s))
    return r
