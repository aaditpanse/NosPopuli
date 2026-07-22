"""Employer-string -> industry classification: the OpenSecrets curation layer,
done with a cheap cached LLM pass instead of hand-coding.

OpenSecrets built its industry rollups by having researchers read each donor's
self-reported employer and assign it an industry, maintained over decades. That
is exactly the kind of judgment a small model does well. Here every *distinct*
employer is classified once (Haiku, batched) and cached effectively forever —
employers don't change industry — so the marginal cost across every member view
trends to zero. Results are advisory/estimated, never authoritative.
"""

import json
import pathlib

import anthropic
from dotenv import load_dotenv

load_dotenv(pathlib.Path(__file__).parent / ".env")

_MODEL = "claude-haiku-4-5"
_CACHE_TTL = 400 * 24 * 3600  # an employer's industry is stable; cache ~a year

# A compact, fixed taxonomy (loosely OpenSecrets' sectors) so totals aggregate
# cleanly. The enum constrains the model to exactly these labels.
INDUSTRIES = [
    "Health & Pharma", "Finance & Insurance", "Real Estate", "Technology",
    "Energy & Natural Resources", "Defense & Aerospace", "Law & Lobbying",
    "Labor Unions", "Education", "Agriculture", "Transportation",
    "Telecom & Media", "Retail & Hospitality", "Construction",
    "Manufacturing", "Government & Public Sector", "Nonprofits & Advocacy",
    "Other",
]

# Single-issue / ideological causes a PAC can represent (beyond an industry).
_CAUSES = [
    "Pro-Israel", "Gun Rights", "Gun Safety", "Abortion Rights", "Anti-Abortion",
    "Environment", "LGBTQ Rights", "Immigration", "Fiscal Conservative",
    "Progressive", "Civil Rights",
]
# PACs classify into an industry, a cause, or a political vehicle. "Leadership
# PAC" (a politician's own PAC) and "Party Committee" aren't interests — they're
# colleague/party money — but they're a big share of many members' PAC totals,
# so they get their own buckets rather than being mislabeled.
PAC_INTERESTS = [i for i in INDUSTRIES if i != "Other"] + _CAUSES + \
    ["Leadership PAC", "Party Committee", "Other"]

_client = None


def _c():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


_EMPLOYER_INSTRUCTIONS = (
    "You classify the employer of a US political donor into its primary "
    "industry, the way OpenSecrets does. Infer from clear signals in the name, "
    "even for firms you don't know:\n"
    "- ...ENT / Medical / Health / Hospital / Clinic / Pharma / Bio / Care -> Health & Pharma\n"
    "- Tech / Technologies / Software / Systems / Digital / Data / Cyber / Labs -> Technology\n"
    "- Law / Legal / LLP / Attorneys / & Associates (law) -> Law & Lobbying\n"
    "- Bank / Capital / Financial / Investments / Advisors / Insurance / Wealth -> Finance & Insurance\n"
    "- Realty / Properties / Real Estate / Homes / Development -> Real Estate\n"
    "- Construction / Builders / Contractors / Roofing / Electric -> Construction\n"
    "- University / School / College / Academy / District / ISD -> Education\n"
    "- Pipe / Steel / Manufacturing / Industries / Products / Mfg -> Manufacturing\n"
    "- Farms / Agriculture / Grain / Dairy / Ranch -> Agriculture\n"
    "- Airlines / Logistics / Freight / Trucking / Transit -> Transportation\n"
    "- City of / County of / State / federal agency / Dept -> Government & Public Sector\n"
    "- Union / Local ### / IBEW / AFSCME / Teamsters -> Labor Unions\n"
    "Only use \"Other\" when the name gives no reasonable signal at all."
)

_PAC_INSTRUCTIONS = (
    "You classify a US political action committee (PAC) by the interest it "
    "represents. A PAC's interest is a matter of public record — use what you "
    "know about the organization and clear signals in its name. This is about "
    "the PAC's own identity, never a guess about individual donors.\n"
    "- Corporate / trade PACs -> their industry: Exxon -> Energy & Natural "
    "Resources; L3Harris/Lockheed -> Defense & Aerospace; a drug company -> "
    "Health & Pharma; Realtors / Home Builders / Realty -> Real Estate; "
    "Bankers / Credit Union / Investment -> Finance & Insurance; Cable/Telecom/"
    "Broadcasters -> Telecom & Media.\n"
    "- Single-issue / ideological PACs -> their cause: AIPAC / NORPAC / Jewish "
    "-> Pro-Israel; NRA / Gun Owners -> Gun Rights; Everytown / Giffords / 'Gun "
    "Safety' -> Gun Safety; NARAL / Planned Parenthood / Pro-Choice -> Abortion "
    "Rights; SBA / Right to Life / Pro-Life -> Anti-Abortion; Conservation "
    "Voters / Sierra / Environmental -> Environment; Human Rights Campaign / "
    "Equality -> LGBTQ Rights; Club for Growth / Taxpayers / Freedom (fiscal) -> "
    "Fiscal Conservative; Emily's List / Progressive -> Progressive.\n"
    "- A politician's own leadership PAC or campaign committee -> \"Leadership "
    "PAC\": this includes any 'Friends of <name>', '<name> for Congress/Senate', "
    "'Committee to Elect', 'Re-elect <name>', or a vague/folksy PAC named for a "
    "person with no interest signal.\n"
    "- Party committees (RNC / DNC / DCCC / NRSC / state parties) -> \"Party Committee\".\n"
    "Use \"Other\" only when the name is genuinely opaque AND doesn't look like a "
    "leadership PAC. Recognizing a well-known organization is not guessing."
)


