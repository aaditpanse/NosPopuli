"""House stock-trade extractor — congressional STOCK Act disclosures.

Members of the House must file a Periodic Transaction Report (PTR) within ~45
days of any securities trade over $1,000. The Clerk publishes them as a free
yearly bulk index (XML) plus one PDF per filing. This batch job walks the index,
pulls each PTR PDF, parses its transaction table, matches the filer to a
bioguide id, and writes an aggregated data/house_stocks.json that the app serves
read-only (no live parsing on request).

Honest limits, surfaced in the UI:
- Amounts are disclosed as ranges ($1,001–$15,000), never exact.
- ~45-day reporting lag; these are trades (PTRs), not a full holdings snapshot.
- House only — the Senate's eFD is gated and PDF-only (a later phase).
- A minority of members paper-file (scanned/handwritten); those don't parse
  and are simply absent, not wrong.

Run:  python house_stock_fetcher.py 2024 2025
"""

import io
import os
import re
import sys
import json
import time
import zipfile
import pathlib
import subprocess
import datetime
import xml.etree.ElementTree as ET

import requests

ROOT = pathlib.Path(__file__).parent
OUT = ROOT / "data" / "house_stocks.json"
BULK = "https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{year}FD.zip"
PTR_PDF = "https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{year}/{doc}.pdf"
_UA = {"User-Agent": "NosPopuli/1.0 (civic transparency; nospopuli.org)"}

# Disclosure amount buckets, keyed by their lower bound (which always lands on
# the transaction line even when the upper bound wraps). Compact display labels.
_RANGES = [
    (1, "$1–$1K"), (1001, "$1K–$15K"), (15001, "$15K–$50K"),
    (50001, "$50K–$100K"), (100001, "$100K–$250K"), (250001, "$250K–$500K"),
    (500001, "$500K–$1M"), (1000001, "$1M–$5M"), (5000001, "$5M–$25M"),
    (25000001, "$25M–$50M"), (50000001, "$50M+"),
]
_TYPE = {"P": "buy", "S": "sell", "E": "exchange"}
_OWNER = {"SP": "spouse", "JT": "joint", "DC": "dependent"}

# The transaction type + both dates + amount lower-bound always sit together on
# one line (those columns never wrap); the asset name and its (TICKER) may wrap
# onto the next line. So anchor on the stable columns, then hunt the ticker.
_ANCHOR = re.compile(
    r"\b([PSE])(\s*\(partial\))?\s+"                        # transaction type
    r"(\d{2}/\d{2}/\d{4})\s+\d{2}/\d{2}/\d{4}\s+"           # tx date, notification date
    r"\$([\d,]+)"                                           # amount lower bound
)
_TICKER = re.compile(r"\(([A-Z][A-Z.]{0,5})\)")            # a stock ticker (letters)
_OWNER_RE = re.compile(r"^(SP|JT|DC)\b\s*")
_TAIL_TAG = re.compile(r"\s*\([A-Z0-9.]+\)\s*(?:\[[A-Z]{2}\])?\s*$")  # trailing (TICKER)[ST]


def _range_label(low):
    label = _RANGES[0][1]
    for bound, lab in _RANGES:
        if low >= bound:
            label = lab
        else:
            break
    return label


def _bioguide_index():
    """(last, state, district) -> bioguide, for current House members."""
    data = json.loads((ROOT / "data" / "legislators-current.json").read_text())
    idx = {}
    for m in data:
        bg = m.get("id", {}).get("bioguide")
        term = (m.get("terms") or [{}])[-1]
        if not bg or term.get("type") != "rep":
            continue
        last = (m.get("name", {}).get("last") or "").upper().strip()
        idx[(last, term.get("state"), term.get("district"))] = {
            "bioguide": bg,
            "name": m.get("name", {}).get("official_full") or f"{m['name'].get('first','')} {m['name'].get('last','')}".strip(),
        }
    return idx


def _match(last, state_dst, idx):
    """Resolve a PTR filer (Last name + 'GA12') to a bioguide entry."""
    m = re.match(r"([A-Z]{2})(\d+|AL)?", (state_dst or "").upper())
    if not m:
        return None
    state = m.group(1)
    dist = 0 if m.group(2) in (None, "AL", "00") else int(m.group(2))
    return idx.get(((last or "").upper().strip(), state, dist))


