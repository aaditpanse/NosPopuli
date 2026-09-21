"""Independent second-source assertion extractor for `fairfax-bos`.

Second source: the county-hosted Granicus video platform
(video.fairfaxcounty.gov). Each archived Board of Supervisors meeting has a
MinutesViewer.php document that independently records, per agenda item, the
motion outcome (carried / failed / vote tallies / named ayes-nays), keyed by
clip_id + doc_id discovered from ViewPublisher / MediaPlayer.

This module NEVER reads the primary CMS (www.fairfaxcounty.gov/
boardofsupervisors ...). It affirms meeting dates and item-level vote
outcomes from the Granicus minutes documents, independently of the Clerk's
PDF summaries used by the primary extractor. An item is asserted ONLY where
the second-source document records real outcome detail (a tally, named
positions, an explicit unanimity statement, or a "carried/failed" outcome).
"""

import re

EXTRACTOR_VERSION = "1"

BASE = "https://video.fairfaxcounty.gov/"
VP_URL = BASE + "ViewPublisher.php?view_id=7"

STORE_MEETINGS = [
    "2026-07-14", "2026-06-23", "2026-06-09", "2026-05-19", "2026-05-05",
    "2026-04-28", "2026-04-16", "2026-04-14", "2026-04-07", "2026-03-17",
    "2026-03-03", "2026-02-17", "2026-02-03", "2026-01-13", "2025-11-18",
    "2025-10-28",
]

MONTHS = {
    'jan': 1, 'feb': 2, 'mar': 3, 'apr': 4, 'may': 5, 'jun': 6, 'jul': 7,
    'aug': 8, 'sep': 9, 'oct': 10, 'nov': 11, 'dec': 12,
}

DATE_RE = re.compile(
    r'\b(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|'
    r'Jul(?:y)?|Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|'
    r'Dec(?:ember)?)\.?\s+(\d{1,2}),?\s+(\d{4})', re.I)

HEAD_RE = re.compile(r'(\d+(?:\.[0-9A-Za-z]+)*)[\.\)]\s+([^<\n]{3,400})')

# Signals that a document actually carries vote detail (used to pick the doc).
VOTE_SIGNAL_RE = re.compile(
    r'(carried by|the motion carried|motion carried|by a vote of|'
    r'failed to carry|failed to pass|unanimous vote|carried by unanimous|'
    r'\bAyes?\b\s*[:\-\u2013]|\bNays?\b\s*[:\-\u2013]|\bMOVER\b|\bSECONDER\b|'
    r'\bResult\b\s*[:\-])', re.I)

STANCE_LABELS = [
    (r'\bAyes?\b', 'aye'),
    (r'\bYeas?\b', 'aye'),
    (r'\bNays?\b', 'no'),
    (r'\bNoes\b', 'no'),
    (r'\bAbstain(?:ed|ing|s)?\b', 'abstain'),
    (r'\bAbstentions?\b', 'abstain'),
    (r'\bAbsent\b', 'absent'),
    (r'\bRecus(?:ed|al|es|ing)?\b', 'recused'),
]

_TITLE_WORDS = {'supervisor', 'chairman', 'chair', 'vice', 'mr', 'ms', 'mrs',
                'dr', 'district', 'the', 'and', 'by', 'of'}


def _iso(mon, day, year):
    key = mon.lower().rstrip('.')[:3]
    m = MONTHS.get(key)
    if not m:
        return None
    try:
        return "%04d-%02d-%02d" % (int(year), m, int(day))
    except (ValueError, TypeError):
        return None


def _parse_publisher(html):
    clips = [(m.start(), m.group(1))
             for m in re.finditer(r'clip_id=(\d+)', html)]
    dates = []
    for m in DATE_RE.finditer(html):
        iso = _iso(m.group(1), m.group(2), m.group(3))
        if iso:
            dates.append((m.start(), iso))
    result = {}
    for i, (pos, iso) in enumerate(dates):
        if iso in result:
            continue
        nextpos = dates[i + 1][0] if i + 1 < len(dates) else len(html) + 1
        cand = None
        for cpos, cid in clips:
            if pos < cpos < nextpos:
                cand = cid
                break
        if cand:
            result[iso] = cand
    return result


