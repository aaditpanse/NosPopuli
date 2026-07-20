"""Deterministic extractor for source `newyork-bos`.

New York City — New York City Council (governing body), a Granicus Legistar
tenant.

Access reality (per verified source profile):
  * The Legistar Web API (webapi.legistar.com/v1/nyc) is token-gated (403);
    per-member roll-call votes there are unavailable without a token.
  * The Legistar HTML MeetingDetail pages are public but only show an
    *aggregate* Action/Result per item; per-item HistoryDetail popups 410
    standalone.
  * NYC Open Data (Socrata dataset m48u-yjt8, "Council Committees And
    Meetings") is public and returns structured JSON meeting metadata:
    event id, body name, meeting date, agenda/minutes status, a link to the
    human-viewable Legistar MeetingDetail page (EventInSiteURL) and, for
    finalized meetings, the minutes document URL (EventMinutesFile).

Strategy:
  1. Enumerate completed meetings via the Socrata SODA endpoint (no key),
     newest first, server-side filtered to the past where possible.
  2. For each meeting that has a finalized minutes document, fetch the
     minutes PDF as text (converted by the runtime) — this is the actions/
     minutes summary, NOT the full agenda packet.
  3. NYC Council minutes record roll-call votes as verbatim
     "Affirmative – ... – N.  Negative – ... – M.  Absent – ..." blocks.
     Parse those blocks into per-member positions (capturing dissent and
     absences), derive meeting attendance from the same rosters, and attach
     the verbatim passage as `evidence`.

Only the injected runtime `rt` performs I/O. Deterministic, stdlib only.
"""

import re
import datetime

EXTRACTOR_VERSION = "1"

SOURCE_ID = "newyork-bos"
RUN_ID = SOURCE_ID + "-" + EXTRACTOR_VERSION

# --- tenant/deployment constants (re-point here for another Legistar tenant)
SOCRATA_URL = "https://data.cityofnewyork.us/resource/m48u-yjt8.json"
LEGISTAR_HOST = "https://legistar.council.nyc.gov"
CALENDAR_URL = LEGISTAR_HOST + "/Calendar.aspx"
DEFAULT_BODY = "New York City Council"

_DEFAULT_MAX = 10
_MAX_CANDIDATES = 60      # meetings to examine at most
_FETCH_BUDGET = 120       # hard cap on rt fetches

_DASH = "[\u2013\u2014\\-]"

# NYC minutes vote-list label -> schema position vocabulary.
LABEL_POS = {
    "affirmative": "aye",
    "negative": "no",
    "abstention": "abstain", "abstentions": "abstain",
    "abstained": "abstain", "abstain": "abstain",
    "absent": "absent", "excused": "absent",
    "recused": "recused",
    "present": "present",
}
_NEGLIKE = {"negative", "abstention", "abstentions", "abstained",
            "abstain", "absent", "excused", "recused"}

_SEG_RE = re.compile(
    r'(Affirmative|Negative|Abstentions?|Abstained|Abstain|Absent|Excused|'
    r'Recused|Present)\b\s*' + _DASH +
    r'\s*(?:None\s*\.|(.{1,1500}?)\s*'
    r'(?:' + _DASH + r'\s*(\d+)|\(\s*(\d+)\s*\))\s*\.)',
    re.S,
)

_TITLE_PATS = [
    re.compile(r'Report of the Committee on [^\.\n;:]{3,90}'),
    re.compile(
        r'(?:Preconsidered\s+)?'
        r'(?:Int|Res|Introduction|Resolution|L\.?\s?U\.?|M|T)\.?\s*'
        r'(?:No\.?\s*)?\d[\w\-/]*'),
]

_TITLES = sorted(
    ["council members", "council member", "vice chair", "chairperson",
     "chairman", "chairwoman", "chair", "supervisor", "mr.", "ms.", "mrs.",
     "dr.", "mayor", "hon.", "the honorable", "the speaker",
     "the majority leader", "the minority leader", "the public advocate"],
    key=len, reverse=True,
)

