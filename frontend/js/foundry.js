// Foundry lab console: renders the quarantine-aware municipal store.
// The one rule that matters here: certified and uncertified records are
// never visually interchangeable — uncertified always carries a warning.

// machine-derived next-meeting lookups (upcoming.py), keyed by source id —
// advisory display metadata, never part of the certified record
let UPCOMING = {};
// machine-derived per-meeting digests (meeting_digests.py), keyed by meeting_id
let DIGESTS = {};

// The earliest genuinely-future meeting for a source. The stored `.next` can
// be stale (a date that has since passed), so never trust it alone — pool
// `.next` with the `.upcoming[]` list and take the first one that is today or
// later. Returns null when we have no future meeting on record.
function nextMeeting(sourceId) {
  const u = UPCOMING[sourceId] || {};
  const today = new Date().toISOString().slice(0, 10);
  const cands = [];
  if (u.next && u.next.date) cands.push(u.next);
  if (Array.isArray(u.upcoming)) cands.push(...u.upcoming);
  return cands.filter(m => m && m.date && m.date >= today)
    .sort((a, b) => a.date.localeCompare(b.date))[0] || null;
}

const REGIONS = {
  "pittsburgh-legistar": { title: "Pittsburgh, Pennsylvania", sub: "City Council · Legistar API + clerk's minutes" },
  "la-primegov": { title: "Los Angeles, California", sub: "City Council · PrimeGov Journal + City Clerk CFMS" },
  "loudoun-bos": { title: "Loudoun County, Virginia", sub: "Board of Supervisors · Laserfiche Action Reports" },
};

function el(tag, cls, html) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (html !== undefined) node.innerHTML = html;
  return node;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

function certStatus(rec) {
  return (rec.certification && rec.certification.status) || "quarantined";
}

function badge(rec) {
  const status = certStatus(rec);
  if (status === "certified")
    return '<span class="badge ok">certified · cross-source</span>';
  if (status === "machine-derived")
    return '<span class="badge derived">machine-derived</span>';
  return '<span class="badge warn">⚠ uncertified</span>';
}

function tally(counts) {
  const order = ["aye", "no", "abstain", "absent", "present", "recused"];
  return order.filter(k => counts[k]).map(k => `${counts[k]} ${k}`).join(" · ") || "—";
}

