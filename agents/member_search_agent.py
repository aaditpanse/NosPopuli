"""Federal members, from the unitedstates legislators files and the bill
graph (Phase 6, 2026-09-26); no call leaves the server."""
import datetime
import os
import re

NICKNAMES = {
    "ted": "edward",
    "bill": "william",
    "bob": "robert",
    "joe": "joseph",
    "jim": "james",
    "mike": "michael",
    "dick": "richard",
    "chuck": "charles",
    "bernie": "bernard",
    "liz": "elizabeth",
    "betty": "elizabeth",
    "jack": "john",
    "mitt": "willard",
    "al": "albert",
    "tom": "thomas",
    "dan": "daniel",
    "fred": "frederick",
    "ed": "edward",
    "pat": "patricia",
}

# Words a person types around a name that are not part of it.
_TITLES = {"sen", "senator", "rep", "representative", "congressman", "congresswoman", "congressperson",
           "mr", "mrs", "ms", "dr", "hon", "the", "jr", "sr", "ii", "iii"}
_PARTY_NAME = {"Democrat": "Democratic"}
_CHAMBER = {"sen": "Senate", "rep": "House of Representatives"}


def _words(text):
    import unicodedata
    folded = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.findall(r"[a-z]+", folded)


def match_members(name, legislators):
    """(member, candidates) for a typed name, from the legislators files.
    Every word of a member's last name must be typed; a typed first name
    must match the member's first, middle or nickname (Joe → Joseph).
    Among the best matches a sitting member wins alone; otherwise two or
    more are candidates and member is None, never a guess. Pure."""
    asked = [w for w in _words(name) if w not in _TITLES]
    if not asked:
        return None, []
    scored = []
    for leg in legislators:
        n = leg.get("name") or {}
        last = _words(n.get("last"))
        if not last or not all(w in asked for w in last):
            continue
        given = set(_words(n.get("first")) + _words(n.get("middle")) + _words(n.get("nickname")))
        rest = [w for w in asked if w not in last]
        hits = sum(1 for w in rest if w in given or NICKNAMES.get(w) in given)
        if rest and not hits:
            continue
        scored.append((hits, leg))
    if not scored:
        return None, []
    top = max(h for h, _ in scored)
    best = [leg for h, leg in scored if h == top]
    sitting = [leg for leg in best if leg.get("_source") == "legislators-current"]
    if len(sitting) == 1:
        return sitting[0], []
    if len(best) == 1:
        return best[0], []
    # Sitting members first, then the most recently serving.
    best.sort(key=lambda leg: (leg.get("_source") == "legislators-current",
                               (leg.get("terms") or [{}])[-1].get("end", "")), reverse=True)
    return None, best


def _summary(leg):
    """A legislator as search_member has always returned one."""
    import graph
    terms = leg.get("terms") or [{}]
    last = terms[-1]
    current = leg.get("_source") == "legislators-current"
    n = leg.get("name") or {}
    return {
        "bioguide_id": leg["id"].get("bioguide"),
        "name": n.get("official_full") or f"{n.get('first', '')} {n.get('last', '')}".strip(),
        "party": _PARTY_NAME.get(last.get("party"), last.get("party") or ""),
        "state": graph.DIVISION_NAMES.get((last.get("state") or "").lower(), last.get("state") or ""),
        "chamber": _CHAMBER.get(last.get("type"), ""),
        "start_year": int(terms[0]["start"][:4]) if terms[0].get("start") else None,
        "end_year": None if current else (int(last["end"][:4]) if last.get("end") else None),
        "current": current,
        "url": None,
    }


def search_member(name):
    """The member a typed name means, from the legislators files, or
    {"candidates": [...]} when the name fits several equally, or None."""
    import graph
    member, candidates = match_members(name, graph.legislators())
    if candidates:
        return {"candidates": [_summary(c) for c in candidates[:8]]}
    return _summary(member) if member else None


def _years(terms, today):
    days = 0
    for t in terms:
        if t.get("start") and t.get("end"):
            days += (min(datetime.date.fromisoformat(t["end"]), today) - datetime.date.fromisoformat(t["start"])).days
    return int(days / 365.25)


