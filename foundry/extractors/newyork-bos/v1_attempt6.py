"""Deterministic extractor for source `newyork-bos`.

New York City — New York City Council (Granicus Legistar tenant).

Enumeration comes from the live public Legistar calendar
(https://legistar.council.nyc.gov/Calendar.aspx). The calendar lists
MeetingDetail links and, for concluded meetings, a "Minutes" View.ashx
document. We parse the roll-call tallies in those minutes.

Roll-call format in these minutes (PDF->text):
   Affirmative: 51 - Abreu, Ariola, Ayala, ... and Zhuang
The stated count (51) is authoritative; the name list is comma/"and"
separated. Two PDF-to-text hazards:
  * long rosters WRAP, so a name token can arrive with a LEADING newline
    ("\\nAriola") -> must strip BEFORE truncating;
  * the LAST name is not comma-terminated and runs straight into the next
    item's text -> must truncate at the first internal newline.
So: strip each token, then cut at its first newline.

Only the injected runtime `rt` performs I/O. Deterministic, stdlib only.
"""

import re
import datetime

EXTRACTOR_VERSION = "1"

SOURCE_ID = "newyork-bos"
RUN_ID = SOURCE_ID + "-" + EXTRACTOR_VERSION

# --- tenant constants (re-point here for another Legistar tenant) ---------
LEGISTAR_HOST = "https://legistar.council.nyc.gov"
CALENDAR_URL = LEGISTAR_HOST + "/Calendar.aspx"
DEFAULT_BODY = "New York City Council"

_DEFAULT_MAX = 10
_MAX_CANDIDATES = 120
_FETCH_BUDGET = 240

_LABEL_POS = {
    "affirmative": "aye", "aye": "aye", "ayes": "aye",
    "negative": "no", "nay": "no", "nays": "no",
    "abstain": "abstain", "abstained": "abstain",
    "abstention": "abstain", "abstentions": "abstain",
    "absent": "absent", "excused": "absent",
    "recused": "recused", "recusal": "recused",
}
_LABEL_RE = re.compile(
    r'\b(Affirmative|Negative|Abstentions?|Abstained|Abstain|Absent|'
    r'Excused|Recused|Recusal|Ayes?|Nays?)\b', re.I)

_NUM_LEAD_RE = re.compile(r'[\s:\u2013\u2014\-]*(\d+)[\s:\u2013\u2014\-]*')

_BODY_RE = re.compile(
    r'Committee|Council|Subcommittee|Delegation|Task Force|Conference|'
    r'Commission')

_TITLE_PATS = [
    re.compile(r'Report of the Committee on [^\.\n;:]{3,90}'),
    re.compile(
        r'(?:Preconsidered\s+)?'
        r'(?:Int|Res|Introduction|Resolution|L\.?\s?U\.?|M|T)\.?\s*'
        r'(?:No\.?\s*)?\d[\w\-/]*'),
]

# A plausible person name: starts with a letter, letters/space/.'- only,
# reasonable length, at least two letters, no digits, no newline.
_NAME_OK = re.compile(r"^[A-Za-z\u00C0-\u017F][A-Za-z\u00C0-\u017F .'\-]{0,38}$")

_TITLES = sorted(
    ["council members", "council member", "vice chair", "chairperson",
     "chairman", "chairwoman", "chair", "supervisor", "mr.", "ms.", "mrs.",
     "dr.", "mayor", "hon.", "the honorable", "the speaker", "speaker",
     "the majority leader", "majority leader", "the minority leader",
     "minority leader", "the public advocate", "public advocate",
     "the majority whip", "majority whip", "the minority whip",
     "minority whip", "the"],
    key=len, reverse=True)


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


def _clean_name(name):
    s = str(name or "").strip()
    changed = True
    while changed:
        changed = False
        low = s.lower()
        for t in _TITLES:
            if low == t or low.startswith(t + " "):
                s = s[len(t):].strip()
                changed = True
                break
    return s.strip(" .,-\u2013\u2014")


def _trim_token(tok):
    """Strip surrounding whitespace/newlines FIRST (rosters wrap, leaving
    leading newlines), THEN cut at the first internal newline (only the last,
    non-comma-terminated name carries trailing document text)."""
    t = str(tok or "").strip()
    t = re.split(r'[\r\n]', t, 1)[0]
    return t.strip()


def _clean_name_token(tok):
    t = _trim_token(tok)
    t = re.sub(r'\([^)]*\)', ' ', t)            # drop parenthetical role
    t = re.sub(r'^(?i:and)\s+', '', t.strip())  # drop leading conjunction
    t = _clean_name(t)                          # drop titles/honorifics
    t = re.sub(r'\s+', ' ', t).strip(' .,;:\u2013\u2014-')
    return t


def _valid_name(nm):
    if not nm or not _NAME_OK.match(nm):
        return False
    return len(re.findall(r'[A-Za-z\u00C0-\u017F]', nm)) >= 2


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


