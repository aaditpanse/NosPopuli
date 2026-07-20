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
    """Pull the CD (state FIPS, district number) and state-leg codes out of a
    Census `geographies` block."""
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
    return {"state_fips": state_fips, "cd119": cd119, "state_leg": state_leg}


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