_STOP = {"none", "the", "and", "council", "members", "member", "majority",
         "minority", "leader", "whip", "speaker", "public", "advocate",
         "president", "pro", "tempore", "acting", "of", "by", "none."}


def _coerce_max(args):
    val = args
    if isinstance(val, (list, tuple)):
        val = val[0] if val else None
    if isinstance(val, bool):
        return _DEFAULT_MAX
    if isinstance(val, int):
        return val if val > 0 else _DEFAULT_MAX
    if isinstance(val, float):
        return int(val) if val > 0 else _DEFAULT_MAX
    if isinstance(val, str):
        m = re.search(r"\d+", val)
        if m:
            n = int(m.group(0))
            return n if n > 0 else _DEFAULT_MAX
    return _DEFAULT_MAX


def _cikey(d, *keys):
    if not isinstance(d, dict):
        return None
    lowered = {k.lower(): k for k in d.keys()}
    for key in keys:
        real = lowered.get(key.lower())
        if real is not None:
            return d[real]
    return None


def _clean_name(name):
    s = str(name or "").strip()
    changed = True
    while changed:
        changed = False
        low = s.lower()
        for t in _TITLES:
            if low.startswith(t + " "):
                s = s[len(t):].strip()
                changed = True
                break
    return s.strip(" .,")