// "approve the following items on consent: 1a, 1b, 2a, and 3" -> ["1a","1b","2a","3"]
function consentItemNos(motion) {
  const m = (motion || "").match(/items? on consent:?\s*([0-9a-zA-Z ,\-and]+?)(?:\.|\()/);
  if (!m) return [];
  return m[1].split(/,|\band\b/).map(s => s.trim().toLowerCase())
    .filter(s => /^[0-9]{1,2}[a-z]?$|^[ir]-\d+$/.test(s))
    .map(s => /^[ir]-/.test(s) ? s.toUpperCase() : s); // match the miner's item_no keys
}

function factLine(fact) {
  const dollars = (fact.dollar_amounts || [])[0];
  return `<div class="factrow"><span class="code">${esc(fact.item_no)}</span>
    ${esc(fact.summary)} ${dollars ? `<span class="topic">${esc(dollars.slice(0, 40))}</span>` : ""}</div>`;
}

function consentBreakdown(vote, meetingDate, factsByMeeting) {
  const nos = consentItemNos(vote.motion);
  const facts = factsByMeeting[meetingDate] || {};
  const found = nos.map(n => facts[n]).filter(Boolean);
  if (!found.length) return "";
  return `<details class="consent"><summary>what's inside these ${nos.length}
      consent items <span class="badge derived">machine-derived</span></summary>
    ${found.map(factLine).join("")}
    ${found.length < nos.length
      ? `<div class="note">(${nos.length - found.length} item(s) had no minable staff report)</div>` : ""}
  </details>`;
}

function voteRow(vote, items, summaries, meetingDate, factsByMeeting, sharedNote) {
  const item = items[vote.item_id];
  const summary = summaries[vote.item_id] || summaries[vote.vote_id];
  let label;
  if (summary) {
    // plain-English first; the official record stays one line below
    const official = (item && item.title) || vote.motion || "";
    label = `<td><div class="plain">${esc(summary.plain_english)}
        <span class="topic">${esc(summary.topic)}</span></div>
      <div class="official">${vote.file_number
          ? `<span class="code">${esc(vote.file_number)}</span> · ` : ""}
        ${esc(official.slice(0, 150))}</div>
      ${consentBreakdown(vote, meetingDate, factsByMeeting)}</td>`;
  } else if (item && item.title) {
    label = `<td>${vote.file_number
        ? `<span class="code">${esc(vote.file_number)}</span> · ` : ""}
      ${esc(item.title.slice(0, 180))}</td>`;
  } else if (vote.motion) {
    label = `<td class="note">${esc(vote.motion.slice(0, 170))}…</td>`;
  } else {
    label = `<td class="code">${esc(vote.file_number || vote.vote_id)}</td>`;
  }
  // the standard source-wide quarantine note renders once at meeting level,
  // not on every row; only row-specific notes (disputes etc.) show here
  const noteText = vote.certification && vote.certification.note;
  const note = noteText && noteText !== sharedNote
    ? `<div class="dispnote">${esc(noteText)}</div>` : "";
  const inconsistent = vote.tally_consistent === false
    ? '<div class="dispnote">parser flagged: derived positions do not reproduce the reported tally</div>' : "";
  // evidence spans: the primary's own source passage, and/or the second
  // source's affirming passage attached at certification time. Quotes are
  // verbatim from the source (the gate greps for them), so scrub any HTML
  // noise at render time only.
  const clean = q => q.replace(/<[^>]+>/g, "").replace(/&[a-z]+;/g, " ")
    .replace(/\s+/g, " ").trim();
  let evidence = "";
  if (vote.evidence && vote.evidence.quote)
    evidence += `<div class="evidence"><span class="evlabel">source text</span>
      “${esc(clean(vote.evidence.quote).slice(0, 220))}”</div>`;
  const affirmed = vote.certification && vote.certification.evidence;
  if (affirmed && affirmed.quote)
    evidence += `<div class="evidence"><span class="evlabel">affirmed by second source</span>
      “${esc(clean(affirmed.quote).slice(0, 220))}”</div>`;
  return `<tr>${label}
    <td class="tallycell">${tally(vote.counts || {})}</td>
    <td class="tallycell result-${esc(vote.result)}">${esc((vote.result || "?").toUpperCase())}</td>
    <td>${badge(vote)}${note}${inconsistent}${evidence}</td></tr>`;
}

function meetingBlock(meeting, votes, items, summaries, factsByMeeting) {
  const details = el("details", "meeting");
  const present = Object.values(meeting.attendance || {}).filter(v => v === "present").length;
  const absent = Object.values(meeting.attendance || {}).filter(v => v === "absent").length;
  const uncert = votes.filter(v => certStatus(v) !== "certified").length
    + (certStatus(meeting) !== "certified" ? 1 : 0); // the meeting record itself counts
  details.innerHTML = `<summary>
      <span class="mdate">${esc(meeting.date)}</span>
      <span class="mmeta">${esc(meeting.body || "")} · ${present} present / ${absent} absent
        · ${votes.length} recorded votes</span>
      ${uncert ? `<span class="badge warn">⚠ ${uncert} uncertified</span>`
               : `<span class="badge ok">all certified</span>`}
    </summary>`;
  const digest = DIGESTS[meeting.meeting_id];
  if (digest && digest.digest) {
    details.appendChild(el("div", "digest",
      `<div class="mmeta">in plain english <span class="badge derived">machine-derived</span></div>
       <div class="digest-text">${esc(digest.digest)}</div>
       ${(digest.notable || []).map(n => `<div class="factrow">${esc(n)}</div>`).join("")}`));
  }
  // source-wide certification note, once — not repeated on every vote row
  const noteCounts = {};
  for (const v of votes) {
    const n = v.certification && v.certification.note;
    if (n) noteCounts[n] = (noteCounts[n] || 0) + 1;
  }
  const sharedNote = Object.keys(noteCounts)
    .find(n => noteCounts[n] >= Math.max(2, votes.length * 0.5)) || null;
  if (sharedNote)
    details.appendChild(el("div", "dispnote", esc(sharedNote)));
  const table = el("table");
  table.innerHTML = `<tr><th>What was voted on</th><th>Tally</th><th>Result</th><th>Trust</th></tr>` +
    votes.map(v => voteRow(v, items, summaries, meeting.date, factsByMeeting, sharedNote)).join("");
  details.appendChild(table);
  if (meeting.source_url)
    details.appendChild(el("div", "note",
      `&nbsp;source: <a href="${esc(meeting.source_url)}" rel="noopener">official record</a>`));
  return details;
}

// --- member voting records: "how did my supervisor vote?" ---
// Names vary per source ("Jefferson" vs "Patrick S. Herrity"); join on the
// canonical last token, mirroring the harness's member_key.
function lastKey(name) {
  const SUFF = new Set(["JR", "SR", "II", "III", "IV"]);
  const toks = (name || "").normalize("NFKD")
    .replace(/[̀-ͯ​-‍﻿]/g, "")
    .replace(/[.,]/g, " ").trim().split(/\s+/);
  while (toks.length > 1 && SUFF.has(toks[toks.length - 1].toUpperCase())) toks.pop();
  return (toks[toks.length - 1] || "").toUpperCase();
}

function voteLabel(vote, store, summaries) {
  const item = (store.agenda_items || {})[vote.item_id] || {};
  const s = summaries[vote.item_id] || summaries[vote.vote_id];
  return (s && s.plain_english) || item.title || vote.motion || vote.vote_id;
}

function memberRecord(name, store, summaries) {
  const key = lastKey(name);
  const dates = {};
  for (const m of Object.values(store.meetings)) dates[m.meeting_id] = m.date;
  const rows = [];
  for (const v of Object.values(store.vote_events)) {
    const p = (v.positions || []).find(p => lastKey(p.member) === key);
    if (p) rows.push({ vote: v, position: p.position, date: dates[v.meeting_id] || "" });
  }
  rows.sort((a, b) => b.date.localeCompare(a.date) ||
    b.vote.vote_id.localeCompare(a.vote.vote_id, undefined, { numeric: true }));
  let present = 0, meetings = 0;
  for (const m of Object.values(store.meetings))
    for (const [n, st] of Object.entries(m.attendance || {}))
      if (lastKey(n) === key) { meetings++; if (st === "present") present++; }
  return { rows, present, meetings };
}

function memberPanel(name, store, summaries) {
  const { rows, present, meetings } = memberRecord(name, store, summaries);
  const tallies = {};
  for (const r of rows) tallies[r.position] = (tallies[r.position] || 0) + 1;
  const deviations = rows.filter(r => r.position !== "aye");
  const row = r => `<tr>
    <td class="code">${esc(r.date)}</td>
    <td>${esc(voteLabel(r.vote, store, summaries).slice(0, 130))}</td>
    <td class="tallycell ${r.position === "no" ? "result-fail" : ""}">${esc(r.position.toUpperCase())}</td>
    <td class="tallycell result-${esc(r.vote.result)}">${esc((r.vote.result || "?").toUpperCase())}</td>
    <td>${badge(r.vote)}</td></tr>`;
  const table = rs => `<table><tr><th>date</th><th>item</th><th>their vote</th>
    <th>outcome</th><th>trust</th></tr>${rs.map(row).join("")}</table>`;

  const panel = el("div", "memberpanel");
  panel.innerHTML = `
    <div class="mp-name">${esc(name)}</div>
    <div class="stats">${[
      [rows.length, "votes recorded"],
      ...Object.entries(tallies).map(([p, n]) => [n, p]),
      [meetings ? `${present}/${meetings}` : "—", "meetings attended"],
    ].map(([n, l]) => `<div class="stat"><div class="n">${n}</div><div class="l">${esc(String(l))}</div></div>`).join("")}</div>
    ${deviations.length
      ? `<h4>Where they broke from the board (${deviations.length})</h4>${table(deviations)}`
      : `<div class="note">No recorded dissents, abstentions, or absences on votes — every
         recorded position is an aye. On a real board that pattern itself is information.</div>`}
    <details class="fullrecord"><summary>full voting record (${rows.length})</summary>
      ${table(rows)}</details>`;
  return panel;
}

function memberStrip(store, summaries) {
  const names = Object.keys(store.members || {});
  if (!names.length) return null;
  const wrap = el("div", "memberstrip");
  wrap.appendChild(el("span", "mmeta", "board members — click for voting record: "));
  const slot = el("div");
  let openFor = null;
  for (const name of names.sort()) {
    const chip = el("button", "memberchip", esc(name));
    chip.addEventListener("click", () => {
      slot.innerHTML = "";
      if (openFor === name) { openFor = null; return; }
      openFor = name;
      slot.appendChild(memberPanel(name, store, summaries));
    });
    wrap.appendChild(chip);
  }
  wrap.appendChild(slot);
  return wrap;
}

// --- Capital projects (from a county CIP): a different record type than
// meetings/votes — what is being BUILT, with budget and timeline — so it
// gets its own panel, filterable by function and district.
function fmtK(k) {  // amounts are stored in $000s
  if (k >= 1e6) return `$${(k / 1e6).toFixed(2)}B`;
  if (k >= 1000) return `$${(k / 1000).toFixed(1)}M`;
  return `$${k}k`;
}

// A CIP mixes already-built projects with planned ones and recurring
// programs; label each plainly. "recurring" = a perpetual program (never
// finishes); "planned" = a discrete project with forward funding;
// "completed" = already built.
const CP_STATUS_LABEL = { completed: "completed", ongoing: "recurring",
  active: "planned" };
function statusChip(status) {
  const label = CP_STATUS_LABEL[status];
  return label ? `<span class="cpstat cpstat-${status}">${label}</span>` : "";
}

// The "- 2018" a CIP puts on a title is the BOND REFERENDUM year that
// authorized funding, not a build date — label it as such so it stops
// reading like "this happened in 2018."
function bondTag(p) {
  const years = (p.bond_years || []);
  if (!years.length) return "";
  const real = years.filter(y => y !== "TBD");
  const label = real.length
    ? `${real.join(" & ")} bond${real.length > 1 ? "s" : ""}`
    : "future bond";
  return `<span class="cpbond" title="bond referendum that authorized funding, not a build date">${label}</span>`;
}

// Whether a project is a brand-new build, a renovation of an existing
// facility, or an addition/expansion — read from the CIP's own work-type
// subheadings. This is what "just a renovation, not being built" needs.
const CP_WORK_LABEL = { renovation: "renovation", new_construction: "new build",
  addition: "addition" };
function workTag(p) {
  const label = CP_WORK_LABEL[p.work_type];
  return label ? `<span class="cpwork cpwork-${p.work_type}">${label}</span>` : "";
}
function doneTag(p) {
  return p.completion_fy ? `<span class="cpdone" title="estimated completion">done FY${p.completion_fy}</span>` : "";
}

// A facility-type emoji for a project's map pin — chosen by title, then
// function. Module-level so both the full panel and the dashboard map use it.
function cpEmoji(p) {
  const t = (p.title || "").toLowerCase(), f = p.function || "";
  if (/fire (station|and rescue)/.test(t)) return "🚒";
  if (/police|k9|tactical|evidence storage/.test(t)) return "🚓";
  if (/court|judicial|detention/.test(t)) return "⚖️";
  if (/library/.test(t)) return "📚";
  if (/school|elementary|middle|\bhigh\b|academy/.test(t)) return "🏫";
  if (/park|garden|preserve|farm|reservoir|trail|pickleball/.test(t)) return "🌳";
  if (/shelter|childcare|crisis|health|human services|community cent/.test(t)) return "🏥";
  if (/waste ?water|water (treatment|resources|supply)|pumping|sewer|treatment plant/.test(t)) return "💧";
  if (/refuse|landfill|transfer station|solid waste|recycl/.test(t)) return "🗑️";
  if (/Public Safety/.test(f)) return "🚒";
  if (/Librar/.test(f)) return "📚";
  if (/School/.test(f)) return "🏫";
  if (/Park/.test(f)) return "🌳";
  if (/Health/.test(f)) return "🏥";
  if (/Water|Wastewater|Stormwater/.test(f)) return "💧";
  if (/Solid Waste/.test(f)) return "🗑️";
  if (/Court/.test(f)) return "🏛️";
  return "📍";
}

function capitalProjectsSection(sourceId, store, opts = {}) {
  const meta = store.meta || { title: sourceId, sub: "" };
  const projects = store.capital_projects || [];
  const section = el("section", "region");
  section.appendChild(el("h2", null,
    `${esc(meta.title)} <span class="tag" style="display:block">${esc(meta.sub)}</span>`));

  const funded = projects.reduce((s, p) => s + (p.five_year_total || 0), 0);
  const programmed = projects.reduce((s, p) => s + (p.total || 0), 0);
  const funcs = [...new Set(projects.map(p => p.function))].sort();
  const districts = [...new Set(projects.flatMap(p => p.districts || []))].sort();
  const stats = el("div", "stats");
  for (const [n, l] of [[projects.length, "funded projects"], [funcs.length, "functional areas"],
                        [fmtK(funded), "programmed FY27–31"], [fmtK(programmed), "total incl. prior/future"]])
    stats.appendChild(el("div", "stat", `<div class="n">${n}</div><div class="l">${l}</div>`));
  section.appendChild(stats);

  section.appendChild(el("div", "warnbox",
    `<strong>${projects.length} uncertified records</strong>
     Parsed directly from the county's published Capital Improvement Program — a single authoritative
     source, ingested only. Each project reconciles against the CIP's own printed FY27–31 subtotal,
     but no independent second source is wired to certify it. Dollar figures are as the CIP states them.`));

  // filter bar
  const bar = el("div", "cpbar");
  const funcSel = el("select");
  funcSel.innerHTML = `<option value="">All functions</option>` +
    funcs.map(f => `<option>${esc(f)}</option>`).join("");
  const distSel = el("select");
  distSel.innerHTML = `<option value="">All districts</option>` +
    districts.map(d => `<option>${esc(d)}</option>`).join("");
  const search = el("input");
  search.type = "search";
  search.placeholder = "search projects…";
  bar.append(funcSel, distSel, search);
  section.appendChild(bar);

  // Map of the located projects (Leaflet + OpenStreetMap). Only projects the
  // geocoder placed get a pin; countywide programs have no single site and
  // stay in the list. Each pin is a facility-type emoji (cpEmoji, module-level).
  const located = projects.filter(p => typeof p.lat === "number");
  let mapCanvas = null, _map = null, _markers = null;
  // opts.map === false: table/list only (the dashboard already shows the map,
  // so the "view all" list modal must not build a second one)
  if (opts.map !== false && located.length && typeof L !== "undefined") {
    const wrap = el("div", "cpmap");
    const done = located.filter(p => p.status === "completed").length;
    wrap.innerHTML = `<div class="mmeta">${located.length} of ${projects.length}
      projects are point-located — named facilities (stations, libraries, schools,
      the courthouse). Countywide programs have no single site and stay in the list below.</div>
      <div class="cplegend">Pins: <span class="cpstat cpstat-active">planned</span>
      <span class="cpstat cpstat-ongoing">recurring</span>
      <span class="cpstat cpstat-completed">completed</span> — completed (already built) pins are
      faded${done ? ` (${done} on the map)` : ""}. A “2018 bond” tag is the year funding was
      authorized, not a build date.</div>`;
    mapCanvas = el("div", "cpmap-canvas");
    wrap.appendChild(mapCanvas);
    section.appendChild(wrap);
  }

  const holder = el("div", "cptable");
  section.appendChild(holder);

  function syncMap(rowset) {
    if (!_map || !_markers) return;
    _markers.clearLayers();
    for (const p of located) {
      if (!rowset.has(p)) continue;
      const icon = L.divIcon({ className: `cp-emoji cp-${p.status || "active"}`,
        html: `<span>${cpEmoji(p)}</span>`,
        iconSize: [26, 26], iconAnchor: [13, 13], popupAnchor: [0, -12] });
      const m = L.marker([p.lat, p.lon], { icon });
      m.bindPopup(`<b>${esc(p.title)}</b><br>${statusChip(p.status)} ${workTag(p)} ${doneTag(p)} ${bondTag(p)}<br>${esc(p.function)}` +
        `${(p.districts || []).length ? " · " + esc(p.districts.join(", ")) : ""}<br>` +
        `FY27–31 ${fmtK(p.five_year_total || 0)} · total ${fmtK(p.total || 0)}<br>` +
        `<span style="color:#6b6355">${esc((p.funding_source_labels || []).join(", "))}</span>`);
      _markers.addLayer(m);
    }
  }

  function draw() {
    const f = funcSel.value, d = distSel.value, q = search.value.trim().toLowerCase();
    const rows = projects.filter(p =>
      (!f || p.function === f) &&
      (!d || (p.districts || []).includes(d)) &&
      (!q || (p.title + " " + p.function + " " + (p.source_project_numbers || []).join(" "))
        .toLowerCase().includes(q)))
      .sort((a, b) => (b.total || 0) - (a.total || 0));
    const body = rows.map(p => {
      const nums = (p.source_project_numbers || []).join(", ");
      const funds = (p.funding_source_labels || []).join(", ") || "—";
      const dist = (p.districts || []).join(", ") || "—";
      const url = p.data_source_url + "#page=" + (p.pdf_page || 1);
      const pin = typeof p.lat === "number"
        ? ` <span class="cppin cp-${p.status || "active"}" title="mapped">${cpEmoji(p)}</span>` : "";
      return `<tr class="cprow cprow-${p.status || "active"}">
        <td><a href="${esc(url)}" target="_blank" rel="noopener">${esc(p.title)}</a>${pin}
            ${statusChip(p.status)} ${workTag(p)} ${doneTag(p)} ${bondTag(p)}
            ${nums ? `<div class="cpid">${esc(nums)}</div>` : ""}</td>
        <td class="cpfunc">${esc(p.function)}</td>
        <td>${esc(dist)}</td>
        <td class="cpfund">${esc(funds)}</td>
        <td class="cpnum">${fmtK(p.five_year_total || 0)}</td>
        <td class="cpnum"><strong>${fmtK(p.total || 0)}</strong></td>
      </tr>`;
    }).join("");
    holder.innerHTML = `<div class="mmeta">${rows.length} project${rows.length === 1 ? "" : "s"}
      · sorted by total cost</div>
      <table><tr><th>What's being built</th><th>Function</th><th>District</th>
      <th>Funding source</th><th>FY27–31</th><th>Total</th></tr>${body}</table>`;
    syncMap(new Set(rows));
  }

  function initMap() {
    if (_map || !mapCanvas) return;
    _map = L.map(mapCanvas, { scrollWheelZoom: false }).setView([38.83, -77.28], 10);
    L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
      { maxZoom: 18, attribution: "© OpenStreetMap contributors" }).addTo(_map);
    _markers = L.layerGroup().addTo(_map);
    _map.fitBounds(L.latLngBounds(located.map(p => [p.lat, p.lon])).pad(0.12));
    setTimeout(() => _map.invalidateSize(), 60);
    draw();
  }
  // county panels start hidden; init the map the first time it's visible
  if (mapCanvas) {
    const io = new IntersectionObserver(es => {
      if (es.some(e => e.isIntersecting)) { initMap(); io.disconnect(); }
    });
    io.observe(mapCanvas);
  }

  funcSel.onchange = distSel.onchange = draw;
  search.oninput = draw;
  draw();
  return section;
}

