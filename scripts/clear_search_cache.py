"""Manually clear the search cache.

Usage:
  python clear_search_cache.py            # clear search cache only
  python clear_search_cache.py --all      # clear every disk_cache entry (feed, elections, search)
"""
import sys

from search.search_cache import clear as clear_search
from correspondence.db import clear_disk_cache


def main(argv):
    if "--all" in argv:
        n = clear_disk_cache()
        print(f"Cleared {n} entries from disk_cache (all namespaces).")
        return
    n = clear_search()
    print(f"Cleared {n} search-cache entries (prefix 'search:v1:').")


if __name__ == "__main__":
    main(sys.argv[1:])