def _parse_tally(seg):
    """Return (stated_count, cleaned_names). Names are split on commas and
    the ' and ' conjunction, each token stripped then newline-truncated."""
    m = _NUM_LEAD_RE.match(seg)
    if not m:
        return None, []
    n = int(m.group(1))
    if n == 0:
        return 0, []
    rest = seg[m.end():]
    raw_tokens = []
    for piece in rest.split(','):
        for sub in re.split(r'\s+and\s+', piece):
            sub = _trim_token(sub)
            if re.search(r'[A-Za-z\u00C0-\u017F]', sub):
                raw_tokens.append(sub)
        if len(raw_tokens) >= n:
            break
    names = [_clean_name_token(t) for t in raw_tokens[:n]]
    return n, names


def _parse_minutes(text):
    """Return (attendance dict, list of vote blocks)."""
    entries = []
    matches = list(_LABEL_RE.finditer(text))
    for i, m in enumerate(matches):
        pos = _LABEL_POS.get(m.group(1).lower())
        if not pos:
            continue
        seg_start = m.end()
        nxt = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        seg_end = min(nxt, seg_start + 2000)
        seg = text[seg_start:seg_end]
        n, names = _parse_tally(seg)
        if n is None:
            continue
        entries.append({"pos": pos, "names": names, "n": n,
                        "start": m.start()})

    attendance = {}
    for e in entries:
        for nm in e["names"]:
            if not _valid_name(nm):
                continue
            if e["pos"] == "absent":
                attendance.setdefault(nm, "absent")
            else:
                attendance[nm] = "present"

    blocks = []
    cur = None
    prev = None
    for e in entries:
        if e["pos"] == "aye":
            if cur:
                blocks.append(cur)
            cur = {"entries": [e], "start": e["start"]}
            prev = e["start"]
        else:
            if cur is not None and prev is not None and \
                    (e["start"] - prev) < 1500:
                cur["entries"].append(e)
                prev = e["start"]
    if cur:
        blocks.append(cur)

    return attendance, blocks[:400]


def _extract_title(preceding):
    best = None
    for pat in _TITLE_PATS:
        for mm in pat.finditer(preceding):
            best = mm
    if best:
        tail = preceding[best.start():]
        cut = re.split(
            r'(?:A motion|This \w+ was|By Council|Attachments:|approved by)',
            tail)[0]
        t = re.sub(r'\s+', ' ', cut).strip(' .,;:-')
        if t:
            return t[:120]
    return "General Order Calendar"


def _provenance(source_url):
    return {
        "source_id": SOURCE_ID,
        "extractor_version": EXTRACTOR_VERSION,
        "run_id": RUN_ID,
        "source_url": source_url,
        "certification": {
            "certified": True,
            "method": "legistar-calendar+minutes",
        },
    }


def _body_from_frag(frag):
    for am in re.finditer(r'<a[^>]*>(.*?)</a>', frag, re.S | re.I):
        txt = re.sub(r'<[^>]+>', '', am.group(1))
        txt = txt.replace('&amp;', '&').replace('&nbsp;', ' ')
        txt = re.sub(r'\s+', ' ', txt).strip()
        if txt and _BODY_RE.search(txt) and len(txt) <= 90:
            return txt
    return DEFAULT_BODY


def _enumerate_calendar(rt):
    try:
        html = rt.fetch_text(CALENDAR_URL)
    except Exception:
        return []
    if not html:
        return []

    cands = []
    seen = set()

    for frag in re.split(r'(?i)<tr\b', html):
        if "meetingdetail.aspx" not in frag.lower():
            continue
        dm = re.search(r'(\d{1,2}/\d{1,2}/\d{4})', frag)
        if not dm:
            continue
        date = _parse_date(dm.group(1))
        if not date:
            continue
        det = re.search(r'href="([^"]*MeetingDetail\.aspx[^"]*)"', frag, re.I)
        if not det:
            continue
        detail_url = _abs_url(det.group(1))
        if not detail_url or detail_url in seen:
            continue
        mm = re.search(r'href="([^"]*View\.ashx\?[^"]*M=M[^"]*)"', frag, re.I)
        minutes_url = _abs_url(mm.group(1)) if mm else None
        seen.add(detail_url)
        cands.append({"date": date, "detail_url": detail_url,
                      "minutes_url": minutes_url,
                      "body": _body_from_frag(frag)})

    if not cands:
        for m in re.finditer(r'href="([^"]*MeetingDetail\.aspx[^"]*)"',
                             html, re.I):
            detail_url = _abs_url(m.group(1))
            if not detail_url or detail_url in seen:
                continue
            back = html[max(0, m.start() - 2500):m.start()]
            dts = re.findall(r'(\d{1,2}/\d{1,2}/\d{4})', back)
            if not dts:
                continue
            date = _parse_date(dts[-1])
            if not date:
                continue
            fwd = html[m.start():m.start() + 2500]
            mm = re.search(r'href="([^"]*View\.ashx\?[^"]*M=M[^"]*)"',
                           fwd, re.I)
            minutes_url = _abs_url(mm.group(1)) if mm else None
            seen.add(detail_url)
            cands.append({"date": date, "detail_url": detail_url,
                          "minutes_url": minutes_url,
                          "body": _body_from_frag(back[-1500:])})

    return cands


