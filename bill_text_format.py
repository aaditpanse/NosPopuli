"""Render Congress.gov's fixed-width bill typescript as readable HTML.

The source is a 70-column typescript: hard-wrapped lines, headings centered
with spaces, ``TeX quotes'', <DOC>/<all> markers and [[Page n]] stamps. This
rebuilds paragraphs from it. A new block starts at a blank line, a section
heading (SEC. 2. / TITLE I / Subtitle A), a table-of-contents line, or an
enumerator such as (a) (1) (A) (i) “(aa). Everything else is a wrapped
continuation and is rejoined. Items sit at columns 4, 12, 20…; their wrapped
lines at 0, 8, 16…, so a "(d)," at column 0 is a wrap, not a new item.

Mirrors formatBillText in frontend/js/ledger.js; keep the two in step.
"""
import html
import re

_ENUM = re.compile(r"^\s*[\u201C\u2018\"']*\(([a-z]{1,3}|[A-Z]{1,3}|\d{1,3}|[ivxlc]{1,5}|[IVXLC]{1,5})\)")
_ENUM_WRAP = re.compile(r"^\s*[\u201C\u2018\"']*\([^)]{1,5}\)\s*[,;:)(]")
_SEC = re.compile(r"^\s*[\u201C]*(SEC(TION)?\.?\s+\d+[A-Z]?\.|TITLE\s+[IVXLC]+|Subtitle\s+[A-Z]|CHAPTER\s+\d+|PART\s+[A-Z0-9]+)(?=[\s\u2014-]|$)")
_SEC_SPLIT = re.compile(r"^([\u201C]*(?:SEC(?:TION)?\.?\s+\d+[A-Z]?\.|TITLE\s+[IVXLC]+|Subtitle\s+[A-Z]|CHAPTER\s+\d+|PART\s+[A-Z0-9]+))\s*\u2014?\s*(.*)$")
_TOC = re.compile(r"^\s*Sec\.\s+\d+[A-Z]?\.")
_HEAD = re.compile(
    r"^\s*(\d+(st|nd|rd|th) (CONGRESS|Congress)|\d+(st|nd) Session|"
    r"(H\. ?R\.|S\.|H\. ?J\. ?Res\.|S\. ?J\. ?Res\.|H\. ?Res\.|S\. ?Res\.|H\. ?Con\. ?Res\.|S\. ?Con\. ?Res\.) \d+|"
    r"Public Law \d+-\d+|An Act|A BILL|A RESOLUTION|A JOINT RESOLUTION|A CONCURRENT RESOLUTION|"
    r"IN THE (HOUSE OF REPRESENTATIVES|SENATE)( OF THE UNITED STATES)?)\s*$"
)
_RULE = re.compile(r"^\s*_{10,}\s*$")
_ALLCAPS = re.compile(r"^[A-Z0-9 .,;:'\u2019\u201C\u201D()-]+$")
_ENUM_LEAD = re.compile(r"^([\u201C]*\([^)]{1,5}\))\s*(.*)$")


