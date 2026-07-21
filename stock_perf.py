"""Post-trade stock performance — how a stock moved after a disclosed trade.

Given a ticker and a member's transaction date, fetch daily closes from the
Yahoo Finance chart API and report the price change one week, one month, and
three months later. That's the accountability lens on congressional trading:
did a buy come right before a jump, a sale right before a drop?

We report only the *stock's* move (a percentage), never a dollar figure — the
disclosed trade is an amount range and an unknown quantity, so realized gains
are unknowable, and this is emphatically not an accusation. Results are
disk-cached per (ticker, date).
"""

import datetime
import requests

_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
_UA = {"User-Agent": "Mozilla/5.0 (NosPopuli; civic transparency)"}
_TTL = 7 * 24 * 3600
_WINDOWS = [(7, "1 week"), (30, "1 month"), (90, "3 months")]

_session = requests.Session()
_session.headers.update(_UA)


def _cache_get(key):
    try:
        from correspondence.db import get_disk_cache
        return get_disk_cache(key, _TTL)
    except Exception:
        return None


def _cache_set(key, value):
    try:
        from correspondence.db import set_disk_cache
        set_disk_cache(key, value)
    except Exception:
        pass


def _yahoo_symbol(ticker):
    # Yahoo uses dashes for class shares (BRK.B -> BRK-B).
    return (ticker or "").strip().upper().replace(".", "-")


def _closes(ticker, start, end):
    """[(date, close)] daily closes in [start, end], or [] on any failure."""
    try:
        r = _session.get(
            _CHART.format(ticker=_yahoo_symbol(ticker)),
            params={"period1": int(start.timestamp()),
                    "period2": int(end.timestamp()), "interval": "1d"},
            timeout=15,
        )
        res = (r.json().get("chart") or {}).get("result")
        if not res:
            return []
        ts = res[0].get("timestamp") or []
        closes = ((res[0].get("indicators") or {}).get("quote") or [{}])[0].get("close") or []
    except Exception:
        return []
    out = []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        out.append((datetime.datetime.utcfromtimestamp(t).date(), float(c)))
    out.sort()
    return out


def _close_on_or_after(series, target):
    for d, c in series:
        if d >= target:
            return d, c
    return None


def windows_after(series, trade):
    """Given a sorted [(date, close)] series and a trade date, return
    ((base_date, base_price), [{label, pct}]) or (None, []). Only windows that
    have actually elapsed (have data) are included."""
    base = _close_on_or_after(series, trade)
    if not base or base[1] <= 0:
        return None, []
    base_date, base_price = base
    windows = []
    for days, label in _WINDOWS:
        hit = _close_on_or_after(series, trade + datetime.timedelta(days=days))
        if not hit or hit[0] < trade + datetime.timedelta(days=days):
            continue
        windows.append({"label": label, "pct": round((hit[1] / base_price - 1) * 100, 1)})
    return (base_date, base_price), windows


def parse_date(date_str):
    try:
        mm, dd, yy = date_str.split("/")
        return datetime.date(int(yy), int(mm), int(dd))
    except Exception:
        return None


def series(ticker, start_date, end_date):
    """Daily closes for a ticker across a date range (used by batch callers)."""
    return _closes(ticker,
                   datetime.datetime.combine(start_date, datetime.time()),
                   datetime.datetime.combine(end_date, datetime.time()))


def perf(ticker, date_str):
    """Post-trade performance for one trade. `date_str` is 'MM/DD/YYYY'.
    Returns {ticker, base_date, base_price, windows:[{label, pct}]} or {}.
    """
    ticker = (ticker or "").strip().upper()
    trade = parse_date(date_str)
    if not ticker or not trade:
        return {}

    ck = f"stockperf:v1:{_yahoo_symbol(ticker)}:{trade.isoformat()}"
    cached = _cache_get(ck)
    if cached is not None:
        return cached or {}

    ser = series(ticker, trade - datetime.timedelta(days=6),
                 trade + datetime.timedelta(days=100))
    base, windows = windows_after(ser, trade) if ser else (None, [])
    if not base:
        _cache_set(ck, {})
        return {}
    out = {
        "ticker": ticker,
        "base_date": base[0].isoformat(),
        "base_price": round(base[1], 2),
        "windows": windows,
    }
    _cache_set(ck, out)
    return out


if __name__ == "__main__":
    import sys, json
    from dotenv import load_dotenv
    import pathlib
    load_dotenv(pathlib.Path(__file__).parent / ".env")
    tk = sys.argv[1] if len(sys.argv) > 1 else "AAPL"
    dt = sys.argv[2] if len(sys.argv) > 2 else "12/14/2024"
    print(json.dumps(perf(tk, dt), indent=1))
