"""Legislation ↔ market linkage.

For a given bill: which Congress-held stocks it plausibly touches, how those
stocks moved around the bill's key date, and which members have traded them —
flagging trades dated near that key action.

Design honesty (this is the whole point):
  * STRICTLY juxtaposition, never causation. A stock moves for a hundred
    reasons; we never claim a bill moved it. We place the bill's date, the
    stock's move, and the member's trade side by side and let the reader judge.
  * The ticker universe is constrained to the stocks members actually disclose
    trading — both so "who traded it" is answerable and so the classifier can't
    invent tickers.
  * The disclosures are House-only and lag ~30-45 days; the caller states that.
  * We hold TRANSACTIONS, not current holdings — so "who holds" is really "who
    has traded", with direction and date shown.

Linkage is sector-level: the bill is tagged with the market sectors it affects
(cached LLM pass, or a policy-area fallback), each Congress-traded ticker is
tagged with its sector once (cached ~forever), and we intersect. Sector-level
plausibility is deliberately modest — we do not claim a bill targets a specific
company.
"""
import os
import json
import pathlib
import datetime
import functools

from dotenv import load_dotenv

import stock_perf

# Match the rest of the app: load the repo .env so ANTHROPIC_API_KEY (and the
# Supabase creds behind the disk cache) are present when run as a standalone
# script, not just inside the already-configured web process.
load_dotenv(pathlib.Path(__file__).parent / ".env")

_MODEL = "claude-haiku-4-5-20251001"
_TTL_SECTOR = 400 * 24 * 3600   # a company's sector is stable — cache ~a year
_TTL_BILL = 45 * 24 * 3600      # a bill's affected sectors, keyed by fingerprint

SECTORS = [
    "Defense & Aerospace", "Technology & Software", "Semiconductors",
    "Healthcare & Pharma", "Financials & Banking", "Energy (Oil & Gas)",
    "Utilities & Renewables", "Consumer & Retail", "Telecom & Media",
    "Industrials & Manufacturing", "Transportation", "Real Estate",
    "Materials & Mining", "Agriculture & Food",
]

# Congress.gov policy areas → market sectors. A no-LLM baseline so the feature
# degrades gracefully (and a sanity anchor for the classifier). Not exhaustive.
_POLICY_TO_SECTORS = {
    "Armed Forces and National Security": ["Defense & Aerospace"],
    "Science, Technology, Communications": ["Technology & Software", "Telecom & Media", "Semiconductors"],
    "Health": ["Healthcare & Pharma"],
    "Finance and Financial Sector": ["Financials & Banking"],
    "Taxation": ["Financials & Banking"],
    "Energy": ["Energy (Oil & Gas)", "Utilities & Renewables"],
    "Environmental Protection": ["Utilities & Renewables", "Energy (Oil & Gas)"],
    "Commerce": ["Consumer & Retail", "Industrials & Manufacturing"],
    "Transportation and Public Works": ["Transportation", "Industrials & Manufacturing"],
    "Agriculture and Food": ["Agriculture & Food"],
    "Housing and Community Development": ["Real Estate"],
    "Public Lands and Natural Resources": ["Materials & Mining", "Energy (Oil & Gas)"],
}

_HS_PATH = os.path.join(os.path.dirname(__file__), "data", "house_stocks.json")


# ---------------------------------------------------------------- disk cache
def _cache_get(key, ttl):
    try:
        from correspondence.db import get_disk_cache
        return get_disk_cache(key, ttl)
    except Exception:
        return None


def _cache_set(key, value):
    try:
        from correspondence.db import set_disk_cache
        set_disk_cache(key, value)
    except Exception:
        pass


# ---------------------------------------------------------------- holdings
@functools.lru_cache(maxsize=1)
def _holdings():
    """Return (ticker_index, company_names).

    ticker_index: TICKER -> {bioguide -> {name, bioguide, state_dst,
                             trades:[{date, type, amount, owner}]}}
    company_names: TICKER -> best asset description seen.
    """
    with open(_HS_PATH) as f:
        d = json.load(f)
    idx, companies = {}, {}
    members = d.get("members", {})
    for m in (members.values() if isinstance(members, dict) else members):
        for t in m.get("trades", []):
            tk = (t.get("ticker") or "").strip().upper()
            if not tk:
                continue
            companies.setdefault(tk, t.get("asset") or tk)
            slot = idx.setdefault(tk, {})
            rec = slot.setdefault(m["bioguide"], {
                "name": m.get("name"), "bioguide": m.get("bioguide"),
                "state_dst": m.get("state_dst"), "trades": [],
            })
            rec["trades"].append({
                "date": t.get("date"), "type": t.get("type"),
                "amount": t.get("amount"), "owner": t.get("owner"),
            })
    return idx, companies


