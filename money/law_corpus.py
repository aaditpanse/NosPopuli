"""A corpus of enacted Public Laws, sector-tagged, for plotting on a stock chart.

Powers the stock-centric view: pick a ticker, see its price history with the
laws in its sector marked on the timeline. Same honesty rules as bill_market —
a law on the chart is a dated event placed beside the price, never a claim that
it moved the stock.

Source: the bills files the daily sync writes from GovInfo's BILLSTATUS
(bills that became Public Law), with the title and the enactment date. Each law is multi-label
sector-tagged with the same plain-text Haiku pass used for tickers (a law can
touch several sectors), cached per law ~forever.
"""
import os
import json
import functools

from money import bill_market  # reuses its client, SECTORS, sector canon, and disk cache

_TTL_LAWSEC = 400 * 24 * 3600   # a law's sectors are stable

# Congresses whose laws we plot — aligned with the price/disclosure window.
CONGRESSES = (117, 118, 119)


def _law_list(congress):
    """[{congress, bill_type, bill_number, law, title, date}] of Public Laws,
    from the bills file the daily sync writes (Phase 6). The date is the day
    the bill became law, from its presidential actions."""
    import graph
    path = graph.data_path("bills", congress=congress)
    if not path.exists():
        return []
    out = []
    for key, rec in json.loads(path.read_text())["instruments"].items():
        laws = [x for x in rec.get("laws") or [] if (x.get("type") or "").lower() == "public law"]
        if not laws:
            continue
        btype, number = key.split("/")
        acts = rec.get("actions") or []
        became = [a for a in acts if "public law" in (a.get("text") or "").lower()] or acts
        out.append({"congress": congress, "bill_type": btype, "bill_number": number,
                    "law": laws[0].get("number"), "title": rec.get("title") or "",
                    "date": max((a.get("date") or "" for a in became), default="") or None})
    return out


def _law_key(law):
    return f"{law['congress']}-{law['bill_type']}-{law['bill_number']}"


def classify_laws(laws):
    """LAW_KEY -> [sectors]. Multi-label, per-law cached; only uncached hit the
    model. Plain-text batch (a law can touch several sectors)."""
    out, todo = {}, []
    for law in laws:
        k = _law_key(law)
        hit = bill_market._cache_get(f"lawsec:v1:{k}", _TTL_LAWSEC)
        if hit is not None:
            out[k] = hit
        else:
            todo.append(law)

    import time
    for bi in range(0, len(todo), 25):
        if bi:
            time.sleep(0.3)
        batch = todo[bi:bi + 25]
        res = _classify_law_batch(batch)
        if res is None:
            for law in batch:
                out.setdefault(_law_key(law), [])
            continue
        for i, law in enumerate(batch):
            k = _law_key(law)
            secs = res.get(i)
            if secs is None:
                out.setdefault(k, [])   # omitted — retry next run, don't cache
            else:
                out[k] = secs
                bill_market._cache_set(f"lawsec:v1:{k}", secs)
    return out


def _classify_law_batch(laws):
    """{index: [sectors]} for the numbered batch, or None on call failure."""
    menu = ", ".join(bill_market.SECTORS)
    listing = "\n".join(f"{i}. {law['title']}" for i, law in enumerate(laws))
    prompt = (
        "Each numbered item is an enacted U.S. law. For EACH one, output a line "
        "'N=' followed by the market sectors whose publicly-traded companies the "
        "law would MATERIALLY and DIRECTLY affect (spending, mandates, "
        "subsidies, restrictions, procurement), comma-separated. Be "
        "conservative; if none clearly apply, output just 'N='. Use only these "
        "sectors:\n" + menu + "\n\nOutput one line per number, nothing else.\n\n"
        + listing
    )
    try:
        resp = bill_market._client().messages.create(
            model=bill_market._MODEL, max_tokens=2000,
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in resp.content if b.type == "text")
    except Exception as ex:
        print(f"[LAW_CORPUS] classify error: {ex}")
        return None

    out = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        left = left.strip().rstrip(".").strip()
        if not left.isdigit():
            continue
        secs = []
        for part in right.split(","):
            canon = bill_market._SECTOR_CANON.get(part.strip().lower())
            if canon and canon != "Other" and canon not in secs:
                secs.append(canon)
        out[int(left)] = secs
    return out


@functools.lru_cache(maxsize=1)
def _all_laws():
    """Every corpus law with its sectors attached, sorted by date."""
    laws = []
    for c in CONGRESSES:
        laws.extend(_law_list(c))
    secs = classify_laws(laws)
    for law in laws:
        law["sectors"] = secs.get(_law_key(law), [])
    laws = [l for l in laws if l.get("date")]
    laws.sort(key=lambda l: l["date"])
    return laws


def laws_in_sector(sector):
    """Corpus laws whose sector tags include `sector`, sorted by date."""
    if not sector or sector == "Other":
        return []
    return [l for l in _all_laws() if sector in l.get("sectors", [])]