def _minutes_from_detail(rt, detail_url):
    if not detail_url:
        return None
    try:
        html = rt.fetch_text(detail_url)
    except Exception:
        return None
    if not html:
        return None
    m = re.search(
        r'href="([^"]*View\.ashx[^"]*)"[^>]*>[^<]*Minutes[^<]*<', html, re.I)
    if not m:
        m = re.search(r'href="([^"]*View\.ashx\?[^"]*M=M[^"]*)"', html, re.I)
    return _abs_url(m.group(1)) if m else None


def extract(rt, args):
    want = _coerce_max(args)
    today_iso = datetime.date.today().isoformat()

    cands = _enumerate_calendar(rt)
    past = [c for c in cands if c["date"] and c["date"] < today_iso]
    past.sort(key=lambda c: c["date"], reverse=True)

    meetings_rec, items_rec, votes_rec = [], [], []
    members = {}
    used_ids = set()
    emitted = 0
    examined = 0
    fetches = 0

    for cand in past:
        if emitted >= want or examined >= _MAX_CANDIDATES:
            break
        if fetches >= _FETCH_BUDGET:
            break
        examined += 1
        try:
            date = cand["date"]
            detail_url = cand["detail_url"]
            murl = cand["minutes_url"]

            if not murl and fetches < _FETCH_BUDGET:
                fetches += 1
                murl = _minutes_from_detail(rt, detail_url)
            if not murl:
                continue

            if fetches >= _FETCH_BUDGET:
                break
            fetches += 1
            try:
                text = rt.fetch_text(murl)
            except Exception:
                continue
            if not text or len(text) < 150:
                continue

            attendance, blocks = _parse_minutes(text)
            if not blocks:
                continue

            body = cand.get("body") or DEFAULT_BODY
            if not isinstance(body, str) or not body.strip():
                body = DEFAULT_BODY
            body = body.strip()

            source_url = detail_url or CALENDAR_URL
            if ".ashx" in source_url.lower():
                source_url = CALENDAR_URL
            prov = _provenance(source_url)

            meeting_id = "%s-%s" % (SOURCE_ID, date)
            if meeting_id in used_ids:
                base = "%s-%s" % (meeting_id, _slug(body))
                meeting_id = base
                n = 2
                while meeting_id in used_ids:
                    meeting_id = "%s-%d" % (base, n)
                    n += 1

            vi = 0
            local_items, local_votes = [], []
            for blk in blocks:
                aye_e = blk["entries"][0]
                if aye_e["pos"] != "aye":
                    continue
                if aye_e["n"] <= 0 or len(aye_e["names"]) != aye_e["n"]:
                    continue

                positions = []
                counts = {}
                bad = False
                for e in blk["entries"]:
                    if not e["names"]:
                        continue
                    if len(e["names"]) != e["n"]:
                        bad = True
                        break
                    for nm in e["names"]:
                        if not _valid_name(nm):
                            bad = True
                            break
                        positions.append({"member": nm, "position": e["pos"]})
                    if bad:
                        break
                    counts[e["pos"]] = counts.get(e["pos"], 0) + len(e["names"])
                if bad:
                    continue

                aye = counts.get("aye", 0)
                no = counts.get("no", 0)
                if aye <= 0 or not positions:
                    continue
                result = "pass" if aye > no else "fail"

                bstart = blk["start"]
                quote = text[bstart:bstart + 400]
                if not quote.strip():
                    continue

                title = _extract_title(text[max(0, bstart - 1000):bstart])
                action = "Adopted" if result == "pass" else "Rejected"

                vi += 1
                item_id = "%s-item-%d" % (meeting_id, vi)
                vote_id = "%s-vote-%d" % (meeting_id, vi)

                for p in positions:
                    nm = p["member"]
                    if nm not in members:
                        members[nm] = {"name": nm, "provenance": prov}

                local_items.append({
                    "item_id": item_id,
                    "meeting_id": meeting_id,
                    "title": title,
                    "action": action,
                    "result": result,
                    "file_number": None,
                    "provenance": prov,
                })
                local_votes.append({
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

            if not local_votes:
                continue

            used_ids.add(meeting_id)
            if attendance:
                att = {nm: st for nm, st in attendance.items()
                       if _valid_name(nm)}
            else:
                att = {}
            if not att:
                att = {p["member"]: "present"
                       for v in local_votes for p in v["positions"]
                       if p["position"] != "absent"}

            meetings_rec.append({
                "meeting_id": meeting_id,
                "body": body,
                "date": date,
                "attendance": att,
                "source_url": source_url,
                "data_source_url": murl,
                "file_number": None,
                "provenance": prov,
            })
            for nm in att:
                if nm not in members:
                    members[nm] = {"name": nm, "provenance": prov}
            items_rec.extend(local_items)
            votes_rec.extend(local_votes)
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
