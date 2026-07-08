// ── Lobbying tab ──
// Self-contained: talks to /lobbying/search and /lobbying/entity (Senate LDA),
// renders an entity search and a per-entity spend/issue/lobbyist profile.
// Loaded alongside index.js; exposes its handlers as globals for inline onclick.

let _lobbyDebounce = null;
let _lobbyLastQuery = '';

function _lobbyEsc(s) {
  return String(s == null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function _lobbyMoney(n) {
  n = Number(n) || 0;
  if (n >= 1e9) return '$' + (n / 1e9).toFixed(2).replace(/\.00$/, '') + 'B';
  if (n >= 1e6) return '$' + (n / 1e6).toFixed(2).replace(/\.00$/, '') + 'M';
  if (n >= 1e3) return '$' + Math.round(n / 1e3) + 'K';
  return '$' + Math.round(n);
}

// "2025 Q1" → "Q1 '25"
function _lobbyQ(label) {
  const m = /^(\d{4})\s+(\S+)$/.exec(label || '');
  return m ? m[2] + " '" + m[1].slice(2) : (label || '');
}

function loadLobbying() {
  const input = document.getElementById('lobby-search-input');
  if (input) setTimeout(() => input.focus(), 100);
}

function lobbyExample(name) {
  const input = document.getElementById('lobby-search-input');
  if (input) input.value = name;
  lobbySearch(name);
}

function lobbyOnInput() {
  const q = (document.getElementById('lobby-search-input').value || '').trim();
  clearTimeout(_lobbyDebounce);
  if (q.length < 2) return;
  _lobbyDebounce = setTimeout(() => lobbySearch(q), 350);
}

function lobbySearchNow() {
  clearTimeout(_lobbyDebounce);
  const q = (document.getElementById('lobby-search-input').value || '').trim();
  if (q.length >= 2) lobbySearch(q);
}

async function lobbySearch(q) {
  _lobbyLastQuery = q;
  document.getElementById('lobby-profile').style.display = 'none';
  document.getElementById('lobby-error').style.display = 'none';
  const results = document.getElementById('lobby-results');
  const loading = document.getElementById('lobby-loading');
  results.innerHTML = '';
  loading.style.display = 'block';

  try {
    const resp = await fetch('/lobbying/search?q=' + encodeURIComponent(q));
    if (!resp.ok) throw new Error('Error ' + resp.status);
    const data = await resp.json();
    if (q !== _lobbyLastQuery) return; // superseded by a newer keystroke
    loading.style.display = 'none';
    renderLobbyResults(data.results || []);
  } catch (err) {
    loading.style.display = 'none';
    const e = document.getElementById('lobby-error');
    e.textContent = 'Could not reach the lobbying data source. Try again in a moment.';
    e.style.display = 'block';
  }
}

function renderLobbyResults(results) {
  const box = document.getElementById('lobby-results');
  if (!results.length) {
    box.innerHTML = '<div class="lobby-empty">No registrants or clients match that name.</div>';
    return;
  }
  const rows = results.map((r, i) => {
    const kindLabel = r.kind === 'registrant' ? 'Lobbying firm' : 'Client';
    const loc = r.state ? _lobbyEsc(r.state) : '—';
    const nameArg = JSON.stringify(r.name).replace(/"/g, '&quot;');
    return `<a class="lobby-trow" onclick="openLobbyEntity('${r.kind}', ${nameArg})">
      <span class="lobby-rank">${String(i + 1).padStart(2, '0')}</span>
      <span class="lobby-entity"><strong>${_lobbyEsc(r.name)}</strong></span>
      <span class="lobby-sector">${kindLabel}</span>
      <span class="lobby-loc">${loc}</span>
    </a>`;
  }).join('');
  box.innerHTML = `
    <section class="bill-section">
      <div class="bill-section-label">Matching entities</div>
      <div class="lobby-table lobby-results">
        <div class="lobby-thead"><span>#</span><span>Entity</span><span>Type</span><span>Location</span></div>
        ${rows}
      </div>
      <p class="poll-summary">Click an entity for its lobbying profile. Registrants are the lobbying firms; clients are the organizations that hire them.</p>
    </section>`;
}

async function openLobbyEntity(kind, name) {
  document.getElementById('lobby-results').innerHTML = '';
  document.getElementById('lobby-error').style.display = 'none';
  const loading = document.getElementById('lobby-loading');
  const profile = document.getElementById('lobby-profile');
  profile.style.display = 'none';
  loading.style.display = 'block';

  try {
    const resp = await fetch('/lobbying/entity?kind=' + encodeURIComponent(kind) + '&name=' + encodeURIComponent(name));
    if (!resp.ok) throw new Error('Error ' + resp.status);
    const p = await resp.json();
    loading.style.display = 'none';
    renderLobbyProfile(p);
  } catch (err) {
    loading.style.display = 'none';
    const e = document.getElementById('lobby-error');
    e.textContent = 'Could not load this entity. Try again in a moment.';
    e.style.display = 'block';
  }
}

function lobbyBackToResults() {
  document.getElementById('lobby-profile').style.display = 'none';
  if (_lobbyLastQuery) lobbySearch(_lobbyLastQuery);
}

// Entry point from a bill's "Who's pushing this" panel: switch to the Lobbying
// tab and open the entity's profile.
function openLobbyFromBill(kind, name) {
  if (typeof showPage === 'function') showPage('page-lobbying');
  document.getElementById('lobby-results').innerHTML = '';
  openLobbyEntity(kind, name);
}

// Open a bill named in a filing. Congress is inferred from the filing year, so
// the occasional reference to a prior-Congress bill may 404 — openDetail shows
// its own retry card in that case. Back returns to this lobbying profile.
function lobbyOpenBill(congress, type, number) {
  if (typeof openDetail === 'function') {
    openDetail({ congress: congress, type: type, number: number });
  }
}

function renderLobbyProfile(p) {
  const box = document.getElementById('lobby-profile');
  const kindLabel = p.kind === 'registrant' ? 'Lobbying firm' : 'Client';
  const yrs = (p.years || []).slice().sort();
  const yrLabel = yrs.length ? yrs[0] + '–' + yrs[yrs.length - 1] : '';
  const cpLabel = p.kind === 'client' ? 'Firms hired' : 'Clients represented';

  // Spend-over-time chart (quarterly bars).
  const q = p.by_quarter || [];
  const maxQ = Math.max(1, ...q.map(x => x.spend));
  const trendCols = q.map(x => `
    <div class="trend-col" title="${_lobbyEsc(x.quarter)}: ${_lobbyMoney(x.spend)}">
      <div class="trend-val">${x.spend ? _lobbyMoney(x.spend) : ''}</div>
      <div class="trend-bar" style="height:${Math.max(3, (x.spend / maxQ) * 100)}%"></div>
      <div class="trend-x">${_lobbyEsc(_lobbyQ(x.quarter))}</div>
    </div>`).join('');

  // Issue-area card (bars scaled by filing count).
  const maxI = Math.max(1, ...(p.issues || []).map(i => i.count));
  const issueRows = (p.issues || []).map(i => `
    <div class="lobby-card-row">
      <span>${_lobbyEsc(i.display)}</span>
      <div class="lobby-bar"><div class="lobby-fill" style="width:${Math.max(6, (i.count / maxI) * 100)}%"></div></div>
      <span>${i.count}</span>
    </div>`).join('') || '<div class="lobby-empty-note">—</div>';

  // Counterparties card (bars scaled by dollars).
  const maxC = Math.max(1, ...(p.counterparties || []).map(c => c.value));
  const cpRows = (p.counterparties || []).map(c => `
    <div class="lobby-card-row">
      <span>${_lobbyEsc(c.name)}</span>
      <div class="lobby-bar"><div class="lobby-fill" style="width:${Math.max(6, (c.value / maxC) * 100)}%"></div></div>
      <span>${_lobbyMoney(c.value)}</span>
    </div>`).join('') || '<div class="lobby-empty-note">—</div>';

  // Bills lobbied — clickable through to the bill detail page.
  const billRows = (p.bills_lobbied || []).map(b => `
    <div class="lobby-bill-row" onclick="lobbyOpenBill(${b.congress}, '${b.type}', ${b.number})">
      <span class="lobby-bill-id">${_lobbyEsc(b.display)}</span>
      <span class="lobby-sector">${b.congress}th Congress</span>
      <span class="lobby-bill-note">${b.count} filing${b.count > 1 ? 's' : ''} →</span>
    </div>`).join('') || '<div class="lobby-empty-note" style="padding:0.75rem 0">No specific bills named in these filings.</div>';

  const lobbyists = (p.lobbyists || []).map(l => `<span class="lobby-person">${_lobbyEsc(l)}</span>`).join('')
    || '<span class="lobby-empty-note">—</span>';

  box.innerHTML = `
    <a class="back-link" href="#" onclick="lobbyBackToResults();return false">← Back to results</a>

    <header class="bill-header">
      <div class="bill-id-row">
        <span class="bill-id">Entity</span>
        <span class="lobby-kind-tag lobby-kind-${p.kind}">${kindLabel}</span>
        <span class="lede-stage stage-passed">Active filer</span>
      </div>
      <h1 class="bill-title">${_lobbyEsc(p.name)}</h1>
      <div class="bill-meta-row">
        <span>${_lobbyMoney(p.total_spend)} reported · ${_lobbyEsc(yrLabel)}</span><span class="dot">·</span>
        <span>${p.filing_count} filings</span><span class="dot">·</span>
        <span>${(p.issues || []).length} issue areas</span><span class="dot">·</span>
        <span>${(p.bills_lobbied || []).length} bills named</span>
      </div>
      <div class="lede-actions">
        <a class="btn-ghost" href="https://lda.senate.gov/filings/public/filing/search/" target="_blank" rel="noopener">Senate LDA filings ↗</a>
      </div>
    </header>

    <section class="bill-section">
      <div class="bill-section-label">Spend over time</div>
      <div class="poll-trend">
        <div class="poll-trend-head">
          <span class="poll-trend-title">Reported lobbying spend by quarter</span>
          <span class="poll-trend-key"><span class="trend-dot"></span> $ reported</span>
        </div>
        <div class="poll-trend-chart" style="grid-template-columns:repeat(${Math.max(1, q.length)},1fr)">${trendCols}</div>
        <p class="poll-trend-note">Bars show income (fees paid to lobbying firms) or expenses (in-house spend) reported on each quarterly filing.</p>
      </div>
    </section>

    <section class="bill-section">
      <div class="bill-section-label">Where the effort goes</div>
      <div class="lobby-grid">
        <div class="lobby-card">
          <div class="lobby-card-label">Issue areas lobbied</div>
          <div class="lobby-card-rows">${issueRows}</div>
        </div>
        <div class="lobby-card">
          <div class="lobby-card-label">${cpLabel}</div>
          <div class="lobby-card-rows">${cpRows}</div>
        </div>
      </div>
    </section>

    <section class="bill-section">
      <div class="bill-section-label">Bills lobbied</div>
      <div class="lobby-bills-list">${billRows}</div>
      <p class="poll-summary">Bills are parsed from the filings' free-text activity descriptions; Congress is inferred from the filing year. Click any bill to read it in plain English.</p>
    </section>

    <section class="bill-section">
      <div class="bill-section-label">Lobbyists registered to act</div>
      <div class="lobby-people">${lobbyists}</div>
      <p class="poll-summary">Prior government roles and revolving-door history require the OpenSecrets normalization layer — a planned addition.</p>
    </section>

    <section class="bill-section">
      <div class="bill-section-label">Sources &amp; methodology</div>
      <p class="poll-summary" style="margin-top:0;border-top:none;padding-top:0">
        Filings: Senate Office of Public Records LD-2 quarterly disclosures (lda.senate.gov). Spend is self-reported;
        totals cover the entity name shown and may not merge affiliated registrations. Contribution and revolving-door
        data (FEC · OpenSecrets) are planned. Figures appear with up to a 45-day lag from end of quarter.
      </p>
    </section>
  `;
  box.style.display = 'block';
  box.scrollIntoView({ behavior: 'smooth', block: 'start' });
}
