import json

# ── Local roll calls ──
#
# Every roll call since 1789 is on disk: the clerks' files from the 118th
# Congress on, Voteview's before (derived/votes/congress-votes-<c>-<n>.json).
# They name members by id; the legislators files give the name, party and
# state the page shows.

_LOCAL_POSITION = {"aye": "Yea", "no": "Nay", "present": "Present", "absent": "Not Voting"}
_PARTY = {"Democrat": "D", "Republican": "R", "Independent": "I"}
_people = {"sig": None}


def _legislators():
    """{("bioguide"|"lis", id): legislator} from both legislators files,
    reread only when a file changes."""
    import graph
    paths = [graph.data_path("public", name=n) for n in (graph.LEGISLATORS_SOURCE, graph.HISTORICAL_SOURCE)]
    sig = tuple(p.stat().st_mtime_ns if p.exists() else 0 for p in paths)
    if _people["sig"] != sig:
        index = {}
        for p in reversed(paths):          # the current file wins a collision
            for leg in (json.loads(p.read_text()) if p.exists() else []):
                for kind in ("bioguide", "lis"):
                    if leg["id"].get(kind):
                        index[(kind, leg["id"][kind])] = leg
        _people.update(sig=sig, index=index)
    return _people["index"]


def member_votes(vote, people):
    """One roll call's positions → [{name, party, state, vote}] as the seat
    map reads them. Party and state are the member's term on the day of the
    vote; an id the legislators files do not know keeps its id as its name
    and no party, rather than being dropped from the count. Pure."""
    kind = vote.get("id_kind") or "bioguide"
    term_type = "rep" if vote["chamber"] == "house" else "sen"
    day = vote.get("date") or ""
    out = []
    for pos, ids in (vote.get("positions") or {}).items():
        for mid in ids:
            leg = people.get((kind, mid))
            if not leg:
                out.append({"name": mid, "party": "", "state": "", "vote": _LOCAL_POSITION.get(pos, "Not Voting")})
                continue
            terms = [t for t in leg.get("terms", []) if t.get("type") == term_type] or leg.get("terms", [])
            term = next((t for t in terms if t.get("start", "") <= day <= t.get("end", "")), terms[-1] if terms else {})
            name = leg.get("name", {})
            out.append({"name": f"{name.get('first', '')} {name.get('last', '')}".strip(),
                        "party": _PARTY.get(term.get("party"), (term.get("party") or "")[:1]),
                        "state": term.get("state", ""), "vote": _LOCAL_POSITION.get(pos, "Not Voting")})
    return out


def find_roll_call(votes, chamber, roll):
    """The roll call a bill's action cites, by the clerk's number. A
    Voteview file carries the clerk's number beside its own; one written
    before it did has no match rather than a wrong one. Pure."""
    for v in votes:
        if v.get("chamber") != chamber:
            continue
        clerk = v.get("clerk_roll") if v.get("source_id") == "voteview" else v.get("roll")
        if clerk == roll:
            return v
    return None


def _local_votes(vote_ref, chamber):
    import graph
    path = graph.data_path("votes", congress=vote_ref["congress"], session=vote_ref["session"])
    if not path.exists():
        return None
    vote = find_roll_call(json.loads(path.read_text())["votes"], chamber, int(vote_ref["roll"]))
    return member_votes(vote, _legislators()) or None if vote else None


# ── House fetcher ──

def fetch_house_votes(vote_ref):
    """
    Fetches individual member votes for a House roll call, from the roll-call
    files on disk. There is no live fallback: a roll call newer than the last
    sync shows no seat map until the next one.
    """
    if not vote_ref:
        return None
    return _local_votes(vote_ref, "house")


# ── Senate fetcher ──

def fetch_senate_votes(vote_ref):
    """
    Fetches individual member votes for a Senate roll call, from the roll-call
    files on disk; no live fallback, as for the House.
    """
    if not vote_ref:
        return None
    return _local_votes(vote_ref, "senate")


if __name__ == "__main__":
    from agents.historian_agent import fetch_bill_actions
    from agents.vote_parser_agent import parse_vote_references

    print("VOTE FETCHER TEST")
    print("-" * 40)

    # ACA
    actions = fetch_bill_actions(111, "hr", 3590)
    refs = parse_vote_references(actions)

    print("ACA House votes:")
    house = fetch_house_votes(refs["house"])
    if house:
        yeas = sum(1 for m in house if m["vote"] == "Yea")
        nays = sum(1 for m in house if m["vote"] == "Nay")
        print(f"  {len(house)} members · Yea: {yeas} · Nay: {nays}")
        print(f"  Sample: {house[0]}")
        print(f"  Sample: {house[1]}")
    else:
        print("  No data found")

    print()
    print("ACA Senate votes:")
    senate = fetch_senate_votes(refs["senate"])
    if senate:
        yeas = sum(1 for m in senate if m["vote"] == "Yea")
        nays = sum(1 for m in senate if m["vote"] == "Nay")
        print(f"  {len(senate)} members · Yea: {yeas} · Nay: {nays}")
        print(f"  Sample: {senate[0]}")
        print(f"  Sample: {senate[1]}")
    else:
        print("  No data found")