def _parse_date(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return "%s-%s-%s" % (m.group(1), m.group(2), m.group(3))
    m = re.match(r"(\d{1,2})/(\d{1,2})/(\d{4})", s)
    if m:
        mo, da, yr = int(m.group(1)), int(m.group(2)), int(m.group(3))
        if 1 <= mo <= 12 and 1 <= da <= 31:
            return "%04d-%02d-%02d" % (yr, mo, da)
    return None


def _slug(s):
    return re.sub(r"[^0-9A-Za-z]+", "-", str(s)).strip("-") or "x"


def _abs_url(u):
    u = str(u or "").strip().replace("&amp;", "&")
    if not u:
        return None
    if u.lower().startswith("http"):
        return u
    if u.startswith("/"):
        return LEGISTAR_HOST + u
    return LEGISTAR_HOST + "/" + u


def _valid_name(n):
    if not n:
        return False
    if re.search(r"\d", n):
        return False
    if not re.search(r"[A-Za-z\u00C0-\u017F]", n):
        return False
    if len(n) > 45:
        return False
    words = [w for w in re.split(r"\s+", n.lower()) if w]
    if not words:
        return False
    if all(w.strip(".") in _STOP for w in words):
        return False
    return True


def _member_from_token(tok):
    tok = str(tok or "").strip().strip(".,").strip()
    if not tok:
        return None
    m = re.search(r"\(([^)]*)\)", tok)
    if m:
        inner = m.group(1)
        inner = re.sub(r"(?i)\bcouncil members?\b", "", inner)
        inner = re.sub(r"(?i)\b(mr|ms|mrs|dr|hon)\.?\b", "", inner)
        inner = inner.strip(" .,")
        return _clean_name(inner) if inner else None
    return _clean_name(tok)


def _names_from_content(content):
    if content is None:
        return []
    c = str(content).strip()
    if c.lower() in ("none", "none."):
        return []
    c = re.sub(r"(?i)^\s*(the\s+)?council members?\b", "", c).strip()
    names = []
    for part in re.split(r",|\band\b", c):
        nm = _member_from_token(part)
        if nm and _valid_name(nm):
            names.append(nm)
    return names


def _fetch_socrata(rt, today_iso):
    variants = [
        {"$order": "event_date DESC",
         "$where": "event_date < '%sT00:00:00'" % today_iso,
         "$limit": "500"},
        {"$order": "event_date DESC", "$limit": "500"},
        {"$limit": "500"},
        None,
    ]
    for params in variants:
        try:
            data = rt.fetch_json(SOCRATA_URL, params=params)
        except Exception:
            continue
        if isinstance(data, list) and data:
            return data
        if isinstance(data, dict):
            for v in data.values():
                if isinstance(v, list) and v:
                    return v
    return []


def _minutes_url(row):
    for k in ("event_minutes_file", "minutes_file", "event_minutes",
              "minutes", "minuteslink", "minutes_url"):
        v = _cikey(row, k)
        if isinstance(v, dict):
            v = v.get("url") or v.get("URL")
        if isinstance(v, str) and v.strip():
            s = v.strip()
            low = s.lower()
            if ("view.ashx" in low or low.endswith(".pdf")
                    or "legistar" in low):
                return _abs_url(s)
    return None


def _insite_url(row):
    for k in ("event_insite_url", "event_in_site_url", "insite_url",
              "url", "event_url", "meeting_detail_url"):
        v = _cikey(row, k)
        if isinstance(v, dict):
            v = v.get("url") or v.get("URL")
        if isinstance(v, str) and v.strip().lower().startswith("http"):
            s = v.strip()
            if ".ashx" not in s.lower() and ".asmx" not in s.lower():
                return s
    return None


def _minutes_from_html(rt, insite):
    if not insite:
        return None
    try:
        html = rt.fetch_text(insite)
    except Exception:
        return None
    if not html:
        return None
    m = re.search(
        r'href="([^"]*View\.ashx[^"]*)"[^>]*>(?:\s|<[^>]+>)*'
        r'(?:Final\s*)?Minutes', html, re.I)
    if not m:
        m = re.search(r'href="([^"]*View\.ashx[^"]*M=M[^"]*)"', html, re.I)
    if m:
        return _abs_url(m.group(1))
    return None


def _extract_title(preceding):
    cands = []
    for pat in _TITLE_PATS:
        for mm in pat.finditer(preceding):
            cands.append((mm.start(), mm.group(0)))
    if not cands:
        return "General Order Calendar"
    cands.sort()
    title = re.sub(r"\s+", " ", cands[-1][1]).strip(" .,;:")
    return title[:120] if title else "General Order Calendar"


def _provenance(source_url):
    return {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "run_id": RUN_ID,
        "source_url": source_url,
        "certification": {
            "certified": True,
            "method": "legistar-socrata+minutes-pdf",
        },
    }


def _parse_minutes(text):
    """Return (attendance dict, list of vote-block dicts)."""
    present = set()
    absent = set()
    matches = list(_SEG_RE.finditer(text))

    # attendance across ALL segments
    for m in matches:
        label = m.group(1).lower()
        pos = LABEL_POS.get(label)
        if not pos:
            continue
        names = _names_from_content(m.group(2))
        for nm in names:
            if pos == "absent":
                absent.add(nm)
            else:
                present.add(nm)

    attendance = {}
    for nm in present:
        attendance[nm] = "present"
    for nm in absent:
        if nm not in attendance:
            attendance[nm] = "absent"

    # group into vote blocks (Affirmative-led)
    blocks = []
    cur = None
    for m in matches:
        label = m.group(1).lower()
        if label == "affirmative":
            if cur:
                blocks.append(cur)
            cur = {"segs": [m], "start": m.start(), "end": m.end()}
        elif label in _NEGLIKE:
            if cur is not None:
                cur["segs"].append(m)
                cur["end"] = m.end()
        else:  # "present" acts as a separator (roll call only)
            if cur:
                blocks.append(cur)
            cur = None
    if cur:
        blocks.append(cur)

    return attendance, blocks[:200]


def extract(rt, args):
    want = _coerce_max(args)
    today_iso = datetime.date.today().isoformat()

    rows = _fetch_socrata(rt, today_iso)

    # parse dates, keep strictly-past, dedupe, newest first
    dated = []
    seen_ev = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        d = _parse_date(_cikey(row, "event_date", "meeting_date", "date"))
        if not d or d >= today_iso:
            continue
        ev = _cikey(row, "event_id", "eventid", "id")
        key = str(ev) if ev is not None else "d:%s:%s" % (
            d, _cikey(row, "event_body_name", "name") or "")
        if key in seen_ev:
            continue
        seen_ev.add(key)
        dated.append((d, row))
    dated.sort(key=lambda t: t[0], reverse=True)

    meetings_rec = []
    items_rec = []
    votes_rec = []
    members = {}

    used_ids = set()
    emitted = 0
    examined = 0
    fetches = 0

    for date, row in dated:
        if emitted >= want or examined >= _MAX_CANDIDATES:
            break
        if fetches >= _FETCH_BUDGET:
            break
        examined += 1
        try:
            insite = _insite_url(row)
            murl = _minutes_url(row)
            if not murl and insite and fetches < _FETCH_BUDGET:
                fetches += 1
                murl = _minutes_from_html(rt, insite)
            if not murl:
                continue

            if fetches >= _FETCH_BUDGET:
                break
            fetches += 1
            try:
                text = rt.fetch_text(murl)
            except Exception:
                continue
            if not text or len(text) < 200:
                continue

            attendance, blocks = _parse_minutes(text)
            if not attendance:
                continue

            body = (_cikey(row, "event_body_name", "name", "committee",
                           "body") or DEFAULT_BODY)
            if not isinstance(body, str) or not body.strip():
                body = DEFAULT_BODY

            source_url = insite or CALENDAR_URL
            prov = _provenance(source_url)

            meeting_id = "%s-%s" % (SOURCE_ID, date)
            if meeting_id in used_ids:
                ev = _cikey(row, "event_id", "eventid", "id")
                meeting_id = "%s-%s" % (meeting_id,
                                        _slug(ev if ev is not None else body))
            used_ids.add(meeting_id)

            meetings_rec.append({
                "meeting_id": meeting_id,
                "body": body.strip(),
                "date": date,
                "attendance": dict(attendance),
                "source_url": source_url,
                "data_source_url": murl,
                "file_number": None,
                "provenance": prov,
            })
            for nm in attendance:
                if nm not in members:
                    members[nm] = {"name": nm, "provenance": prov}

            vi = 0
            for blk in blocks:
                positions = []
                counts = {}
                seen = set()
                for m in blk["segs"]:
                    label = m.group(1).lower()
                    pos = LABEL_POS.get(label)
                    if not pos:
                        continue
                    for nm in _names_from_content(m.group(2)):
                        if nm in seen:
                            continue
                        seen.add(nm)
                        positions.append({"member": nm, "position": pos})
                        counts[pos] = counts.get(pos, 0) + 1
                        if nm not in members:
                            members[nm] = {"name": nm, "provenance": prov}

                aye = counts.get("aye", 0)
                no = counts.get("no", 0)
                if aye == 0 and no == 0:
                    continue
                if not positions:
                    continue
                result = "pass" if aye > no else "fail"

                bstart, bend = blk["start"], blk["end"]
                quote = text[bstart:bend]
                if len(quote) > 400:
                    quote = quote[:400]
                quote = quote.strip()
                if not quote:
                    continue

                preceding = text[max(0, bstart - 900):bstart]
                title = _extract_title(preceding)
                action = "Adopted" if result == "pass" else "Rejected"

                vi += 1
                item_id = "%s-item-%d" % (meeting_id, vi)
                vote_id = "%s-vote-%d" % (meeting_id, vi)

                items_rec.append({
                    "item_id": item_id,
                    "meeting_id": meeting_id,
                    "title": title,
                    "action": action,
                    "result": result,
                    "file_number": None,
                    "provenance": prov,
                })
                votes_rec.append({
                    "vote_id": vote_id,
                    "meeting_id": meeting_id,
                    "item_id": item_id,
                    "positions": positions,
                    "counts": counts,
                    "result": result,
                    "file_number": None,
                    "evidence": {"quote": quote, "doc_url": murl},
                    "provenance": prov,
                })

            emitted += 1
        except Exception:
            continue

    members_rec = [members[k] for k in sorted(members.keys())]

    records = {
        "meetings": meetings_rec,
        "agenda_items": items_rec,
        "vote_events": votes_rec,
        "members": members_rec,
    }
    run_meta = {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": "1.3",
        "row_counts": {
            "meetings": len(meetings_rec),
            "agenda_items": len(items_rec),
            "vote_events": len(votes_rec),
            "members": len(members_rec),
        },
    }
    return records, run_meta
