"""Guardrails against hallucination: generated text may only use facts the agent was given.

- unsupported_numbers(): metrics/figures in a draft (79.8M, 3.75 us, 0.70, 57 tests, 2.3x, 40%) that appear nowhere in the sources it was written from.
  A model that invents a result is the most damaging failure for a job application, so drafts with such numbers are rewritten or flagged.
- unseen_urls() / strip_unseen_urls(): links in an answer that never appeared in the prompt or in any tool result are removed, so chat and the agent
  can't hand you made-up profile links or postings.
- est_tokens(): rough token count used to keep prompts inside each model's context window (no silent truncation, no context rot)."""
import re

_NUM = re.compile(r"(?<![\w./-])(\d+(?:[.,]\d+)?)\s*(%|x|×|k|m|b|ms|us|µs|s|gb|mb|tb|tok/s|tokens|records|tests|users|stars|qps|fps|hours|days)?(?![\w])", re.I)
_URL = re.compile(r"https?://[^\s<>\"')\]]+", re.I)


def _norm_num(n):
    return n.replace(",", "").rstrip("0").rstrip(".") if "." in n else n.replace(",", "")


def unsupported_numbers(text, sources):
    """Numbers in `text` that do not occur in `sources` (str or list). Ignores years, ordinals in dates, and small counts (<= 10) without a unit."""
    src = sources if isinstance(sources, str) else "\n".join(str(s) for s in sources if s)
    have = {_norm_num(m.group(1)) for m in _NUM.finditer(src)}
    out = []
    for m in _NUM.finditer(text or ""):
        n, unit = m.group(1), (m.group(2) or "")
        v = _norm_num(n)
        try:
            f = float(v)
        except ValueError:
            continue
        if (1900 <= f <= 2100 and "." not in n) or (f <= 10 and not unit and "." not in n):
            continue
        if v not in have:
            out.append(m.group(0).strip())
    return sorted(set(out))


def _canon(u):
    """Compare links by host+path: scheme, 'www.', trailing slash, query and fragment don't make a link different."""
    u = u.rstrip(".,;:").lower().split("#")[0].split("?")[0]
    u = re.sub(r"^https?://(www\.)?", "", u)
    return u.rstrip("/")


def unseen_urls(text, seen_text):
    """Links in `text` that never appeared in `seen_text`. A link counts as seen only when the same page appeared (ignoring query, fragment and
    trailing slash), or when it is a shortened form of a seen link. A bare prefix that was seen (linkedin.com/in/) does not vouch for every
    profile under it: that loophole let invented profile URLs through."""
    seen = {_canon(u) for u in _URL.findall(seen_text or "")}
    out = []
    for u in {u.rstrip(".,;:") for u in _URL.findall(text or "")}:
        c = _canon(u)
        if c in seen or any(s.startswith(c + "/") for s in seen if "/" in c):
            continue
        out.append(u)
    return out


def strip_unseen_urls(text, seen_text, note=True):
    bad = unseen_urls(text, seen_text)
    for u in bad:
        text = text.replace(u, "[link removed: not seen in any source]")
    if bad and note:
        text += f"\n\n(Removed {len(bad)} link(s) that did not come from a page or tool result.)"
    return text, bad


def est_tokens(text):
    """~3.6 characters per token for English prompts with JSON; deliberately a little pessimistic."""
    return int(len(text or "") / 3.6) + 1