def _run_batch(items, taxonomy, instructions, item_key=None):
    """{ITEM_UPPER: label} for the items the model labeled — omitted items are
    simply absent (the caller retries; it never caches a guessed 'Other').

    Uses a plain-text NUMBERED listing ('N=Label') rather than a json_schema
    array. Haiku silently truncates a constrained JSON array after a few items
    (stop_reason end_turn), which was mislabeling the dropped ones as 'Other';
    it completes a numbered text list reliably. Numbering (not echoing the name)
    also dodges the model mangling long, messy employer strings."""
    menu = ", ".join(taxonomy)
    listing = "\n".join(f"{i}. {x}" for i, x in enumerate(items))
    prompt = (
        instructions
        + "\n\nFor EACH numbered item below, output one line in the form "
        "'N=Label' using only these labels:\n" + menu + "\n\nOutput one line "
        "for every number and nothing else.\n\nItems:\n" + listing
    )
    resp = _c().messages.create(
        model=_MODEL, max_tokens=2000,
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    canon = {t.lower(): t for t in taxonomy}
    out = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        left = left.strip().rstrip(".").strip()
        if not left.isdigit():
            continue
        idx = int(left)
        label = canon.get(right.strip().lower())
        if 0 <= idx < len(items) and label:
            out[items[idx].strip().upper()] = label
    return out


def _classify(items, taxonomy, instructions, cache_prefix, item_key="name", force=False):
    """Map each name -> label. Per-name disk-cached; only uncached hit the model.
    force=True re-classifies and overwrites (used to heal a poisoned cache)."""
    try:
        from correspondence.db import get_disk_cache, set_disk_cache
    except Exception:
        get_disk_cache = set_disk_cache = None

    out, todo, seen = {}, [], set()
    for x in items:
        key = (x or "").strip().upper()
        if not key or key in seen:
            continue
        seen.add(key)
        cached = None
        if get_disk_cache and not force:
            try:
                cached = get_disk_cache(f"{cache_prefix}{key}", _CACHE_TTL)
            except Exception:
                cached = None
        if cached is not None:
            out[key] = cached
        else:
            todo.append(x.strip())

    for batch in _chunks(todo, 30):
        try:
            res = _run_batch(batch, taxonomy, instructions)
        except Exception as ex:
            print(f"[CLASSIFY] {cache_prefix} error: {ex}")
            res = {}
        for x in batch:
            key = x.strip().upper()
            if key in res:
                out[key] = res[key]
                if set_disk_cache:
                    try:
                        set_disk_cache(f"{cache_prefix}{key}", res[key])
                    except Exception:
                        pass
            else:
                # Model omitted it (or the call failed) — default in-memory for
                # this request but DON'T cache, so a later run resolves it.
                out.setdefault(key, "Other")
    return out


def classify(employers, force=False):
    """Employer string -> industry label. Returns {EMPLOYER_UPPER: industry}.
    Prefix bumped v2->v3 with the plain-text fix: old rows (some wrongly cached
    as 'Other' from the truncated json_schema batches) are left behind and
    re-classified correctly on next view — a non-destructive, self-healing heal."""
    return _classify(employers, INDUSTRIES, _EMPLOYER_INSTRUCTIONS, "ind:v3:", "employer", force)


def classify_pacs(names, force=False):
    """PAC name -> interest (industry, cause, or political vehicle).
    Returns {PAC_UPPER: interest}. Prefix bumped v3->v4 (see classify)."""
    return _classify(names, PAC_INTERESTS, _PAC_INSTRUCTIONS, "pac:v4:", "pac", force)


if __name__ == "__main__":
    import sys
    demo = sys.argv[1:] or ["GOOGLE", "STAR PIPE PRODUCTS", "BAYLOR",
                            "DEVOTED HEALTH INC.", "TPMG", "ZILLION TECHNOLOGIES",
                            "LOCKHEED MARTIN", "ACME LAW FIRM LLP"]
    for k, v in classify(demo).items():
        print(f"  {k[:34]:36} -> {v}")
