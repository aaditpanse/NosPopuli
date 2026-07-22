"""Warm the ticker→sector cache used by the bill↔market feature.

Classifies each stock Congress has disclosed trading into a market sector
(Haiku, batched, disk-cached ~1 year). Idempotent: only uncached tickers hit
the model, so re-running is cheap and safe. Needs ANTHROPIC_API_KEY with a
balance; writes go to the shared disk cache (prod Supabase if SUPABASE_DB_URL
is set), so warming once benefits every environment.

    python warm_ticker_sectors.py
"""
import bill_market as bm


def main():
    idx, companies = bm._holdings()
    tickers = list(idx.keys())
    print(f"Universe: {len(tickers)} tickers disclosed by Congress.")

    # Count what's already cached so re-runs report honestly.
    already = 0
    for tk in tickers:
        if bm._cache_get(f"tkrsec:v1:{tk}", bm._TTL_SECTOR) is not None:
            already += 1
    print(f"Already classified (cached): {already}. To do: {len(tickers) - already}.")

    # force=True re-classifies and overwrites every entry. Safe & idempotent;
    # also heals a cache poisoned by an earlier failed (unauthenticated) run.
    result = bm.sector_of_tickers(tickers, force=True)

    from collections import Counter
    dist = Counter(result.values())
    print("\nSector distribution:")
    for sector, n in dist.most_common():
        print(f"  {n:4d}  {sector}")
    print(f"\nDone. {len(result)} tickers classified and cached.")


if __name__ == "__main__":
    main()
