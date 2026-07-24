"""Warm the ticker→sector cache used by the bill↔market feature.

Classifies each stock Congress has disclosed trading into a market sector
(Haiku, disk-cached ~1 year). Idempotent: only uncached tickers hit the model,
so re-running is cheap and safe. Needs ANTHROPIC_API_KEY with a balance;
writes go to the shared disk cache (prod Supabase if SUPABASE_DB_URL is set),
so warming once benefits every environment.

Uses the Message Batches API by default — 50% of synchronous token prices,
same prompt, same parser, same only-cache-what-the-model-returned rule.

    python warm_ticker_sectors.py            # warm uncached only (batch, cheap)
    python warm_ticker_sectors.py --force    # re-classify everything (batch)
    python warm_ticker_sectors.py --sync     # old synchronous path (2x price)
"""
import argparse

import bill_market as bm


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true",
                        help="re-classify and overwrite every cached label")
    parser.add_argument("--sync", action="store_true",
                        help="use the synchronous API instead of batches")
    args = parser.parse_args()

    idx, companies = bm._holdings()
    tickers = list(idx.keys())
    print(f"Universe: {len(tickers)} tickers disclosed by Congress.")

    already = sum(1 for tk in tickers
                  if bm._cache_get(f"tkrsec:v1:{tk}", bm._TTL_SECTOR) is not None)
    print(f"Already classified (cached): {already}. "
          f"To do: {len(tickers) if args.force else len(tickers) - already}.")

    if args.sync:
        result = bm.sector_of_tickers(tickers, force=args.force)
        stats = {"path": "sync"}
    else:
        result, stats = bm.sector_of_tickers_batchapi(tickers, force=args.force)
        if stats.get("pending"):
            print("\nBatch still processing — nothing cached yet. "
                  "Re-run later; only uncached tickers resubmit.")
            return

    from collections import Counter
    dist = Counter(result.values())
    print("\nSector distribution:")
    for sector, n in dist.most_common():
        print(f"  {n:4d}  {sector}")
    print(f"\nDone. {len(result)} tickers classified and cached. {stats}")


if __name__ == "__main__":
    main()
