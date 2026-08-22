"""Resolve a location to the representative who ACTUALLY represents it.

The old path (civic_resolver.resolve_zip) mapped a 3-digit ZIP prefix to a
state and then returned the *first* House member in that state — wrong for
anyone outside that state's district 1, because a ZIP cannot determine a
congressional district (ZIP areas straddle district lines). This module
resolves the district properly: geocode the address or point with the free,
official U.S. Census Geocoder (no API key), read the 119th Congressional
District it returns, and look up the exact member in legislators-current.json.

    resolve_address("4000 Legato Rd, Fairfax, VA 22033")  -> VA-11, Gerry Connolly
    resolve_point(38.86, -77.36)                          -> same

Federal only for now (the shipped legislators file is Congress). The geocoder
also returns state-legislative district codes; those ride along in the result
for the map to use, but resolving statehouse members needs a state dataset.
"""

import json
import urllib.parse
import urllib.request

from civic_resolver import LEGISLATORS

GEOCODER = "https://geocoding.geo.census.gov/geocoder/geographies/"
BENCHMARK, VINTAGE = "Public_AR_Current", "Current_Current"
CD_LAYER = "119th Congressional Districts"

# Census state FIPS -> USPS, so the geocoder's numeric state matches the
# legislators file's postal abbreviations.
FIPS_TO_USPS = {
    "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
    "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
    "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
    "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
    "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
    "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
    "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
    "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
    "54": "WV", "55": "WI", "56": "WY", "60": "AS", "66": "GU", "69": "MP",
    "72": "PR", "78": "VI",
}


def _current(last_term):
    end = last_term.get("end", "")
    return not end or end >= "2025-01-01"


def _person(member, last_term, state):
    mtype = last_term.get("type", "")
    person = {
        "name": f"{member['name']['first']} {member['name']['last']}",
        "bioguide_id": member["id"].get("bioguide", ""),
        "party": last_term.get("party", ""),
        "state": state,
        "chamber": "Senate" if mtype == "sen" else "House",
        "contact_form": last_term.get("contact_form", ""),
        "url": last_term.get("url", ""),
        "term_start": (last_term.get("start") or "")[:4],
        "term_end": (last_term.get("end") or "")[:4],
    }
    if mtype == "rep":
        person["district"] = last_term.get("district")
    return person


def resolve_district(state_abbr, district):
    """Given a state (USPS) and a congressional district number, return the
    exact House member plus both senators from the shipped legislators file."""
    senators, representative = [], None
    for member in LEGISLATORS:
        terms = member.get("terms", [])
        if not terms:
            continue
        lt = terms[-1]
        if not _current(lt) or lt.get("state") != state_abbr:
            continue
        mtype = lt.get("type", "")
        if mtype == "sen":
            senators.append(_person(member, lt, state_abbr))
        elif mtype == "rep" and representative is None:
            # match the district; at-large states use 0
            md = lt.get("district")
            if md is not None and int(md) == int(district):
                representative = _person(member, lt, state_abbr)
    return {"state": state_abbr, "district": int(district),
            "senators": senators[:2], "representative": representative}


def resolve_geoid(geoid):
    """Resolve a district GEOID (state FIPS + 2-digit district) straight to its
    reps — used when the user clicks a district on the map (no geocoding)."""
    geoid = str(geoid)
    state_fips, cd = geoid[:2], geoid[2:]
    state_abbr = FIPS_TO_USPS.get(state_fips)
    if not state_abbr or not cd.isdigit():
        return {"error": f"unrecognized district {geoid}", "method": "geoid"}
    out = resolve_district(state_abbr, int(cd))
    out["method"] = "geoid"
    out["state_fips"] = state_fips
    out["geoid"] = geoid
    out["district_label"] = f"{state_abbr}-{'AL' if out['district'] == 0 else out['district']}"
    return out


def _geocode(url):
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            return json.load(r)
    except Exception:
        return None


def _districts_from(geographies):
    """Pull the CD (state FIPS, district number), state-leg codes, and
    county/place identity out of a Census `geographies` block. The geocoder
    call already returns county and incorporated-place layers alongside the
    CD layer; county/place ride along here for Foundry's local-government
    lookup (see FOUNDRY_JURISDICTIONS below) rather than needing a second
    API call."""
    cds = geographies.get(CD_LAYER) or []
    if not cds:
        return None
    cd = cds[0]
    state_fips, cd119 = cd.get("STATE"), cd.get("CD119")
    if state_fips is None or cd119 is None:
        return None
    state_leg = {}
    for key, label in (("Upper", "upper"), ("Lower", "lower")):
        for lname, block in geographies.items():
            if "State Legislative" in lname and key in lname and block:
                state_leg[label] = block[0].get("GEOID")
    county = (geographies.get("Counties") or [None])[0]
    place = (geographies.get("Incorporated Places") or [None])[0]
    return {
        "state_fips": state_fips, "cd119": cd119, "state_leg": state_leg,
        "county_fips": county.get("GEOID") if county else None,
        "county_name": county.get("NAME") if county else None,
        "place_geoid": place.get("GEOID") if place else None,
        "place_name": place.get("NAME") if place else None,
    }


