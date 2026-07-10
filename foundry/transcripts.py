"""Video-transcript layer: Granicus closed captions, segmented by agenda item.

    python transcripts.py <clip_id> [--view 92] [--tenant loudoun]

Granicus exposes per-clip JSON at /JSON.php?clip_id=N: thousands of
timestamped caption lines with agenda-index entries (type "index" carries
`title`) interleaved. That means "what was said during item X" costs one
GET — no ASR. Live-caption quality is rough (misheard names, dropped
words), so this layer is CONTEXT: searchable, linkable to vote records by
meeting/time, and never certified. It can, however, serve as a secondary
affirmation signal (the chair announcing a tally on the record).
"""

import argparse
import html
import json
import pathlib
import re
import sys
import urllib.request

FOUNDRY = pathlib.Path(__file__).parent
STORE = FOUNDRY / "data" / "store"


def fetch_clip(tenant, clip_id):
    url = f"https://{tenant}.granicus.com/JSON.php?clip_id={clip_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "nospopuli-foundry-lab"})
    return json.loads(urllib.request.urlopen(req, timeout=40).read())[0]


def player_title(tenant, view_id, clip_id):
    url = f"https://{tenant}.granicus.com/player/clip/{clip_id}?view_id={view_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "nospopuli-foundry-lab"})
    page = urllib.request.urlopen(req, timeout=30).read().decode("utf-8", "replace")
    m = re.search(r"<title>([^<]+)</title>", page)
    return html.unescape(m.group(1)).strip() if m else f"clip {clip_id}"


def segment(entries):
    """Group caption lines under the agenda-index titles they follow."""
    segments = []
    current = {"title": "(pre-meeting)", "start": 0, "lines": []}
    for e in entries:
        if e.get("title"):
            if current["lines"]:
                segments.append(current)
            current = {"title": html.unescape(e["title"]),
                       "start": e.get("time", 0), "lines": []}
        elif e.get("text"):
            current["lines"].append(e["text"].strip())
    if current["lines"]:
        segments.append(current)
    for s in segments:
        s["text"] = re.sub(r"\s+", " ", " ".join(s.pop("lines")))
    return segments


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("clip_id", type=int)
    parser.add_argument("--view", type=int, default=92)
    parser.add_argument("--tenant", default="loudoun")
    args = parser.parse_args()

    entries = fetch_clip(args.tenant, args.clip_id)
    title = player_title(args.tenant, args.view, args.clip_id)
    segments = segment(entries)
    out_dir = STORE / f"{args.tenant}-transcripts"
    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"clip_{args.clip_id}.json"
    out.write_text(json.dumps({
        "clip_id": args.clip_id, "meeting": title,
        "source": f"https://{args.tenant}.granicus.com/player/clip/{args.clip_id}?view_id={args.view}",
        "caption_lines": sum(1 for e in entries if e.get("text")),
        "trust": "live captions — context layer only, never certified",
        "segments": segments}, indent=1))

    words = sum(len(s["text"].split()) for s in segments)
    print(f"{title}\n  {len(segments)} agenda segments, "
          f"{words:,} words of captions -> {out.relative_to(FOUNDRY)}")
    for s in segments[:8]:
        mins = int(float(s["start"] or 0) // 60)
        print(f"  [{mins:3}m] {s['title'][:64]} ({len(s['text'].split())} words)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