def _candidate_minutes_urls(clip, pub_html, mp_html):
    urls = []
    seen = set()

    def add(u):
        if u and u not in seen:
            seen.add(u)
            urls.append(u)

    for html in (mp_html, pub_html):
        if not html:
            continue
        for m in re.finditer(r'MinutesViewer\.php\?([^"\'<>\s\\]+)', html):
            q = m.group(1).replace('&amp;', '&')
            add(BASE + "MinutesViewer.php?" + q)

    if mp_html:
        docids = re.findall(
            r'doc_id["\'=:\s\\]{1,4}([0-9a-fA-F][0-9a-fA-F\-]{15,})', mp_html)
        clipids = re.findall(r'clip_id["\'=:\s\\]{1,4}(\d+)', mp_html)
        cids = []
        for c in [clip] + clipids:
            if c not in cids:
                cids.append(c)
        for did in dict.fromkeys(docids):
            for cid in cids[:4]:
                add(BASE + "MinutesViewer.php?view_id=7&clip_id=%s&doc_id=%s"
                    % (cid, did))
    return urls


def _load_document(rt, clip, pub_html):
    mp_url = BASE + "MediaPlayer.php?view_id=7&clip_id=" + clip
    try:
        mp_html = rt.fetch_text(mp_url)
    except Exception:
        mp_html = None

    candidates = _candidate_minutes_urls(clip, pub_html, mp_html)

    best_doc, best_url, best_score = None, None, 0
    for u in candidates[:6]:
        try:
            t = rt.fetch_text(u)
        except Exception:
            t = None
        if not t:
            continue
        score = len(VOTE_SIGNAL_RE.findall(t))
        if score > best_score:
            best_doc, best_url, best_score = t, u, score
        if score >= 8:
            return t, u

    if best_doc:
        return best_doc, best_url
    if mp_html:
        return mp_html, mp_url
    return None, None


def _headings(doc):
    return [(m.start(), m.group(1), m.group(2)) for m in HEAD_RE.finditer(doc)]


def _names(text):
    text = re.sub(r'\band\b', ',', text, flags=re.I)
    out = []
    for part in re.split(r'[;,/]', text):
        part = re.sub(r'^[\-\u2013\d\.\s]+', '', part.strip())
        words = re.findall(r"[A-Za-z][A-Za-z'\-]+", part)
        words = [w for w in words if w.lower() not in _TITLE_WORDS]
        if words:
            last = words[-1]
            if len(last) >= 2 and last[0].isupper():
                out.append(last)
    return out


def _parse_votes(block):
    counts = {}
    positions = {}
    for pat, stance in STANCE_LABELS:
        m = re.search(pat + r'\s*[:\-\u2013]\s*([^\n\r]{0,200})', block, re.I)
        if not m:
            continue
        val = m.group(1).strip()
        numm = re.match(r'(\d+)', val)
        names_part = val
        if numm:
            counts[stance] = int(numm.group(1))
            names_part = val[numm.end():]
        names = _names(names_part)
        if names:
            counts.setdefault(stance, len(names))
            for nm in names:
                positions.setdefault(nm, stance)
    return counts, positions


def _result_of(block, counts):
    if re.search(r'\b(failed to carry|failed to pass|did not carry|DENIED|'
                 r'denied|defeated|rejected|not adopted)\b', block, re.I):
        return 'fail'
    if re.search(r'\b(carried|APPROVED|approved|ADOPTED|adopted|passed|'
                 r'confirmed)\b', block, re.I):
        return 'pass'
    if counts:
        return 'pass' if counts.get('aye', 0) > counts.get('no', 0) else 'fail'
    return None