// --- Local elections (mayor / school board) from the composed open dataset.
// Closes the civic loop: the election that seated an official, next to their
// votes and what's built. Matched to a county by jurisdiction name.
// Explicit city-name matches for covered CITIES whose mayor won't be found by
// a county-name lookup (a city's name isn't its county's name).
const PLACE_ELECTIONS = {
  newyork: ["new york"], pittsburgh: ["pittsburgh"], la: ["los angeles"],
};

// state FIPS (2-digit) -> USPS, for turning a county's FIPS into its state
const CD_FIPS_USPS = {
  "01": "AL", "02": "AK", "04": "AZ", "05": "AR", "06": "CA", "08": "CO",
  "09": "CT", "10": "DE", "11": "DC", "12": "FL", "13": "GA", "15": "HI",
  "16": "ID", "17": "IL", "18": "IN", "19": "IA", "20": "KS", "21": "KY",
  "22": "LA", "23": "ME", "24": "MD", "25": "MA", "26": "MI", "27": "MN",
  "28": "MS", "29": "MO", "30": "MT", "31": "NE", "32": "NV", "33": "NH",
  "34": "NJ", "35": "NM", "36": "NY", "37": "NC", "38": "ND", "39": "OH",
  "40": "OK", "41": "OR", "42": "PA", "44": "RI", "45": "SC", "46": "SD",
  "47": "TN", "48": "TX", "49": "UT", "50": "VT", "51": "VA", "53": "WA",
  "54": "WV", "55": "WI", "56": "WY", "72": "PR",
};

