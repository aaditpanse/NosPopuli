"""Geocode the point-locatable capital projects so they can be mapped.

    python geocode_projects.py [--source fairfax-cip]

A CIP lists projects by name, not coordinates, and only some projects ARE
places: named fire stations, libraries, schools, the courthouse, specific
parks. Countywide programs ("Athletic Field Maintenance", "LED Streetlights")
have no single point and get no pin — faking one would be a lie. This pass
geocodes only the named-facility projects via OpenStreetMap's Nominatim
(free), biased to the jurisdiction, validates the hit is inside the county's
bounding box, and writes lat/lon + a confidence onto the record. Results are
cached, so re-runs cost no network; a project that doesn't resolve stays
unpinned rather than guessed.

Deterministic and $0 (no LLM). Nominatim etiquette: <=1 req/sec, real
User-Agent — honored below.
"""

import argparse
import json
import pathlib
import re
import time
import urllib.parse
import urllib.request

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"

# The jurisdiction bias and the bounding box that rejects out-of-area matches
# are derived per-county from the store's meta (see county_bounds), so this
# geocoder is not Fairfax-specific — any onboarded CIP works.
UA = "NosPopuli-Foundry/1.0 (civic data lab; aaditpanse@pm.me)"
NOMINATIM = "https://nominatim.openstreetmap.org/search?"

# a project is a candidate pin only if its title names a kind of place
PLACE_WORDS = re.compile(
    r"\b(Station|Library|Center|Centre|Park|School|Elementary|Middle|High|Plant"
    r"|Facility|Court|Courthouse|Reservoir|Farm|Gardens|Preserve|Complex|Shelter"
    r"|Campus|Mill|Annex|Academy|Detention|Judicial|Museum|Theater|Pool|Rec)\b",
    re.I)
# ...but not if it's really a countywide program that happens to contain one
PROGRAM_WORDS = re.compile(
    r"\b(Maintenance|Services? Fee|Upgrades|Replacement|Renovations? and|Compliance"
    r"|Assessments?|Reinvestment|Contributory|Allocation|Sinking Fund|Snow Removal"
    r"|Playground Assessments|Preventative|Custodial|Scholarships|Diamonds)\b", re.I)
# OSM classes/types that confirm we hit an actual facility
GOOD_TYPES = {"fire_station", "library", "school", "college", "university",
              "courthouse", "hospital", "park", "nature_reserve", "garden",
              "museum", "theatre", "community_centre", "sports_centre",
              "swimming_pool", "recycling", "wastewater_plant", "water_works",
              "prison", "townhall", "public_building", "government"}

CACHE = FOUNDRY / "data" / "onboard" / "geocode_cache.json"


_FUNC_TAILS = (r"Public Schools|Public Safety(?: Police| Fire and Rescue)?"
               r"|Health and Human Services|Park Authority|NOVA Parks")
_ACTION_TAILS = (r"Renovations?(?: Program)?|Improvements?|Enhancements?|Expansion"
                 r"|Conversion|Rehabilitation|Retrofits|Realignments|Program"
                 r"|Demo/?Reno|Share|Initiatives?|Opportunities")


def clean_query(title):
    q = re.sub(r"\s*-\s*\d{4}.*$", "", title)       # drop "- 2030 TBD" bond-year tags
    q = re.sub(r"\bTBD\b", "", q)
    q = re.sub(r"\([^)]*\)", "", q)                 # drop parentheticals
    q = re.sub(r"\s*/\s*.*$", "", q)                # drop "/ Shelter" style suffixes
    # a title wrap sometimes trails the next section's name onto the title
    q = re.sub(rf"\s+(?:{_FUNC_TAILS})\s*$", "", q)
    # strip trailing "... Phase 2", and action words, so the place name remains
    q = re.sub(r"\s+Phase\s+\d+\b.*$", "", q)
    for _ in range(2):
        q = re.sub(rf"\s+(?:{_ACTION_TAILS})\s*$", "", q, flags=re.I)
    q = re.sub(r"\s{2,}", " ", q).strip(" -,")
    return q