def _unanimity(block, counts):
    if re.search(r'unanimous', block, re.I):
        return True
    if re.search(r'\bnot unanimous\b', block, re.I):
        return False
    if counts:
        no = counts.get('no', 0) + counts.get('abstain', 0)
        if counts.get('aye', 0) > 0 and no == 0:
            return True
        if counts.get('no', 0) > 0:
            return False
    return None


def _quote(doc, start, block):
    anchors = [
        r'carried by[^\n]{0,90}',
        r'failed to carry[^\n]{0,90}',
        r'by a vote of[^\n]{0,90}',
        r'unanimous[^\n]{0,60}',
        r'Ayes?\s*[:\-\u2013][^\n]{0,150}',
        r'Nays?\s*[:\-\u2013][^\n]{0,150}',
        r'Result\s*[:\-][^\n]{0,90}',
        r'\b(?:carried|APPROVED|approved|ADOPTED|adopted|failed|denied|'
        r'defeated)\b[^\n]{0,70}',
    ]
    pos = None
    for pat in anchors:
        mm = re.search(pat, block, re.I)
        if mm:
            pos = mm.start()
            break
    if pos is None:
        pos = 0
    a = max(0, pos - 40)
    seg = block[a:a + 380]
    abs0 = start + a
    quote = doc[abs0:abs0 + len(seg)].strip()
    return quote[:400]


def _extract_attendance(doc):
    return {"present": [], "absent": []}


def _extract_items(doc, doc_url):
    items = {}
    heads = _headings(doc)
    if not heads:
        return items
    for i, (start, num, title) in enumerate(heads):
        end = heads[i + 1][0] if i + 1 < len(heads) else len(doc)
        block = doc[start:end]

        counts, positions = _parse_votes(block)
        unanimous = _unanimity(block, counts)
        has_narr = bool(re.search(
            r'\b(carried|failed to carry|failed to pass|by a vote of|'
            r'the motion)\b', block, re.I))
        has_detail = bool(counts) or bool(positions) or unanimous is not None \
            or has_narr
        if not has_detail:
            continue

        result = _result_of(block, counts)
        if result is None:
            continue

        key = num.strip().lower().rstrip(')').rstrip('.')
        clean_title = re.sub(r'\s+', ' ', title).strip()
        quote = _quote(doc, start, block)

        entry = {
            "title": clean_title,
            "result": result,
            "counts": counts or None,
            "positions": positions,
            "unanimous": unanimous,
            "evidence": {"quote": quote, "doc_url": doc_url},
        }
        items.setdefault(key, []).append(entry)
    return items


def extract(rt, args):
    max_meetings = int(args[0]) if args else 0

    assertions = {}
    without = set()

    try:
        pub = rt.fetch_text(VP_URL)
    except Exception:
        pub = None

    fetched_map = _parse_publisher(pub) if pub else {}
    completed = sorted(fetched_map.keys(), reverse=True)

    target_dates = set(completed[:max_meetings]) if max_meetings > 0 else set()
    for d in STORE_MEETINGS:
        if d in fetched_map:
            target_dates.add(d)

    entries_count = 0
    for d in sorted(target_dates, reverse=True):
        clip = fetched_map.get(d)
        if not clip:
            if d in STORE_MEETINGS:
                without.add(d)
            continue
        doc, doc_url = _load_document(rt, clip, pub)
        if not doc:
            if d in STORE_MEETINGS:
                without.add(d)
            continue
        attendance = _extract_attendance(doc)
        items = _extract_items(doc, doc_url)
        assertions[d] = {"attendance": attendance, "items": items}
        entries_count += sum(len(v) for v in items.values())

    for d in STORE_MEETINGS:
        if d not in fetched_map:
            without.add(d)

    run_meta = {
        "source_id": "fairfax-bos-oracle",
        "extractor_version": EXTRACTOR_VERSION,
        "row_counts": {
            "meetings": len(assertions),
            "entries": entries_count,
        },
        "meetings_without_document": sorted(without, reverse=True),
    }
    return assertions, run_meta
