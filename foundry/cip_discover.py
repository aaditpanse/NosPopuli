"""Stage 1 — CIP discovery: find a county's Capital Improvement Program
document autonomously, so onboarding needs only a jurisdiction name.

    python cip_discover.py "Prince William County, Virginia" princewilliam

Municipal CIPs have no platform families (unlike Legistar/Granicus meetings),
so there's no URL pattern to probe — discovery is genuine web search. This
runs Claude (Sonnet, cheap — navigation not reasoning) with the server-side
web_search tool to locate the current adopted/proposed CIP PDF(s) and the
fiscal window, then VERIFIES each candidate deterministically (downloads it,
converts with pdftotext, checks it is text-bearing and actually contains
project cost tables) before writing a profile that cip_onboard.py consumes.
A scanned-image CIP fails verification and is reported, not onboarded.
"""

import argparse
import json
import pathlib
import re
import subprocess
import sys
import tempfile
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import anthropic
from dotenv import load_dotenv

import budget

load_dotenv(pathlib.Path(__file__).parent.parent / ".env")
FOUNDRY = pathlib.Path(__file__).parent
OUT = FOUNDRY / "data" / "discovery"
MODEL = "claude-sonnet-4-6"
PRICES = (3.00, 0.30, 15.00)  # $/MTok input, cache-read, output

SYSTEM = """You find the current Capital Improvement Program (CIP) document \
for a U.S. county or city. The CIP is the multi-year capital budget that \
lists funded capital projects with their costs and funding sources — often a \
single large PDF, sometimes split into per-section PDFs.

Use web_search to find the CURRENT adopted or advertised/proposed CIP on the \
government's official domain (.gov / .us). You want the volume(s) that \
contain the per-project cost tables, not a summary slide deck or a news page. \
Verify by looking at the actual document link, not memory.

Finish with ONLY a fenced json block, no prose after it:
```json
{
  "jurisdiction": "<full name, e.g. Prince William County, Virginia>",
  "edition": "<e.g. FY2026-2031 Capital Improvement Program>",
  "fiscal_first": <first CIP fiscal year, int>,
  "fiscal_last": <last CIP fiscal year, int>,
  "source_url": "<human-viewable CIP landing page>",
  "cip_urls": ["<direct url to the CIP PDF(s) with the project cost tables>"],
  "notes": "<one line: single volume or split; anything unusual>"
}
```"""


def discover(jurisdiction, log=print):
    budget.check("discovery")
    client = anthropic.Anthropic()
    resp = client.messages.create(
        model=MODEL, max_tokens=3000,
        tools=[{"type": "web_search_20260209", "name": "web_search"}],
        system=SYSTEM,
        messages=[{"role": "user", "content":
                   f"Find the current CIP document for {jurisdiction}."}])
    u = resp.usage
    cost = (u.input_tokens * PRICES[0] + getattr(u, "cache_read_input_tokens", 0)
            * PRICES[1] + u.output_tokens * PRICES[2]) / 1e6
    budget.record("discovery", cost)
    text = "".join(b.text for b in resp.content if b.type == "text")
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.S)
    if not m:
        log("no profile json returned by discovery")
        return None
    prof = json.loads(m.group(1))
    log(f"discovery proposed: {prof.get('edition')} — {len(prof.get('cip_urls', []))} "
        f"url(s), FY{prof.get('fiscal_first')}-{prof.get('fiscal_last')} (${cost:.3f})")
    return prof


def verify(prof, log=print):
    """Keep only CIP urls that download, convert to text, and carry cost-table
    signal — so a dead link or a scanned image never reaches synthesis."""
    good = []
    for url in prof.get("cip_urls", []):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "nospopuli-foundry-lab"})
            body = urllib.request.urlopen(req, timeout=60).read()
        except Exception as exc:
            log(f"  {url} -> unreachable ({str(exc)[:60]})")
            continue
        if body[:5] != b"%PDF-":
            log(f"  {url} -> not a PDF")
            continue
        with tempfile.NamedTemporaryFile(suffix=".pdf") as tmp:
            tmp.write(body)
            tmp.flush()
            text = subprocess.run(["pdftotext", "-layout", tmp.name, "-"],
                                  capture_output=True, text=True).stdout
        signal = sum(text.count(k) for k in
                     ("Project Cost", "Funding Source", "Total Project", "of Funds",
                      "Expenditure", "Appropriat", "Revenue", "CIP"))
        if len(text) < 5000 or signal < 5:
            log(f"  {url} -> {len(text)} chars, weak cost-table signal ({signal}) — "
                "likely scanned or wrong document")
            continue
        log(f"  {url} -> OK ({len(text)} chars, signal {signal})")
        good.append(url)
    prof["cip_urls"] = good
    return good


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("jurisdiction")
    ap.add_argument("slug")
    args = ap.parse_args()
    prof = discover(args.jurisdiction)
    if not prof:
        return 1
    if not verify(prof):
        print("verdict: no verifiable CIP document found — cannot onboard "
              "(may be split oddly, scanned, or behind a portal)")
        return 1
    prof["source_id"] = f"{args.slug}-cip"
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{args.slug}-cip_profile.json"
    path.write_text(json.dumps(prof, indent=1))
    print(f"profile -> {path.relative_to(FOUNDRY)}  ({len(prof['cip_urls'])} verified url(s))")
    print("next: python cip_onboard.py " + str(path.relative_to(FOUNDRY)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
