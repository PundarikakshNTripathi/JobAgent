"""Cheap heuristic pre-filter. No LLM. Decides what is worth spending Claude on."""
import re
from common import FILTERS as F, PROFILE

TIER_BOOST = {"S": 14, "A": 9, "B": 4, "C": 0}


def _any(patterns, text):
    return any(re.search(p, text) for p in patterns)


def _count(patterns, text):
    return sum(1 for p in patterns if re.search(p, text))


def classify_location(loc):
    t = (loc or "").lower()
    homes = list(F.get("home_locations") or F.get("india_locations") or [])
    hc = ((PROFILE.get("preferences") or {}).get("home_country") or "").strip().lower()
    if hc:
        homes.append(hc)
    if any(re.search(r"(?<![a-z])" + re.escape(w) + r"(?![a-z])", t) for w in homes):
        return "india"  # label kept for compatibility: means "in the candidate's home country"
    if any(w in t for w in F["remote_markers"]):
        # "Remote - US" etc. is not really open to someone in India
        if _any(F["remote_restricted"], t) and not re.search(r"apac|asia|worldwide|anywhere|global|" + re.escape(((PROFILE.get("preferences") or {}).get("home_country") or "zzzz").lower()), t):
            return "relocate"
        return "remote"
    return "relocate"


VISA_NO = [r"no (visa )?sponsorship", r"(not|unable to|cannot|can.t|won.t|will not) (be able to )?(offer |provide )?(visa )?sponsor", r"does not (offer|provide) (visa )?sponsorship",
           r"without (the need for )?(visa )?sponsorship", r"u\.?s\.? citizen(?!ship/visa not required)", r"citizens? only", r"citizenship is required", r"security clearance", r"visa only",
           r"must (currently )?(be )?(legally )?(authori[sz]ed|eligible) to work in the (u\.?s|united states|uk|canada)"]
VISA_YES = [r"will sponsor", r"offers? (visa )?sponsorship", r"visa sponsorship (is )?(available|provided|offered)", r"we (can |do )?sponsor", r"sponsorship (is )?(available|provided)",
            r"relocation (assistance|support|package|bonus|allowance)", r"visa (support|assistance)", r"(h-?1b|o-?1|l-?1|skilled worker|blue card|work permit)[^.]{0,40}(sponsor|support|provide)",
            r"(sponsor|support|provide)[^.]{0,40}(h-?1b|work permit|work visa|skilled worker|blue card)", r"international (candidates|applicants|students) (are )?(welcome|encouraged|eligible)",
            r"open to (candidates|applicants) (from|outside)", r"citizenship/visa not required"]


def visa_status(text):
    t = (text or "").lower().replace("’", "'")
    m = re.search(r"visa: (will sponsor|us citizenship/visa not required|us citizen/visa only)", t)  # YC's explicit field wins
    if m:
        return "no" if "only" in m.group(1) else "yes"
    if _any(VISA_NO, t) and not re.search(r"will sponsor|offers? sponsorship", t):
        return "no"
    if _any(VISA_YES, t):
        return "yes"
    return "unknown"


_NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
_NUMRX = r"(\d{1,2}|one|two|three|four|five|six|seven|eight|nine|ten)"


def _n(s):
    return int(s) if s.isdigit() else _NUM.get(s.lower(), 0)


_RANGE = r"(?:\s*\+)?(?:\s*(?:-|to|–|—|or)\s*" + _NUMRX + r")?"
_YOE_A = re.compile(_NUMRX + _RANGE + r"\s*\+?\s*(?:years?|yrs?)(?:'|’)?(?:\s+of)?[^.\n]{0,70}?(?:experience|exp\b|background|working|industry)", re.I)
_YOE_B = re.compile(r"(?:experience|minimum|at least|min\.?|requires?)[^.\n]{0,50}?" + _NUMRX + _RANGE + r"\s*\+?\s*(?:years?|yrs?)", re.I)
# experience a student can have: "1+ years of programming experience (including coursework, internships or projects)"
_STUDENT_OK = re.compile(r"intern(ship)?s?\b|coursework|academic|school|university|college|personal projects|side projects|research projects|including projects|class(es)?\b|hobby", re.I)
_REQ_CONTEXT = re.compile(r"requir|must|need|minimum|at least|qualif|you have|you bring|looking for|ideal(ly)? (candidate|you)|should have", re.I)