def bill_text_blocks(txt):
    s = re.sub(r"</?(DOC|all|html|body|pre)[^>]*>", "", txt or "", flags=re.I)
    s = re.sub(r"\[\[Page [^\]]*\]\]", "", s)
    s = re.sub(r"<<NOTE:[^>]*>>[ \t]*", "", s)  # Statutes at Large margin notes
    s = s.replace("``", "\u201C").replace("''", "\u201D").replace("\r", "")
    blocks = []
    cur = None

    def push(kind, text, lead):
        nonlocal cur
        cur = {"kind": kind, "text": text, "lead": lead}
        blocks.append(cur)

    for raw in s.split("\n"):
        line = raw.rstrip()
        if not line.strip():
            cur = None
            continue
        if _RULE.match(line):
            push("rule", "", 0)
            cur = None
            continue
        lead = len(line) - len(line.lstrip(" "))
        text = line.strip()
        is_enum = bool(_ENUM.match(line)) and lead % 8 == 4 and not _ENUM_WRAP.match(line)
        centered = lead >= 14 and len(text) <= 72 - lead + 8 and not is_enum \
            and not re.search(r"[.;:]$", text) and re.search(r"[A-Za-z]", text)
        allcaps = bool(_ALLCAPS.match(text)) and bool(re.search(r"[A-Z]{3}", text))
        cur_done = cur is None or cur["kind"] != "p" or bool(re.search(r"[.;:\u201D\"]$", cur["text"]))
        if _HEAD.match(line):
            push("head", text, lead); continue
        if _SEC.match(line):
            push("sec", text, 0); continue
        if _TOC.match(line):
            push("toc", text, lead); continue
        if cur and cur["kind"] == "sec" and (allcaps or lead >= 10) and not is_enum:
            cur["text"] += " " + text; continue
        if cur is None and allcaps and len(text) < 40 and not re.search(r"[.;:,]$", text):
            push("head", text, lead); continue
        if centered and (allcaps or lead >= 20) and cur_done and (cur is None or cur["kind"] != "p" or cur["lead"] < 8):
            push("head", text, lead); continue
        if is_enum or cur is None or cur["kind"] in ("head", "rule"):
            push("p", text, lead); continue
        joiner = "" if (cur["text"].endswith("-") and not cur["text"].endswith("--")) else " "
        cur["text"] += joiner + text

    for b in blocks:
        b["text"] = re.sub(r"`([^`'\n]{1,80})'", "\u2018\\1\u2019", b["text"].replace("--", "\u2014"))
    return blocks


def bill_text_html(txt):
    out = []
    for b in bill_text_blocks(txt):
        k, t = b["kind"], b["text"]
        if k == "rule":
            out.append('<hr class="bt-rule">')
        elif k == "head":
            out.append(f'<div class="bt-head">{html.escape(t)}</div>')
        elif k == "sec":
            m = _SEC_SPLIT.match(t)
            big = bool(re.match(r"^(TITLE|Subtitle|CHAPTER|PART)", t))
            no, rest = (m.group(1), m.group(2)) if m else ("", t)
            sep = " \u2014 " if big and rest else " "
            out.append(f'<div class="bt-sec {"bt-title" if big else ""}"><span class="bt-secno">{html.escape(no)}</span>{sep}{html.escape(rest)}</div>')
        elif k == "toc":
            out.append(f'<p class="bt-p bt-toc">{html.escape(t)}</p>')
        else:
            pad = min(max(0, b["lead"] - 4), 40) * 0.22
            m = _ENUM_LEAD.match(t)
            body = f'<span class="bt-enum">{html.escape(m.group(1))}</span> {html.escape(m.group(2))}' if m else html.escape(t)
            out.append(f'<p class="bt-p" style="padding-left:{pad:.2f}em">{body}</p>')
    return "".join(out)


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} · text — NosPopuli</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;700&family=IBM+Plex+Mono:wght@400;500&family=Source+Serif+4:wght@300;400;600&display=swap" rel="stylesheet">
<style>
  :root {{ --ink:#0e0e0e; --paper:#f5f0e8; --card:#faf7f2; --accent:#8b1a1a; --muted:#6b6355; --rule:#c8bfaa;
          --fd:"Playfair Display",Georgia,serif; --fb:"Source Serif 4",Georgia,serif; --fm:"IBM Plex Mono",monospace; }}
  html,body {{ margin:0; background:var(--paper); color:var(--ink); }}
  .wrap {{ max-width: 46em; margin: 0 auto; padding: 28px 22px 80px; }}
  .top {{ display:flex; justify-content:space-between; align-items:baseline; gap:16px; border-bottom:1px solid var(--ink); padding-bottom:10px; margin-bottom:22px; }}
  .brand {{ font-family:var(--fd); font-size:20px; font-weight:700; color:var(--ink); text-decoration:none; }}
  .brand i {{ color:var(--accent); font-style:italic; }}
  .kick {{ font-family:var(--fm); font-size:10px; letter-spacing:.16em; text-transform:uppercase; color:var(--muted); }}
  .kick a {{ color:var(--accent); text-decoration:none; }}
  h1 {{ font-family:var(--fd); font-size:26px; line-height:1.15; margin:0 0 6px; }}
  .meta {{ font-family:var(--fb); font-size:13px; color:var(--muted); font-style:italic; margin:0 0 22px; }}
  .billtext {{ font-family:var(--fb); font-size:16px; line-height:1.65; background:var(--card); border:1px solid var(--rule); padding:28px 34px; }}
  .bt-head {{ text-align:center; font-family:var(--fm); font-size:11px; letter-spacing:.12em; text-transform:uppercase; color:var(--muted); margin:4px 0 8px; }}
  .bt-rule {{ border:0; border-top:1px solid var(--rule); margin:16px 0; }}
  .bt-sec {{ font-family:var(--fd); font-size:17px; font-weight:700; margin:24px 0 8px; line-height:1.3; }}
  .bt-secno {{ color:var(--accent); }}
  .bt-title {{ text-align:center; font-family:var(--fm); font-size:11.5px; letter-spacing:.14em; text-transform:uppercase; font-weight:400; margin:30px 0 12px; }}
  .bt-title .bt-secno {{ color:var(--ink); }}
  .bt-p {{ margin:0 0 9px; }}
  .bt-enum {{ font-family:var(--fm); font-size:12.5px; color:var(--muted); }}
  .bt-toc {{ font-size:14px; color:var(--muted); margin:0 0 3px; padding-left:1.5em; }}
  .foot {{ font-family:var(--fb); font-size:13px; color:var(--muted); margin-top:18px; line-height:1.6; }}
  .foot a {{ color:var(--accent); }}
  @media (max-width:600px) {{ .billtext {{ padding:18px 16px; font-size:15px; }} }}