# Foundry local-government coverage, keyed by the area each body actually
# governs. "place" bodies (city councils) govern only their incorporated
# place even though the nearest Census match is a county; "county" bodies
# (boards of supervisors) govern everyone in the county. GEOIDs verified
# directly against the Census Geocoder (onelineaddress, layers=all) against
# a real address in each jurisdiction — not guessed from source-id naming,
# which turned out to be unreliable (Chicago/Seattle/NYC's source ids all
# end in "-bos" despite being city councils, not counties).
FOUNDRY_JURISDICTIONS = [
    {"source_id": "pittsburgh-legistar", "label": "Pittsburgh City Council",
     "scope": "place", "county_fips": "42003", "place_geoid": "4261000"},
    {"source_id": "la-primegov", "label": "Los Angeles City Council",
     "scope": "place", "county_fips": "06037", "place_geoid": "0644000"},
    {"source_id": "chicago-bos", "label": "Chicago City Council",
     "scope": "place", "county_fips": "17031", "place_geoid": "1714000"},
    {"source_id": "seattle-bos", "label": "Seattle City Council",
     "scope": "place", "county_fips": "53033", "place_geoid": "5363000"},
    # NYC's single incorporated place spans all five boroughs/counties, so
    # county_fips is left unset — every address inside any of the five
    # counties that's part of the city resolves to the same place_geoid,
    # and there's no "in the county but outside city limits" case to catch.
    {"source_id": "newyork-bos", "label": "New York City Council",
     "scope": "place", "county_fips": None, "place_geoid": "3651000"},
    {"source_id": "fairfax-bos", "label": "Fairfax County Board of Supervisors",
     "scope": "county", "county_fips": "51059", "place_geoid": None},
    {"source_id": "loudoun-bos", "label": "Loudoun County Board of Supervisors",
     "scope": "county", "county_fips": "51107", "place_geoid": None},
    {"source_id": "princewilliam-bos", "label": "Prince William Board of County Supervisors",
     "scope": "county", "county_fips": "51153", "place_geoid": None},
    {"source_id": "stafford-bos", "label": "Stafford County Board of Supervisors",
     "scope": "county", "county_fips": "51179", "place_geoid": None},
]


def _foundry_jurisdiction_for(county_fips, place_geoid):
    """Match a resolved county/place to a Foundry-covered governing body.

    Three outcomes, not two: a county-wide body covers everyone in its
    county; a place body (city council) only covers its incorporated place,
    so someone elsewhere in the same county gets an honest "partial" gap
    naming the real jurisdiction they're missing, not a generic "not
    covered." See docs/spec_self_building_pipelines.md and foundry/README.md
    for what "covered" means upstream (certified vs. ingest-only)."""
    for j in FOUNDRY_JURISDICTIONS:
        if j["scope"] == "county" and j["county_fips"] == county_fips:
            return {"status": "covered", **j}
        if j["scope"] == "place" and j["place_geoid"] == place_geoid:
            return {"status": "covered", **j}
    for j in FOUNDRY_JURISDICTIONS:
        if j["scope"] == "place" and j["county_fips"] == county_fips:
            return {"status": "partial", **j}
    return {"status": "none"}


def _resolve(geo, coords, method):
    if not geo:
        return {"error": "no district found for that location", "method": method}
    state_abbr = FIPS_TO_USPS.get(geo["state_fips"])
    if not state_abbr:
        return {"error": f"unknown state FIPS {geo['state_fips']}", "method": method}
    out = resolve_district(state_abbr, int(geo["cd119"]))
    out["method"] = method
    out["district_label"] = f"{state_abbr}-{'AL' if out['district'] == 0 else out['district']}"
    # GEOID (state FIPS + 2-digit district) = the id the CD boundary file uses,
    # so the map can highlight the resolved district directly.
    out["state_fips"] = geo["state_fips"]
    out["geoid"] = f"{geo['state_fips']}{str(geo['cd119']).zfill(2)}"
    out["state_legislative"] = geo["state_leg"]
    out["foundry"] = _foundry_jurisdiction_for(geo.get("county_fips"), geo.get("place_geoid"))
    if coords:
        out["lat"], out["lon"] = coords.get("y"), coords.get("x")
    return out


def resolve_address(address):
    q = urllib.parse.urlencode({"address": address, "benchmark": BENCHMARK,
                                "vintage": VINTAGE, "layers": "all", "format": "json"})
    data = _geocode(GEOCODER + "onelineaddress?" + q)
    matches = (((data or {}).get("result") or {}).get("addressMatches")) or []
    if not matches:
        return {"error": "address not found — check it or try nearby", "method": "address"}
    m = matches[0]
    result = _resolve(_districts_from(m.get("geographies", {})), m.get("coordinates"), "address")
    result["matched_address"] = m.get("matchedAddress")
    return result


def resolve_point(lat, lon):
    q = urllib.parse.urlencode({"x": lon, "y": lat, "benchmark": BENCHMARK,
                                "vintage": VINTAGE, "layers": "all", "format": "json"})
    data = _geocode(GEOCODER + "coordinates?" + q)
    geographies = (((data or {}).get("result") or {}).get("geographies")) or None
    if not geographies:
        return {"error": "no district at that point (outside the US?)", "method": "point"}
    return _resolve(_districts_from(geographies), {"x": lon, "y": lat}, "point")


if __name__ == "__main__":
    for addr in ["4000 Legato Rd, Fairfax, VA 22033",
                 "233 S Wacker Dr, Chicago, IL 60606",
                 "1 Dr Carlton B Goodlett Pl, San Francisco, CA 94102"]:
        r = resolve_address(addr)
        rep = r.get("representative")
        print(f"{addr}\n   {r.get('district_label')}  ->  "
              f"{rep['name'] + ' (' + rep['party'] + ')' if rep else 'no rep'}"
              f"  | senators: {[s['name'] for s in r.get('senators', [])]}")