// every election contest, flattened at render time so the empty-county view
// (which isn't in the county model) can look up its own data too
let ALL_CONTESTS = [];
const normJ = s => (s || "").toLowerCase().replace(/[^a-z0-9 ]+/g, " ").replace(/\s+/g, " ").trim();

// Contests for a place: explicit city-name matches (PLACE_ELECTIONS) plus a
// county-name match against the WHOLE national dataset — so any county in the
// 721-jurisdiction store shows its elections, not just the seven we wired.
function electionsForPlace(key, countyName, state) {
  const names = PLACE_ELECTIONS[key] || [];
  const base = normJ(countyName).replace(/ county$/, "");
  const out = new Map();
  for (const c of ALL_CONTESTS) {
    const j = normJ(c.jurisdiction);
    const byCity = names.includes(j);
    const byCounty = base && state && c.state === state &&
      (j === base + " county" || j === base + " unified" || j === base);
    if (byCity || byCounty) out.set(c.contest_id, c);
  }
  return [...out.values()];
}

// state (USPS) for a covered place, from its county FIPS
function stateOfPlace(key) {
  const fips = (FIPS_BY_PLACE[key] || [])[0];
  return fips ? CD_FIPS_USPS[fips.slice(0, 2)] : null;
}

function electionsSection(contests) {
  const section = el("section", "region");
  section.appendChild(el("h2", null,
    `Local elections <span class="tag" style="display:block">who was elected — mayor & school board</span>`));
  section.appendChild(el("div", "warnbox",
    `<strong>${contests.length} contests · single-source</strong>
     Composed from the American Local Government Elections Database (CC-BY 4.0,
     1989–2021). Vote shares reconcile within each contest; the certified canvass
     is the natural oracle, not yet wired.`));
  const byOffice = {};
  for (const c of contests) (byOffice[c.office] = byOffice[c.office] || []).push(c);
  for (const office of Object.keys(byOffice).sort()) {
    const rows = byOffice[office].sort((a, b) => (b.year - a.year) || ((b.month || 0) - (a.month || 0)));
    const body = rows.map(c => {
      const cands = (c.candidates || []).slice().sort((a, b) => (b.votes || 0) - (a.votes || 0));
      const cl = cands.map(x =>
        `<div class="elcand ${x.winner ? "elwin" : ""}">${x.winner ? "✓ " : ""}${esc(x.name)}${x.incumbent ? " (i)" : ""}
         <span class="elvotes">${x.votes != null ? x.votes.toLocaleString() : "—"}${x.vote_share != null ? " · " + Math.round(x.vote_share * 100) + "%" : ""}</span></div>`).join("");
      const turn = (c.total_votes && c.population_2020)
        ? ` · ${c.total_votes.toLocaleString()} ballots (${Math.round(100 * c.total_votes / c.population_2020)}% of 2020 pop.)` : "";
      return `<tr><td class="elyear">${c.year}${c.district ? "<br><span class='cpid'>" + esc(c.district) + "</span>" : ""}</td>
        <td>${cl}${turn ? `<div class="elturn">${turn.slice(3)}</div>` : ""}</td></tr>`;
    }).join("");
    section.appendChild(el("div", "eloffice",
      `<div class="mmeta">${esc(office)} — ${rows.length} contest${rows.length === 1 ? "" : "s"}</div>`));
    const t = el("table");
    t.innerHTML = `<tr><th>Year</th><th>Candidates (winner ✓, incumbent (i))</th></tr>${body}`;
    section.appendChild(t);
  }
  return section;
}