# ---------------------------------------------------------------- classifiers
def _client():
    from anthropic import Anthropic
    # More retries than the default (2): warming fires ~33 batches back-to-back
    # and the account's rate limit was tripping some, leaving them uncached.
    return Anthropic(max_retries=6)  # reads ANTHROPIC_API_KEY from env (.env)


def sector_of_tickers(tickers, force=False):
    """TICKER -> sector. Per-ticker cached ~forever; only uncached hit the model.
    The model sees the company name for context but keys on the ticker.

    force=True re-classifies every ticker and overwrites the cache — used to heal
    a poisoned cache (e.g. a run where auth failed and wrote "Other" for all)."""
    _, companies = _holdings()
    out, todo = {}, []
    for tk in {(t or "").strip().upper() for t in tickers if t}:
        hit = None if force else _cache_get(f"tkrsec:v1:{tk}", _TTL_SECTOR)
        if hit is not None:
            out[tk] = hit
        else:
            todo.append(tk)

    import time
    # Plain-text 'TICKER=Sector' output completes reliably at this size (the old
    # json_schema array truncated after a few items regardless of batch size).
    batches = [todo[i:i + 30] for i in range(0, len(todo), 30)]
    for bi, batch in enumerate(batches):
        if bi:
            time.sleep(0.3)  # gentle spacing to stay under the rate limit
        pairs = [(tk, companies.get(tk, tk)) for tk in batch]
        res = _batch_sector(pairs)
        if res is None:
            res = {}  # call failed entirely — nothing to cache
        for tk in batch:
            if tk in res:
                out[tk] = res[tk]
                _cache_set(f"tkrsec:v1:{tk}", res[tk])
            else:
                # Model omitted it (or the call failed). Default it in-memory for
                # this request but DON'T cache — let a later run resolve it.
                out.setdefault(tk, "Other")
    return out


_SECTOR_CANON = {s.lower(): s for s in SECTORS + ["Other"]}


def _sector_prompt(pairs):
    """The shared classification prompt — one builder so the synchronous path
    and the Batch API warm path can never drift apart."""
    sector_menu = ", ".join(SECTORS + ["Other"])
    listing = "\n".join(f"{tk} ({name})" for tk, name in pairs)
    return (
        "For EACH ticker below, output exactly one line in the form "
        "TICKER=Sector, using only these sectors:\n" + sector_menu + "\n\n"
        "Output one line for every ticker and nothing else. Use 'Other' only "
        "for a non-operating instrument (ETF, index or mutual fund, bond, note) "
        "— for a real company, infer its sector from the name.\n\nTickers:\n"
        + listing
    )


def _parse_sector_text(text):
    """Parse 'TICKER=Sector' lines into {TICKER: canonical sector}."""
    out = {}
    for line in text.splitlines():
        if "=" not in line:
            continue
        left, right = line.split("=", 1)
        tk = left.strip().split()[0].strip("-•*() ").upper() if left.strip() else ""
        canon = _SECTOR_CANON.get(right.strip().lower())
        if tk and canon:
            out[tk] = canon
    return out


def _batch_sector(pairs):
    """{TICKER: sector} for the batch, or None if the call failed entirely.

    Uses a plain-text 'TICKER=Sector' listing rather than a json_schema array:
    Haiku reliably completes a text list, but truncates a constrained JSON array
    after a few items (stop_reason end_turn at ~3-8 of 20). Tickers the model
    omits simply aren't in the returned dict — the caller retries them, never
    caches a guessed label."""
    try:
        resp = _client().messages.create(
            model=_MODEL, max_tokens=1600,
            messages=[{"role": "user", "content": _sector_prompt(pairs)}],
        )
        text = next(b.text for b in resp.content if b.type == "text")
    except Exception as ex:
        print(f"[BILL_MARKET] sector classify error: {ex}")
        return None  # signal failure so the caller doesn't cache a wrong label
    return _parse_sector_text(text)


