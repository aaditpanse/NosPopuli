"""Deterministic relevance ranking for bill search and ledger shelves.

GovInfo's default sort for topic asks was recency, and Haiku then scored and
grouped the hits. Both wander: the same ask could put a weakly related new
bill above the one that is actually about the topic, and could drop that bill
into a different shelf on the next run.

This module is the opposite of that. Same titles + same question always
produce the same order. The LLM may still drop junk; it does not pick the
winner.
"""
import re

_STOP = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "about",
    "with", "from", "by", "at", "as", "is", "be", "this", "that", "these",
    "those", "bill", "bills", "act", "law", "laws", "legislation", "congress",
    "federal", "show", "me", "find", "give", "related", "regarding", "what",
    "has", "done", "passed", "signed", "enacted", "recent", "current", "year",
    "years", "some", "any", "one", "few", "many", "please", "into", "over",
    "under", "their", "its", "united", "states", "national", "public",
})

_TOKEN = re.compile(r"[a-z0-9][a-z0-9'+-]*", re.I)


def query_stems(question, extra=None):
    """Distinct content tokens from the ask, plus any router/expander terms."""
    parts = [question or ""]
    if extra:
        if isinstance(extra, str):
            parts.append(extra)
        else:
            parts.extend(str(x) for x in extra if x)
    stems = []
    seen = set()
    for tok in _TOKEN.findall(_fold(" ".join(parts))):
        if tok in _STOP or len(tok) < 2:
            continue
        if tok not in seen:
            seen.add(tok)
            stems.append(tok)
    return stems


_ALIASES = (
    ("artificial intelligence", "ai"),
    ("health care", "healthcare"),
    ("united states", "us"),
)


def _fold(text):
    h = (text or "").lower()
    for long, short in _ALIASES:
        h = h.replace(long, short)
    return h


def relevance_score(title, stems, extra_text=""):
    """Integer score: phrase match > every stem present > individual hits."""
    hay = _fold(f"{title or ''} {extra_text or ''}")
    if not stems or not hay.strip():
        return 0
    score = 0
    hits = 0
    for s in stems:
        variants = [s]
        if len(s) > 3 and s.endswith("s"):
            variants.append(s[:-1])
        elif len(s) > 3:
            variants.append(s + "s")
        hit = False
        for v in variants:
            if len(v) <= 2:
                if re.search(r"\b" + re.escape(v) + r"\b", hay):
                    hit = True
                    break
            elif v in hay:
                hit = True
                break
        if hit:
            score += 2 if re.search(r"\b" + re.escape(s) + r"\b", hay) or any(
                re.search(r"\b" + re.escape(v) + r"\b", hay) for v in variants
            ) else 1
            hits += 1
    if len(stems) >= 2:
        for n in (4, 3, 2):
            if n > len(stems):
                continue
            phrase = " ".join(stems[:n])
            if phrase in hay:
                score += 2 + n
                break
    if stems and hits == len(stems):
        score += 3
    return score


def _row_text(row):
    return " ".join(
        str(row.get(k) or "")
        for k in ("english_title", "title", "policy_area", "abstract")
    )


def _row_date(row):
    return (
        row.get("latest_action_date")
        or row.get("date")
        or row.get("date_issued")
        or row.get("introduced")
        or ""
    )


def _row_id(row):
    return (
        (row.get("type") or "").lower(),
        int(row.get("number") or 0),
        int(row.get("congress") or 0),
        row.get("id") or "",
    )


def rank_by_relevance(rows, question, extra=None):
    """Stable sort: higher lexical score, then newer date, then bill id.

    Attaches `_relevance` on each row so shelves and district notes can reuse
    it without scoring twice.
    """
    stems = query_stems(question, extra)
    out = list(rows or [])
    for r in out:
        r["_relevance"] = relevance_score(_row_text(r), stems)
    # Three stable passes: id ASC, date DESC (empty last), score DESC.
    out.sort(key=_row_id)
    out.sort(key=lambda r: _row_date(r) or "", reverse=True)
    out.sort(key=lambda r: r.get("_relevance") or 0, reverse=True)
    return out
