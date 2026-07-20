"""Spend ledger + daily budget for the LLM-spending Foundry paths.

Every synthesis (Opus) and discovery (Sonnet) call records its cost here;
before starting, they check today's total against FOUNDRY_DAILY_BUDGET
(default $8). Enrichment (Haiku: summaries, digests, schedules) runs in
pennies and is recorded but never blocked. The point is that a runaway
repair loop or an enthusiastic session stops itself instead of surprising
the bill.

    python budget.py            # today's and this month's spend by kind
"""

import datetime
import json
import os
import pathlib

try:  # the cap lives in .env; honor it however the caller was launched
    from dotenv import load_dotenv
    load_dotenv(pathlib.Path(__file__).parent.parent / ".env")
except ImportError:
    pass

LEDGER = pathlib.Path(__file__).parent / "data" / "spend_log.jsonl"
DEFAULT_DAILY_USD = 8.0


def record(kind, usd):
    if usd <= 0:
        return
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER.open("a") as fh:
        fh.write(json.dumps({"ts": datetime.datetime.now().isoformat(timespec="seconds"),
                             "kind": kind, "usd": round(usd, 4)}) + "\n")


def spent_since(prefix):
    if not LEDGER.exists():
        return 0.0
    total = 0.0
    for line in LEDGER.read_text().splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if row.get("ts", "").startswith(prefix):
            total += row.get("usd", 0.0)
    return total


def check(kind):
    """Raise before an expensive call when today's ledger exceeds the cap."""
    cap = float(os.environ.get("FOUNDRY_DAILY_BUDGET", DEFAULT_DAILY_USD))
    today = spent_since(datetime.date.today().isoformat())
    if today >= cap:
        raise RuntimeError(
            f"daily LLM budget reached: ${today:.2f} spent today >= "
            f"${cap:.2f} cap — refusing to start '{kind}'. Raise it for one "
            "run with FOUNDRY_DAILY_BUDGET=<usd> if this spend is deliberate.")


def main():
    today = datetime.date.today()
    for label, prefix in (("today", today.isoformat()),
                          ("this month", today.isoformat()[:7])):
        by_kind = {}
        if LEDGER.exists():
            for line in LEDGER.read_text().splitlines():
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if row.get("ts", "").startswith(prefix):
                    by_kind[row["kind"]] = by_kind.get(row["kind"], 0) + row["usd"]
        total = sum(by_kind.values())
        detail = ", ".join(f"{k} ${v:.2f}" for k, v in
                           sorted(by_kind.items(), key=lambda x: -x[1]))
        print(f"{label}: ${total:.2f}" + (f"  ({detail})" if detail else ""))
    return 0


if __name__ == "__main__":
    import sys
    sys.exit(main())