def sector_of_tickers_batchapi(tickers, force=False, poll_seconds=15,
                               timeout_seconds=3600, log=print):
    """Same contract as sector_of_tickers, but via the Message Batches API —
    50% of synchronous token prices. Async (usually minutes, up to 24h), so
    this is for offline warm scripts only, never the request path.

    Cache semantics are identical: only labels the model actually returned are
    cached; omissions/failures stay uncached for a later run."""
    import time
    from anthropic.types.message_create_params import MessageCreateParamsNonStreaming
    from anthropic.types.messages.batch_create_params import Request

    _, companies = _holdings()
    out, todo = {}, []
    for tk in {(t or "").strip().upper() for t in tickers if t}:
        hit = None if force else _cache_get(f"tkrsec:v1:{tk}", _TTL_SECTOR)
        if hit is not None:
            out[tk] = hit
        else:
            todo.append(tk)
    if not todo:
        return out, {"submitted": 0, "cached": len(out)}

    chunks = [sorted(todo)[i:i + 30] for i in range(0, len(todo), 30)]
    requests = [
        Request(
            custom_id=f"sector-{i}",
            params=MessageCreateParamsNonStreaming(
                model=_MODEL, max_tokens=1600,
                messages=[{"role": "user", "content": _sector_prompt(
                    [(tk, companies.get(tk, tk)) for tk in chunk])}],
            ),
        )
        for i, chunk in enumerate(chunks)
    ]
    client = _client()
    batch = client.messages.batches.create(requests=requests)
    log(f"[BATCH] submitted {len(requests)} requests ({len(todo)} tickers) "
        f"as {batch.id} — 50% token pricing")

    deadline = time.time() + timeout_seconds
    while True:
        batch = client.messages.batches.retrieve(batch.id)
        if batch.processing_status == "ended":
            break
        if time.time() > deadline:
            log(f"[BATCH] still {batch.processing_status} after "
                f"{timeout_seconds}s — results stay retrievable for 29 days; "
                f"re-run this script later to collect {batch.id}")
            return out, {"submitted": len(requests), "batch_id": batch.id,
                         "pending": True}
        time.sleep(poll_seconds)

    labeled = 0
    for result in client.messages.batches.results(batch.id):
        if result.result.type != "succeeded":
            log(f"[BATCH] {result.custom_id}: {result.result.type} — "
                "those tickers stay uncached for a later run")
            continue
        msg = result.result.message
        text = next((b.text for b in msg.content if b.type == "text"), "")
        asked = set(todo)
        for tk, sector in _parse_sector_text(text).items():
            if tk in asked:  # never cache a ticker the model invented
                out[tk] = sector
                _cache_set(f"tkrsec:v1:{tk}", sector)
                labeled += 1
    for tk in todo:
        out.setdefault(tk, "Other")  # in-memory default, never cached
    return out, {"submitted": len(requests), "labeled": labeled,
                 "omitted": len(todo) - labeled, "batch_id": batch.id}


def bill_sectors(bill, fingerprint=""):
    """Which market sectors this bill plausibly affects (0..N). Cached per bill
    fingerprint. Falls back to the policy-area map if the model is unavailable."""
    congress = bill.get("congress")
    btype = (bill.get("type") or "").lower()
    number = bill.get("number")
    ck = f"billsec:v1:{congress}:{btype}:{number}:{fingerprint}"
    hit = _cache_get(ck, _TTL_BILL)
    if hit is not None:
        return hit

    policy = (bill.get("policyArea") or {}).get("name", "")
    fallback = _POLICY_TO_SECTORS.get(policy, [])

    title = bill.get("title", "")
    summary = ""
    subjects = bill.get("subjects") or {}
    if isinstance(subjects, dict):
        summary = ", ".join(
            s.get("name", "") for s in (subjects.get("legislativeSubjects") or [])[:15])

    result = _llm_bill_sectors(title, policy, summary)
    sectors = result if result is not None else fallback
    _cache_set(ck, sectors)
    return sectors


