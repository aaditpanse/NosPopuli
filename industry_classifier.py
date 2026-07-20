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

_client = None


def _c():
    global _client
    if _client is None:
        _client = anthropic.Anthropic()
    return _client


def _chunks(seq, n):
    for i in range(0, len(seq), n):
        yield seq[i:i + n]


def _classify_batch(employers):
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "employer": {"type": "string"},
                "industry": {"type": "string", "enum": INDUSTRIES},
            },
            "required": ["employer", "industry"],
            "additionalProperties": False,
        }}},
        "required": ["items"],
        "additionalProperties": False,
    }
    prompt = (
        "You classify the employer of a US political donor into its primary "
        "industry, the way OpenSecrets does. Return one entry per employer, "
        "echoing the employer string exactly as given, using only the provided "
        "industry labels.\n\n"
        "Infer from clear signals in the name, even for firms you don't know:\n"
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
        "Only use \"Other\" when the name gives no reasonable signal at all.\n\n"
        "Employers:\n" + "\n".join(f"- {e}" for e in employers)
    )
    resp = _c().messages.create(
        model=_MODEL, max_tokens=2000,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    return {it["employer"].strip().upper(): it["industry"]
            for it in data.get("items", [])}


def classify(employers):
    """Map each employer string -> industry label. Per-employer disk-cached;
    only the uncached ones hit the model (batched). Returns {EMPLOYER_UPPER: industry}."""
    try:
        from correspondence.db import get_disk_cache, set_disk_cache
    except Exception:
        get_disk_cache = set_disk_cache = None

    out, todo, seen = {}, [], set()
    for e in employers:
        key = (e or "").strip().upper()
        if not key or key in seen:
            continue
        seen.add(key)
        cached = None
        if get_disk_cache:
            try:
                cached = get_disk_cache(f"ind:v2:{key}", _CACHE_TTL)
            except Exception:
                cached = None
        if cached is not None:
            out[key] = cached
        else:
            todo.append(e.strip())

    for batch in _chunks(todo, 40):
        try:
            res = _classify_batch(batch)
        except Exception as ex:
            print(f"[INDUSTRY] classify error: {ex}")
            res = {}
        for e in batch:
            key = e.upper()
            label = res.get(key) or "Other"
            out[key] = label
            if set_disk_cache:
                try:
                    set_disk_cache(f"ind:v2:{key}", label)
                except Exception:
                    pass
    return out


if __name__ == "__main__":
    import sys
    demo = sys.argv[1:] or ["GOOGLE", "STAR PIPE PRODUCTS", "BAYLOR",
                            "DEVOTED HEALTH INC.", "TPMG", "ZILLION TECHNOLOGIES",
                            "LOCKHEED MARTIN", "ACME LAW FIRM LLP"]
    for k, v in classify(demo).items():
        print(f"  {k[:34]:36} -> {v}")