def _ptr_filings(year):
    """PTR filings from the yearly bulk index: [{doc, last, first, state_dst, date}]."""
    r = requests.get(BULK.format(year=year), headers=_UA, timeout=60)
    r.raise_for_status()
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    xml = zf.read(f"{year}FD.xml")
    root = ET.fromstring(xml)
    out = []
    for m in root.findall(".//Member"):
        if (m.findtext("FilingType") or "") != "P":  # P = periodic transaction report
            continue
        doc = m.findtext("DocID")
        if not doc:
            continue
        out.append({
            "doc": doc.strip(),
            "last": (m.findtext("Last") or "").strip(),
            "state_dst": (m.findtext("StateDst") or "").strip(),
            "date": (m.findtext("FilingDate") or "").strip(),
        })
    return out


def _parse_ptr(year, doc):
    """Download + parse one PTR PDF into [{ticker, asset, type, date, amount, owner}]."""
    try:
        r = requests.get(PTR_PDF.format(year=year, doc=doc), headers=_UA, timeout=45)
        if r.status_code != 200 or not r.content:
            return []
    except Exception:
        return []
    try:
        text = subprocess.run(
            ["pdftotext", "-layout", "-", "-"],
            input=r.content, capture_output=True, timeout=60,
        ).stdout.decode("utf-8", "replace")
    except Exception:
        return []
    if len(text) < 200:  # scanned/handwritten image PDF — nothing to parse
        return []

    lines = text.splitlines()
    txns = []
    for i, line in enumerate(lines):
        m = _ANCHOR.search(line)
        if not m:
            continue
        ttype, partial, txdate, amt = m.groups()
        head = line[:m.start()].strip()
        # Ticker is on this line, or wrapped onto the next one with the asset tail.
        tk = _TICKER.search(head)
        asset = head
        if not tk and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            tk = _TICKER.search(nxt)
            if tk:
                asset = (head + " " + nxt[:nxt.find(")") + 1]).strip()
        owner = ""
        om = _OWNER_RE.match(asset)
        if om:
            owner = _OWNER.get(om.group(1), "")
            asset = asset[om.end():].strip()
        asset = _TAIL_TAG.sub("", asset).strip()
        try:
            low = int(amt.replace(",", ""))
        except ValueError:
            continue
        txns.append({
            "ticker": tk.group(1) if tk else None,
            "asset": asset[:80],
            "type": _TYPE.get(ttype, ttype) + (" (partial)" if partial else ""),
            "date": txdate,
            "amount": _range_label(low),
            "amount_low": low,
            "owner": owner,
        })
    return txns


def build(years, limit=None, delay=0.25, verbose=True):
    idx = _bioguide_index()
    members, seen_docs, filed = {}, set(), set()
    matched = unmatched = scanned = 0

    for year in years:
        filings = _ptr_filings(year)
        if verbose:
            print(f"[{year}] {len(filings)} PTR filings")
        for i, f in enumerate(filings):
            if limit and i >= limit:
                break
            if f["doc"] in seen_docs:
                continue
            seen_docs.add(f["doc"])
            # Record that this member filed *something*, even if its PDF won't
            # parse — that's what separates "no trades" from "we couldn't read it."
            hit = _match(f["last"], f["state_dst"], idx)
            if hit:
                filed.add(hit["bioguide"])
            txns = _parse_ptr(year, f["doc"])
            time.sleep(delay)
            if not txns:
                scanned += 1
                continue
            key = hit["bioguide"] if hit else None
            if key:
                matched += 1
            else:
                unmatched += 1
                continue  # can't tie to a member page without a bioguide
            slot = members.setdefault(key, {
                "bioguide": key, "name": hit["name"], "state_dst": f["state_dst"],
                "trades": [],
            })
            for t in txns:
                t["filed"] = f["date"]
                slot["trades"].append(t)
            if verbose and (matched % 25 == 0):
                print(f"  … {matched} members matched ({i}/{len(filings)} in {year})")

    for m in members.values():
        # newest transaction first
        m["trades"].sort(key=lambda t: _key_date(t.get("date")), reverse=True)
        m["trade_count"] = len(m["trades"])

    payload = {
        "generated": datetime.date.today().isoformat(),
        "source": "U.S. House Clerk — Periodic Transaction Reports",
        "cycles": years,
        "members": members,
        "filed": sorted(filed),  # every House member who filed a PTR (parsed or not)
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")))
    if verbose:
        print(f"done: {len(members)} members, matched={matched} unmatched={unmatched} "
              f"scanned/empty={scanned} -> {OUT.name}")
    return payload


def _key_date(d):
    try:
        mm, dd, yy = d.split("/")
        return (int(yy), int(mm), int(dd))
    except Exception:
        return (0, 0, 0)


if __name__ == "__main__":
    yrs = [int(a) for a in sys.argv[1:] if a.isdigit()] or [datetime.date.today().year]
    build(yrs)
