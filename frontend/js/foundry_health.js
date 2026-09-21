// Foundry scraper-health console. Reads /admin/foundry/health and renders one
// row per store: how fresh it is, how much of it is certified, why the rest
// is not, and what the last run of each pipeline stage did.
//
// The distinction this page exists to make: a source can be uncertified
// because WE never wired an oracle, or because the clerk has not published
// the minutes yet. Those are different problems and the UI must not blur
// them — see the quarantine-reason breakdown.
//
// Status has three levels, not a traffic light: ink means fine, muted means
// absent or not applicable, accent means broken and worth your attention.
// The house palette has no green and no amber, and colouring nine rows red
// because they are merely uncertified would make the accent decorative.

const SECRET = new URLSearchParams(window.location.search).get('secret') || '';
const q = (url) => `${url}${url.includes('?') ? '&' : '?'}secret=${encodeURIComponent(SECRET)}`;
const api = (url, opts = {}) => fetch(q(url), opts);

const el = (tag, cls, html) => {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (html !== undefined) n.innerHTML = html;
  return n;
};
const esc = (s) => String(s === null || s === undefined ? '' : s)
  .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  .replace(/"/g, '&quot;');
const usd = (n) => `$${Number(n || 0).toFixed(2)}`;

// Verdicts that mean something is actually wrong and a person should look.
const BAD = new Set(['error', 'failed', 'drift', 'gate-failed', 'attempt-failed',
                     'rejected', 'stalled', 'blocked', 'stale',
                     'promoted-drifted', 'no-second-source']);
// Verdicts that mean the stage did its job.
const GOOD = new Set(['ok', 'certified', 'done', 'attempt-passed', 'fresh',
                      'promoted', 'curated']);

const tag = (text) => {
  const level = BAD.has(text) ? ' bad' : GOOD.has(text) ? ' good' : '';
  return `<span class="tag${level}">${esc(text)}</span>`;
};

function ago(ts) {
  if (!ts) return '—';
  const then = new Date(ts.length <= 10 ? `${ts}T00:00:00` : ts);
  const days = Math.floor((Date.now() - then.getTime()) / 86400000);
  if (Number.isNaN(days)) return ts;
  if (days <= 0) return 'today';
  if (days === 1) return 'yesterday';
  if (days < 60) return `${days} days ago`;
  return `${Math.floor(days / 30)} months ago`;
}

const expanded = new Set();
let DATA = null;

// ── summary figures ────────────────────────────────────────────────────────
function renderStrip(data) {
  const strip = document.getElementById('strip');
  strip.innerHTML = '';
  strip.appendChild(el('div', 'section-label', 'The fleet'));
  const stats = el('div', 'stats');
  const stat = (value, label, sub) => {
    const s = el('div', 'stat');
    s.appendChild(el('div', 'n', value));
    s.appendChild(el('div', 'l', esc(label)));
    if (sub) s.appendChild(el('div', 's', sub));
    stats.appendChild(s);
  };

  const rows = data.sources;
  const totalRecords = rows.reduce((a, r) => a + r.total_records, 0);
  const totalCert = rows.reduce((a, r) => a + r.total_certified, 0);
  const pct = totalRecords ? Math.round(1000 * totalCert / totalRecords) / 10 : 0;
  const meetingSources = rows.filter((r) => r.staleness !== 'n/a');
  const withOracle = meetingSources.filter((r) => r.oracle.artifact
                                          || r.oracle.status === 'curated').length;
  const unhealthy = meetingSources.filter(
    (r) => r.staleness === 'stale' || r.staleness === 'due').length;

  stat(`${totalCert.toLocaleString()} <small>of ${totalRecords.toLocaleString()}</small>`,
       'Records certified', `${pct}% of the fleet`);
  stat(String(withOracle) + ` <small>of ${meetingSources.length}</small>`,
       'Sources with an oracle', 'the rest cannot certify anything');
  stat(String(unhealthy), 'Sources needing a refresh',
       `${meetingSources.length} meeting sources tracked`);

  const run = data.last_run;
  stat(run ? ago(run.ts) : '—', 'Last refresh cycle',
       run ? `${esc(run.host)} · ${esc(run.argv || 'full cycle')}` : 'none recorded');

  const b = data.budget;
  stat(usd(b.today_usd), 'Spend today',
       b.ledger_present ? `cap ${usd(b.cap_usd)} · ${usd(b.month_usd)} this month`
                        : 'no ledger on this host');

  if (data.ci && data.ci.available && data.ci.runs.length) {
    const last = data.ci.runs[0];
    const bad = data.ci.runs.filter((r) => r.conclusion && r.conclusion !== 'success').length;
    stat(tag(last.conclusion === 'success' ? 'ok' : (last.conclusion || last.status)),
         'Scheduled CI refresh',
         `${ago(last.createdAt)} · ${bad} of ${data.ci.runs.length} recent runs failed`);
  } else {
    stat('<small>unknown</small>', 'Scheduled CI refresh', 'gh not available here');
  }
  strip.appendChild(stats);
}

// ── one source row ─────────────────────────────────────────────────────────
function certBar(r) {
  const total = r.total_records || 1;
  const reasons = r.quarantine_reasons || {};
  const seg = (n, cls) => (n ? `<i class="${cls}" style="width:${100 * n / total}%"></i>` : '');
  return `<div class="bar">${seg(r.total_certified, 'cert')}`
       + `${seg(reasons.disputed, 'disp')}${seg(reasons.lag, 'lag')}`
       + `${seg((reasons.uncertifiable || 0) + (reasons.unreached || 0), 'unc')}</div>`;
}

function reasonText(r) {
  const reasons = r.quarantine_reasons || {};
  const parts = [];
  if (reasons.disputed) parts.push(`${reasons.disputed} disputed`);
  if (reasons.lag) parts.push(`${reasons.lag} awaiting the clerk`);
  if (reasons.uncertifiable) parts.push(`${reasons.uncertifiable} no oracle`);
  if (reasons.unreached) parts.push(`${reasons.unreached} not affirmed`);
  return parts.join(' · ') || 'all certified';
}

function verdictOf(event) {
  if (!event) return '<span class="meta">never run</span>';
  const rep = event.repeats > 1 ? ` <span class="meta">&times;${event.repeats}</span>` : '';
  return `${tag(event.verdict)}${rep}<div class="meta">${esc(ago(event.ts))}</div>`;
}

function renderFindings(findings, total) {
  if (!findings || !findings.length) return '';
  const extra = total && total > findings.length
    ? `<div class="meta">and ${total - findings.length} more</div>` : '';
  return findings.map((f) =>
    `<div class="finding"><b>${esc(f.check)}</b>`
    + `${f.ref ? `<span class="code">${esc(f.ref)}</span><br>` : ''}`
    + `${esc(f.msg)}</div>`).join('') + extra;
}

function detailPanel(r) {
  const td = el('td');
  td.colSpan = 8;
  const inner = el('div', 'detail-inner');

  // What is blocking certification, in words.
  const why = el('div');
  why.appendChild(el('div', 'section-label', 'Certification'));
  const o = r.oracle;
  let text;
  if (o.status === 'curated') {
    text = 'Certified by a hand-built oracle that predates the synthesis path. '
         + 'It has no promoted artifact, so certification rides its curated '
         + 'refresh rather than the recertify pass.';
  } else if (o.status === 'no-second-source') {
    const d = (o.last_attempt || {}).detail || {};
    text = `No usable second source. ${esc(d.reason || '')} Synthesis is not the `
         + 'blocker here, so spending attempts on it would certify nothing. This '
         + 'needs a different document, not a better extractor.';
  } else if (!o.artifact && o.status === 'never-run') {
    text = 'No oracle has ever been synthesized for this source, so every record '
         + 'is ingest-only until a second source affirms it.';
  } else if (!o.artifact && o.status === 'failed') {
    const d = (o.last_attempt || {}).detail || {};
    text = 'Oracle synthesis has been attempted and did not clear the 60% '
         + `agreement gate${d.rate != null ? ` (best rate ${Math.round(d.rate * 100)}%)` : ''}. `
         + 'The attempt findings are committed beside the artifact.';
  } else {
    const d = (o.last_recertify || {}).detail || {};
    text = `Oracle <code>${esc(o.artifact)}</code> is promoted. `
         + (d.covered_meetings !== undefined
            ? `The last pass covered ${d.covered_meetings} of ${d.store_meetings} stored meetings.`
            : 'It has not been re-run since promotion.');
    if ((r.quarantine_reasons || {}).lag) {
      text += ` ${r.quarantine_reasons.lag} records wait on documents the `
            + 'jurisdiction has not published yet. That is our gap to report, not a failure.';
    }
  }
  why.appendChild(el('div', 'prose', text));

  const actions = el('div', 'actions');
  const mk = (label, action, spend) => {
    const b = el('button', 'btn-ghost', label);
    b.disabled = r.busy || (spend && !DATA.gates.llm_open);
    b.onclick = (ev) => { ev.stopPropagation(); runAction(r.source_id, action, b); };
    return b;
  };
  if (r.staleness !== 'n/a') {
    actions.appendChild(mk('Refresh now', 'refresh'));
    if (o.artifact) actions.appendChild(mk('Recertify', 'recertify'));
    actions.appendChild(mk('Synthesize oracle', 'oracle', true));
  }
  why.appendChild(actions);
  if (r.staleness !== 'n/a' && !DATA.gates.llm_open) {
    why.appendChild(el('div', 'note',
      'Oracle synthesis spends Opus and is disabled here. Run it from localhost '
      + 'or set FOUNDRY_ONBOARD=on.'));
  }
  if (!DATA.gates.local) {
    why.appendChild(el('div', 'note',
      'Not localhost: this host serves a committed store, so any write lasts '
      + 'only until the next deploy.'));
  }
  const log = el('div', 'joblog hidden');
  log.id = `log-${r.source_id}`;
  why.appendChild(log);
  inner.appendChild(why);

  // Open findings.
  if (r.open_findings && r.open_findings.length) {
    const f = el('div');
    f.appendChild(el('div', 'section-label', 'Findings blocking the merge'));
    f.appendChild(el('div', '', renderFindings(
      r.open_findings, (r.last_refresh.detail || {}).findings_total)));
    inner.appendChild(f);
  }

  // Event history.
  const hist = el('div');
  hist.appendChild(el('div', 'section-label', 'Recent pipeline events'));
  if (!r.events.length) {
    hist.appendChild(el('div', 'prose',
      'Nothing recorded yet. Events appear once a refresh, deepen, oracle or '
      + 'recertify run touches this source.'));
  }
  [...r.events].reverse().forEach((e) => {
    const d = e.detail || {};
    const node = el('div', `event${BAD.has(e.verdict) ? ' bad' : ''}`);
    node.appendChild(el('div', 'event-head',
      `<span class="event-stage">${esc(e.stage)}</span>${tag(e.verdict)}`
      + `<span class="meta">${esc(ago(e.ts))}`
      + `${e.repeats > 1 ? ` · ${e.repeats} runs since ${esc(e.first_ts.slice(0, 10))}` : ''}</span>`));
    const bits = Object.entries(d)
      .filter(([k]) => k !== 'findings' && k !== 'error' && k !== 'reason' && k !== 'next')
      .map(([k, v]) => `${k} ${typeof v === 'object' ? JSON.stringify(v) : v}`);
    if (d.reason) node.appendChild(el('div', 'prose', esc(d.reason)));
    if (d.next) node.appendChild(el('div', 'prose', `Next: ${esc(d.next)}`));
    if (bits.length) node.appendChild(el('div', 'meta', esc(bits.join('   '))));
    if (d.error) node.appendChild(el('div', 'finding', esc(d.error)));
    if (d.findings) node.appendChild(el('div', '', renderFindings(d.findings, d.findings_total)));
    hist.appendChild(node);
  });
  inner.appendChild(hist);

  td.appendChild(inner);
  const tr = el('tr', 'detail');
  tr.appendChild(td);
  return tr;
}

function renderTable(rows, container) {
  const table = el('table');
  table.innerHTML = `<thead><tr>
    <th>Source</th><th class="hide-sm">Platform</th><th>Freshness</th>
    <th class="num">Records</th><th>Certified</th><th>Oracle</th>
    <th>Last refresh</th><th class="hide-sm">Last deepen</th>
  </tr></thead>`;
  const tbody = el('tbody');
  rows.forEach((r) => {
    const tr = el('tr', 'row');
    tr.innerHTML = `
      <td><div class="name">${esc(r.title)}</div>
          <div class="code">${esc(r.source_id)}</div>
          ${r.busy ? '<div class="meta">running</div>' : ''}</td>
      <td class="hide-sm"><span class="meta">${esc(r.platform || '—')}</span></td>
      <td>${r.staleness === 'n/a' ? '<span class="meta">—</span>' : tag(r.staleness)}
          <div class="meta">${r.newest_meeting ? `newest ${esc(r.newest_meeting)}` : ''}${
            r.next_expected ? `<br>next ${esc(r.next_expected)}` : ''}</div></td>
      <td class="num"><span class="figure">${r.total_records.toLocaleString()}</span>
          <div class="meta">${r.records.meetings} mtg / ${r.records.vote_events} votes</div></td>
      <td><span class="figure">${r.certified_pct}%</span>${certBar(r)}
          <div class="meta">${esc(reasonText(r))}</div></td>
      <td>${tag(r.oracle.status)}</td>
      <td>${verdictOf(r.last_refresh)}</td>
      <td class="hide-sm">${verdictOf(r.last_deepen)}</td>`;
    tbody.appendChild(tr);

    if (expanded.has(r.source_id)) tbody.appendChild(detailPanel(r));
    tr.onclick = () => {
      if (expanded.has(r.source_id)) expanded.delete(r.source_id);
      else expanded.add(r.source_id);
      render();
    };
  });
  table.appendChild(tbody);
  container.appendChild(table);
}

function render() {
  const data = DATA;
  document.getElementById('generated').textContent =
    `As of ${data.generated_at.replace('T', ' ')}`;
  renderStrip(data);

  const root = document.getElementById('content');
  root.innerHTML = '';

  const meetings = data.sources.filter((r) => r.staleness !== 'n/a');
  const others = data.sources.filter((r) => r.staleness === 'n/a');

  root.appendChild(el('div', 'section-label', 'Meeting sources'));
  if (meetings.length) renderTable(meetings, root);
  else root.appendChild(el('div', 'empty', 'No meeting stores found.'));

  if (others.length) {
    root.appendChild(el('div', 'section-label', 'Public works and elections'));
    renderTable(others, root);
  }

  if (data.orphan_extractors && data.orphan_extractors.length) {
    root.appendChild(el('div', 'section-label', 'Onboarding attempts that never landed'));
    const list = el('div', 'prose');
    list.innerHTML = data.orphan_extractors.map((o) =>
      `<span class="code">${esc(o.source_id)}</span> — ${o.attempts} attempt`
      + `${o.attempts === 1 ? '' : 's'}`
      + `${o.last_attempt ? `, last ${esc(o.last_attempt)}` : ', no artifact on disk'}`
    ).join('<br>');
    root.appendChild(list);
    root.appendChild(el('div', 'note',
      'These have extractor directories but no store: synthesis never cleared the gate.'));
  }
}

// ── actions ────────────────────────────────────────────────────────────────
async function runAction(sourceId, action, button) {
  const logBox = document.getElementById(`log-${sourceId}`);
  button.disabled = true;
  if (logBox) { logBox.classList.remove('hidden'); logBox.textContent = `starting ${action}`; }
  let started;
  try {
    const resp = await api(`/admin/foundry/${action}/${sourceId}`, { method: 'POST' });
    started = await resp.json();
    if (!resp.ok) throw new Error(started.detail || resp.statusText);
  } catch (err) {
    if (logBox) logBox.textContent = err.message;
    button.disabled = false;
    return;
  }
  if (started.ephemeral && logBox) {
    logBox.textContent = 'This host serves a committed store, so the result lasts '
      + 'until the next deploy.\n';
  }
  poll(started.job_id, logBox);
}

async function poll(jobId, logBox) {
  const resp = await api(`/admin/foundry/jobs/${jobId}`);
  const job = await resp.json();
  if (logBox) {
    logBox.textContent = (job.log || []).join('\n') || `${job.status}`;
    logBox.scrollTop = logBox.scrollHeight;
  }
  if (job.status === 'running') setTimeout(() => poll(jobId, logBox), 2000);
  else setTimeout(load, 500);
}

// ── boot ───────────────────────────────────────────────────────────────────
async function load() {
  try {
    const resp = await api('/admin/foundry/health');
    if (!resp.ok) {
      const body = await resp.json().catch(() => ({}));
      document.getElementById('content').innerHTML =
        `<div class="empty">${esc(body.detail || `${resp.status} ${resp.statusText}`)}</div>`;
      return;
    }
    DATA = await resp.json();
    render();
  } catch (err) {
    document.getElementById('content').innerHTML =
      `<div class="empty">${esc(err.message)}</div>`;
  }
}

document.getElementById('reload').onclick = load;
load();
