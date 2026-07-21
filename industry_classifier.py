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


def _run_batch(items, taxonomy, instructions, item_key):
    schema = {
        "type": "object",
        "properties": {"items": {"type": "array", "items": {
            "type": "object",
            "properties": {
                item_key: {"type": "string"},
                "label": {"type": "string", "enum": taxonomy},
            },
            "required": [item_key, "label"],
            "additionalProperties": False,
        }}},
        "required": ["items"],
        "additionalProperties": False,
    }
    prompt = (instructions + "\n\nUse only the provided labels, and echo each "
              "name exactly as given.\n\nNames:\n" + "\n".join(f"- {x}" for x in items))
    resp = _c().messages.create(
        model=_MODEL, max_tokens=2200,
        output_config={"format": {"type": "json_schema", "schema": schema}},
        messages=[{"role": "user", "content": prompt}],
    )
    text = next(b.text for b in resp.content if b.type == "text")
    data = json.loads(text)
    return {it[item_key].strip().upper(): it["label"] for it in data.get("items", [])}


def _classify(items, taxonomy, instructions, cache_prefix, item_key="name"):
    """Map each name -> label. Per-name disk-cached; only uncached hit the model."""
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
        if get_disk_cache:
            try:
                cached = get_disk_cache(f"{cache_prefix}{key}", _CACHE_TTL)
            except Exception:
                cached = None
        if cached is not None:
            out[key] = cached
        else:
            todo.append(x.strip())

    for batch in _chunks(todo, 40):
        try:
            res = _run_batch(batch, taxonomy, instructions, item_key)
        except Exception as ex:
            print(f"[CLASSIFY] {cache_prefix} error: {ex}")
            res = {}
        for x in batch:
            key = x.upper()
            label = res.get(key) or "Other"
            out[key] = label
            if set_disk_cache:
                try:
                    set_disk_cache(f"{cache_prefix}{key}", label)
                except Exception:
                    pass
    return out


def classify(employers):
    """Employer string -> industry label. Returns {EMPLOYER_UPPER: industry}."""
    return _classify(employers, INDUSTRIES, _EMPLOYER_INSTRUCTIONS, "ind:v2:", "employer")


def classify_pacs(names):
    """PAC name -> interest (industry, cause, or political vehicle).
    Returns {PAC_UPPER: interest}."""
    return _classify(names, PAC_INTERESTS, _PAC_INSTRUCTIONS, "pac:v3:", "pac")


if __name__ == "__main__":
    import sys
    demo = sys.argv[1:] or ["GOOGLE", "STAR PIPE PRODUCTS", "BAYLOR",
                            "DEVOTED HEALTH INC.", "TPMG", "ZILLION TECHNOLOGIES",
                            "LOCKHEED MARTIN", "ACME LAW FIRM LLP"]
    for k, v in classify(demo).items():
        print(f"  {k[:34]:36} -> {v}")
