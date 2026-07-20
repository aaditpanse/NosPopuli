"""Extractor for source `county-bos`.

Gate status: this extractor intentionally emits ZERO records given the
current inputs, and will therefore fail `too_few_meetings` / `too_few_votes`.
That is the correct, non-fabricating outcome — not a defect to be patched by
inventing data. Read why:

The discovery agent's verified source profile reports:
  * jurisdiction: "Unknown — no jurisdiction was provided in the seed prompt"
  * primary_source.base_urls: []            (nothing to enumerate)
  * second_source.exists: false             (no corroborating source)
  * votes_located: "No votes located — discovery could not begin ..."
  * systems_surveyed: [{status: "No probes issued ..."}]
  * live document samples: none supplied in the artifact

A deterministic extractor cannot conjure an enumeration endpoint, a URL
scheme, a document format, an attendance roster, or vote language it has
never observed. The contract is explicit that fabricating votes (e.g. a
full-roster unanimous aye) is worse than emitting nothing and is itself
grounds for rejection. Passing the count gate by manufacturing meetings and
9-0 tallies would corrupt the durable domain schema — the exact asset this
pipeline protects.

The correct fix lives UPSTREAM: name the jurisdiction, let discovery verify
the platform (Legistar / PrimeGov / Granicus / eScribe / CivicPlus / custom)
and its base URLs, capture real document samples, then wire those into
`_base_urls()` and a parser written against the observed shapes. Until that
exists, this module refuses to guess.

Behavior: never crashes, never guesses, never fabricates; returns empty,
schema-valid records plus run_meta that flags the blocking condition.
"""

EXTRACTOR_VERSION = "1"

SOURCE_ID = "county-bos"
SCHEMA_VERSION = "1.2"


def _base_urls():
    """Verified base URLs to enumerate meetings from.

    The source profile reports `primary_source.base_urls == []` and no second
    source, so this is empty. Populate it ONLY with URLs a discovery agent has
    actually verified for a named jurisdiction; do not hand-write guesses.
    """
    return []


def _empty_records():
    return {
        "meetings": [],
        "agenda_items": [],
        "vote_events": [],
        "members": [],
    }


def _run_meta(records, blocked, note):
    return {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": SCHEMA_VERSION,
        "row_counts": {
            "meetings": len(records["meetings"]),
            "agenda_items": len(records["agenda_items"]),
            "vote_events": len(records["vote_events"]),
            "members": len(records["members"]),
        },
        "blocked": blocked,
        "notes": note,
    }


def extract(rt, max_meetings):
    """Extract municipal legislative records for `county-bos`.

    Returns (records, run_meta). With the current (empty) source profile there
    is no source to read, so this returns an empty but schema-valid result.
    Defensive: any unexpected condition yields an empty result, never a crash,
    and never fabricated data.
    """
    records = _empty_records()

    try:
        bases = [u for u in (_base_urls() or []) if isinstance(u, str) and u.strip()]
    except Exception:
        bases = []

    if not bases:
        note = (
            "BLOCKED: no jurisdiction and no verified source URLs were supplied "
            "by discovery (primary_source.base_urls == [], second_source.exists "
            "== false, no document samples). Enumeration and parsing are "
            "impossible. Emitting zero records rather than fabricating meetings "
            "or votes. Remediation: name the target jurisdiction, verify its "
            "platform and base URLs, capture real actions/minutes samples, then "
            "populate _base_urls() and implement the observed parser."
        )
        return records, _run_meta(records, True, note)

    # Unreachable while _base_urls() is empty. Left as a deliberate, safe
    # scaffold: even given base URLs, no parser can be written without observed
    # document samples and a confirmed vote-record convention. We do not guess a
    # platform API, URL scheme, document layout, or vote language. Until samples
    # exist, produce nothing rather than fabricate.
    note = (
        "BLOCKED: base URLs present but no verified document format or "
        "vote-record convention was discovered; no deterministic parser can be "
        "written without live samples. Emitting zero records to avoid "
        "fabrication."
    )
    return records, _run_meta(records, True, note)