def _nominatim(params, cache_key, cache):
    if cache_key in cache:
        return cache[cache_key]
    try:
        req = urllib.request.Request(
            NOMINATIM + urllib.parse.urlencode(params), headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=25) as r:
            rows = json.load(r)
        time.sleep(1.2)  # Nominatim: <=1 req/sec
    except Exception:
        rows = []
    cache[cache_key] = rows
    return rows


def county_bounds(jurisdiction, cache):
    """Derive the county's own bounding box from OSM so out-of-area hits are
    rejected — per-county, not hardcoded to Fairfax."""
    rows = _nominatim({"q": jurisdiction, "format": "json", "limit": 1,
                       "featuretype": "county", "countrycodes": "us"},
                      f"__bounds__{jurisdiction}", cache)
    if not rows:
        return None
    s, n, w, e = (float(x) for x in rows[0]["boundingbox"])
    return {"min_lat": s, "max_lat": n, "min_lon": w, "max_lon": e}


def geocode(query, county_query, bbox, cache):
    key = f"{query}|{county_query}"
    if key in cache:
        return cache[key]
    rows = _nominatim({"q": f"{query}, {county_query}", "format": "json",
                       "limit": 1, "countrycodes": "us", "addressdetails": 0},
                      f"__q__{key}", cache)
    hit = rows[0] if rows else None
    result = None
    if hit:
        lat, lon = float(hit["lat"]), float(hit["lon"])
        in_box = (bbox is None or (bbox["min_lat"] <= lat <= bbox["max_lat"]
                                   and bbox["min_lon"] <= lon <= bbox["max_lon"]))
        if in_box:
            good = hit.get("type") in GOOD_TYPES or hit.get("class") in ("amenity", "leisure")
            result = {"lat": round(lat, 6), "lon": round(lon, 6),
                      "confidence": "high" if good else "medium",
                      "osm_type": hit.get("type"),
                      "matched": hit.get("display_name", "")[:120]}
    cache[key] = result
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="fairfax-cip")
    args = ap.parse_args()
    path = STORE / f"{args.source}.json"
    store = json.loads(path.read_text())
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}

    # jurisdiction + bounds come from the store's own meta, so any onboarded
    # county's projects geocode against ITS bounding box
    county_query = store.get("meta", {}).get("jurisdiction", "")
    bbox = county_bounds(county_query, cache) if county_query else None
    print(f"jurisdiction: {county_query or '(none in meta)'}; "
          f"bbox: {'derived' if bbox else 'none — accepting all US hits'}")

    projects = store["capital_projects"]
    candidates = [p for p in projects
                  if PLACE_WORDS.search(p["title"]) and not PROGRAM_WORDS.search(p["title"])]
    print(f"{len(projects)} projects; {len(candidates)} named-facility candidates to geocode")
    pinned = 0
    for p in projects:
        p.pop("lat", None)
        p.pop("lon", None)
        p.pop("geocode", None)
    for p in candidates:
        res = geocode(clean_query(p["title"]), county_query, bbox, cache)
        CACHE.parent.mkdir(parents=True, exist_ok=True)
        CACHE.write_text(json.dumps(cache, indent=1))
        if res:
            p["lat"], p["lon"] = res["lat"], res["lon"]
            p["geocode"] = {"confidence": res["confidence"], "osm_type": res["osm_type"],
                            "matched": res["matched"]}
            pinned += 1

    path.write_text(json.dumps(store, indent=1))
    hi = sum(1 for p in projects if p.get("geocode", {}).get("confidence") == "high")
    print(f"pinned {pinned}/{len(candidates)} candidates "
          f"({hi} high-confidence); {len(projects) - pinned} unpinned "
          "(countywide programs or unresolved)")
    print("landed lat/lon into", path.name)


if __name__ == "__main__":
    main()