def _llm_bill_sectors(title, policy, summary):
    """Return a list of affected sectors, or None on failure (caller falls back)."""
    schema = {
        "type": "object",
        "properties": {"sectors": {
            "type": "array", "items": {"type": "string", "enum": SECTORS}}},
        "required": ["sectors"],
        "additionalProperties": False,
    }
    prompt = (
        "A U.S. bill is described below. List only the market sectors whose "
        "publicly-traded companies this bill would MATERIALLY and DIRECTLY "
        "affect (via spending, mandates, subsidies, restrictions, or "
        "procurement). Be conservative: if a sector is only tangentially "
        "related, leave it out. Return an empty list if none clearly apply.\n\n"
        f"Title: {title}\nPolicy area: {policy}\nSubjects: {summary}"
    )
    try:
        resp = _client().messages.create(
            model=_MODEL, max_tokens=300,
            output_config={"format": {"type": "json_schema", "schema": schema}},
            messages=[{"role": "user", "content": prompt}],
        )
        text = next(b.text for b in resp.content if b.type == "text")
        return json.loads(text).get("sectors", [])
    except Exception as ex:
        print(f"[BILL_MARKET] bill sector error: {ex}")
        return None


# ---------------------------------------------------------------- linkage
def _near(trade_date_str, event_date, before=75, after=30):
    """Is a trade dated within [-before, +after] days of the bill's key date?
    The asymmetry favors trades made AHEAD of the action (the sharper signal),
    accounting for disclosure lag."""
    d = stock_perf.parse_date(trade_date_str)
    if not d or not event_date:
        return False
    return -after <= (event_date - d).days <= before


def linkage(bill, event_date_iso, fingerprint="", max_stocks=25):
    """Assemble the bill↔market view.

    Returns {event_date, sectors, stocks:[...], universe, disclaimer} where each
    stock is {ticker, company, sector, move, traders:[...], near_count}.
    `move` is stock_perf.perf() around the event date (empty if no data).
    """
    try:
        event_date = datetime.date.fromisoformat(event_date_iso) if event_date_iso else None
    except Exception:
        event_date = None

    sectors = bill_sectors(bill, fingerprint)
    idx, companies = _holdings()
    if not sectors:
        return {"event_date": event_date_iso, "sectors": [], "stocks": [],
                "universe": len(idx), "disclaimer": _DISCLAIMER}

    tsec = sector_of_tickers(list(idx.keys()))
    wanted = set(sectors)
    candidates = [tk for tk, s in tsec.items() if s in wanted and tk in idx]

    stocks = []
    for tk in candidates:
        traders = []
        near_count = 0
        for rec in idx[tk].values():
            trades = sorted(rec["trades"], key=lambda t: stock_perf.parse_date(t["date"]) or datetime.date.min)
            near = [t for t in trades if _near(t["date"], event_date)]
            if near:
                near_count += 1
            traders.append({
                "name": rec["name"], "bioguide": rec["bioguide"],
                "state_dst": rec["state_dst"],
                "last_trade": trades[-1] if trades else None,
                "trade_count": len(trades),
                "near": near,  # trades dated near the bill's key action
            })
        # surface members who traded near the action first, then by activity
        traders.sort(key=lambda x: (-len(x["near"]), -x["trade_count"]))
        move = stock_perf.perf(tk, _mmddyyyy(event_date)) if event_date else {}
        stocks.append({
            "ticker": tk, "company": companies.get(tk, tk),
            "sector": tsec.get(tk), "move": move,
            "traders": traders, "trader_count": len(traders),
            "near_count": near_count,
        })

    # rank: bills where someone traded near the action, then biggest move,
    # then most-traded — the combination most worth a reader's eye
    stocks.sort(key=lambda s: (-s["near_count"], -_abs_move(s["move"]), -s["trader_count"]))
    return {
        "event_date": event_date_iso, "sectors": sectors,
        "stocks": stocks[:max_stocks], "universe": len(idx),
        "disclaimer": _DISCLAIMER,
    }


def _mmddyyyy(d):
    return d.strftime("%m/%d/%Y") if d else ""


def _abs_move(move):
    """Largest-magnitude post-event window move, for ranking."""
    ws = (move or {}).get("windows") or []
    return max((abs(w["pct"]) for w in ws), default=0.0)


_DISCLAIMER = (
    "Coincidence, not proof. Stocks move for many reasons; this places the "
    "bill's date, each stock's move around it, and members' disclosed trades "
    "side by side — it does not claim the bill caused the move or that any "
    "trade was improper. Disclosures are House-only and lag 30-45 days; sectors "
    "are an approximate, machine-assigned link."
)
