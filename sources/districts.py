"""Congressional district shapes for every Congress since 1789.

Jeffrey B. Lewis et al., "United States Congressional District Shapefiles"
(UCLA; MIT licence), kept current on GitHub as one GeoJSON file per state
and span of Congresses (Virginia_118_to_119.geojson). They let a district
be a node per Congress with the land it covered, instead of one node for
VA-1 across two centuries of redistricting. Downloaded as published; the
graph step that reads them is Phase 4's.

Each file is fetched only when its git blob hash changes, so a second run
downloads nothing.

    python -m sources.districts
"""

import json
import pathlib
import sys
import time

_HERE = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_HERE))
import graph  # noqa: E402  — DATA_DIR

REPO = "JeffreyBLewis/congressional-district-boundaries"
TREE = f"https://api.github.com/repos/{REPO}/git/trees/master?recursive=1"
RAW = f"https://raw.githubusercontent.com/{REPO}/master/"
_UA = "NosPopuli bulk sync (nospopuli.org)"


def sync(pause=0.05):
    """Download every changed GeoJSON under GeoJson/. Fail-open per file:
    a failure is recorded and the old copy stays. Returns (fetched, errors)."""
    import requests
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    out = graph.DATA_DIR / "raw" / "districts"
    out.mkdir(parents=True, exist_ok=True)
    manifest_path = out / "manifest.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else {}
    r = s.get(TREE, timeout=60)
    r.raise_for_status()
    tree = r.json()
    if tree.get("truncated"):
        # A truncated listing would read as deleted files; stop instead.
        raise RuntimeError("GitHub returned a truncated tree; nothing changed")
    files = [t for t in tree["tree"] if t["type"] == "blob"
             and t["path"].startswith("GeoJson/") and t["path"].endswith(".geojson")]
    fetched, errors = [], []
    for t in files:
        name = t["path"].split("/", 1)[1]
        if manifest.get(name) == t["sha"] and (out / name).exists():
            continue
        try:
            g = s.get(RAW + t["path"], timeout=300)
            g.raise_for_status()
            json.loads(g.content)   # fail-closed: never keep a file that is not GeoJSON
            tmp = out / (name + ".part")
            tmp.write_bytes(g.content)
            tmp.replace(out / name)
        except Exception as e:
            errors.append(f"{RAW}{t['path']}: {type(e).__name__}")
            continue
        manifest[name] = t["sha"]
        manifest_path.write_text(json.dumps(manifest, indent=1, sort_keys=True))
        fetched.append(name)
        time.sleep(pause)
    return fetched, errors


if __name__ == "__main__":
    fetched, errors = sync()
    print(f"{len(fetched)} file(s) fetched, {len(errors)} error(s)")
    for e in errors[:10]:
        print("  -", e)
