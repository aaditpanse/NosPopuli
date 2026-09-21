import math
from agents.documentor_agent import log_action

# ── Seat layout constants ──

# House: 435 seats across 8 arced rows
# Distributed to approximate the real chamber layout
HOUSE_ROWS = [32, 48, 58, 66, 72, 76, 48, 35]  # = 435

# Senate: 100 seats across 4 arced rows
SENATE_ROWS = [18, 24, 30, 28]  # = 100

VOTE_COLORS = {
    "Yea":        "#2a6e2a",   # Dark green
    "Nay":        "#8b1a1a",   # Accent red (matches design system)
    "Present":    "#8b7a1a",   # Muted gold
    "Not Voting": "#c8bfaa",   # Rule color (muted)
}

def _semicircle_positions(row_counts, cx, cy, r_start, r_step, angle_padding=0.08):
    """
    Generate (x, y) positions for seats arranged in a semicircle.
    Seats go left to right across each arc.
    angle_padding keeps seats from touching the edges.
    """
    positions = []
    for row_idx, count in enumerate(row_counts):
        r = r_start + row_idx * r_step
        for i in range(count):
            if count == 1:
                t = 0.5
            else:
                t = i / (count - 1)
            # angle goes from π (left) to 0 (right)
            angle = math.pi * (1 - angle_padding) - t * math.pi * (1 - 2 * angle_padding)
            x = cx + r * math.cos(angle)
            y = cy - r * math.sin(angle)
            positions.append((round(x, 2), round(y, 2)))
    return positions

def _sort_members_by_party(members):
    """
    Sort members so Democrats sit left, Republicans right,
    Independents in between — matching real chamber seating.
    """
    order = {"D": 0, "I": 1, "ID": 1, "L": 1, "R": 2}
    return sorted(members, key=lambda m: (order.get(m["party"], 1), m["state"]))

def map_house_votes(members):
    """
    Maps 435 House member votes to semicircle seat positions.
    Returns list of seat dicts with coordinates and vote data.
    """
    if not members:
        return None

    sorted_members = _sort_members_by_party(members)

    # SVG canvas: 500 wide, 260 tall
    # Center bottom of semicircle
    cx, cy = 250, 250
    r_start, r_step = 60, 22

    positions = _semicircle_positions(HOUSE_ROWS, cx, cy, r_start, r_step)

    seats = []
    for i, (x, y) in enumerate(positions):
        if i >= len(sorted_members):
            break
        m = sorted_members[i]
        seats.append({
            "x": x,
            "y": y,
            "name": m["name"],
            "party": m["party"],
            "state": m["state"],
            "vote": m["vote"],
            "color": VOTE_COLORS.get(m["vote"], VOTE_COLORS["Not Voting"])
        })

    # Summary counts
    summary = {
        "yea": sum(1 for m in members if m["vote"] == "Yea"),
        "nay": sum(1 for m in members if m["vote"] == "Nay"),
        "present": sum(1 for m in members if m["vote"] == "Present"),
        "not_voting": sum(1 for m in members if m["vote"] == "Not Voting"),
    }

    log_action(
        agent_name="vote_mapper",
        action="map_house_votes",
        input_data={"member_count": len(members)},
        output_data=summary
    )

    return {"seats": seats, "summary": summary}

def map_senate_votes(members):
    """
    Maps 100 Senate member votes to semicircle seat positions.
    """
    if not members:
        return None

    sorted_members = _sort_members_by_party(members)

    # SVG canvas: 300 wide, 200 tall
    cx, cy = 150, 190
    r_start, r_step = 50, 32

    positions = _semicircle_positions(SENATE_ROWS, cx, cy, r_start, r_step)

    seats = []
    for i, (x, y) in enumerate(positions):
        if i >= len(sorted_members):
            break
        m = sorted_members[i]
        seats.append({
            "x": x,
            "y": y,
            "name": m["name"],
            "party": m["party"],
            "state": m["state"],
            "vote": m["vote"],
            "color": VOTE_COLORS.get(m["vote"], VOTE_COLORS["Not Voting"])
        })

    summary = {
        "yea": sum(1 for m in members if m["vote"] == "Yea"),
        "nay": sum(1 for m in members if m["vote"] == "Nay"),
        "present": sum(1 for m in members if m["vote"] == "Present"),
        "not_voting": sum(1 for m in members if m["vote"] == "Not Voting"),
    }

    log_action(
        agent_name="vote_mapper",
        action="map_senate_votes",
        input_data={"member_count": len(members)},
        output_data=summary
    )

    return {"seats": seats, "summary": summary}

if __name__ == "__main__":
    from agents.historian_agent import fetch_bill_actions
    from agents.vote_parser_agent import parse_vote_references
    from agents.vote_fetcher_agent import fetch_house_votes, fetch_senate_votes

    print("VOTE MAPPER TEST")
    print("-" * 40)

    actions = fetch_bill_actions(111, "hr", 3590)
    refs = parse_vote_references(actions)

    house_raw = fetch_house_votes(refs["house"])
    senate_raw = fetch_senate_votes(refs["senate"])

    house_mapped = map_house_votes(house_raw)
    senate_mapped = map_senate_votes(senate_raw)

    if house_mapped:
        print(f"House seats mapped: {len(house_mapped['seats'])}")
        print(f"Summary: {house_mapped['summary']}")
        print(f"First seat: {house_mapped['seats'][0]}")
        print(f"Last seat:  {house_mapped['seats'][-1]}")

    print()

    if senate_mapped:
        print(f"Senate seats mapped: {len(senate_mapped['seats'])}")
        print(f"Summary: {senate_mapped['summary']}")
        print(f"First seat: {senate_mapped['seats'][0]}")
        print(f"Last seat:  {senate_mapped['seats'][-1]}")