function regionSection(sourceId, store, itemFacts, summaries) {
  // display metadata: curated entry, else whatever the store carries
  // (auto-onboarded sources write their own meta), else the raw id
  const meta = REGIONS[sourceId] || store.meta || { title: sourceId, sub: "" };
  const section = el("section", "region");
  section.appendChild(el("h2", null,
    `${esc(meta.title)} <span class="tag" style="display:block">${esc(meta.sub)}</span>`));

  const next = nextMeeting(sourceId);
  if (next) {
    const when = new Date(next.date + "T12:00:00").toLocaleDateString("en-US",
      { weekday: "long", month: "long", day: "numeric" });
    section.appendChild(el("div", "nextmeet",
      `<div class="nm-label">Next meeting <span class="badge derived">machine-derived</span></div>
       <div class="nm-when">${esc(when)}${next.time ? " · " + esc(next.time) : ""}</div>
       <div class="nm-body">${esc(next.body)}</div>`));
  } else {
    section.appendChild(el("div", "nextmeet",
      `<div class="nm-label">Next meeting <span class="badge derived">machine-derived</span></div>
       <div class="nm-when" style="font-size:0.9rem">Not on our calendar</div>
       <div class="nm-body">Our meeting-schedule data is out of date — a gap on our end.</div>`));
  }

  const meetings = Object.values(store.meetings).sort((a, b) => b.date.localeCompare(a.date));
  const votes = Object.values(store.vote_events);
  const items = Object.values(store.agenda_items || {});
  const all = [...meetings, ...votes, ...items];
  const certified = all.filter(r => certStatus(r) === "certified").length;
  const uncertified = all.length - certified;

  const stats = el("div", "stats");
  for (const [n, l] of [[meetings.length, "meetings"], [items.length, "agenda items"],
                        [votes.length, "vote events"], [certified, "certified"],
                        [uncertified, "uncertified"]])
    stats.appendChild(el("div", "stat", `<div class="n">${n}</div><div class="l">${l}</div>`));
  section.appendChild(stats);

  if (uncertified > 0) {
    const pct = Math.round(100 * certified / all.length);
    section.appendChild(el("div", "warnbox",
      `<strong>⚠ ${uncertified} uncertified record${uncertified === 1 ? "" : "s"}</strong>
       ${certified === 0
         ? "No independent second source is wired for this jurisdiction yet — every record is ingested only, none is publishable."
         : `${pct}% of records are affirmed by an independent second source. The remainder is quarantined:
            source disagreements, second-source gaps, or items with no final action to affirm.`}`));
  }

  // worth your attention: the deviations — failed motions, dissents,
  // abstentions. Deterministic, straight from the record.
  const mdates = {};
  for (const m of meetings) mdates[m.meeting_id] = m.date;
  const attention = [];
  for (const v of votes) {
    const nos = (v.positions || []).filter(p => p.position === "no").map(p => p.member);
    const abst = (v.positions || []).filter(p => p.position === "abstain").map(p => p.member);
    if (v.result === "fail")
      attention.push({ v, why: "motion FAILED" });
    else if (nos.length)
      attention.push({ v, why: `${nos.join(", ")} voted no` });
    else if (abst.length)
      attention.push({ v, why: `${abst.join(", ")} abstained` });
  }
  attention.sort((a, b) => (mdates[b.v.meeting_id] || "").localeCompare(mdates[a.v.meeting_id] || ""));
  if (attention.length) {
    const rows = attention.slice(0, 6).map(({ v, why }) =>
      `<div class="factrow"><span class="code">${esc(mdates[v.meeting_id] || "")}</span>
       ${esc(voteLabel(v, store, summaries).slice(0, 110))}
       <span class="attn-why">— ${esc(why)}</span></div>`).join("");
    section.appendChild(el("div", "attention",
      `<div class="mmeta">worth your attention — where the board didn't just agree
       (${attention.length} of ${votes.length} votes)</div>${rows}`));
  }

  // what they voted on, by topic (from the machine-derived item summaries)
  const topicCount = {};
  for (const v of votes) {
    const s = summaries[v.item_id] || summaries[v.vote_id];
    if (s && s.topic) topicCount[s.topic] = (topicCount[s.topic] || 0) + 1;
  }
  const topics = Object.entries(topicCount).sort((a, b) => b[1] - a[1]).slice(0, 10);
  if (topics.length)
    section.appendChild(el("div", "topicstrip",
      `<span class="mmeta">what they voted on: </span>` + topics.map(([t, n]) =>
        `<span class="topicchip">${esc(t)} · ${n}</span>`).join(" ")));

  const strip = memberStrip(store, summaries);
  if (strip) section.appendChild(strip);

  // item facts indexed by meeting date -> item number (Loudoun consent expansion)
  const factsByMeeting = {};
  for (const f of Object.values(itemFacts))
    if (f.meeting_date && f.item_no)
      (factsByMeeting[f.meeting_date] = factsByMeeting[f.meeting_date] || {})[f.item_no] = f;

  const byMeeting = {};
  for (const v of votes) (byMeeting[v.meeting_id] = byMeeting[v.meeting_id] || []).push(v);
  for (const m of meetings)
    section.appendChild(meetingBlock(m, (byMeeting[m.meeting_id] || [])
      .sort((a, b) => a.vote_id.localeCompare(b.vote_id, undefined, { numeric: true })),
      store.agenda_items || {}, summaries,
      sourceId === "loudoun-bos" ? factsByMeeting : {}));
  return section;
}

// --- search: filter loaded regions instantly; unknown names kick off an
// async onboarding job (platform probes -> family preview extraction).

function toStoreShape(records) {
  const store = { meetings: {}, agenda_items: {}, vote_events: {}, members: {} };
  for (const [type, idf] of [["meetings", "meeting_id"], ["agenda_items", "item_id"],
                             ["vote_events", "vote_id"], ["members", "name"]])
    for (const rec of records[type] || []) store[type][rec[idf]] = rec;
  return store;
}

function kv(label, value) {
  if (value === undefined || value === null || value === "") return "";
  const text = typeof value === "object" ? JSON.stringify(value, null, 1) : String(value);
  return `<div class="factrow"><span class="mmeta">${esc(label)}</span><br>${esc(text)}</div>`;
}

function profileSection(result) {
  const p = result.profile;
  const section = el("section", "region");
  section.appendChild(el("h2", null,
    `${esc(result.title)} <span class="tag" style="display:block">${esc(result.sub)}</span>`));
  section.appendChild(el("div", "warnbox",
    `<strong>⚠ discovered, not onboarded</strong> The agent located where this
     jurisdiction publishes its records. No extractor exists yet, so no data is
     ingested — onboarding (synthesis + oracle + gates) is the next step.`));
  const second = p.second_source || {};
  section.innerHTML += [
    kv("primary source", p.primary_source),
    kv("second source" + (second.exists === false ? " — NONE FOUND (cannot certify)" : ""), second),
    kv("where votes live", p.votes_located),
    kv("access constraints", p.access_constraints),
  ].join("");
  if (p.systems_surveyed && p.systems_surveyed.length) {
    const table = el("table");
    table.innerHTML = `<tr><th>System surveyed</th><th>Status</th><th>Era</th></tr>` +
      p.systems_surveyed.map(s => `<tr><td class="note">${esc(s.system || "")}<br>
        <span class="official">${esc(s.url || "")}</span></td>
        <td class="note">${esc(s.status || "")}</td>
        <td class="tallycell">${esc(s.era_covered || "")}</td></tr>`).join("");
    section.appendChild(table);
  }
  if (p.open_questions && p.open_questions.length)
    section.appendChild(el("div", "note",
      "open questions: " + p.open_questions.map(esc).join(" · ")));
  return section;
}