def yoe_mentions(text, require_context=False):
    """[(years, snippet)] for every stated experience requirement; a range counts as its lower bound ('1-3 years' = 1, '0-2 years' = 0).
    Mentions a student can meet (internships, coursework, projects) are left out. require_context=True keeps only sentences that read as a
    requirement (for blog posts and other prose without a requirements section)."""
    out = []
    ym = re.search(r"min experience: (\d{1,2})", text, re.I)
    if ym:
        out.append((int(ym.group(1)), ym.group(0)))
    for rx in (_YOE_A, _YOE_B):
        for m in rx.finditer(text):
            v = _n(m.group(1))
            if v > 20:
                continue
            around = text[max(0, m.start() - 120):m.end() + 90]
            sent = re.split(r"(?<=[.!?\n])\s", text[max(0, m.start() - 160):m.end() + 120])
            if v <= 2 and _STUDENT_OK.search(text[m.start():m.end() + 90]):
                continue
            if require_context and not _REQ_CONTEXT.search(around):
                continue
            out.append((v, re.sub(r"\s+", " ", text[max(0, m.start() - 40):m.end() + 20]).strip()))
    return out


def min_yoe(text, require_context=False):
    """Years of experience the posting requires: the smallest lower bound among its stated requirements (0 for '0-2 years'). None if none stated."""
    vals = [v for v, _ in yoe_mentions(text or "", require_context)]
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
        if yoe is not None and yoe >= 4:      # a "fellowship"/"program" that wants 4+ years is not an early-career role for this candidate
            r["drop"] = f"needs {yoe}+ yrs"
            return r
        if yoe is not None and yoe >= 3:
            s -= 20
    elif yoe is not None and yoe <= 1 and job["ats"] == "yc":
        s += 14  # YC startups asking for 0-1 years are exactly the lenient-bar targets
    elif yoe is not None:
        if yoe >= F["yoe_hard_exclude_at"]:
            r["drop"] = f"needs {yoe}+ yrs"
            return r
        if yoe >= F["yoe_penalty_at"]:
            s -= 30
    else:
        s -= 6  # unknown seniority, not marked early-career
    r["visa"] = visa_status(desc + " " + job["location"])
    loc_l = job["location"].lower()
    if r["loc_class"] == "relocate" and any(w in loc_l for w in F.get("drop_locations", [])) and not any(w in loc_l for w in (F.get("home_locations") or F.get("india_locations") or [])):
        r["drop"] = "location not realistic (needs local work permit)"
        return r
    if r["loc_class"] == "relocate" and r["visa"] != "yes" and re.search(r"\bintern|co-?op|apprentice", title) and re.search(r"\b(usa|united states|canada|united kingdom|uk)\b|, (ca|ny|wa|tx|ma)\b", loc_l + " "):
        s += F.get('visa_weights', {}).get('us_uk_ca_intern_without_sponsorship', -10)  # in-country enrolment / right-to-work is usually required
    s += TIER_BOOST.get(job.get("_tier", "C"), 0)
    vw = F.get("visa_weights", {})
    if r["loc_class"] == "relocate":
        s += vw.get("relocate_" + r["visa"], {"no": -25, "yes": 15, "unknown": -10}[r["visa"]])  # abroad: clear logistics rank up, unclear ones down
    elif r["loc_class"] == "remote" and r["visa"] == "no":
        s += vw.get("remote_no", -15)
    s += {"india": 14, "remote": 9, "relocate": -6}[r["loc_class"]]
    s += min(14, 2 * _count(F["niche_keywords"], title + " " + desc[:3000]))
    try:
        import memory
        for tw in memory.score_tweaks():
            if re.search(tw["pattern"], title):
                s += tw["delta"]
    except Exception:
        pass
    r["score"] = max(0.0, min(100.0, s))
    return r
