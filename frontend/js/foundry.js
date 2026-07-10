// Foundry lab console: renders the quarantine-aware municipal store.
// The one rule that matters here: certified and uncertified records are
// never visually interchangeable — uncertified always carries a warning.

// machine-derived next-meeting lookups (upcoming.py), keyed by source id —
// advisory display metadata, never part of the certified record
let UPCOMING = {};

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

function voteRow(vote, items, summaries, meetingDate, factsByMeeting) {
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
  const note = vote.certification && vote.certification.note
    ? `<div class="dispnote">${esc(vote.certification.note)}</div>` : "";
  const inconsistent = vote.tally_consistent === false
    ? '<div class="dispnote">parser flagged: derived positions do not reproduce the reported tally</div>' : "";
  return `<tr>${label}
    <td class="tallycell">${tally(vote.counts || {})}</td>
    <td class="tallycell result-${esc(vote.result)}">${esc((vote.result || "?").toUpperCase())}</td>
    <td>${badge(vote)}${note}${inconsistent}</td></tr>`;
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
  const table = el("table");
  table.innerHTML = `<tr><th>What was voted on</th><th>Tally</th><th>Result</th><th>Trust</th></tr>` +
    votes.map(v => voteRow(v, items, summaries, meeting.date, factsByMeeting)).join("");
  details.appendChild(table);
  if (meeting.source_url)
    details.appendChild(el("div", "note",
      `&nbsp;source: <a href="${esc(meeting.source_url)}" rel="noopener">official record</a>`));
  return details;
}

function regionSection(sourceId, store, itemFacts, summaries) {
  // display metadata: curated entry, else whatever the store carries
  // (auto-onboarded sources write their own meta), else the raw id
  const meta = REGIONS[sourceId] || store.meta || { title: sourceId, sub: "" };
  const section = el("section", "region");
  section.appendChild(el("h2", null,
    `${esc(meta.title)} <span class="tag" style="display:block">${esc(meta.sub)}</span>`));

  const next = (UPCOMING[sourceId] || {}).next;
  if (next) {
    const when = new Date(next.date + "T12:00:00").toLocaleDateString("en-US",
      { weekday: "long", month: "long", day: "numeric" });
    section.appendChild(el("div", "nextmeet",
      `<div class="nm-label">Next meeting <span class="badge derived">machine-derived</span></div>
       <div class="nm-when">${esc(when)}${next.time ? " · " + esc(next.time) : ""}</div>
       <div class="nm-body">${esc(next.body)}</div>`));
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
  // instant path: a region we already display
  for (const [sourceId, meta] of Object.entries(REGIONS)) {
    if (meta.title.toLowerCase().includes(q)) {
      log.textContent = "";
      const target = document.getElementById(`region-${sourceId}`);
      if (target) { target.scrollIntoView({ behavior: "smooth" }); return; }
    }
  }
  log.innerHTML = `<div class="jl-head">Running the pipeline for “${esc(query)}”…</div>`;
  const start = await fetch("/api/foundry/onboard", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: query }) });
  const { job_id } = await start.json();
  const timer = setInterval(async () => {
    const job = await (await fetch(`/api/foundry/onboard/${job_id}`)).json();
    const p = job.progress || { pct: 0, stage: job.status };
    log.innerHTML = `<div class="jl-head">Pipeline: ${esc(query)} — ${esc(p.stage)}</div>
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
      // records were merged into the store — pull fresh data and render
      const data = await (await fetch("/api/foundry/data")).json();
      const store = data.sources[result.source_id];
      if (!store) return;
      REGIONS[result.source_id] = store.meta || { title: result.source_id, sub: "" };
      const section = regionSection(result.source_id, store,
        data.item_facts, data.item_summaries || {});
      section.id = `region-${result.source_id}`;
      const existing = document.getElementById(`region-${result.source_id}`);
      const root = document.getElementById("regions");
      if (existing) existing.replaceWith(section);
      else root.insertBefore(section, root.firstChild);
      section.scrollIntoView({ behavior: "smooth" });
      return;
    }
    if (result.profile) {
      const section = profileSection(result);
      const root = document.getElementById("regions");
      root.insertBefore(section, root.firstChild);
      section.scrollIntoView({ behavior: "smooth" });
      return;
    }
    REGIONS[result.source_id] = { title: result.title, sub: result.sub };
    const section = regionSection(result.source_id, toStoreShape(result.records), {}, {});
    section.id = `region-${result.source_id}`;
    const root = document.getElementById("regions");
    root.insertBefore(section, root.firstChild);
    section.scrollIntoView({ behavior: "smooth" });
  }, 1500);
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
  const root = document.getElementById("regions");
  // curated regions first, then anything else the store has (auto-onboarded)
  const order = [...Object.keys(REGIONS).filter(s => data.sources[s]),
                 ...Object.keys(data.sources).filter(s => !REGIONS[s])];
  for (const sourceId of order) {
    const store = data.sources[sourceId];
    if (!REGIONS[sourceId])
      REGIONS[sourceId] = store.meta || { title: sourceId, sub: "" };
    const section = regionSection(sourceId, store,
      data.item_facts, data.item_summaries || {});
    section.id = `region-${sourceId}`;
    root.appendChild(section);
  }
  document.getElementById("loading").remove();
}

init();