async function runSearch(query) {
  const q = query.trim().toLowerCase();
  const log = document.getElementById("joblog");
  // instant path: a county we already display — open its view
  for (const [sourceId, meta] of Object.entries(REGIONS)) {
    if (meta.title.toLowerCase().includes(q)) {
      const box = document.getElementById(`county-${placeKey(sourceId)}`);
      if (box) { log.textContent = ""; selectCounty(placeKey(sourceId)); return; }
    }
  }
  log.innerHTML = `<div class="jl-head">Running the pipeline for “${esc(query)}”…</div>`;
  const start = await fetch("/api/foundry/onboard", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: query }) });
  if (!start.ok) {
    const err = await start.json().catch(() => ({}));
    log.innerHTML = `<div class="jl-head">Pipeline: ${esc(query)}</div>`
      + esc(err.detail || `request failed (${start.status})`);
    return;
  }
  const { job_id } = await start.json();
  window._foundryCancelJob = async () => {
    await fetch(`/api/foundry/onboard/${job_id}/cancel`, { method: "POST" });
  };
  const timer = setInterval(async () => {
    const job = await (await fetch(`/api/foundry/onboard/${job_id}`)).json();
    const p = job.progress || { pct: 0, stage: job.status };
    const cancelBtn = job.status === "running"
      ? ` <button class="jl-cancel" onclick="_foundryCancelJob()">cancel run</button>` : "";
    log.innerHTML = `<div class="jl-head">Pipeline: ${esc(query)} — ${esc(p.stage)}${cancelBtn}</div>
      <div class="pbar"><div class="pfill" style="width:${p.pct}%"></div></div>`
      + job.log.map(esc).join("\n");
    if (job.status === "running") return;
    clearInterval(timer);
    const result = job.result;
    if (!result || result.platform_only) {
      if (result && result.message) log.innerHTML += `\n\n${esc(result.message)}`;
      return;
    }
    if (result.onboarded) {
      // records were merged into the store — pull fresh data, rebuild the
      // county index, and open the newly-onboarded county
      const data = await (await fetch("/api/foundry/data")).json();
      UPCOMING = data.upcoming || {};
      DIGESTS = data.meeting_digests || {};
      if (!data.sources[result.source_id]) return;
      renderCounties(data);
      renderMap(data);
      selectCounty(placeKey(result.source_id));
      return;
    }
    if (result.profile) {
      // a survey-only result (no records yet): show it in the pipeline log
      // area, not the county grid
      log.appendChild(profileSection(result));
      return;
    }
    // live preview (not yet in the store): render below the pipeline log
    REGIONS[result.source_id] = { title: result.title, sub: result.sub };
    const section = regionSection(result.source_id, toStoreShape(result.records), {}, {});
    section.id = `region-${result.source_id}`;
    log.appendChild(section);
    section.scrollIntoView({ behavior: "smooth" });
  }, 1500);
}

// --- County grouping: one place can carry several sources (a legislation
// source AND a public-works CIP source). Group them so the console shows one
// county at a time instead of laying every jurisdiction out at once.
function placeKey(sourceId) { return sourceId.split("-")[0]; }

function placeLabel(sourceId, store) {
  const t = (REGIONS[sourceId] && REGIONS[sourceId].title)
    || (store && store.meta && store.meta.title) || sourceId;
  return t.split("—")[0].split(",")[0].trim();
}

// Build the ordered county model from a /api/foundry/data payload. Each entry:
// { key, label, kinds:Set, sections:[DOM] } — legislation section(s) first,
// then public-works. Curated meetings sources lead, in their canonical order.
function buildCounties(data) {
  const byKey = new Map();
  const place = (sourceId, store) => {
    const key = placeKey(sourceId);
    if (!byKey.has(key))
      byKey.set(key, { key, label: placeLabel(sourceId, store), kinds: new Set(),
                       meetings: [], cip: null, contests: [] });
    return byKey.get(key);
  };
  const order = [...Object.keys(REGIONS).filter(s => data.sources[s]),
                 ...Object.keys(data.sources).filter(s => !REGIONS[s])];
  for (const sourceId of order) {
    const store = data.sources[sourceId];
    if (!REGIONS[sourceId])
      REGIONS[sourceId] = store.meta || { title: sourceId, sub: "" };
    const c = place(sourceId, store);
    c.meetings.push({ sourceId, store });
    c.kinds.add("legislation");
  }
  for (const [sourceId, store] of Object.entries(data.capital_projects || {})) {
    const c = place(sourceId, store);
    if (!c.meetings.length) c.label = placeLabel(sourceId, store);
    c.cip = { sourceId, store };
    c.kinds.add("public works");
  }
  ALL_CONTESTS = Object.values(data.elections || {}).flatMap(s => s.contests || []);
  for (const c of byKey.values()) {
    c.contests = electionsForPlace(c.key, c.label, stateOfPlace(c.key));
    if (c.contests.length) c.kinds.add("elections");
  }
  return [...byKey.values()];
}

// approximate county centers, for the public-works map when a county has no
// project pins to fit to
const PLACE_CENTER = {
  fairfax: [38.83, -77.28], loudoun: [39.09, -77.64], princewilliam: [38.70, -77.48],
  stafford: [38.42, -77.46], newyork: [40.71, -74.00], pittsburgh: [40.44, -79.99],
  la: [34.05, -118.24],
};

// next U.S. general election: the Tuesday after the first Monday in November
function nextGeneralElection() {
  const now = new Date();
  for (let y = now.getFullYear(); y <= now.getFullYear() + 2; y++) {
    const d = new Date(y, 10, 1);
    while (d.getDay() !== 1) d.setDate(d.getDate() + 1);
    d.setDate(d.getDate() + 1);
    if (d >= now) return d;
  }
}

// --- The county dashboard: meetings/legislation in the main column; a
// public-works map in the top-right corner; an election tracker bottom-right.
// Each panel shows a summary; a "view all" link expands the full detail.
function countyDashboard(c, data) {
  const box = el("div", "county");
  box.dataset.key = c.key;
  box.id = `county-${c.key}`;
  const sub = c.kinds.size ? [...c.kinds].join(" · ")
    : "nothing onboarded for this jurisdiction yet — a gap on our end";
  box.appendChild(el("h2", "dash-title",
    `${esc(c.label)} <span class="tag" style="display:block">${esc(sub)}</span>`));
  const grid = el("div", "dash");
  grid.appendChild(dashMeetings(c, data));
  // right sidebar: map, then elections directly beneath it — a self-contained
  // column so expanding a meeting on the left never pushes these apart
  const side = el("div", "dash-side");
  side.appendChild(dashPublicWorks(c));
  side.appendChild(dashElections(c));
  grid.appendChild(side);
  box.appendChild(grid);
  return box;
}

// "view all" opens a full-width overlay so expanding a panel never reflows the
// dashboard (public works must not shove elections down). Content is built
// lazily on first open.
function modalLink(label, title, build) {
  const link = el("button", "dash-more", label);
  link.addEventListener("click", () => openModal(title, build()));
  return link;
}

function openModal(title, contentEl) {
  const overlay = el("div", "modal-overlay");
  const modal = el("div", "modal");
  const bar = el("div", "modal-bar");
  const h = el("div", "modal-h", esc(title));
  const close = el("button", "modal-close", "✕");
  const dismiss = () => overlay.remove();
  close.addEventListener("click", dismiss);
  overlay.addEventListener("click", e => { if (e.target === overlay) dismiss(); });
  document.addEventListener("keydown", function esc2(e) {
    if (e.key === "Escape") { dismiss(); document.removeEventListener("keydown", esc2); }
  });
  bar.append(h, close);
  modal.append(bar, contentEl);
  overlay.appendChild(modal);
  document.body.appendChild(overlay);
}