</style></head>
<body><div class="wrap">
  <div class="top"><a class="brand" href="/">Nos<i>Populi</i></a><span class="kick"><a href="{back}">← Our English for this bill</a></span></div>
  <div class="kick">{label} · {congress}th Congress · full text</div>
  <h1>{title}</h1>
  <p class="meta">{sub}</p>
  <div class="billtext">{body}</div>
  <p class="foot">This is the official text as published by Congress.gov, reflowed for reading. Nothing has been added or removed. The original typescript is <a href="{cg}" target="_blank" rel="noopener">on Congress.gov</a>.</p>
</div></body></html>"""

_CG_TYPES = {"hr": "house-bill", "s": "senate-bill", "hres": "house-resolution", "sres": "senate-resolution",
             "hjres": "house-joint-resolution", "sjres": "senate-joint-resolution",
             "hconres": "house-concurrent-resolution", "sconres": "senate-concurrent-resolution"}


def _ordinal(n):
    n = int(n)
    if 10 <= n % 100 <= 20:
        return f"{n}th"
    return f"{n}{ {1: 'st', 2: 'nd', 3: 'rd'}.get(n % 10, 'th') }"


def congress_gov_text_url(congress, bill_type, number):
    t = _CG_TYPES.get((bill_type or "").lower())
    if not t:
        return f"https://www.congress.gov/search?q={bill_type.upper()}%20{number}"
    return f"https://www.congress.gov/bill/{_ordinal(congress)}-congress/{t}/{number}/text"


def bill_text_page(congress, bill_type, number, txt, title=None):
    label = f"{bill_type.upper()} {number}"
    body = bill_text_html(txt) if txt else '<p class="bt-p">The text of this bill has not been published yet.</p>'
    n_pages = max(1, round(len(txt or "") / 3200)) if txt else 0
    return _PAGE.format(
        title=html.escape(title or label),
        label=html.escape(label),
        congress=html.escape(str(congress)),
        sub=html.escape(f"About {n_pages} page{'s' if n_pages != 1 else ''} as printed." if txt else "No text yet."),
        body=body,
        back=f"/bill/{congress}/{bill_type}/{number}",
        cg=congress_gov_text_url(congress, bill_type, number),
    )
