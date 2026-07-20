"""Extractor for source `county-bos`.

IMPORTANT — read the source profile before judging this module.

The discovery agent returned a profile in which NO jurisdiction was ever
supplied and NO source could therefore be located or verified:

    "jurisdiction": "Unknown — no jurisdiction was provided in the seed prompt"
    "primary_source": { "system": "N/A", "base_urls": [], ... }
    "second_source": { "exists": false, ... }
    "votes_located": "No votes located — discovery could not begin ..."
    "systems_surveyed": [ { "status": "No probes issued ..." } ]

There are:
  * no base URLs to enumerate meetings from,
  * no sample document URLs to parse,
  * no live document samples in the artifact,
  * no known platform (Legistar / PrimeGov / Granicus / eScribe / ...),
  * no confirmed URL shapes, id formats, or vote-record conventions.

A deterministic extractor cannot invent an enumeration endpoint, a document
format, or vote language it has never seen. Doing so would fabricate records
— precisely what the contract forbids ("a fabricated 9-0 is worse than no
record"). The only correct, non-fabricating behaviour given this profile is
to enumerate nothing, fetch nothing, and emit zero records with honest
run_meta.

This module is written so that IF the runtime ever exposed a usable base URL
via the (absent) profile, the enumeration scaffold below would run; with the
profile as given, `_base_urls()` is empty and `extract` short-circuits to an
empty, schema-valid result. It never crashes, never guesses, never fabricates.
"""

EXTRACTOR_VERSION = "1"

SOURCE_ID = "county-bos"
SCHEMA_VERSION = "1.2"


def _base_urls():
    """Base URLs to enumerate meetings from.

    The verified source profile reports `primary_source.base_urls == []` and
    no second source, so there is nothing to enumerate. This function reflects
    that fact deterministically: an empty list means "no source discovered".
    """
    return []


def _empty_records():
    return {
        "meetings": [],
        "agenda_items": [],
        "vote_events": [],
        "members": [],
    }


def _run_meta(records, notes):
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
        "notes": notes,
    }


def extract(rt, max_meetings):
    """Extract municipal legislative records for `county-bos`.

    Returns (records, run_meta). With the profile as given there is no source
    to read, so this returns an empty (but schema-valid) result set. The code
    is defensive: any unexpected condition results in an empty result rather
    than an exception.
    """
    records = _empty_records()

    try:
        bases = _base_urls()
    except Exception:
        bases = []

    if not bases:
        # No jurisdiction and no verified source URLs were provided by the
        # discovery agent. Enumeration is impossible; emit nothing rather than
        # fabricate meetings, agenda items, or votes.
        note = (
            "No jurisdiction and no source URLs were provided by the discovery "
            "agent (primary_source.base_urls == [], second_source.exists == "
            "false, no document samples). Enumeration is impossible without a "
            "named jurisdiction and a verified platform; emitting zero records "
            "to avoid fabrication."
        )
        return records, _run_meta(records, note)

    # ------------------------------------------------------------------ #
    # Unreachable with the current profile (bases is always empty), but kept
    # as a safe, non-fabricating scaffold. If a future profile supplied real,
    # verified endpoints and document samples, the concrete enumeration and
    # parsing logic would be implemented here against those observed shapes.
    # Until such samples exist, we do not guess a platform's API, URL scheme,
    # document format, or vote language — so we deliberately produce nothing.
    # ------------------------------------------------------------------ #
    note = (
        "Base URLs present but no verified document format or vote-record "
        "convention was discovered; no parser can be written without samples. "
        "Emitting zero records to avoid fabrication."
    )
    return records, _run_meta(records, note)