def fetch_member_profile(bioguide_id):
    """
    The member's profile from the legislators files: bio, terms, and stats.
    """
    import graph
    leg = next((x for x in graph.legislators() if x["id"].get("bioguide") == bioguide_id), None)
    if not leg:
        return None
    s = _summary(leg)
    terms = leg.get("terms") or []
    last = terms[-1] if terms else {}
    n = leg.get("name") or {}
    slug = "-".join(_words(f"{n.get('first', '')} {n.get('last', '')}"))
    profile = {
        **{k: s[k] for k in ("bioguide_id", "name", "party", "state", "current", "start_year", "end_year")},
        "birth_year": ((leg.get("bio") or {}).get("birthday") or "")[:4],
        "photo_url": "",
        "chambers": sorted({_CHAMBER[t["type"]] for t in terms if t.get("type") in _CHAMBER}),
        "terms": [{"chamber": _CHAMBER.get(t.get("type"), ""), "startYear": int(t["start"][:4]),
                   "endYear": int(t["end"][:4]), "stateCode": t.get("state"),
                   **({"district": t["district"]} if t.get("type") == "rep" and "district" in t else {}),
                   "partyName": _PARTY_NAME.get(t.get("party"), t.get("party") or "")}
                  for t in terms if t.get("start") and t.get("end")],
        "years_served": _years(terms, datetime.date.today()),
        "official_url": last.get("url", "") if s["current"] else "",
        "congress_url": f"https://www.congress.gov/member/{slug}/{bioguide_id}",
    }
    if last.get("type") == "rep" and last.get("district"):
        profile["district"] = last["district"]
    return profile


def sponsorship_summary(rows, limit):
    """Sponsored-edge rows → the member page's legislation block. rows:
    (congress, type, number, title, topic, introduced, role, withdrawn).
    A withdrawn cosponsorship is not counted. Pure."""
    mine = sorted((r for r in rows if r[6] == "sponsor"), key=lambda r: (r[5] or "", r[0]), reverse=True)
    areas = {}
    for r in mine:
        areas[r[4] or "Other"] = areas.get(r[4] or "Other", 0) + 1
    return {
        # A bill node is named "S. 5151: MRRRI Act"; the page prints the number itself.
        "sponsored": [{"congress": r[0], "type": r[1], "number": r[2], "title": _LABEL.sub("", r[3] or ""),
                       "latest_action": "", "date": r[5] or "", "policy_area": r[4] or "Other"}
                      for r in mine[:limit]],
        "sponsored_count": len(mine),
        "cosponsored_count": sum(1 for r in rows if r[6] != "sponsor" and not r[7]),
        "policy_areas": areas,
        "counted_since": FIRST_BILL_YEAR,
    }


_LABEL = re.compile(r"^[A-Z][A-Za-z. ]*\d+: ")
FIRST_BILL_YEAR = 2003          # the graph's bills begin with the 108th Congress


def fetch_member_legislation(bioguide_id, limit=20):
    """The bills a member sponsored and the count cosponsored, from the
    graph's sponsored edges: every bill since the 108th Congress (2003), so a
    longer career is counted from 2003 and the payload says so. Fail-open:
    without the database the block is empty and names why."""
    import graph
    if not os.getenv("SUPABASE_DB_URL"):
        return {"sponsored": [], "sponsored_count": None, "cosponsored_count": None, "policy_areas": {},
                "empty_reason": "the bill graph is not reachable from this server"}
    from correspondence.db import _get_pool
    try:
        with _get_pool().connection() as conn, conn.cursor() as cur:
            cur.execute("""
                SELECT (n.props->>'congress')::int, n.props->>'instrument_type', n.props->>'number',
                       n.name, n.props->>'topic', n.props->>'introduced', e.props->>'role', e.props->>'withdrawn'
                FROM graph_edge e JOIN graph_node n ON n.id = e.dst
                WHERE e.src = %s AND e.predicate = 'sponsored'""",
                        (graph.node_id("person", f"bioguide/{bioguide_id}"),))
            rows = cur.fetchall()
    except Exception as e:
        print(f"[MEMBER] legislation lookup error {bioguide_id}: {type(e).__name__}")
        return {"sponsored": [], "sponsored_count": None, "cosponsored_count": None, "policy_areas": {},
                "empty_reason": "the bill graph did not answer"}
    return sponsorship_summary(rows, limit)


if __name__ == "__main__":
    print("MEMBER SEARCH TEST")
    print("-" * 40)
    
    member = search_member("Ted Kennedy")
    if member:
        print(f"Found: {member['name']}")
        print(f"Bioguide: {member['bioguide_id']}")
        print(f"Party: {member['party']} · State: {member['state']}")
        print()
        
        profile = fetch_member_profile(member['bioguide_id'])
        if profile:
            print(f"Years served: {profile['years_served']}")
            print(f"Chambers: {profile['chambers']}")
            print(f"Photo URL: {profile['photo_url'][:70]}")
            print()
        
        legislation = fetch_member_legislation(member['bioguide_id'])
        print(f"Sponsored: {legislation['sponsored_count']}")
        print(f"Cosponsored: {legislation['cosponsored_count']}")
        print(f"Policy areas: {legislation['policy_areas']}")
        print()
        print("Recent bills:")
        for bill in legislation['sponsored'][:3]:
            print(f"  {bill['type'].upper()}{bill['number']} — {bill['title'][:60]}")
    else:
        print("Member not found")