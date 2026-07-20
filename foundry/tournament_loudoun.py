"""Offline Loudoun oracle tournament: score every cached assertion set under
the compatibility join, no API spend. Run from foundry/:

    python tournament_loudoun.py           # score candidates
    python tournament_loudoun.py --pick X  # certify from candidate X
"""

import argparse
import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import run_oracle

F = pathlib.Path(__file__).parent
STORE_PATH = F / "data" / "store" / "loudoun-bos.json"


def candidates():
    out = {}
    for p in sorted(F.glob("data/oracle/_cand_v1_attempt*.json")):
        out[p.stem.replace("_cand_", "")] = json.loads(p.read_text())
    earlier = F / "data" / "oracle" / "loudoun_assertions.json"
    if earlier.exists():
        out["earlier-4-meeting-run"] = json.loads(earlier.read_text())
    return out


def score(store, name, assertions, verbose=False):
    dates = {m["meeting_id"]: m["date"] for m in store["meetings"].values()}
    findings, affirmed = run_oracle.reconcile(store, assertions, "loudoun-bos")
    disputed = {f["ref"] for f in findings}
    covered = [ve for ve in store["vote_events"].values()
               if dates.get(ve["meeting_id"]) in assertions]
    clean = sum(1 for ve in covered if ve["vote_id"] in affirmed
                and affirmed[ve["vote_id"]][0] not in disputed
                and ve["meeting_id"] not in disputed)
    mism = [f for f in findings if f["check"] == "vote_mismatch"]
    print(f"{name:26} agreement {clean}/{len(covered)} "
          f"({len(mism)} mismatches, {len(findings)} findings, "
          f"{len(set(assertions) & set(dates.values()))} mtgs covered)")
    if verbose:
        for f in findings[:14]:
            print(f"   [{f['check']}] {f['ref']}: {f['msg'][:110]}")
    return clean, len(covered), findings, affirmed


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--pick", help="certify from this candidate")
    parser.add_argument("-v", action="store_true")
    args = parser.parse_args()
    store = json.loads(STORE_PATH.read_text())
    cands = candidates()
    if not args.pick:
        for name, a in cands.items():
            score(store, name, a, verbose=args.v)
        return 0

    assertions = cands[args.pick]
    clean, total, findings, affirmed = score(store, args.pick, assertions, verbose=True)
    if total and clean / total < 0.6:
        print(f"refusing to certify below the 60% agreement gate "
              f"({clean}/{total})")
        return 1
    method = ("cross-source: primary extractor × independent second-source "
              "document (per-meeting Board minutes, Laserfiche)")
    counts = run_oracle.certify(store, assertions, findings, affirmed, method)
    meta = store.get("meta") or {}
    meta["sub"] = (meta.get("sub", "") + " · cross-source certified (minutes)") \
        if "cross-source" not in meta.get("sub", "") else meta["sub"]
    if args.pick.startswith("v1_attempt"):
        meta["oracle_artifact"] = f"extractors/loudoun-bos-oracle/{args.pick}.py"
    store["meta"] = meta
    STORE_PATH.write_text(json.dumps(store, indent=1))
    pct = counts["certified"] / counts["total"] if counts["total"] else 0
    print(f"certified: {counts['certified']}/{counts['total']} ({pct:.0%}), "
          f"{counts['disputes']} disputed refs -> {STORE_PATH.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
