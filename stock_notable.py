"""Most dramatic congressional trades — biggest post-trade stock moves.

For every equity trade in data/house_stocks.json, compute how the stock moved
after the transaction date and rank the trades where the move most favored the
trade: a buy right before a jump, a sale right before a slide. One price
history is fetched per ticker (not per trade), so ~1,300 requests cover ~10k
trades. Writes data/notable_trades.json for the app to serve.

Neutral by construction: we report the stock's percentage move, never a dollar
gain (amounts are ranges, quantities unknown), and never assert wrongdoing —
just "this trade was unusually well-timed," for the reader to weigh.

Run:  python stock_notable.py
"""

import json
import time
import pathlib
import datetime
from collections import defaultdict

import stock_perf

ROOT = pathlib.Path(__file__).parent
SRC = ROOT / "data" / "house_stocks.json"
OUT = ROOT / "data" / "notable_trades.json"
_MIN_ELAPSED = 30      # need at least the 1-month window to have data
_TOP = 60              # how many dramatic trades to keep
_MIN_FAVORABLE = 12.0  # ignore trades whose best favorable move is under this %


def build(verbose=True):
    data = json.loads(SRC.read_text())
    by_ticker = defaultdict(list)
    for bg, m in data.get("members", {}).items():
        for t in m["trades"]:
            tk = t.get("ticker")
            d = stock_perf.parse_date(t.get("date", ""))
            if not tk or not d:
                continue
            dir_ = "buy" if t["type"].startswith("buy") else "sell" if t["type"].startswith("sell") else None
            if not dir_:
                continue
            by_ticker[tk].append((bg, m["name"], t, d, dir_))

    today = datetime.date.today()
    notable, done = [], 0
    for tk, items in by_ticker.items():
        earliest = min(d for *_, d, _ in items)
        ser = stock_perf.series(tk, earliest - datetime.timedelta(days=6), today)
        time.sleep(0.2)
        done += 1
        if verbose and done % 100 == 0:
            print(f"  … {done}/{len(by_ticker)} tickers, {len(notable)} candidates")
        if not ser:
            continue
        for bg, name, t, d, dir_ in items:
            if (today - d).days < _MIN_ELAPSED:
                continue
            base, windows = stock_perf.windows_after(ser, d)
            if not base or not windows:
                continue
            # Favorable = move in the trade's direction (buy wants up, sell wants
            # down). Score by the strongest favorable window.
            fav = max((w["pct"] if dir_ == "buy" else -w["pct"]) for w in windows)
            if fav < _MIN_FAVORABLE:
                continue
            notable.append({
                "bioguide": bg, "member": name, "ticker": tk,
                "type": t["type"], "dir": dir_, "date": t["date"],
                "amount": t["amount"], "owner": t.get("owner", ""),
                "base_price": round(base[1], 2), "base_date": base[0].isoformat(),
                "windows": windows, "favorable": round(fav, 1),
            })

    notable.sort(key=lambda x: x["favorable"], reverse=True)
    payload = {
        "generated": today.isoformat(),
        "cycles": data.get("cycles"),
        "count": len(notable),
        "trades": notable[:_TOP],
    }
    OUT.write_text(json.dumps(payload, separators=(",", ":")))
    if verbose:
        print(f"done: {len(notable)} notable trades (kept top {_TOP}) -> {OUT.name}")
    return payload


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    build()