function dashMeetings(c, data) {
  const wrap = el("div", "dash-meet");
  wrap.appendChild(el("div", "dash-h", "Meetings & legislation"));
  if (!c.meetings.length) {
    wrap.appendChild(el("div", "dash-nodata",
      `We don't track a governing body for ${esc(c.label)} yet — a gap on our end.`));
    return wrap;
  }
  for (const { sourceId, store } of c.meetings) {
    const sec = regionSection(sourceId, store, data.item_facts, data.item_summaries || {});
    sec.id = `region-${sourceId}`;
    const blocks = [...sec.querySelectorAll("details.meeting")];
    if (blocks.length > 3) {
      blocks.slice(3).forEach(b => b.style.display = "none");
      const btn = el("button", "dash-more", `view all ${blocks.length} meetings →`);
      btn.addEventListener("click", () => {
        const hidden = blocks[3].style.display === "none";
        blocks.slice(3).forEach(b => b.style.display = hidden ? "" : "none");
        btn.textContent = hidden ? "show fewer ↑" : `view all ${blocks.length} meetings →`;
      });
      sec.appendChild(btn);
    }
    wrap.appendChild(sec);
  }
  return wrap;
}

function dashPublicWorks(c) {
  const wrap = el("div", "dash-map");
  wrap.appendChild(el("div", "dash-h", "Public works"));
  const canvas = el("div", "dash-mapcanvas");
  wrap.appendChild(canvas);
  const center = c.center || PLACE_CENTER[c.key] || null;
  const projects = c.cip ? (c.cip.store.capital_projects || []) : [];
  const pins = projects.filter(p => typeof p.lat === "number");
  if (c.cip) {
    wrap.appendChild(el("div", "mmeta",
      `${projects.length} funded projects · ${pins.length} on the map`));
    // the corner IS the full map (all markers); "view all" is the LIST only,
    // opened in a modal so it never pushes the elections panel down
    wrap.appendChild(modalLink(`view all ${projects.length} projects →`,
      `${c.label} — public works`,
      () => capitalProjectsSection(c.cip.sourceId, c.cip.store, { map: false })));
  } else {
    wrap.appendChild(el("div", "dash-nodata",
      `No public works data for ${esc(c.label)} yet. That's a gap on our end —
       its Capital Improvement Program just isn't onboarded, not that nothing
       is being built.`));
  }
  initDashMap(canvas, center, pins);
  return wrap;
}

function initDashMap(canvas, center, pins) {
  if (typeof L === "undefined" || !center) {
    canvas.innerHTML = `<div class="dash-nodata" style="padding:1rem">map unavailable for this jurisdiction</div>`;
    return;
  }
  const io = new IntersectionObserver(es => {
    if (es.some(e => e.isIntersecting)) { io.disconnect(); buildDashMap(canvas, center, pins); }
  });
  io.observe(canvas);
}

function buildDashMap(canvas, center, pins) {
  const map = L.map(canvas, { scrollWheelZoom: false, zoomControl: true })
    .setView(center, pins.length ? 11 : 10);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png",
    { maxZoom: 18, attribution: "© OpenStreetMap" }).addTo(map);
  const layer = L.layerGroup().addTo(map);
  for (const p of pins) {
    const icon = L.divIcon({ className: `cp-emoji cp-${p.status || "active"}`,
      html: `<span>${cpEmoji(p)}</span>`, iconSize: [22, 22], iconAnchor: [11, 11] });
    L.marker([p.lat, p.lon], { icon }).addTo(layer)
      .bindPopup(`<b>${esc(p.title)}</b><br>${esc(p.function)}`);
  }
  if (pins.length) map.fitBounds(L.latLngBounds(pins.map(p => [p.lat, p.lon])).pad(0.2));
  setTimeout(() => map.invalidateSize(), 80);
}

function dashElections(c) {
  const wrap = el("div", "dash-elec");
  wrap.appendChild(el("div", "dash-h", "Elections"));
  const next = nextGeneralElection();
  wrap.appendChild(el("div", "dash-next",
    `Next general election <span class="dash-when">${next.toLocaleDateString("en-US",
      { month: "long", day: "numeric", year: "numeric" })}</span>`));
  if (!c.contests.length) {
    wrap.appendChild(el("div", "dash-nodata",
      `We don't have election results for ${esc(c.label)} yet — a gap on our end.`));
    return wrap;
  }
  const latest = c.contests.slice().sort((a, b) =>
    (b.year - a.year) || ((b.month || 0) - (a.month || 0)))[0];
  const winner = (latest.winner_names || []).join(", ") || "—";
  const stale = latest.year < new Date().getFullYear() - 2;
  wrap.appendChild(el("div", "dash-recent",
    `<div class="dash-rlabel">Most recent result we have</div>
     <div class="dash-rbody"><b>${esc(latest.office)} ${latest.year}</b>${latest.district ? " · " + esc(latest.district) : ""}<br>
     won by ${esc(winner)}</div>
     ${stale ? `<div class="dash-stale">This is the latest we hold — we don't have more recent results for this county yet.</div>` : ""}`));
  wrap.appendChild(modalLink(`view all ${c.contests.length} contests →`,
    `${c.label} — elections`, () => electionsSection(c.contests)));
  return wrap;
}

function selectCounty(key) {
  for (const c of document.querySelectorAll(".county"))
    c.classList.toggle("active", c.dataset.key === key);
  if (key) history.replaceState(null, "", `#${key}`);
}

function renderCounties(data) {
  const counties = buildCounties(data);
  const root = document.getElementById("regions");
  root.innerHTML = "";
  for (const c of counties) root.appendChild(countyDashboard(c, data));
  // The map is the navigator — open a county only when one is named in the
  // URL hash (a shared/bookmarked link); otherwise start on the map alone.
  const wanted = location.hash.slice(1);
  if (counties.find(c => c.key === wanted)) selectCounty(wanted);
  return counties;
}

// --- Coverage map: a US choropleth that scales where a button wall can't.
// Boundaries are the Census-derived us-atlas TopoJSON (vendored, not drawn
// here); d3-geo projects them. Click a state to see its counties, click a
// covered county to open its panel. Pure frontend — no onboarding on click.

// Map each place to its county FIPS. New onboarded sources can self-describe
// by writing meta.fips; this covers the current curated/onboarded set.
const FIPS_BY_PLACE = {
  pittsburgh: ["42003"],                                  // Allegheny County, PA
  la: ["06037"],                                          // Los Angeles County, CA
  loudoun: ["51107"], fairfax: ["51059"],
  princewilliam: ["51153"], stafford: ["51179"],          // Virginia counties
  newyork: ["36061", "36047", "36081", "36005", "36085"], // the five boroughs
};

const SVG_NS = "http://www.w3.org/2000/svg";
let _usTopo = null;  // cached TopoJSON

function coverageIndex(data) {
  // county FIPS -> placeKey, plus per-state covered counts
  const fipsToPlace = new Map();
  const allStores = { ...(data.sources || {}), ...(data.capital_projects || {}) };
  for (const [sourceId, store] of Object.entries(allStores)) {
    const key = placeKey(sourceId);
    const fips = (store.meta && store.meta.fips) || FIPS_BY_PLACE[key] || [];
    for (const f of fips) fipsToPlace.set(String(f), key);
  }
  const byState = new Map();
  for (const f of fipsToPlace.keys())
    byState.set(f.slice(0, 2), (byState.get(f.slice(0, 2)) || 0) + 1);
  return { fipsToPlace, byState };
}

function lerpColor(a, b, t) {
  const c = a.map((v, i) => Math.round(v + (b[i] - v) * t));
  return `rgb(${c[0]},${c[1]},${c[2]})`;
}
const UNCOVERED = [232, 224, 204], COVERED = [139, 26, 26];  // --aged -> --accent
function stateFill(count) {
  return count ? lerpColor(UNCOVERED, COVERED, Math.max(0.4, Math.min(count, 6) / 6))
    : lerpColor(UNCOVERED, [245, 240, 232], 0.5);
}

function tip(html, evt) {
  const t = document.getElementById("maptip");
  if (!html) { t.style.opacity = 0; return; }
  t.innerHTML = html;
  t.style.opacity = 1;
  t.style.left = (evt.clientX + 12) + "px";
  t.style.top = (evt.clientY + 12) + "px";
}

function svgPath(d, cls, fill) {
  const p = document.createElementNS(SVG_NS, "path");
  p.setAttribute("d", d);
  p.setAttribute("class", cls);
  p.setAttribute("fill", fill);
  return p;
}

function drawMapUS(cov) {
  const host = document.getElementById("usmap");
  const states = topojson.feature(_usTopo, _usTopo.objects.states);
  const counties = topojson.feature(_usTopo, _usTopo.objects.counties);
  const totalByState = new Map();
  for (const c of counties.features) {
    const s = String(c.id).slice(0, 2);
    totalByState.set(s, (totalByState.get(s) || 0) + 1);
  }
  const path = d3.geoPath(d3.geoAlbersUsa().fitSize([975, 610], states));
  const covStates = [...cov.byState.values()].reduce((a, b) => a + b, 0);
  host.innerHTML = `<div class="map-head">
    <span class="map-title">Coverage map</span>
    <span class="map-sub">${cov.fipsToPlace.size} localities · ${cov.byState.size} states — click a state to drill in</span>
    <span class="map-legend"><span class="legend-swatch" style="background:${stateFill(0)}"></span>none
      <span class="legend-swatch" style="background:${stateFill(2)}"></span>covered</span></div>`;
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 975 610");
  for (const f of states.features) {
    const d = path(f);
    if (!d) continue;  // territories outside geoAlbersUsa
    const count = cov.byState.get(String(f.id)) || 0;
    const el = svgPath(d, "geo-area clickable", stateFill(count));
    const total = totalByState.get(String(f.id)) || 0;
    el.addEventListener("mousemove", e => tip(
      `<b>${esc(f.properties.name)}</b> — ${count} of ${total} counties covered`, e));
    el.addEventListener("mouseleave", () => tip(null));
    el.addEventListener("click", () => { tip(null); drawMapState(f.id, f.properties.name, cov); });
    svg.appendChild(el);
  }
  host.appendChild(svg);
}

// Clicking an un-onboarded county on the coverage map opens an honest empty
// dashboard: the map centered on the county, and every panel saying plainly
// that the gap is on our end — not a dead end.
function openEmptyCounty(feature, stateName) {
  const key = "gap-" + feature.id;
  const centroid = d3.geoCentroid(feature);  // [lon, lat]
  const label = /city|town/i.test(feature.properties.name)
    ? `${feature.properties.name}, ${stateName}`
    : `${feature.properties.name} County, ${stateName}`;
  const state = CD_FIPS_USPS[String(feature.id).slice(0, 2)];
  const contests = electionsForPlace(null, feature.properties.name, state);
  const c = { key, label, kinds: new Set(contests.length ? ["elections"] : []),
              meetings: [], cip: null, contests, center: [centroid[1], centroid[0]] };
  const box = countyDashboard(c, { item_facts: {}, item_summaries: {} });
  const root = document.getElementById("regions");
  const existing = document.getElementById(`county-${key}`);
  if (existing) existing.replaceWith(box); else root.appendChild(box);
  selectCounty(key);
  box.scrollIntoView({ behavior: "smooth" });
}

function drawMapState(stateFips, stateName, cov) {
  const host = document.getElementById("usmap");
  const counties = topojson.feature(_usTopo, _usTopo.objects.counties)
    .features.filter(c => String(c.id).slice(0, 2) === String(stateFips));
  const fc = { type: "FeatureCollection", features: counties };
  const path = d3.geoPath(d3.geoAlbersUsa().fitSize([975, 610], fc));
  const coveredHere = counties.filter(c => cov.fipsToPlace.has(String(c.id))).length;
  host.innerHTML = `<div class="map-head">
    <button class="map-back">back to all states</button>
    <span class="map-title">${esc(stateName)}</span>
    <span class="map-sub">${coveredHere} of ${counties.length} covered — click a covered county to open it</span>
    <span class="map-legend"><span class="legend-swatch" style="background:${lerpColor(UNCOVERED, [245, 240, 232], 0.5)}"></span>none
      <span class="legend-swatch" style="background:${lerpColor(UNCOVERED, COVERED, 1)}"></span>covered</span></div>`;
  host.querySelector(".map-back").addEventListener("click", () => drawMapUS(cov));
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 975 610");
  for (const c of counties) {
    const d = path(c);
    if (!d) continue;
    const place = cov.fipsToPlace.get(String(c.id));
    const el = svgPath(d, "geo-area clickable",
      place ? lerpColor(UNCOVERED, COVERED, 1) : lerpColor(UNCOVERED, [245, 240, 232], 0.5));
    el.addEventListener("mousemove", e => tip(
      `<b>${esc(c.properties.name)}</b> — ${place ? "covered" : "nothing yet — click to see"}`, e));
    el.addEventListener("mouseleave", () => tip(null));
    el.addEventListener("click", () => {
      tip(null);
      if (place) {
        selectCounty(place);
        const box = document.getElementById(`county-${place}`);
        if (box) box.scrollIntoView({ behavior: "smooth" });
      } else {
        openEmptyCounty(c, stateName);  // clicked an un-onboarded county
      }
    });
    svg.appendChild(el);
  }
  host.appendChild(svg);
}

async function renderMap(data) {
  if (typeof d3 === "undefined" || typeof topojson === "undefined") return;  // assets absent
  try {
    if (!_usTopo)
      _usTopo = await (await fetch("/static/geo/counties-10m.json")).json();
    drawMapUS(coverageIndex(data));
  } catch (e) { /* map is a nav aid; never block the ledger on it */ }
}

async function init() {
  document.getElementById("searchform").addEventListener("submit", e => {
    e.preventDefault();
    const q = document.getElementById("searchbox").value;
    if (q.trim()) runSearch(q);
  });
  const resp = await fetch("/api/foundry/data");
  if (!resp.ok) {
    document.getElementById("loading").textContent =
      "The foundry store is empty — run foundry/backfill.py first.";
    return;
  }
  const data = await resp.json();
  UPCOMING = data.upcoming || {};
  DIGESTS = data.meeting_digests || {};
  renderCounties(data);
  document.getElementById("loading").remove();
  renderMap(data);  // async, non-blocking — the ledger renders without waiting on geo
}

init();
