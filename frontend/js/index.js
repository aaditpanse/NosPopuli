// ── State ──
let currentResults = [];
let currentSearchContext = {};
let _searchState = { question: '', maxResults: 10, endpoint: '/search', isState: false, fullHistory: false, beforeCongress: null };
let previousPage = 'page-home';
let currentJurisdiction = 'federal';
let currentStateCode = null;
let _feedItems = [];
let _feedExpanded = false;
const FEED_COLLAPSED_COUNT = 5;
let _trackedElections = new Set(JSON.parse(localStorage.getItem('np_tracked_elections') || '[]'));
const tooltip = document.getElementById('tooltip');

// ── HTML escape (use for any user-influenced string injected via innerHTML) ──
function escapeHtml(s) {
  if (s == null) return '';
  return String(s).replace(/[&<>"']/g, c => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[c]));
}
window.escapeHtml = escapeHtml;

// ── localStorage keys ──
const PREFS_KEY = 'np_preferences';

function getPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || null; }
  catch { return null; }
}

function savePrefs(prefs) {
  localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));
}

// ── Pages ──
function showPage(id) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.getElementById(id).classList.add('active');
  window.scrollTo({ top: 0, behavior: 'smooth' });
}

function goHome() {
  showPage('page-home');
  document.getElementById('results-section').innerHTML = '';
  clearStatus();
  if (typeof setCurrentBillContext === 'function') setCurrentBillContext(null);
}

function goBack() {
  showPage(previousPage);
}

// ── Search ──
const input = document.getElementById('search-input');
const btn = document.getElementById('search-btn');
const statusBar = document.getElementById('status-bar');
const statusInner = document.getElementById('status-inner');
const resultsSection = document.getElementById('results-section');

input.addEventListener('keydown', e => { if (e.key === 'Enter') runSearch(); });
function setQuery(q) { input.value = q; input.focus(); }

function setStatus(msg) {
  statusBar.classList.add('visible');
  const line = document.createElement('div');
  line.className = 'status-step';
  line.textContent = '› ' + msg;
  statusInner.appendChild(line);
  requestAnimationFrame(() => line.classList.add('visible'));
}
function clearStatus() {
  statusInner.innerHTML = '';
  statusBar.classList.remove('visible');
}

function billIdFromParts(type, number) {
  return (type || '').toUpperCase() + ' ' + number;
}

function formatCongress(n) {
  if (!n) return '';
  const s = n % 100;
  if (s >= 11 && s <= 13) return `${n}th`;
  switch (n % 10) {
    case 1: return `${n}st`;
    case 2: return `${n}nd`;
    case 3: return `${n}rd`;
    default: return `${n}th`;
  }
}

function formatTimelineDate(dateStr) {
  if (!dateStr) return { day: '', year: '' };
  const months = ['Jan','Feb','Mar','Apr','May','Jun',
                  'Jul','Aug','Sep','Oct','Nov','Dec'];
  const parts = dateStr.split('-');
  if (parts.length < 2) return { day: '', year: dateStr };
  const year = parts[0];
  const month = months[parseInt(parts[1]) - 1] || '';
  const day = parts[2] ? parseInt(parts[2]) : '';
  return { day: `${month} ${day}`, year };
}

function chamberLabel(event) {
  const c = (event.chamber || '').toLowerCase();
  const t = event.event_type;
  if (t === 'signed') return 'President · Signed into Law';
  if (t === 'vetoed') return 'President · Vetoed';
  if (t === 'conference') return 'Conference Committee';
  if (c === 'house') return `House · ${capitalize(t)}`;
  if (c === 'senate') return `Senate · ${capitalize(t)}`;
  return capitalize(t);
}

function capitalize(str) {
  if (!str) return '';
  return str.charAt(0).toUpperCase() + str.slice(1);
}

function renderTimeline(events, fallbackMarkdown) {
  const el = document.getElementById('detail-timeline');

  if (!events || !events.length) {
    el.innerHTML = renderMarkdown(fallbackMarkdown);
    return;
  }

  // Pick the latest non-future event by ACTUAL date so the array order
  // (which may be newest-first from Congress.gov, oldest-first from OpenStates)
  // doesn't matter. Future/pending events stay as hollow rings.
  let lastDoneIdx = -1;
  let lastDoneDate = '';
  events.forEach((ev, i) => {
    if (ev.event_type === 'future' || ev.event_type === 'pending') return;
    const d = ev.date || '';
    if (d > lastDoneDate) {
      lastDoneDate = d;
      lastDoneIdx = i;
    }
  });

  let html = '<ol class="timeline">';

  events.forEach((event, i) => {
    const { day, year } = formatTimelineDate(event.date);
    const isFuture  = event.event_type === 'future' || event.event_type === 'pending';
    const isCurrent = i === lastDoneIdx;
    const stateClass = isFuture ? 'tl-future' : isCurrent ? 'tl-current' : 'tl-done';

    const dateText = isFuture
      ? (event.date ? `${day} ${year}` : 'Pending')
      : `${day}, ${year}${isCurrent ? ' · Latest' : ''}`;

    const chamber = chamberLabel(event);
    const chamberHtml = chamber
      ? `<span class="tl-chamber-inline">${escapeHtml(chamber)}</span>`
      : '';

    const detailRaw = (event.detail && event.detail !== event.text) ? event.detail : '';
    const detail = detailRaw.length > 200 ? detailRaw.slice(0, 197) + '…' : detailRaw;

    const voteHtml = (event.yea != null && event.nay != null) ? `
      <div class="tl-vote-row">
        <span class="tl-vote-pill tl-vote-yea">Yea ${event.yea}</span>
        <span class="tl-vote-pill tl-vote-nay">Nay ${event.nay}</span>
      </div>` : '';

    html += `
      <li class="tl-event ${stateClass}">
        <div class="tl-date">${escapeHtml(dateText)} ${chamberHtml}</div>
        <div class="tl-body">
          <div class="tl-title">${escapeHtml(event.text || '')}</div>
          ${detail ? `<div class="tl-detail">${escapeHtml(detail)}</div>` : ''}
          ${voteHtml}
        </div>
      </li>`;
  });

  html += '</ol>';
  const isStateBill = !!(_currentBill && (_currentBill.is_state_bill || _currentBill.state || _currentBill.ocd_id));
  const sourceLabel = isStateBill ? 'OpenStates' : 'Congress.gov';
  html += `<div class="tl-note">Data sourced from ${sourceLabel} legislative actions.</div>`;

  el.innerHTML = html;
}

function renderMarkdown(text) {
  return (text || '')
    .replace(/^### (.+)$/gm, '<h3>$1</h3>')
    .replace(/^## (.+)$/gm, '<h2>$1</h2>')
    .replace(/^# (.+)$/gm, '<h1>$1</h1>')
    .replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
    .replace(/\*(.+?)\*/g, '<em>$1</em>')
    .replace(/^- (.+)$/gm, '<li>$1</li>')
    .replace(/^---$/gm, '<hr style="border:none;border-top:1px solid var(--rule);margin:0.75rem 0">');
}

// ── Tooltip ──
function showTooltip(e, seat) {
  let label;
  if (seat.source === 'state') {
    const voteStr = seat.vote ? seat.vote.charAt(0).toUpperCase() + seat.vote.slice(1) : 'Unknown';
    label = seat.name
      ? `${escapeHtml(seat.name)} · ${voteStr} — click to view profile`
      : voteStr;
  } else {
    const partyLabel = seat.party === 'D' ? 'Democrat' : seat.party === 'R' ? 'Republican' : 'Independent';
    label = `${escapeHtml(seat.name)} · ${seat.state} · ${partyLabel} · ${seat.vote} — click to view profile`;
  }
  tooltip.textContent = label;
  tooltip.classList.add('visible');
  moveTooltip(e);
}
function moveTooltip(e) {
  tooltip.style.left = (e.clientX + 12) + 'px';
  tooltip.style.top  = (e.clientY - 28) + 'px';
}
function hideTooltip() { tooltip.classList.remove('visible'); }

let memberLoadingInProgress = false;

async function openStateMemberFromVote(seat) {
  if (memberLoadingInProgress || !seat.name) return;
  memberLoadingInProgress = true;
  hideTooltip();

  const loadingEl = document.getElementById('member-loading');
  const contentEl = document.getElementById('member-page-content');
  const steps = ['mstep-1', 'mstep-2', 'mstep-3'].map(id => document.getElementById(id));

  steps.forEach(s => s.classList.remove('visible', 'done'));
  loadingEl.style.display = 'block';
  contentEl.style.display = 'none';
  previousPage = document.querySelector('.page.active').id;
  showPage('page-member');

  steps[0].classList.add('visible');
  const t1 = setTimeout(() => steps[1].classList.add('visible'), 400);
  const t2 = setTimeout(() => steps[2].classList.add('visible'), 900);

  try {
    const res = await fetch('/state/member/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: seat.name, state_code: seat.state })
    });
    const data = await res.json();
    clearTimeout(t1); clearTimeout(t2);
    steps.forEach(s => s.classList.add('done'));

    if (data.found) {
      renderMemberPage(data);
    } else {
      loadingEl.style.display = 'none';
      contentEl.style.display = 'block';
      setStatus(`No profile found for ${escapeHtml(seat.name)}`);
      setTimeout(clearStatus, 2500);
    }
  } catch {
    clearTimeout(t1); clearTimeout(t2);
    loadingEl.style.display = 'none';
    contentEl.style.display = 'block';
    setStatus('Could not load member profile');
    setTimeout(clearStatus, 2500);
  } finally {
    memberLoadingInProgress = false;
  }
}

async function openMemberFromVote(seat) {
  if (memberLoadingInProgress) return;
  memberLoadingInProgress = true;
  hideTooltip();

  const loadingEl = document.getElementById('member-loading');
  const contentEl = document.getElementById('member-page-content');
  const steps = ['mstep-1', 'mstep-2', 'mstep-3'].map(id => document.getElementById(id));

  steps.forEach(s => s.classList.remove('visible', 'done'));
  loadingEl.style.display = 'block';
  contentEl.style.display = 'none';
  previousPage = document.querySelector('.page.active').id;
  showPage('page-member');

  steps[0].classList.add('visible');
  const t1 = setTimeout(() => steps[1].classList.add('visible'), 400);
  const t2 = setTimeout(() => steps[2].classList.add('visible'), 900);

  try {
    const res = await fetch('/member/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: seat.name })
    });
    const data = await res.json();
    clearTimeout(t1); clearTimeout(t2);
    steps.forEach(s => s.classList.add('done'));

    if (data.found) {
      renderMemberPage(data);
    } else {
      loadingEl.style.display = 'none';
      contentEl.style.display = 'block';
      setStatus(`No profile found for ${escapeHtml(seat.name)}`);
      setTimeout(clearStatus, 2500);
    }
  } catch {
    clearTimeout(t1); clearTimeout(t2);
    loadingEl.style.display = 'none';
    contentEl.style.display = 'block';
    setStatus('Could not load member profile');
    setTimeout(clearStatus, 2500);
  } finally {
    memberLoadingInProgress = false;
  }
}

// ── Chamber SVG ──
function renderChamber(title, data, defaultSvgW, defaultSvgH, repNames = { full: new Set(), last: new Set() }) {
  if (!data || !data.seats || !data.seats.length) return null;
  const s = data.summary;
  const isState = data.seats.some(seat => seat.source === 'state');
  const svgW   = data.svgW   || defaultSvgW;
  const svgH   = data.svgH   || defaultSvgH;
  const dot_r  = data.dot_r  || (title.includes('House') ? 4.5 : 6);

  const yesLabel = isState ? 'YES' : 'YEA';
  const noLabel  = isState ? 'NO'  : 'NAY';

  const block = document.createElement('div');
  block.className = 'chamber-block';

  const titleEl = document.createElement('div');
  titleEl.className = 'chamber-title';
  titleEl.textContent = title;
  block.appendChild(titleEl);

  const summary = document.createElement('div');
  summary.className = 'vote-summary';
  summary.innerHTML = `
    <span class="vote-count-yea">${yesLabel} ${s.yea}</span>
    <span class="vote-count-nay">${noLabel} ${s.nay}</span>
    ${s.present    > 0 ? `<span class="vote-count-other">ABSTAIN ${s.present}</span>`    : ''}
    ${s.not_voting > 0 ? `<span class="vote-count-other">ABSENT ${s.not_voting}</span>` : ''}
  `;
  block.appendChild(summary);

  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', `0 0 ${svgW} ${svgH}`);
  svg.setAttribute('class', 'chamber-svg');

  // Render non-rep seats first so rep dots layer on top
  const repSeats = [];
  data.seats.forEach(seat => {
    const seatNorm = (seat.name || '').toLowerCase().trim();
    const isSingleWord = seatNorm && !seatNorm.includes(' ');
    const isRep = repNames.full.size > 0 && (
      repNames.full.has(seatNorm) ||
      (isSingleWord && repNames.last.has(seatNorm))
    );
    if (isRep) { repSeats.push(seat); return; }

    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', seat.x);
    circle.setAttribute('cy', seat.y);
    circle.setAttribute('r', dot_r);
    circle.setAttribute('fill', seat.color);
    circle.setAttribute('opacity', '0.9');

    const clickable = !!seat.name;
    if (clickable) {
      circle.style.cursor = 'pointer';
      circle.addEventListener('click', () =>
        seat.source === 'state' ? openStateMemberFromVote(seat) : openMemberFromVote(seat)
      );
    }
    circle.addEventListener('mouseenter', e => showTooltip(e, seat));
    circle.addEventListener('mousemove',  e => moveTooltip(e));
    circle.addEventListener('mouseleave', hideTooltip);
    svg.appendChild(circle);
  });

  // Render user's reps on top with highlight color + larger radius
  repSeats.forEach(seat => {
    const circle = document.createElementNS('http://www.w3.org/2000/svg', 'circle');
    circle.setAttribute('cx', seat.x);
    circle.setAttribute('cy', seat.y);
    circle.setAttribute('r', dot_r * 1.6);
    circle.setAttribute('fill', USER_REP_HIGHLIGHT[seat.vote] || seat.color);
    circle.setAttribute('opacity', '1');
    circle.setAttribute('stroke', '#f5f0e8');
    circle.setAttribute('stroke-width', '1.2');
    circle.style.cursor = 'pointer';
    circle.addEventListener('click', () =>
      seat.source === 'state' ? openStateMemberFromVote(seat) : openMemberFromVote(seat)
    );
    circle.addEventListener('mouseenter', e => showTooltip(e, seat));
    circle.addEventListener('mousemove',  e => moveTooltip(e));
    circle.addEventListener('mouseleave', hideTooltip);
    svg.appendChild(circle);
  });

  block.appendChild(svg);
  return block;
}

const _connAmendedByFull = { items: [], shown: 5 };

function _billRow(b) {
  const billId = formatBillId(b.type, b.number);
  const title = (b.title || billId).slice(0, 100) + ((b.title || '').length > 100 ? '…' : '');
  return `<div class="member-bill-row" onclick='openDetail(${JSON.stringify(b).replace(/'/g, "&#39;")})'>
    <div class="member-bill-id">${billId} · ${b.congress}th Congress</div>
    <div class="member-bill-title">${escapeHtml(title)}</div>
    <div class="member-bill-date">${b.latest_action_date || ''}</div>
  </div>`;
}

const _amendmentTypeMap = {
  samdt: 'senate-amendment',
  hamdt: 'house-amendment',
  sa:    'senate-amendment',
  ha:    'house-amendment',
};

function _amendmentCongUrl(a) {
  const typeSlug = _amendmentTypeMap[(a.type || '').toLowerCase()] || (a.type || '').toLowerCase();
  const ordinal = `${a.congress}th-congress`;
  return `https://www.congress.gov/amendment/${ordinal}/${typeSlug}/${a.number}`;
}

function _amendmentRow(a) {
  const aId = `${(a.type || '').toUpperCase()} ${a.number}`;
  const title = (a.title || '').slice(0, 120) + ((a.title || '').length > 120 ? '…' : '');
  const url = _amendmentCongUrl(a);
  return `<div class="member-bill-row" onclick="window.open('${url}','_blank')" title="View on Congress.gov">
    <div class="member-bill-id">${aId} · ${a.congress}th Congress ↗</div>
    <div class="member-bill-title">${escapeHtml(title)}</div>
    <div class="member-bill-date">${a.latest_action_date || ''}</div>
  </div>`;
}

function _showCategory(id, html) {
  const el = document.getElementById(id);
  if (!el) return;
  el.querySelector('.conn-category-body').innerHTML = html;
  el.style.display = 'block';
}

function connectionsShowAll(cat) {
  if (cat === 'amended-by') {
    const el = document.getElementById('connections-amended-by');
    el.querySelector('.conn-category-body').innerHTML =
      _connAmendedByFull.items.map(_amendmentRow).join('');
    document.getElementById('conn-amended-show-all').style.display = 'none';
  }
}

function _reportCard(r) {
  const citation = escapeHtml(r.citation || '');
  const title    = escapeHtml(r.title || '');
  const subParts = [
    r.committee,
    r.chamber,
    r.issue_date ? new Date(r.issue_date).toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' }) : null,
    r.part && r.part > 1 ? `Part ${r.part}` : null,
  ].filter(Boolean).map(escapeHtml).join(' · ');
  const confTag = r.is_conference_report
    ? `<span class="report-conf-tag">Conference report</span>`
    : '';
  const link = r.full_url
    ? `<a href="${escapeHtml(r.full_url)}" target="_blank" rel="noopener" class="report-link">Read full report →</a>`
    : '';
  return `
    <div class="report-card">
      <div class="report-citation">${citation}${confTag}</div>
      ${subParts ? `<div class="report-subline">${subParts}</div>` : ''}
      ${title ? `<div class="report-title">${title}</div>` : ''}
      ${link}
    </div>`;
}

// "Who's pushing this" — entities that named this bill in their lobbying
// filings (from the reverse index). Empty payload → keep the section hidden.
function renderLobbying(entities) {
  const section = document.getElementById('lobbying-section');
  const body = document.getElementById('lobbying-body');
  if (!section || !body) return;
  entities = entities || [];
  if (!entities.length) { section.style.display = 'none'; return; }

  const money = (n) => {
    n = Number(n) || 0;
    if (n >= 1e9) return '$' + (n / 1e9).toFixed(2).replace(/\.00$/, '') + 'B';
    if (n >= 1e6) return '$' + (n / 1e6).toFixed(2).replace(/\.00$/, '') + 'M';
    if (n >= 1e3) return '$' + Math.round(n / 1e3) + 'K';
    return '$' + Math.round(n);
  };

  body.innerHTML = entities.map(e => {
    const kind = e.kind === 'registrant' ? 'firm' : 'client';
    const nameArg = JSON.stringify(e.name).replace(/"/g, '&quot;');
    const filings = e.mentions ? `${e.mentions} filing${e.mentions > 1 ? 's' : ''}` : '';
    // Prefer the bill-specific figure (spend on filings that named this bill)
    // over the entity's lifetime total — it's the more honest "on this bill".
    const spend = e.bill_spend
      ? `${money(e.bill_spend)} on filings naming this bill`
      : (e.spend ? `${money(e.spend)} total lobbying` : '');
    const meta = [filings, spend].filter(Boolean).join(' · ');
    return `<div class="pushing-row" onclick="openLobbyFromBill('${e.kind}', ${nameArg})">
      <span class="pushing-name">${escapeHtml(e.name)}<span class="pushing-kind">${kind}</span></span>
      <span class="pushing-meta">${escapeHtml(meta)}</span>
    </div>`;
  }).join('') +
  `<p class="pushing-note">Organizations that named this bill in their Senate LDA lobbying filings, ranked by frequency. Dollar figures are the spend reported on the filings that named this bill — a ceiling, since a filing covers all of an entity's activity, not one bill. Click any to see its full profile.</p>`;
  section.style.display = 'block';
}

// "Money behind the sponsors" — FEC campaign totals for whoever introduced the
// bill, shown beside the lobbying panel. Empty payload → keep it hidden.
function renderSponsorMoney(sponsors) {
  const section = document.getElementById('sponsor-money-section');
  const body = document.getElementById('sponsor-money-body');
  if (!section || !body) return;
  sponsors = sponsors || [];
  if (!sponsors.length) { section.style.display = 'none'; return; }

  const money = (n) => {
    n = Number(n) || 0;
    if (n >= 1e6) return '$' + (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M';
    if (n >= 1e3) return '$' + Math.round(n / 1e3) + 'K';
    return '$' + Math.round(n);
  };

  body.innerHTML = sponsors.map(s => {
    const f = s.finance || {};
    const pacPct = f.receipts ? Math.round(100 * (f.from_pacs || 0) / f.receipts) : null;
    const cycle = f.cycle ? `${f.cycle} cycle` : 'most recent filing';
    const bits = [
      f.cash_on_hand != null ? `<b>${money(f.cash_on_hand)}</b> cash on hand` : '',
      pacPct != null ? `<b>${pacPct}%</b> from PACs` : '',
      cycle,
    ].filter(Boolean).join(' · ');
    const link = f.fec_url ? ` <a href="${f.fec_url}" target="_blank" rel="noopener">FEC ↗</a>` : '';
    return `<div class="sponsor-money-row">
      <span class="pushing-name">${escapeHtml(s.name || '')}</span>
      <span class="pushing-meta">${money(f.receipts)} raised</span>
      <div class="sponsor-money-stats">${bits}${link}</div>
    </div>`;
  }).join('') +
  `<p class="pushing-note">The sponsor's campaign receipts this cycle, reported to the FEC — shown next to who's lobbying, not as a link between them. Which of these organizations gave to the sponsor requires entity-to-PAC resolution (the OpenSecrets layer), a planned addition.</p>`;
  section.style.display = 'block';
}

function renderConnections(conn) {
  const section = document.getElementById('connections-section');

  // Reset all categories
  ['connections-amends','connections-committee-reports','connections-identical','connections-amended-by',
   'connections-related','connections-superseded'].forEach(id => {
    const el = document.getElementById(id);
    if (el) { el.style.display = 'none'; el.querySelector('.conn-category-body').innerHTML = ''; }
  });
  document.getElementById('conn-amended-show-all').style.display = 'none';

  if (!conn) { section.style.display = 'none'; return; }

  let anyVisible = false;

  // Amends / Reauthorizes
  if (conn.amends) {
    const el = document.getElementById('connections-amends');
    el.querySelector('.conn-category-label').textContent = conn.amends.label;
    el.querySelector('.conn-category-body').innerHTML =
      `<div class="conn-amends-row"><a href="#" class="conn-amends-link" onclick="event.preventDefault();searchForAct(${JSON.stringify(conn.amends.act_name).replace(/"/g, '&quot;')})">${escapeHtml(conn.amends.act_name)}</a></div>`;
    el.style.display = 'block';
    anyVisible = true;
  }

  // Committee Reports — positioned between Amends and Identical per spec
  if (conn.committee_reports && conn.committee_reports.length) {
    _showCategory('connections-committee-reports', conn.committee_reports.map(_reportCard).join(''));
    anyVisible = true;
  }

  // Identical bill
  if (conn.identical && conn.identical.length) {
    _showCategory('connections-identical', conn.identical.map(_billRow).join(''));
    anyVisible = true;
  }

  // Amended by (with show-all)
  if (conn.amended_by && conn.amended_by.length) {
    const LIMIT = 5;
    _connAmendedByFull.items = conn.amended_by;
    const visible = conn.amended_by.slice(0, LIMIT);
    _showCategory('connections-amended-by', visible.map(_amendmentRow).join(''));
    if (conn.amended_by.length > LIMIT) {
      document.getElementById('conn-amended-show-all').style.display = 'block';
      document.getElementById('conn-amended-show-all').textContent =
        `Show all ${conn.amended_by.length}`;
    }
    anyVisible = true;
  }

  // Related legislation
  if (conn.related && conn.related.length) {
    _showCategory('connections-related', conn.related.map(_billRow).join(''));
    anyVisible = true;
  }

  // Superseded by
  if (conn.superseded && conn.superseded.length) {
    _showCategory('connections-superseded', conn.superseded.map(_billRow).join(''));
    anyVisible = true;
  }

  section.style.display = anyVisible ? 'block' : 'none';
}

const EXPLANATION_COLLAPSE_CHARS = 700;

function renderExplanation(markdown, becameLaw) {
  const el = document.getElementById('detail-explanation');

  // Remove stale law banner / expand button from a previous bill
  el.parentNode.querySelectorAll('.became-law-banner, .explanation-expand-btn').forEach(n => n.remove());
  el.classList.remove('explanation-collapsed');

  // Show enacted banner above the explanation when the bill became law
  if (becameLaw) {
    const lawLabel = `${becameLaw.type || 'Public Law'} ${becameLaw.number}`;
    const banner = document.createElement('div');
    banner.className = 'became-law-banner';
    banner.innerHTML = `✓ Signed into law &nbsp;·&nbsp; <strong>${lawLabel}</strong>`;
    el.insertAdjacentElement('beforebegin', banner);
  }

  el.innerHTML = renderMarkdown(markdown);

  if ((markdown || '').length > EXPLANATION_COLLAPSE_CHARS) {
    el.classList.add('explanation-collapsed');
    const btn = document.createElement('button');
    btn.className = 'explanation-expand-btn';
    btn.textContent = 'Show full summary ↓';
    btn.onclick = () => { el.classList.remove('explanation-collapsed'); btn.remove(); };
    el.insertAdjacentElement('afterend', btn);
  }
}

let _currentBill = null;
// Bumped on every openDetail call. A stream whose token is no longer current
// has been superseded by a newer open, so its (possibly very late) sections
// must not render over the newer bill's page.
let _detailToken = 0;

function renderFullText(text) {
  const section = document.getElementById('full-text-section');
  const content = document.getElementById('full-text-content');
  const cta     = document.getElementById('read-bill-text-btn');
  const body    = document.getElementById('full-text-body');
  const chevron = document.getElementById('full-text-chevron');

  if (text && text.trim().length >= 100) {
    content.textContent = text;
    content.classList.remove('full-text-empty');
    section.style.display = 'block';
    if (cta) {
      cta.style.display = 'inline-flex';
      cta.textContent = 'Read bill text →';
      cta.disabled = false;
    }
    return;
  }

  // No text available — show an explicit empty state instead of hiding silently.
  // Branch on jurisdiction: federal bills come from Congress.gov/GovInfo; state
  // bills come from OpenStates and their primary text source is each state's
  // own legislature website.
  content.textContent = '';
  content.classList.add('full-text-empty');

  const isStateBill = !!(_currentBill && _currentBill.is_state_bill);
  let messageHtml;

  if (isStateBill) {
    const stateName = stateNameFromCode(_currentBill.state) || _currentBill.state || 'the state legislature';
    // Prefer the state's own primary source (where the published text actually
    // lives). Fall back to OpenStates' page, then to a generic message.
    const sources = Array.isArray(_currentBill.sources) ? _currentBill.sources : [];
    const primarySource = sources[0];
    let linkUrl = null;
    let linkLabel = null;
    if (primarySource && primarySource.url) {
      linkUrl = primarySource.url;
      linkLabel = `View on ${stateName} legislature site →`;
    } else if (_currentBill.source_url) {
      linkUrl = _currentBill.source_url;
      linkLabel = 'View on LegiScan →';
    }
    const linkHtml = linkUrl
      ? `<p><a href="${escapeHtml(linkUrl)}" target="_blank" rel="noopener" class="conn-amends-link">${escapeHtml(linkLabel)}</a></p>`
      : '';
    messageHtml =
      `<p>The formal text for this bill isn’t available through OpenStates. State legislatures publish bill text on their own websites — OpenStates ingests it on a delay, and some legislatures publish only after a bill advances past committee.</p>` +
      linkHtml;
  } else {
    let congressUrl = '#';
    if (_currentBill && _currentBill.congress && _currentBill.type && _currentBill.number) {
      const t = String(_currentBill.type).toLowerCase();
      congressUrl = `https://www.congress.gov/bill/${_currentBill.congress}th-congress/${t === 'hr' ? 'house-bill' : t === 's' ? 'senate-bill' : t}/${_currentBill.number}/text`;
    }
    messageHtml =
      `<p>The formal text for this bill isn’t available from either Congress.gov or GovInfo. This usually means the bill was filed very recently and hasn’t propagated to either system yet — try again in a day or two.</p>` +
      `<p><a href="${congressUrl}" target="_blank" rel="noopener" class="conn-amends-link">Check Congress.gov →</a></p>`;
  }

  content.innerHTML = messageHtml;
  section.style.display = 'block';
  if (body)    body.style.display = 'block';
  if (chevron) chevron.classList.add('open');
  if (cta) {
    cta.style.display = 'inline-flex';
    cta.textContent = 'Bill text not yet published';
    cta.disabled = true;
  }
}

function openBillText() {
  const section = document.getElementById('full-text-section');
  const body    = document.getElementById('full-text-body');
  const chevron = document.getElementById('full-text-chevron');
  if (!section || section.style.display === 'none') return;
  if (body.style.display === 'none') {
    body.style.display = 'block';
    chevron.classList.add('open');
  }
  section.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function toggleFullText() {
  const body = document.getElementById('full-text-body');
  const chevron = document.getElementById('full-text-chevron');
  const isOpen = body.style.display !== 'none';
  body.style.display = isOpen ? 'none' : 'block';
  chevron.classList.toggle('open', !isOpen);
}

function renderSponsors(sponsors, cosponsors) {
  const section = document.getElementById('sponsors-section');
  const body = document.getElementById('sponsors-body');

  const allEmpty = (!sponsors || !sponsors.length) && (!cosponsors || !cosponsors.length);
  if (allEmpty) {
    section.style.display = 'none';
    return;
  }

  const PARTY_LABEL = { D: 'D', R: 'R', I: 'I', ID: 'I' };

  function memberChip(person, role) {
    const party = PARTY_LABEL[person.party] || person.party || '';
    const partyClass = person.party === 'D' ? 'party-d' : person.party === 'R' ? 'party-r' : 'party-i';
    const label = [person.name || person.last_name, person.state].filter(Boolean).join(', ');
    const byRequest = role === 'sponsor' && person.is_by_request ? ' <span class="sponsor-by-request">(by request)</span>' : '';
    const payload = JSON.stringify({ name: person.name }).replace(/"/g, '&quot;');
    return `<button class="sponsor-chip ${partyClass}" onclick="openMemberFromVote(${payload})">
      ${party ? `<span class="sponsor-party">${party}</span>` : ''}
      <span class="sponsor-name">${label}</span>${byRequest}
    </button>`;
  }

  let html = '';

  if (sponsors && sponsors.length) {
    html += `<div class="sponsor-row">
      <span class="sponsor-role-label">Sponsor</span>
      <div class="sponsor-chips">${sponsors.map(s => memberChip(s, 'sponsor')).join('')}</div>
    </div>`;
  }

  if (cosponsors && cosponsors.length) {
    const SHOW_LIMIT = 10;
    const visible = cosponsors.slice(0, SHOW_LIMIT);
    const hidden = cosponsors.slice(SHOW_LIMIT);
    const hiddenHtml = hidden.length
      ? `<div class="sponsor-overflow" id="sponsor-overflow" style="display:none">${hidden.map(c => memberChip(c, 'cosponsor')).join('')}</div>
         <button class="sponsor-show-more" id="sponsor-show-more" onclick="document.getElementById('sponsor-overflow').style.display='flex';this.style.display='none'">
           + ${hidden.length} more cosponsor${hidden.length !== 1 ? 's' : ''}
         </button>`
      : '';
    html += `<div class="sponsor-row">
      <span class="sponsor-role-label">Cosponsors <span class="sponsor-count">${cosponsors.length}</span></span>
      <div class="sponsor-chips">${visible.map(c => memberChip(c, 'cosponsor')).join('')}${hiddenHtml}</div>
    </div>`;
  }

  body.innerHTML = html;
  section.style.display = 'block';
}

const USER_REP_HIGHLIGHT = {
  "Yea":       "#52b788",
  "Nay":       "#c44b4b",
  "Present":   "#aaaaaa",
  "Not Voting":"#bbbbbb",
};

function _userRepNames() {
  const prefs = getPrefs();
  if (!prefs) return { full: new Set(), last: new Set() };
  const norm = n => (n || '').toLowerCase().trim();
  const full = new Set();
  const last = new Set();
  const addRep = r => {
    if (!r) return;
    const n = norm(r.name);
    full.add(n);
    last.add(n.split(' ').pop());
  };
  (prefs.senators || []).forEach(addRep);
  addRep(prefs.representative);
  return { full, last };
}

function renderVotes(votes, isStateBill = false) {
  const section = document.getElementById('votes-section');
  const grid    = document.getElementById('chambers-grid');
  grid.innerHTML = '';
  const hasHouse  = votes && votes.house  && votes.house.seats  && votes.house.seats.length;
  const hasSenate = votes && votes.senate && votes.senate.seats && votes.senate.seats.length;

  if (!hasHouse && !hasSenate) {
    if (isStateBill) {
      section.style.display = 'block';
      grid.innerHTML = '<div class="no-vote-notice">No recorded roll call vote available for this bill.</div>';
    } else {
      section.style.display = 'none';
    }
    return;
  }

  const repNames = _userRepNames();
  section.style.display = 'block';
  if (hasHouse)  { const b = renderChamber('House',  votes.house,  500, 260, repNames); if (b) grid.appendChild(b); }
  if (hasSenate) { const b = renderChamber('Senate', votes.senate, 300, 200, repNames); if (b) grid.appendChild(b); }
  grid.style.gridTemplateColumns = (hasHouse && hasSenate) ? '1fr 1fr' : '1fr';
}

// ── Bill detail ──
// Reveal the detail shell once the first section is in hand, and drop
// lightweight placeholders into the two always-visible AI sections so they
// don't look broken while their Haiku calls are still in flight. Idempotent.
function _revealDetail(title) {
  document.getElementById('detail-loading').style.display = 'none';
  document.getElementById('detail-content').style.display = 'block';
  document.getElementById('detail-bill-title').textContent = title;
  const exp = document.getElementById('detail-explanation');
  if (exp) exp.innerHTML = '<div class="section-loading">Translating to plain English…</div>';
  const tl = document.getElementById('detail-timeline');
  if (tl) tl.innerHTML = '<div class="section-loading">Building legislative timeline…</div>';
  const bg = document.getElementById('detail-background');
  if (bg) { bg.innerHTML = ''; bg.style.display = 'none'; }
  // Hide the conditionally-rendered sections up front. Streaming only calls a
  // section's render fn when that section arrives, so a section the new stream
  // never emits (a producer erroring, or the /law not-indexed path) would
  // otherwise leave the PREVIOUS bill's sponsors/text/votes/connections visible.
  ['sponsors-section', 'full-text-section', 'connections-section', 'votes-section', 'lobbying-section', 'sponsor-money-section'].forEach(id => {
    const s = document.getElementById(id);
    if (s) s.style.display = 'none';
  });
  // Drop any enacted-law banner / expand button left over from a prior bill —
  // they're siblings of #detail-explanation, so resetting its innerHTML above
  // doesn't remove them.
  document.querySelectorAll('.became-law-banner, .explanation-expand-btn').forEach(n => n.remove());
}

function _setBillContext(billId, bill, title, translation) {
  if (typeof setCurrentBillContext === 'function') {
    setCurrentBillContext({
      bill_id:      billId,
      bill_title:   title,
      bill_summary: translation || '',
      latest_action: bill.latest_action || '',
      _reopen: { type: 'federal', congress: bill.congress, billType: bill.type, number: bill.number, title },
    });
  }
}

// Background is the resolved-references block under the explanation — a list of
// {term, summary, source} items. It arrives late (a slow web search) and is
// often empty, so it renders only when it has content; otherwise stays hidden.
function renderBackground(items) {
  const el = document.getElementById('detail-background');
  if (!el) return;
  items = Array.isArray(items) ? items : [];
  if (!items.length) { el.innerHTML = ''; el.style.display = 'none'; return; }

  el.innerHTML =
    '<div class="bg-label">Background — terms this bill references</div>' +
    items.map(it => {
      const src = /^https?:\/\//i.test(it.source || '')
        ? `<a class="bg-source" href="${escapeHtml(it.source)}" target="_blank" rel="noopener">Source ↗</a>`
        : '';
      return `<div class="bg-item">
        <div class="bg-term">${escapeHtml(it.term || '')}</div>
        <div class="bg-summary">${escapeHtml(it.summary || '')}</div>
        ${src}
      </div>`;
    }).join('');
  el.style.display = 'block';
}

function _showDetailError(bill) {
  document.getElementById('detail-loading').innerHTML = `
    <div class="empty-state">
      <p>This couldn't be loaded right now.</p>
      <p style="margin-top:0.5rem">The data may be temporarily unavailable.</p>
      <button class="pill"
        style="margin-top:1rem"
        onclick="openDetail(${JSON.stringify(bill).replace(/"/g, '&quot;')})">
        Try again
      </button>
    </div>`;
  document.getElementById('detail-loading').style.display = 'block';
  document.getElementById('detail-content').style.display = 'none';
}

async function openDetail(bill) {
  const myToken = ++_detailToken;
  bill = {
    ...bill,
    congress: parseInt(bill.congress) || bill.congress,
    number: parseInt(bill.number) || bill.number,
    law_number: bill.law_number ? parseInt(bill.law_number) : null
  };
  _currentBill = bill;
  const activePage = document.querySelector('.page.active').id;
  if (activePage !== 'page-detail') previousPage = activePage;
  const billId = bill.is_law
    ? `Public Law ${bill.congress}-${bill.law_number}`
    : billIdFromParts(bill.type, bill.number);

  document.getElementById('detail-bill-id').textContent = billId;
  document.getElementById('detail-bill-title').textContent = bill.title || billId;
  document.getElementById('detail-loading').style.display = 'block';
  document.getElementById('detail-content').style.display = 'none';
  document.getElementById('votes-section').style.display = 'none';

  showPage('page-detail');

  // Authoritative title from Congress.gov overrides whatever title the search
  // result carried. GovInfo indexes vehicle-bill print versions under different
  // titles than a bill's canonical name, so a "SAVE Act" hit might land on a
  // bill whose current text is unrelated — Congress.gov is the truth.
  let authoritativeTitle = bill.title || billId;

  try {
    // Both bills (/bill) and enacted laws (/law) stream section-by-section as
    // NDJSON — a Public Law resolves to its underlying bill server-side.
    const endpoint = bill.is_law ? '/law' : '/bill';
    const reqBody = bill.is_law
      ? { congress: bill.congress, law_number: bill.law_number, user_context: getPrefs() }
      : { congress: parseInt(bill.congress), bill_type: bill.type, number: parseInt(bill.number), user_context: getPrefs() };

    const response = await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(reqBody)
    });
    if (!response.ok || !response.body) throw new Error(`Error: ${response.status}`);

    let revealed = false;
    let translationText = '';
    const ensureRevealed = () => {
      if (!revealed) { _revealDetail(authoritativeTitle); revealed = true; }
    };

    const handleSection = (msg) => {
      // A newer openDetail has superseded this stream — drop its sections so
      // they can't paint over the current bill.
      if (myToken !== _detailToken) return;
      switch (msg.section) {
        case 'meta':
          authoritativeTitle = msg.title || authoritativeTitle;
          ensureRevealed();
          document.getElementById('detail-bill-title').textContent = authoritativeTitle;
          // Notify button needs only the bill id — wire it up immediately
          // rather than waiting on the (slow) Background section.
          _initNotifyBtn(billId, bill.congress, bill.type, bill.number, null);
          break;
        case 'translation':
          ensureRevealed();
          translationText = msg.translation || '';
          renderExplanation(translationText, msg.became_law);
          _setBillContext(billId, bill, authoritativeTitle, translationText);
          break;
        case 'background':
          // Bonus context that resolves late (a slow web search) and often
          // comes back empty. Render only when it has content — no persistent
          // placeholder, since the explanation already reads fine alone.
          renderBackground(msg.items || []);
          break;
        case 'sponsors':
          ensureRevealed();
          renderSponsors(msg.sponsors || [], msg.cosponsors || []);
          break;
        case 'timeline':
          ensureRevealed();
          renderTimeline(msg.timeline_events, msg.timeline);
          break;
        case 'votes':
          ensureRevealed();
          renderVotes(msg.votes);
          break;
        case 'bill_text':
          ensureRevealed();
          renderFullText(msg.bill_text);
          break;
        case 'connections':
          ensureRevealed();
          renderConnections(msg.connections);
          break;
        case 'lobbying':
          ensureRevealed();
          renderLobbying(msg.entities);
          break;
        case 'sponsor_money':
          ensureRevealed();
          renderSponsorMoney(msg.sponsors);
          break;
        case 'done':
          break;
      }
    };

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      // Superseded by a newer open — stop consuming this stream and let it go.
      if (myToken !== _detailToken) { try { await reader.cancel(); } catch (e) {} return; }
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line) continue;
        try { handleSection(JSON.parse(line)); }
        catch (e) { console.error('bad stream line', e, line); }
      }
    }
    const tail = buffer.trim();
    if (tail) { try { handleSection(JSON.parse(tail)); } catch (e) {} }

    // Notify button + context are wired during the stream (meta/translation).
    // Just guarantee the shell is revealed if the stream produced nothing —
    // unless a newer open has already taken over the detail page.
    if (myToken === _detailToken) ensureRevealed();

  } catch (err) {
    _showDetailError(bill);
  }
}

function openDetailFromBill(bill) {
  openDetail({ congress: bill.congress, type: bill.type, number: parseInt(bill.number), title: bill.title });
}

// ── Member profile ──
function _mfMoney(n) {
  n = Number(n) || 0;
  if (n >= 1e6) return '$' + (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + 'M';
  if (n >= 1e3) return '$' + Math.round(n / 1e3) + 'K';
  return '$' + Math.round(n);
}

// Campaign-finance composition for a federal member (FEC). Answers "what
// funding, and from whom" at the source level — named donors/industries are
// the OpenSecrets layer, called out honestly in the note.
function _renderMemberFinance(fin) {
  const section = document.getElementById('member-finance-section');
  const body = document.getElementById('member-finance-body');
  if (!section || !body) return;
  const R = fin && fin.receipts || 0;
  if (!R && !(fin && fin.disbursements)) { section.style.display = 'none'; return; }

  const segs = [
    { label: 'Small-dollar donors', sub: 'individuals under $200', val: fin.indiv_unitemized || 0, cls: 'mf-small' },
    { label: 'Larger individuals', sub: 'itemized, $200+', val: fin.indiv_itemized || 0, cls: 'mf-indiv' },
    { label: 'PACs', sub: 'political action committees', val: fin.from_pacs || 0, cls: 'mf-pac' },
    { label: 'Party committees', sub: '', val: fin.from_party || 0, cls: 'mf-party' },
    { label: 'Self-funded', sub: 'the candidate’s own money', val: fin.self_funding || 0, cls: 'mf-self' },
  ].filter(s => s.val > 0);
  const known = segs.reduce((a, s) => a + s.val, 0);
  const other = Math.max(0, R - known);
  if (other > R * 0.02) segs.push({ label: 'Other', sub: 'transfers, loans, etc.', val: other, cls: 'mf-other' });

  const bar = segs.map(s =>
    `<div class="mf-seg ${s.cls}" style="width:${(100 * s.val / R).toFixed(1)}%" title="${s.label}: ${_mfMoney(s.val)}"></div>`).join('');
  const rows = segs.map(s => `<div class="mf-row">
      <span class="mf-swatch ${s.cls}"></span>
      <span class="mf-label">${s.label}${s.sub ? `<span class="mf-sub">${s.sub}</span>` : ''}</span>
      <span class="mf-pct">${Math.round(100 * s.val / R)}%</span>
      <span class="mf-amt">${_mfMoney(s.val)}</span>
    </div>`).join('');
  const cyc = fin.cycle ? `${fin.cycle} cycle` : 'most recent filing';

  const pacs = (fin.top_pacs || []).filter(p => p.amount > 0);
  const pacHtml = pacs.length ? `
    <div class="mf-pac-head">Top PAC contributors <span class="mf-sub">${cyc}</span></div>
    <div class="mf-pac-list">${pacs.map(p => `
      <div class="mf-pac-row">
        <span class="mf-pac-name">${escapeHtml(p.name)}</span>
        <span class="mf-pac-amt">${_mfMoney(p.amount)}</span>
      </div>`).join('')}</div>` : '';

  body.innerHTML = `
    <div class="mf-head">
      <div class="mf-headline"><span class="mf-big">${_mfMoney(R)}</span> raised
        · <span class="mf-big">${_mfMoney(fin.cash_on_hand || 0)}</span> cash on hand</div>
      <div class="mf-cyc">${cyc}${fin.fec_url ? ` · <a href="${fin.fec_url}" target="_blank" rel="noopener">FEC ↗</a>` : ''}</div>
    </div>
    <div class="mf-bar">${bar}</div>
    <div class="mf-legend">${rows}</div>
    ${pacHtml}
    <div id="mf-industries"></div>
    <p class="pushing-note">Percentages above are shares of <strong>total money raised</strong> this
      cycle. Top PAC contributors are the largest names within the PAC share (not the full list).
      Both are from FEC filings, with the candidate's own joint-fundraising committees and
      pass-through conduits (ActBlue, WinRed) removed.</p>`;
  section.style.display = 'block';
}

// Estimated industry breakdown of a member's donors — loads a moment after the
// finance section, since it runs a (cached) classification pass.
function _renderMemberIndustries(data) {
  const host = document.getElementById('mf-industries');
  if (!host) return;
  const inds = (data && data.industries || []).filter(i => i.total > 0);
  if (!inds.length) { host.innerHTML = ''; return; }
  // FEC cycles are named by their even election year but span the two prior
  // years — show the full span so "2024" doesn't read as stale on a member who
  // took office in 2025 (elected in the Nov 2024 cycle).
  const cyc = data.cycle
    ? `estimated · ${data.cycle - 1}–${String(data.cycle).slice(-2)} election cycle`
    : 'estimated';
  host.innerHTML = `
    <div class="mf-pac-head">Individual donors by industry <span class="mf-sub">${cyc}</span></div>
    <div class="mf-ind-list">${inds.map(i => {
      const other = i.industry === 'Unclassified employers';
      // Bar = share of this section's own total, so 27% reads as 27% — not
      // scaled to the max category (which made the biggest bar look like 100%).
      return `<div class="mf-ind-row${other ? ' mf-ind-other' : ''}">
        <span class="mf-ind-name">${escapeHtml(i.industry)}</span>
        <span class="mf-ind-bar"><span class="mf-ind-fill" style="width:${Math.min(100, Math.max(2, 100 * i.share))}%"></span></span>
        <span class="mf-ind-pct">${Math.round(i.share * 100)}%</span>
        <span class="mf-ind-amt">${_mfMoney(i.total)}</span>
      </div>`;
    }).join('')}</div>
    <p class="pushing-note">A separate lens: the member's <em>itemized individual</em> donors ($200+),
      grouped by the industry of each donor's employer — the curation OpenSecrets did by hand, here an
      estimate. Percentages are shares of this classified individual money, <strong>not</strong> of
      total money raised, and this is often an earlier cycle than the totals above. Approximate.</p>`;
}

async function loadMemberIndustries(cid, cycle) {
  try {
    const res = await fetch(`/member/industries?cid=${encodeURIComponent(cid)}&cycle=${encodeURIComponent(cycle)}`);
    _renderMemberIndustries(await res.json());
  } catch { /* leave industries empty on error */ }
}

async function loadMemberFinance(name, state, chamber) {
  const section = document.getElementById('member-finance-section');
  if (section) section.style.display = 'none';
  try {
    const q = new URLSearchParams({ name });
    if (state) q.set('state', state);
    if (chamber) q.set('chamber', chamber);
    const res = await fetch('/member/finance?' + q.toString());
    const fin = await res.json();
    _renderMemberFinance(fin);
    if (fin && fin.candidate_id && fin.cycle) loadMemberIndustries(fin.candidate_id, fin.cycle);
  } catch { /* leave the section hidden on any error */ }
}

// Disclosed House stock trades (STOCK Act PTRs). For senators we state the gap
// explicitly (Senate disclosures aren't machine-readable); for House members
// with no trades we stay silent, since absence there is ambiguous.
function _renderMemberStocks(data, isSenator) {
  const section = document.getElementById('member-stocks-section');
  const body = document.getElementById('member-stocks-body');
  if (!section || !body) return;
  const trades = (data && data.trades) || [];
  if (!trades.length) {
    const yrs = (data && data.cycles || []).join('–');
    if (isSenator) {
      body.innerHTML = `<p class="pushing-note">Senate stock disclosures live in a separate,
        agreement-gated system (the Senate eFD) that isn't machine-readable the way the House's
        filings are — so we can't show a senator's trades yet. This is our gap, not a sign the
        senator doesn't trade. House members only for now.</p>`;
      section.style.display = 'block';
    } else if (data && data.filed === false) {
      // We checked the House filing index: this member reported no trades.
      body.innerHTML = `<p class="pushing-note">No stock trades disclosed${yrs ? ` in ${yrs}` : ''}.
        This member filed no Periodic Transaction Reports — many hold only funds, blind trusts, or
        no reportable securities. Under the STOCK Act, individual trades over $1,000 must be reported.</p>`;
      section.style.display = 'block';
    } else {
      section.style.display = 'none';  // filed but unparseable (paper) — a silent gap
    }
    return;
  }

  const chips = (data.top_tickers || []).map(t =>
    `<span class="stk-chip">${escapeHtml(t.ticker)}<span class="stk-chip-n">${t.count}</span></span>`).join('');
  const rows = trades.map(t => {
    const dir = t.type.startsWith('buy') ? 'buy' : t.type.startsWith('sell') ? 'sell' : 'exch';
    const label = t.ticker ? escapeHtml(t.ticker) : escapeHtml((t.asset || '').slice(0, 32));
    const who = t.owner && t.owner !== '' ? ` · ${escapeHtml(t.owner)}` : '';
    // Rows with a ticker are clickable — expand to show the stock's move after.
    const click = t.ticker
      ? ` stk-clickable" data-ticker="${escapeHtml(t.ticker)}" data-date="${escapeHtml(t.date || '')}" data-dir="${dir}" onclick="toggleStockPerf(this)`
      : '';
    return `<div class="stk-item">
      <div class="stk-row${click}">
        <span class="stk-date">${escapeHtml(t.date || '')}</span>
        <span class="stk-dir stk-${dir}">${escapeHtml(t.type)}</span>
        <span class="stk-tkr" title="${escapeHtml(t.asset || '')}">${label}${t.ticker ? '<span class="stk-caret">›</span>' : ''}</span>
        <span class="stk-amt">${escapeHtml(t.amount || '')}${who}</span>
      </div>
      <div class="stk-perf" style="display:none"></div>
    </div>`;
  }).join('');
  const cyc = (data.cycles || []).join(', ');
  body.innerHTML = `
    <div class="mf-head">
      <div class="mf-headline"><span class="mf-big">${data.trade_count}</span> disclosed trades
        · <span class="stk-buy">${data.buys} buys</span> / <span class="stk-sell">${data.sells} sells</span></div>
      <div class="mf-cyc">${cyc ? cyc + ' · ' : ''}House STOCK Act filings</div>
    </div>
    ${chips ? `<div class="stk-chips">${chips}</div>` : ''}
    <div class="stk-list">${rows}</div>
    <p class="pushing-note">Trades the member (or their spouse/dependent) disclosed under the STOCK Act,
      from U.S. House filings. Amounts are the reported <strong>ranges</strong>, not exact figures, with a
      ~45-day filing lag. This is trading activity, not a full holdings snapshot; senators and members who
      paper-file aren't covered yet.</p>`;
  section.style.display = 'block';
}

async function loadMemberStocks(bioguide, isSenator) {
  const section = document.getElementById('member-stocks-section');
  if (section) section.style.display = 'none';
  try {
    const res = await fetch('/member/stocks?bioguide=' + encodeURIComponent(bioguide));
    _renderMemberStocks(await res.json(), isSenator);
  } catch { /* leave hidden on error */ }
}

// Click a trade → show how the stock moved 1wk / 1mo / 3mo after the trade date.
function _renderStockPerf(d) {
  const w = (d && d.windows) || [];
  if (!w.length) return '<div class="stk-perf-note">No price history available for this ticker.</div>';
  const cells = w.map(x => {
    const up = x.pct >= 0;
    return `<span class="stk-perf-cell">
      <span class="stk-perf-lbl">${x.label} later</span>
      <span class="stk-perf-pct ${up ? 'up' : 'down'}">${up ? '+' : ''}${x.pct}%</span>
    </span>`;
  }).join('');
  return `<div class="stk-perf-cells">${cells}</div>
    <div class="stk-perf-note">${escapeHtml(d.ticker)} closed at $${d.base_price} on ${d.base_date}; figures are the
      stock's price change after the trade — not the member's realized gain, and not an accusation.</div>`;
}

async function toggleStockPerf(row) {
  const box = row.nextElementSibling; // .stk-perf
  if (!box) return;
  if (box.style.display === 'block') { box.style.display = 'none'; row.classList.remove('open'); return; }
  row.classList.add('open');
  box.style.display = 'block';
  if (box.dataset.loaded) return;
  box.innerHTML = '<div class="stk-perf-note">Loading price history…</div>';
  try {
    const res = await fetch(`/stock/perf?ticker=${encodeURIComponent(row.dataset.ticker)}&date=${encodeURIComponent(row.dataset.date)}`);
    box.innerHTML = _renderStockPerf(await res.json());
    box.dataset.loaded = '1';
  } catch {
    box.innerHTML = '<div class="stk-perf-note">Couldn\'t load price history.</div>';
  }
}

function renderMemberPage(data) {
  const activeMemberPage = document.querySelector('.page.active').id;
  if (activeMemberPage !== 'page-member') previousPage = activeMemberPage;
  document.getElementById('member-loading').style.display = 'none';
  document.getElementById('member-page-content').style.display = 'block';
  const backBtn = document.querySelector('#member-page-content .detail-back');
  backBtn.textContent = previousPage === 'page-detail' ? '← Back to bill' : '← Back to search';
  const m = data.member;
  const leg = data.legislation || { sponsored: data.sponsored_bills || [] };

  document.getElementById('member-party-tag').textContent =
    [m.party || 'Independent', m.chambers ? m.chambers.join(' & ') : m.chamber || ''].filter(Boolean).join('  ·  ');
  document.getElementById('member-name').textContent = m.name || '';
  document.getElementById('member-meta-line').textContent = [
    m.state,
    m.start_year ? `${m.start_year}–${m.end_year || 'present'}` : '',
    m.birth_year ? `b. ${m.birth_year}` : ''
  ].filter(Boolean).join('  ·  ');
  document.getElementById('member-status').textContent = m.current ? 'Currently serving' : 'Former member';

  const photo = document.getElementById('member-photo');
  if (m.bioguide_id) {
    photo.style.display = 'block';
    photo.src = `/member/photo/${m.bioguide_id.toLowerCase()}`;
    photo.alt = m.name;
  } else if (m.photo_url) {
    photo.style.display = 'block';
    photo.src = m.photo_url;
    photo.alt = m.name;
  } else {
    photo.style.display = 'none';
  }

  const isState = !!m.is_state_legislator;

  document.getElementById('member-meta-line').textContent = [
    m.state,
    m.district ? `District ${m.district}` : '',
    m.start_year ? `${m.start_year}–${m.end_year || 'present'}` : '',
    m.birth_year ? `b. ${m.birth_year}` : ''
  ].filter(Boolean).join('  ·  ');

  const statsGrid = document.getElementById('member-stats-grid');
  const sponsoredBills = isState ? (data.sponsored_bills || []) : (leg.sponsored || []);
  const stats = isState ? [
    { number: sponsoredBills.length, label: 'Bills Sponsored' },
    { number: m.chamber || '—', label: 'Chamber' },
    { number: m.district || '—', label: 'District' },
  ] : [
    { number: m.years_served || '—', label: 'Years Served' },
    { number: (leg.sponsored_count || 0).toLocaleString(), label: 'Bills Sponsored' },
    { number: (leg.cosponsored_count || 0).toLocaleString(), label: 'Cosponsored' },
    { number: Object.keys(leg.policy_areas || {}).filter(k => k && k !== 'None').length, label: 'Policy Areas' },
  ];
  statsGrid.innerHTML = stats.map(s => `
    <div class="stat-block">
      <div class="stat-number">${s.number}</div>
      <div class="stat-label">${s.label}</div>
    </div>
  `).join('');

  // Federal campaign finance (FEC). State legislators have none.
  // State legislators have no money column — collapse to a single column.
  const _cols = document.getElementById('member-cols');
  if (_cols) _cols.classList.toggle('single', isState);
  const _finSection = document.getElementById('member-finance-section');
  if (_finSection) _finSection.style.display = 'none';
  const _stkSection = document.getElementById('member-stocks-section');
  if (_stkSection) _stkSection.style.display = 'none';
  if (!isState && m.name) {
    loadMemberFinance(m.name, m.state, (m.chambers && m.chambers.join(' ')) || m.chamber || '');
  }
  if (!isState && m.bioguide_id) {
    const _ch = (m.chambers && m.chambers.join(' ')) || m.chamber || '';
    loadMemberStocks(m.bioguide_id, /sen/i.test(_ch));
  }

  const policyChart = document.getElementById('policy-chart');
  if (!isState) {
    const areas = Object.entries(leg.policy_areas || {})
      .filter(([k]) => k && k !== 'None' && k !== 'Other' && k !== 'null')
      .sort((a, b) => b[1] - a[1]);
    if (areas.length) {
      const max = areas[0][1];
      policyChart.innerHTML = areas.map(([label, count]) => `
        <div class="policy-bar-row">
          <div class="policy-bar-label">${label}</div>
          <div class="policy-bar-track"><div class="policy-bar-fill" style="width:${(count/max*100).toFixed(1)}%"></div></div>
          <div class="policy-bar-count">${count}</div>
        </div>
      `).join('') + `<p class="policy-disclaimer">Based on sponsored bills only (up to 250 most recent). Does not include cosponsored legislation.</p>`;
    }
  } else {
    policyChart.innerHTML = '';
  }

  const billsEl = document.getElementById('member-bills');
  if (isState) {
    billsEl.innerHTML = sponsoredBills.map(b => `
      <div class="member-bill-row" onclick='openStateBill(${JSON.stringify(b).replace(/'/g, "&#39;")})'>
        <div class="member-bill-id">${escapeHtml(b.identifier || '')}</div>
        <div class="member-bill-title">${escapeHtml((b.title || '').slice(0, 100))}${(b.title || '').length > 100 ? '…' : ''}</div>
        <div class="member-bill-date">${b.date || ''}</div>
      </div>
    `).join('') || '<p style="font-family:\'IBM Plex Mono\',monospace;font-size:0.7rem;color:var(--muted)">No recent bills found</p>';
  } else {
    const validBills = (leg.sponsored || []).filter(b => b.title && b.number);
    billsEl.innerHTML = validBills.map(b => `
      <div class="member-bill-row" onclick='openDetailFromBill(${JSON.stringify(b)})'>
        <div class="member-bill-id">${formatBillId(b.type, b.number)}</div>
        <div class="member-bill-title">${escapeHtml((b.title || '').slice(0, 100))}${(b.title || '').length > 100 ? '…' : ''}</div>
        <div class="member-bill-date">${b.date || ''}</div>
      </div>
    `).join('') || '<p style="font-family:\'IBM Plex Mono\',monospace;font-size:0.7rem;color:var(--muted)">No recent bills found</p>';
  }

  showPage('page-member');
}

// ── Feed helpers ──
function billKey(item) {
  return item.is_state_bill ? (item.ocd_id || '') : `${item.type || ''}${item.number || ''}`;
}

// Format a bill ID with newspaper dots: HR → H.R., S → S., HJRES stays HJRES.
function formatBillId(type, number) {
  const t = (type || '').toUpperCase();
  if (!t) return String(number || '');
  if (t === 'HR') return `H.R. ${number || ''}`.trim();
  if (t === 'S')  return `S. ${number || ''}`.trim();
  return `${t} ${number || ''}`.trim();
}

function getStatusLabel(action, item) {
  // Public Law shorthand wins if we know it
  if (item && item.is_law && item.law_number && item.congress) {
    return `P.L. ${item.congress}-${item.law_number}`;
  }
  if (!action) return 'Active';
  const a = action.toLowerCase();
  if (a.includes('became public law') || a.includes('enacted') || a.includes('became law')) return 'Public Law';
  if (a.includes('signed by president') || a.includes('signed into law')) return 'Signed into Law';
  if (a.includes('signed by governor') || a.includes('chaptered'))       return 'Signed';
  if (a.includes('passed house') || a.includes('agreed to in house'))    return 'Passed House';
  if (a.includes('passed senate') || a.includes('agreed to in senate'))  return 'Passed Senate';
  if (a.includes('passed') || a.includes('agreed to') || a.includes('concurred in')) return 'Passed';
  if (a.includes('vote scheduled') || a.includes('scheduled for'))       return 'Vote Scheduled';
  if (a.includes('reported by') || a.includes('ordered to be reported')) return 'Reported';
  if (a.includes('committee'))   return 'In Committee';
  if (a.includes('referred'))    return 'Referred';
  if (a.includes('introduced'))  return 'Introduced';
  return 'Active';
}

function getStatusClass(label) {
  if (!label) return 'status-muted';
  if (label.startsWith('P.L.') || label === 'Public Law' || label.startsWith('Signed')) return 'status-enacted';
  if (label.startsWith('Passed')) return 'status-passed';
  if (label === 'Reported' || label === 'In Committee' || label === 'Vote Scheduled') return 'status-active';
  return 'status-muted';
}

function getStatusRank(label) {
  if (!label) return 0;
  if (label.startsWith('P.L.') || label === 'Public Law' || label.startsWith('Signed')) return 5;
  if (label.startsWith('Passed')) return 3;
  if (label === 'Reported' || label === 'Vote Scheduled') return 2;
  if (label === 'In Committee' || label === 'Referred') return 1;
  return 0;
}

// Strip the bureaucratic preface and tail from federal bill titles so the
// lede card reads like a headline. "A bill to require the Secretary of State
// to develop a strategy for supporting free and fair elections in Venezuela…
// and for other purposes." → "Require the Secretary of State to develop a
// strategy for supporting free and fair elections in Venezuela".
const _LEDE_TITLE_MAX = 140;
function _compactBillTitle(raw) {
  if (!raw) return '';
  let t = String(raw).trim();
  // Strip "A bill to ", "To ", "An Act to " prefix (case-insensitive)
  t = t.replace(/^(a bill to|an act to|to)\s+/i, '');
  // Strip ", and for other purposes." / "for other purposes." tail
  t = t.replace(/[,;]?\s*(and\s+)?for\s+other\s+purposes\.?\s*$/i, '');
  // Final cleanup: trim trailing punctuation/spaces, capitalize first letter
  t = t.replace(/[\s.,;:]+$/, '').trim();
  if (t.length > 0) t = t.charAt(0).toUpperCase() + t.slice(1);
  // Cap length at a word boundary
  if (t.length > _LEDE_TITLE_MAX) {
    t = t.slice(0, _LEDE_TITLE_MAX);
    const lastSpace = t.lastIndexOf(' ');
    if (lastSpace > _LEDE_TITLE_MAX - 30) t = t.slice(0, lastSpace);
    t = t.replace(/[\s.,;:]+$/, '') + '…';
  }
  return t;
}

function formatDeck(action) {
  if (!action) return '';
  let t = action
    .replace(/^Referred to the (House |Senate )?(Committee on )/, 'In committee — ')
    .replace(/^(Read twice and referred|Read the first time) to.+?Committee on (.+?)\./, 'Referred to Senate Committee on $2.')
    .replace(/^Introduced in (House|Senate)\.?$/, 'Recently introduced.')
    .replace(/Became Public Law No[\. ]+[\d-]+\.?/, 'Signed into law.')
    .replace(/^Signed by (the )?[Pp]resident\.?/, 'Signed into law by the President.')
    .replace(/^(Passed|Agreed to in) (House|Senate)/, 'Passed the $2.');
  return t.length > 150 ? t.slice(0, 147) + '…' : t;
}

function _leadCardHtml(item, idx) {
  const status = getStatusLabel(item.latest_action, item);
  const billId = item.is_state_bill
    ? `${item.identifier || ''} · ${item.state || ''}`
    : formatBillId(item.type, item.number);
  const deck = formatDeck(item.latest_action);
  return `<div class="story-lead" data-fi="${idx}">
    <div class="lead-eyebrow">
      <span class="lead-bill-id">${billId}</span>
      <span class="story-status ${getStatusClass(status)}">${status}</span>
    </div>
    <div class="lead-headline">${escapeHtml(_compactBillTitle(item.title) || billId)}</div>
    ${deck ? `<div class="lead-deck">${deck}</div>` : ''}
    <div class="lead-dateline">${item.date || ''}</div>
  </div>`;
}

function _storyCardHtml(item, idx) {
  const status = getStatusLabel(item.latest_action, item);
  const billId = item.is_state_bill
    ? (item.identifier || '')
    : formatBillId(item.type, item.number);
  const title = _compactBillTitle(item.title) || billId;
  const dual = item._matchesBoth ? '<span class="dual-match-tag">· your rep</span>' : '';
  return `<div class="story-card" data-fi="${idx}">
    <div class="story-card-top">
      <div class="story-card-body">
        <div class="story-bill-id">${billId}${dual}</div>
        <div class="story-title">${escapeHtml(title)}</div>
      </div>
      <div class="story-status ${getStatusClass(status)}">${status}</div>
    </div>
    <div class="story-date">${item.date || ''}</div>
  </div>`;
}

// ── Feed rendering ──
function _feedFollowingHtml() {
  const subs = _getSubs();
  const active = Object.entries(subs).filter(([, v]) => v.active);
  if (!active.length) return '';

  const rows = active.map(([billId, sub]) => `
    <div class="feed-following-row" onclick="reopenBillFromNotif(${JSON.stringify(billId)}, ${JSON.stringify(sub)})">
      <div class="feed-following-id">${billId}</div>
      ${sub.title ? `<div class="feed-following-title">${escapeHtml(sub.title)}</div>` : ''}
    </div>`).join('');

  return `
    <div class="section-rule" style="margin-top:1.5rem"><span>Bills You're Following</span></div>
    ${rows}
    <a class="feed-elections-more" href="#" onclick="showPage('page-notifications');loadNotificationsPage();return false">Manage →</a>`;
}

function _feedElectionsHtml(elections) {
  if (!elections || !elections.length) return '';
  const cards = elections.map(e => {
    const days = e.countdown_days ?? null;
    const cls = days !== null && days <= 30 ? 'urgent' : days !== null && days <= 90 ? 'near' : 'far';
    const date = (() => { try { return new Date(e.date + 'T12:00:00').toLocaleDateString('en-US', {month:'short',day:'numeric',year:'numeric'}); } catch { return e.date; } })();
    const detailParams = new URLSearchParams();
    const _p = getPrefs();
    if (_p?.zip)   detailParams.set('zip', _p.zip);
    if (_p?.state) detailParams.set('state', _p.state);
    return `
      <div class="feed-election-card" onclick="showPage('page-elections');loadElections()"  style="cursor:pointer">
        <span class="feed-election-countdown ${cls}">${days !== null ? days + 'd' : '?'}</span>
        <div class="feed-election-info">
          <div class="feed-election-name">${escapeHtml(e.name)}</div>
          <div class="feed-election-date">${date}</div>
        </div>
      </div>`;
  }).join('');
  return `
    <div class="section-rule" style="margin-top:1.5rem"><span>Upcoming Elections</span></div>
    ${cards}
    <a class="feed-elections-more" href="#" onclick="showPage('page-elections');loadElections();return false">All elections →</a>`;
}

// ── Helpers for newspaper home layout ──
function _initials(name) {
  return (name || '').split(/\s+/).map(p => p[0]).filter(Boolean).slice(0, 2).join('').toUpperCase();
}
function _sectionTag(item) {
  if (item.is_law) return 'Federal · Enacted';
  const t = (item.type || '').toUpperCase();
  if (t === 'HJRES' || t === 'SJRES') return 'Federal · Joint Resolution';
  if (t === 'HCONRES' || t === 'SCONRES') return 'Federal · Concurrent Resolution';
  if (t === 'HRES' || t === 'SRES') return 'Federal · Resolution';
  if (t.startsWith('S')) return 'Federal · Senate';
  if (t.startsWith('H')) return 'Federal · House';
  return 'Federal · Bill';
}
function _sponsorLastName(bioguide, prefs) {
  if (!bioguide) return '';
  const pool = [...(prefs.senators || []), prefs.representative].filter(Boolean);
  const hit = pool.find(p => p && p.bioguide_id === bioguide);
  if (!hit || !hit.name) return '';
  return hit.name.split(',')[0].split(' ').pop();
}
function _whySuffix(item) {
  const status = getStatusLabel(item.latest_action, item);
  if (status && (status.startsWith('P.L.') || status === 'Public Law' || status.startsWith('Signed'))) {
    return 'Now public law';
  }
  if (status && status.startsWith('Passed')) return status;
  if (status === 'Vote Scheduled') return 'Vote scheduled';
  const d = item.latest_action_date || item.date;
  if (!d) return '';
  try {
    const days = Math.floor((Date.now() - new Date(d + 'T12:00:00').getTime()) / 86400000);
    if (days <= 0) return 'Action today';
    if (days === 1) return 'Action yesterday';
    if (days < 7)  return 'Action this week';
  } catch {}
  return '';
}
function _repActivity(rep, items) {
  if (!rep || !rep.bioguide_id || !items || !items.length) return '';
  const hit = items.find(it => it && it.sponsor_bioguide === rep.bioguide_id);
  if (!hit) return 'No floor activity this week';
  const id = formatBillId(hit.type, hit.number);
  const title = (hit.title || '').replace(/\s*\(.*?\)\s*$/, '');
  const short = title.length > 38 ? title.slice(0, 35) + '…' : title;
  const status = getStatusLabel(hit.latest_action, hit);
  const verb = status && status.startsWith('Passed') ? 'Last vote: Yea on' : 'Last sponsored:';
  return `${verb} ${id}${short ? ` (${short})` : ''}`;
}
function _whyText(item, prefs) {
  let base;
  if (item.feed_reason === 'your_rep') {
    const last = _sponsorLastName(item.sponsor_bioguide, prefs);
    const isSen = (prefs.senators || []).some(s => s && s.bioguide_id === item.sponsor_bioguide);
    const role = isSen ? 'senator' : 'representative';
    base = last
      ? `Why: your ${role} (${last}) is the lead sponsor`
      : `Why: your ${role} is a sponsor`;
  } else if (item.feed_reason === 'state_legislature') {
    base = `Why: ${prefs.stateName || prefs.state || 'your state'} legislature`;
  } else if (item.feed_reason) {
    base = `Why: your topic — ${capitalize(item.feed_reason.replace(/_/g, ' '))}`;
  } else {
    return '';
  }
  const suffix = _whySuffix(item);
  return suffix ? `${base} · ${suffix}` : base;
}
function _electionLevels(e) {
  const contests = e.contests || [];
  if (!contests.length) {
    const n = (e.name || '').toLowerCase();
    if (n.includes('president')) return 'National';
    return '';
  }
  const FED = /\b(president|u\.?s\.? senate|u\.?s\.? house|congress)\b/i;
  const STATE = /\b(governor|lieutenant governor|attorney general|secretary of state|state senate|state house|state assembly|delegate|comptroller|treasurer)\b/i;
  const LOCAL = /\b(mayor|county|sheriff|city council|school board|alderm|borough|township)\b/i;
  const BALLOT = /\b(amendment|proposition|referend|measure|question)\b/i;
  const seen = new Set();
  for (const c of contests) {
    const o = (c.office || c.type || c.referendumTitle || '').toLowerCase();
    if (FED.test(o)) seen.add('Federal');
    else if (STATE.test(o)) seen.add('State');
    else if (LOCAL.test(o)) seen.add('Local');
    if (BALLOT.test(o)) seen.add('Ballot measures');
  }
  return [...seen].join(', ');
}
function _electionSubLine(e, days) {
  const parts = [];
  if (e.registration_deadline) {
    const m = String(e.registration_deadline).match(/([A-Za-z]+)\s+(\d{1,2})/);
    parts.push(m ? `Register by ${m[1].slice(0,3)} ${m[2]}` : `Register by ${e.registration_deadline}`);
  }
  const n = (e.contests || []).length;
  if (n) {
    parts.push(`${n} contest${n === 1 ? '' : 's'} on ballot`);
  } else if (days !== null && days > 180) {
    const months = Math.round(days / 30);
    parts.push(`Polls open ~${months} months`);
  }
  return parts.join(' · ');
}
function _relativeDate(dateStr) {
  if (!dateStr) return '';
  try {
    const d = new Date(dateStr + 'T12:00:00');
    const days = Math.floor((Date.now() - d.getTime()) / 86400000);
    if (days === 0) return 'Today';
    if (days === 1) return 'Yesterday';
    if (days < 7)  return `${days} days ago`;
    if (days < 30) return `${Math.floor(days/7)} week${Math.floor(days/7) > 1 ? 's' : ''} ago`;
    return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', year: 'numeric' });
  } catch { return dateStr; }
}

// Lede action button handler — opens the bill then triggers the requested action.
function ledeAction(idx, action) {
  const item = _feedItems[idx];
  if (!item) return;
  const opener = () => {
    if (item.is_state_bill) {
      openStateBill(item);
    } else {
      openDetail({
        congress: parseInt(item.congress),
        type: item.type,
        number: parseInt(item.number),
        title: item.title,
        is_law: item.is_law || false,
        law_number: item.law_number ? parseInt(item.law_number) : null
      });
    }
  };
  opener();
  if (action === 'notify') {
    // Notify button on bill detail appears after the bill loads.
    const tryClick = (attempts = 0) => {
      const btn = document.getElementById('notify-btn');
      if (btn && btn.style.display !== 'none') btn.click();
      else if (attempts < 20) setTimeout(() => tryClick(attempts + 1), 250);
    };
    tryClick();
  }
}
window.ledeAction = ledeAction;

function _toggleFeedExpanded() {
  _feedExpanded = !_feedExpanded;
  const prefs = getPrefs() || {};
  renderFeedSection(_feedItems, prefs, window._lastUpcomingElections || []);
}
window._toggleFeedExpanded = _toggleFeedExpanded;

function renderFeedSection(items, prefs, upcomingElections = []) {
  const feedSection = document.getElementById('feed-section');

  if (!items || !items.length) {
    feedSection.innerHTML = `
      <div class="feed-toolbar">
        <span class="feed-tag">${escapeHtml(prefs.stateName || prefs.state || '')}</span>
        <button class="feed-edit" onclick="showPage('page-onboarding');showStep(1)">Edit preferences</button>
      </div>
      <div class="empty-state">
        <p>No recent legislation found for your interests.</p>
        <p style="margin-top:0.5rem">Try updating your topics.</p>
      </div>`;
    return;
  }

  _feedItems = items;

  // Sort all items by status rank for "top stories" selection
  const sortedAll = items
    .map((item, i) => ({ item, i }))
    .sort((a, b) =>
      getStatusRank(getStatusLabel(b.item.latest_action, b.item)) -
      getStatusRank(getStatusLabel(a.item.latest_action, a.item))
    );

  // Appropriations bills can show in the ranked list but never as the
  // lede or in the 3-up grid — their titles are noise for a front page.
  const headlineEligible = sortedAll.filter(({ item }) => !item.is_appropriations);
  const ineligibleIdx = new Set(
    sortedAll
      .filter(({ item }) => item.is_appropriations)
      .map(({ i }) => i)
  );

  // Lede (top 1), 3-up (next 3), then ranked feed (everything else)
  const lede     = headlineEligible[0] || null;
  const top3     = headlineEligible.slice(1, 4);
  const usedIdx  = new Set([lede, ...top3].filter(Boolean).map(s => s.i));
  const rest     = sortedAll.filter(({ i }) => !usedIdx.has(i));
  // (appropriations remain in `rest` via ineligibleIdx; usedIdx never includes them)
  void ineligibleIdx;

  // ── Lede (placeholder slot — uses the top feed item) ──
  const ledeHtml = lede ? (() => {
    const item = lede.item;
    const status = getStatusLabel(item.latest_action, item);
    const billId = item.is_state_bill
      ? (item.identifier || '')
      : formatBillId(item.type, item.number);
    const congressLabel = item.congress ? `${item.congress}th Congress` : '';
    const title = _compactBillTitle(item.title) || billId;
    const deck  = formatDeck(item.latest_action) || '';
    const when  = _relativeDate(item.date) || 'just now';
    const sponsor = item.sponsor_name || item.sponsor || '';
    return `
      <section class="lede" data-fi="${lede.i}">
        <div class="lede-kicker">
          <span class="kicker-tag">Front Page</span>
          <span class="kicker-update">Updated ${escapeHtml(when.toLowerCase())}</span>
        </div>
        <h2 class="lede-title">${escapeHtml(title)}</h2>
        ${deck ? `<p class="lede-deck">${escapeHtml(deck)}</p>` : ''}
        <div class="lede-meta">
          <span class="lede-stage ${getStatusClass(status)}">${status}</span>
          <span class="lede-meta-sep">·</span>
          <span>${escapeHtml([congressLabel, billId].filter(Boolean).join(' · '))}</span>
          ${sponsor ? `
            <span class="lede-meta-sep">·</span>
            <span>Sponsored by <a class="lede-sponsor-link" href="#" onclick="event.preventDefault();event.stopPropagation();openMemberFromVote(${JSON.stringify({ name: sponsor }).replace(/"/g, '&quot;')})">${escapeHtml(sponsor)}</a></span>
          ` : ''}
        </div>
        <div class="lede-actions">
          <button class="btn-primary" onclick="event.stopPropagation();ledeAction(${lede.i}, 'read')">Read the explanation →</button>
          <button class="btn-ghost"  onclick="event.stopPropagation();ledeAction(${lede.i}, 'notify')">Notify me when this moves</button>
        </div>
      </section>`;
  })() : '';

  // ── 3-up story grid ──
  const stories = top3.map(({ item, i }) => {
    const status = getStatusLabel(item.latest_action, item);
    const tag    = _sectionTag(item);
    const title  = _compactBillTitle(item.title) || '';
    const deck   = formatDeck(item.latest_action) || '';
    const when   = _relativeDate(item.date);
    return `
      <article class="story" data-fi="${i}">
        <div class="story-section-tag">${tag}</div>
        <h3 class="story-title">${escapeHtml(title)}</h3>
        ${deck ? `<p class="story-deck">${escapeHtml(deck)}</p>` : ''}
        <div class="story-meta">
          <span class="story-status ${getStatusClass(status)}">${status}</span>
          ${when ? `<span>·</span><span>${when}</span>` : ''}
        </div>
      </article>`;
  }).join('');

  // ── Your Delegation (reps) ──
  const senators = (prefs.senators || []).map(r => ({ ...r, _isSen: true }));
  const rep = prefs.representative ? { ...prefs.representative, _isSen: false } : null;
  const reps = [...senators, rep].filter(Boolean);
  const repsHtml = reps.length ? reps.map(r => {
    const init = _initials(r.name);
    const loc  = r.district ? `${r.state}-${r.district}` : (r.state || '');
    const role = r._isSen ? 'U.S. Senate' : 'U.S. House';
    const partyFull = r.party || '';
    const termLine = (r.term_start && r.term_end)
      ? `Party: ${partyFull || '—'} · Term: ${r.term_start}–${r.term_end}`
      : partyFull;
    const activity = _repActivity(r, items);
    return `
      <div class="rep-card" onclick="openMemberFromVote(${JSON.stringify({ name: r.name }).replace(/"/g, '&quot;')})">
        <div class="rep-portrait">${init || '??'}</div>
        <div class="rep-body">
          <div class="rep-name">${escapeHtml(r.name || '')}</div>
          <div class="rep-role">${role} · ${loc}</div>
          <div class="rep-line">${escapeHtml(termLine)}</div>
          ${activity ? `<div class="rep-activity">${escapeHtml(activity)}</div>` : ''}
        </div>
      </div>`;
  }).join('') : '<div class="feed-empty-col">Set your zip to see your representatives.</div>';

  // ── Upcoming Elections (right col) ──
  const electionsHtml = (upcomingElections || []).slice(0, 3).map(e => {
    const days = e.countdown_days ?? null;
    const cls  = days !== null && days <= 30 ? 'urgent' : days !== null && days <= 90 ? 'near' : 'far';
    let date;
    try { date = new Date(e.date + 'T12:00:00').toLocaleDateString('en-US',
      { weekday: 'long', month: 'long', day: 'numeric', year: 'numeric' }); }
    catch { date = e.date; }
    const tags = _electionLevels(e);
    const dateLine = tags ? `${date} · ${tags}` : date;
    const sub = _electionSubLine(e, days);
    return `
      <div class="election-row ${cls}" onclick="showPage('page-elections');loadElections()">
        <div class="election-countdown">
          <div class="count-num">${days ?? '?'}</div>
          <div class="count-unit">days</div>
        </div>
        <div class="election-body">
          <div class="election-name">${escapeHtml(e.name || '')}</div>
          <div class="election-date">${escapeHtml(dateLine)}</div>
          ${sub ? `<div class="election-line">${escapeHtml(sub)}</div>` : ''}
        </div>
      </div>`;
  }).join('') || '<div class="feed-empty-col">No upcoming elections.</div>';

  // ── Ranked feed (the rest) ──
  const visibleRest = _feedExpanded ? rest : rest.slice(0, FEED_COLLAPSED_COUNT);
  const hiddenCount = rest.length - visibleRest.length;
  const showMoreHtml = hiddenCount > 0
    ? `<a class="feed-show-more" href="#" onclick="_toggleFeedExpanded();return false">Show more →</a>`
    : (_feedExpanded && rest.length > FEED_COLLAPSED_COUNT
        ? `<a class="feed-show-more" href="#" onclick="_toggleFeedExpanded();return false">Show less ↑</a>`
        : '');
  const feedRows = visibleRest.map(({ item, i }, idx) => {
    const status = getStatusLabel(item.latest_action, item);
    const billId = item.is_state_bill
      ? (item.identifier || '')
      : formatBillId(item.type, item.number);
    const title = _compactBillTitle(item.title) || billId;
    const rank  = String(idx + 1).padStart(2, '0');
    const why   = _whyText(item, prefs);
    return `
      <a class="feed-row" data-fi="${i}">
        <span class="feed-rank">${rank}</span>
        <div class="feed-body">
          <div class="feed-line">
            <span class="feed-id">${billId}</span>
            <span class="feed-sep">·</span>
            <span class="feed-title">${escapeHtml(title.length > 100 ? title.slice(0, 97) + '…' : title)}</span>
          </div>
          ${why ? `<div class="feed-sub"><span class="feed-reason">${why}</span></div>` : ''}
        </div>
        <span class="feed-status ${getStatusClass(status)}">${status}</span>
      </a>`;
  }).join('');

  // ── Toolbar tag ──
  const interestsLabel = (prefs.interests || []).map(t =>
    capitalize(String(t).replace(/_/g, ' '))
  ).join(', ');
  const toolbarTag = ['Personalized', prefs.state || prefs.stateName, interestsLabel]
    .filter(Boolean).join(' · ');

  // ── Compose ──
  feedSection.innerHTML = `
    ${ledeHtml}

    <section class="grid-3up">${stories}</section>

    <div class="section-divider"><span>The Briefing</span></div>

    <section class="two-col">
      <div class="col-reps">
        <div class="col-head">
          <span class="col-label">Your Delegation</span>
          <a class="col-action" onclick="showPage('page-onboarding');showStep(1)">Change zip →</a>
        </div>
        ${repsHtml}
      </div>
      <div class="col-elections">
        <div class="col-head">
          <span class="col-label">Upcoming Elections</span>
          <a class="col-action" onclick="showPage('page-elections');loadElections()">All elections →</a>
        </div>
        ${electionsHtml}
      </div>
    </section>

    <div class="section-divider"><span>What's Moving</span></div>

    <section class="feed">
      <div class="feed-toolbar">
        <span class="feed-tag">${escapeHtml(toolbarTag)}</span>
        <button class="feed-edit" onclick="showPage('page-onboarding');showStep(1)">Edit topics</button>
      </div>
      ${feedRows}
      ${showMoreHtml}
      ${_feedFollowingHtml()}
    </section>
  `;

  // Attach click handlers after DOM is set
  feedSection.querySelectorAll('[data-fi]').forEach(el => {
    const item = _feedItems[parseInt(el.dataset.fi)];
    el.onclick = () => {
      if (item.is_state_bill) {
        openStateBill(item);
      } else {
        openDetail({
          congress: parseInt(item.congress),
          type: item.type,
          number: parseInt(item.number),
          title: item.title,
          is_law: item.is_law || false,
          law_number: item.law_number ? parseInt(item.law_number) : null
        });
      }
    };
  });
}

async function loadFeed() {
  const prefs = getPrefs();
  const feedSection = document.getElementById('feed-section');

  if (!prefs) {
    feedSection.innerHTML = `
      <div class="onboarding-prompt">
        <div class="onboarding-prompt-title">Your personal civic feed</div>
        <div class="onboarding-prompt-text">
          Tell us where you live and what issues matter to you.<br>
          We'll curate a daily feed of legislation that affects you.<br>
          No account needed. Stored only in your browser.
        </div>
        <button class="onboarding-start-btn" onclick="showPage('page-onboarding');showStep(1)">
          Set up my feed →
        </button>
      </div>`;
    return;
  }

  feedSection.innerHTML = `<div style="padding:2rem 0;text-align:center;font-family:'IBM Plex Mono',monospace;font-size:0.7rem;color:var(--muted)">Loading your feed...</div>`;

  try {
    const electionParams = new URLSearchParams();
    if (prefs.zip)   electionParams.set('zip',   prefs.zip);
    if (prefs.state) electionParams.set('state', prefs.state);

    const [feedResp, electionsResp] = await Promise.all([
      fetch('/feed', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          interests: prefs.interests,
          senator_bioguides: prefs.senators.map(s => s.bioguide_id),
          rep_bioguide: prefs.representative ? prefs.representative.bioguide_id : null,
          state_code: prefs.state || null
        })
      }),
      fetch(`/api/elections?${electionParams}`).catch(() => null),
    ]);

    const data = await feedResp.json();
    const electionsData = electionsResp ? await electionsResp.json().catch(() => null) : null;

    window._lastUpcomingElections = ((electionsData && electionsData.upcoming) || []).slice(0, 3);
    _feedExpanded = false;
    renderFeedSection(data.items, prefs, window._lastUpcomingElections);

    // Pre-populate the elections page so clicking the tab is instant
    if (electionsData) _renderElectionsPage(electionsData, prefs?.zip);

  } catch(err) {
    feedSection.innerHTML = `<div class="empty-state"><p>Could not load feed.</p></div>`;
  }
}

// ── Committee page ──
function renderCommitteePage(data) {
  const c = data.committee;
  resultsSection.innerHTML = '';

  const header = document.createElement('div');
  header.className = 'results-header';
  header.innerHTML = `
      <h2>${c.name}</h2>
      <span class="results-count">${c.chamber}</span>`;
  resultsSection.appendChild(header);

  if (!data.bills || !data.bills.length) {
      resultsSection.innerHTML += `<div class="empty-state"><p>No recent bills found for this committee.</p></div>`;
      return;
  }

  data.bills.forEach((bill, i) => {
      const card = document.createElement('div');
      card.className = 'result-card';
      card.style.animationDelay = (i * 0.05) + 's';
      card.onclick = () => openDetail({
          congress: parseInt(bill.congress),
          type: bill.type,
          number: parseInt(bill.number),
          title: bill.title,
          date: bill.date,
          latest_action: bill.latest_action,
      });

      const billId = billIdFromParts(bill.type, bill.number);

      card.innerHTML = `
          <div class="result-card-inner">
              <div class="result-card-left">
                  <div class="result-bill-id">${billId}</div>
                  <div class="result-bill-title">${escapeHtml(bill.title || billId)}</div>
              </div>
              <div class="result-card-arrow">Read more →</div>
          </div>
          <div class="result-meta">
              <div class="meta-item"><strong>${formatCongress(bill.congress)} Congress</strong></div>
              <div class="meta-item"><strong>${bill.date || ''}</strong></div>
              <div class="meta-item">${bill.latest_action ? bill.latest_action.slice(0,50) : ''}</div>
          </div>`;

      resultsSection.appendChild(card);
  });
}

// ── Search results ──
function _makeResultCard(bill, animIndex) {
  const card = document.createElement('div');
  card.className = 'result-card';
  card.style.animationDelay = (animIndex * 0.05) + 's';
  card.onclick = () => openDetail({
    congress: parseInt(bill.congress),
    type: bill.type,
    number: parseInt(bill.number),
    title: bill.title,
    is_law: bill.is_law,
    law_number: bill.law_number ? parseInt(bill.law_number) : null
  });
  const billId = bill.is_law
    ? `Public Law ${bill.congress}-${bill.law_number}`
    : billIdFromParts(bill.type || '', bill.number || '');
  const congress = bill.congress ? `${formatCongress(bill.congress)} Congress` : '';
  const date = bill.date_issued ? bill.date_issued.slice(0, 4) : '';
  card.innerHTML = `
    <div class="result-card-inner">
      <div class="result-card-left">
        <div class="result-bill-id">${billId}</div>
        <div class="result-bill-title">${escapeHtml(bill.title || billId)}</div>
      </div>
      <div class="result-card-arrow">Read more →</div>
    </div>
    <div class="result-meta">
      <div class="meta-item"><strong>${congress}</strong></div>
      <div class="meta-item"><strong>${date}</strong></div>
      <div class="meta-item">Click for full analysis</div>
    </div>`;
  return card;
}

function _makeCompactResultCard(bill, animIndex) {
  const card = document.createElement('div');
  card.className = 'result-card--compact';
  card.style.animationDelay = (animIndex * 0.03) + 's';
  card.onclick = () => openDetail({
    congress: parseInt(bill.congress),
    type: bill.type,
    number: parseInt(bill.number),
    title: bill.title,
    is_law: bill.is_law,
    law_number: bill.law_number ? parseInt(bill.law_number) : null
  });
  const billId = bill.is_law
    ? `Public Law ${bill.congress}-${bill.law_number}`
    : billIdFromParts(bill.type || '', bill.number || '');
  const year = bill.date_issued ? bill.date_issued.slice(0, 4) : '';
  const congress = bill.congress ? `${formatCongress(bill.congress)} Congress` : '';
  card.innerHTML = `
    <div class="result-bill-id">${billId}</div>
    <div class="result-bill-title">${escapeHtml(bill.title || billId)}</div>
    <div class="compact-year">${congress}${year ? ' · ' + year : ''}</div>`;
  return card;
}

function _appendShowMoreFooter() {
  const footer = document.createElement('div');
  footer.id = 'results-footer';
  footer.style.cssText = 'margin-top:1.5rem;padding-top:1rem;border-top:1px solid var(--rule);display:flex;flex-direction:column;align-items:center;gap:0.75rem';
  footer.innerHTML = `
    <button id="show-more-btn" class="show-more-btn" onclick="loadMoreResults()">Show more</button>
    <button class="feed-settings-btn" onclick="showSearchFlag()">
      Didn't find what you were looking for? Flag this search
    </button>`;
  resultsSection.appendChild(footer);
}

async function loadMoreResults() {
  const btn = document.getElementById('show-more-btn');
  if (btn) { btn.textContent = 'Loading…'; btn.disabled = true; }

  const enteringHistory = !_searchState.fullHistory && !_searchState.isState;
  if (enteringHistory) {
    _searchState.fullHistory = true;
    _searchState.maxResults  = 50;
  } else {
    _searchState.maxResults += 20;
  }

  try {
    let body;
    if (_searchState.isState) {
      body = { question: _searchState.question, state_code: _searchState.stateCode, max_results: _searchState.maxResults };
    } else {
      body = {
        question: _searchState.question,
        max_results: _searchState.maxResults,
        full_history: _searchState.fullHistory,
        ...((_searchState.beforeCongress != null) ? { before_congress: _searchState.beforeCongress } : {})
      };
    }

    const res = await fetch(_searchState.endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body)
    });
    if (!res.ok) throw new Error();
    const data = await res.json();

    // History is a fresh independent fetch — deduplicate against already-shown results
    const seenIds = new Set(currentResults.map(r => `${r.congress}${r.type}${r.number}`));
    const freshResults = _searchState.fullHistory && !_searchState.isState
      ? (data.results || []).filter(r => !seenIds.has(`${r.congress}${r.type}${r.number}`))
      : (data.results || []).slice(currentResults.length);

    currentResults = [...currentResults, ...freshResults];
    const newResults = freshResults;

    const footer = document.getElementById('results-footer');
    if (footer) footer.remove();

    const countEl = resultsSection.querySelector('.results-count');
    if (countEl) countEl.textContent = `${currentResults.length} result${currentResults.length !== 1 ? 's' : ''} found`;

    if (_searchState.fullHistory && !_searchState.isState) {
      if (enteringHistory && newResults.length > 0) {
        const divider = document.createElement('div');
        divider.className = 'history-divider';
        divider.innerHTML = `<span>More results through history</span>`;
        resultsSection.appendChild(divider);
        const grid = document.createElement('div');
        grid.className = 'history-grid';
        grid.id = 'history-grid';
        resultsSection.appendChild(grid);
      }
      const grid = document.getElementById('history-grid');
      newResults.forEach((bill, i) => {
        const card = _makeCompactResultCard(bill, i);
        (grid || resultsSection).appendChild(card);
      });
    } else {
      newResults.forEach((bill, i) => {
        const card = _searchState.isState ? _makeStateCard(bill, i) : _makeResultCard(bill, i);
        resultsSection.appendChild(card);
      });
    }

    if (newResults.length === 0 || (data.results || []).length < _searchState.maxResults) {
      const footer2 = document.createElement('div');
      footer2.style.cssText = 'margin-top:1.5rem;padding-top:1rem;border-top:1px solid var(--rule);text-align:center';
      footer2.innerHTML = `<button class="feed-settings-btn" onclick="showSearchFlag()">Didn't find what you were looking for? Flag this search</button>`;
      resultsSection.appendChild(footer2);
    } else {
      _appendShowMoreFooter();
    }
  } catch {
    if (btn) { btn.textContent = 'Show more'; btn.disabled = false; }
  }
}

function renderResults(data) {
  resultsSection.innerHTML = '';
  currentResults = data.results || [];

  _searchState.question    = input.value.trim();
  _searchState.maxResults      = 10;
  _searchState.endpoint        = '/search';
  _searchState.isState         = false;
  _searchState.fullHistory     = false;
  // Store smallest congress from initial search so history starts before it
  const initialCongresses = data.query?.congress_numbers || [];
  _searchState.beforeCongress  = initialCongresses.length ? Math.min(...initialCongresses) : null;

  currentSearchContext = {
    query: _searchState.question,
    expanded_terms: data.query?.expanded_terms || [],
    congress_numbers: data.query?.congress_numbers || [],
    confidence: data.confidence || 1.0,
    results_shown: currentResults.map(r => ({
      bill_id: `${(r.type||'').toUpperCase()}${r.number}`,
      title: r.title,
      date: r.date_issued
    }))
  };

  if (!currentResults.length) {
    resultsSection.innerHTML = `
      <div class="empty-state">
        <p>No bills found for that query.</p>
        <p style="margin-top:0.5rem">Try different keywords or a broader question.</p>
      </div>`;
    return;
  }

  const header = document.createElement('div');
  header.className = 'results-header';
  header.innerHTML = `
    <h2>Search Results</h2>
    <span class="results-count">${currentResults.length} result${currentResults.length !== 1 ? 's' : ''} found</span>`;
  resultsSection.appendChild(header);

  currentResults.forEach((bill, i) => resultsSection.appendChild(_makeResultCard(bill, i)));

  _appendShowMoreFooter();
}

// ── Clarification bar ──
function showClarificationBar(confidence, reason, question) {
  if (confidence >= 0.7 || !reason) return;

  const bar = document.createElement('div');
  bar.style.cssText = `
      border-left: 2px solid var(--accent);
      padding: 0.75rem 1rem;
      margin-bottom: 1.5rem;
      background: var(--card-bg);
      border: 1px solid var(--rule);
  `;
  bar.innerHTML = `
      <div style="font-family:'IBM Plex Mono',monospace;font-size:0.6rem;
                  letter-spacing:0.15em;text-transform:uppercase;
                  color:var(--muted);margin-bottom:0.5rem">
          Ambiguous query · confidence ${Math.round(confidence * 100)}%
      </div>
      <div style="font-size:0.88rem;color:var(--ink);margin-bottom:0.75rem">
          ${reason}
      </div>
      <div style="display:flex;gap:0.5rem;flex-wrap:wrap">
          <button class="pill"
              onclick="setQuery('${question} legislation');runSearch()">
              Search as legislation
          </button>
          <button class="pill"
              onclick="setQuery('${question} senator');runSearch()">
              Search as person
          </button>
      </div>
  `;
  resultsSection.insertBefore(bar, resultsSection.firstChild);
}

// ── Jurisdiction toggle ──
function setJurisdiction(j, el) {
  currentJurisdiction = j;
  document.querySelectorAll('.jurisdiction-btn').forEach(b => b.classList.remove('active'));
  el.classList.add('active');
}

const STATE_CODES = ['AL','AK','AZ','AR','CA','CO','CT','DE','FL','GA','HI','ID','IL','IN','IA','KS','KY','LA','ME','MD','MA','MI','MN','MS','MO','MT','NE','NV','NH','NJ','NM','NY','NC','ND','OH','OK','OR','PA','RI','SC','SD','TN','TX','UT','VT','VA','WA','WV','WI','WY'];
const STATE_NAMES_BY_CODE = {
  AL:"Alabama",AK:"Alaska",AZ:"Arizona",AR:"Arkansas",CA:"California",
  CO:"Colorado",CT:"Connecticut",DE:"Delaware",FL:"Florida",GA:"Georgia",
  HI:"Hawaii",ID:"Idaho",IL:"Illinois",IN:"Indiana",IA:"Iowa",KS:"Kansas",
  KY:"Kentucky",LA:"Louisiana",ME:"Maine",MD:"Maryland",MA:"Massachusetts",
  MI:"Michigan",MN:"Minnesota",MS:"Mississippi",MO:"Missouri",MT:"Montana",
  NE:"Nebraska",NV:"Nevada",NH:"New Hampshire",NJ:"New Jersey",NM:"New Mexico",
  NY:"New York",NC:"North Carolina",ND:"North Dakota",OH:"Ohio",OK:"Oklahoma",
  OR:"Oregon",PA:"Pennsylvania",RI:"Rhode Island",SC:"South Carolina",
  SD:"South Dakota",TN:"Tennessee",TX:"Texas",UT:"Utah",VT:"Vermont",
  VA:"Virginia",WA:"Washington",WV:"West Virginia",WI:"Wisconsin",WY:"Wyoming"
};
function stateNameFromCode(code) {
  return STATE_NAMES_BY_CODE[(code || '').toUpperCase()] || code || '';
}
let _stateIdx = STATE_CODES.indexOf('VA');

function _renderStatePicker() {
  const cur   = document.getElementById('state-cur');
  const cells = document.querySelectorAll('#state-picker .state-cell');
  const up    = document.getElementById('state-up');
  const down  = document.getElementById('state-down');
  if (!cur) return;
  cur.textContent = STATE_CODES[_stateIdx];
  cells.forEach(c => {
    const rel = parseInt(c.dataset.rel);
    const i = _stateIdx + rel;
    if (i < 0 || i >= STATE_CODES.length) {
      c.textContent = '';
      c.classList.add('empty');
      c.dataset.idx = '';
    } else {
      c.textContent = STATE_CODES[i];
      c.classList.remove('empty');
      c.dataset.idx = String(i);
    }
  });
  up   && up.classList.toggle('disabled', _stateIdx <= 0);
  down && down.classList.toggle('disabled', _stateIdx >= STATE_CODES.length - 1);
}

function _shiftState(delta) {
  const next = Math.max(0, Math.min(STATE_CODES.length - 1, _stateIdx + delta));
  if (next === _stateIdx) return;
  _stateIdx = next;
  currentStateCode = STATE_CODES[_stateIdx];
  _renderStatePicker();
  const picker = document.getElementById('state-picker');
  setJurisdiction('state', picker);
}

function _initStatePicker() {
  const picker = document.getElementById('state-picker');
  if (!picker || picker._wired) return;
  picker._wired = true;
  const up    = document.getElementById('state-up');
  const down  = document.getElementById('state-down');
  const cur   = document.getElementById('state-cur');
  const cells = picker.querySelectorAll('.state-cell');
  up.addEventListener('click',   e => { e.stopPropagation(); _shiftState(-1); });
  down.addEventListener('click', e => { e.stopPropagation(); _shiftState(1); });
  cur.addEventListener('click',  e => { e.stopPropagation(); setJurisdiction('state', picker); });
  picker.addEventListener('wheel', e => {
    if (!picker.matches(':hover, :focus-within')) return;
    e.preventDefault();
    _shiftState(e.deltaY > 0 ? 1 : -1);
  }, { passive: false });
  cells.forEach(c => c.addEventListener('click', e => {
    e.stopPropagation();
    if (!c.dataset.idx) return;
    _stateIdx = parseInt(c.dataset.idx);
    currentStateCode = STATE_CODES[_stateIdx];
    _renderStatePicker();
    setJurisdiction('state', picker);
  }));
  picker.addEventListener('keydown', e => {
    if (e.key === 'ArrowUp')   { e.preventDefault(); _shiftState(-1); return; }
    if (e.key === 'ArrowDown') { e.preventDefault(); _shiftState(1);  return; }
    if (!/^[a-zA-Z]$/.test(e.key)) return;
    const letter = e.key.toUpperCase();
    const start = (_stateIdx + 1) % STATE_CODES.length;
    const order = [...STATE_CODES.slice(start), ...STATE_CODES.slice(0, start)];
    const hit = order.findIndex(s => s.startsWith(letter));
    if (hit < 0) return;
    _stateIdx = STATE_CODES.indexOf(order[hit]);
    currentStateCode = STATE_CODES[_stateIdx];
    _renderStatePicker();
    setJurisdiction('state', picker);
  });
  _renderStatePicker();
}

function updateJurisdictionToggle(prefs) {
  _initStatePicker();
  if (prefs && prefs.state && STATE_CODES.includes(prefs.state)) {
    _stateIdx = STATE_CODES.indexOf(prefs.state);
    currentStateCode = prefs.state;
  } else {
    currentStateCode = STATE_CODES[_stateIdx];
  }
  _renderStatePicker();
}

// ── State search ──
async function runStateSearch(question, stateCode) {
  btn.disabled = true;
  resultsSection.innerHTML = '';
  clearStatus();
  showPage('page-home');
  const stateName = stateNameFromCode(stateCode) || stateCode || 'State';
  setStatus(`Searching ${stateName}...`);

  try {
    const response = await fetch('/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, state_code: stateCode, max_results: 10 })
    });
    if (!response.ok) throw new Error(`Server error: ${response.status}`);
    const data = await response.json();
    clearStatus();
    if (data.query_type === 'state_member' && data.member) {
      renderMemberPage(data);
      showPage('page-member');
    } else if (data.query_type === 'state_member' && !data.member) {
      resultsSection.innerHTML = `
        <div class="empty-state">
          <p>No legislator found by that name.</p>
          <p style="margin-top:0.5rem">Try searching for a state lawmaker — governors and other officials aren't included.</p>
        </div>`;
    } else {
      renderStateResults(data);
      resultsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  } catch (err) {
    clearStatus();
    resultsSection.innerHTML = `
      <div class="empty-state">
        <p>State search failed. Please try again.</p>
      </div>`;
  } finally {
    btn.disabled = false;
  }
}

function _makeStateCard(bill, animIndex) {
  const card = document.createElement('div');
  card.className = 'result-card';
  card.style.animationDelay = (animIndex * 0.05) + 's';
  card.onclick = () => openStateBill(bill);

  const chamberLabel = bill.chamber === 'lower' ? 'House' : bill.chamber === 'upper' ? 'Senate' : (bill.chamber || '');
  const stateName = stateNameFromCode(bill.state) || bill.state || '';
  const abstractSnippet = bill.abstract ? bill.abstract.slice(0, 140) + (bill.abstract.length > 140 ? '…' : '') : '';
  const sponsorLine = bill.sponsor ? `<div class="meta-item">Sponsor: ${bill.sponsor}</div>` : '';

  card.innerHTML = `
    <div class="result-card-inner">
      <div class="result-card-left">
        <div class="result-bill-id">${escapeHtml(bill.identifier)} · ${escapeHtml(stateName)} · ${chamberLabel}</div>
        <div class="result-bill-title">${escapeHtml(bill.title)}</div>
        ${abstractSnippet ? `<div class="result-bill-abstract">${abstractSnippet}</div>` : ''}
      </div>
      <div class="result-card-arrow">Read more →</div>
    </div>
    <div class="result-meta">
      <div class="meta-item"><strong>${bill.latest_action_date || ''}</strong></div>
      <div class="meta-item">${(bill.latest_action || '').slice(0, 60)}</div>
      ${sponsorLine}
    </div>`;
  return card;
}

function renderStateResults(data) {
  resultsSection.innerHTML = '';
  currentResults = data.results || [];

  _searchState.question    = input.value.trim();
  _searchState.maxResults  = 10;
  _searchState.endpoint    = '/search';
  _searchState.isState     = true;
  _searchState.stateCode   = currentStateCode;
  _searchState.fullHistory = false;

  // Authoritative state for this result set comes from the backend response,
  // not from getPrefs() which can be stale after the user changes states.
  const responseStateCode = (data.state_code || currentStateCode || '').toUpperCase();
  const stateName = stateNameFromCode(responseStateCode) || responseStateCode || 'State';

  // Off-topic — show the polite empty state with the router's reason text.
  if (data.query_type === 'off_topic') {
    resultsSection.innerHTML = `
      <div class="empty-state">
        <p>${escapeHtml(data.ambiguity_reason || `That doesn't look like a question about ${stateName} legislation.`)}</p>
      </div>`;
    return;
  }

  // Jurisdiction nudge — router thinks the user meant a federal query.
  if (data.suggested_jurisdiction === 'federal') {
    const nudge = document.createElement('div');
    nudge.className = 'jurisdiction-nudge';
    nudge.innerHTML =
      `Searching ${escapeHtml(stateName)}. ` +
      `<a href="#" onclick="event.preventDefault();_switchToFederalAndSearch()">Switch to Federal? →</a>`;
    resultsSection.appendChild(nudge);
  }

  // Ambiguity / cross-session note (e.g. "showing HB 1557 from 2024 — add a year…")
  if (data.ambiguity_reason && data.query_type !== 'off_topic') {
    const note = document.createElement('div');
    note.className = 'result-ambiguity-note';
    note.textContent = data.ambiguity_reason;
    resultsSection.appendChild(note);
  }

  if (!currentResults.length) {
    const empty = document.createElement('div');
    empty.className = 'empty-state';
    empty.innerHTML =
      `<p>No ${escapeHtml(stateName)} bills found.</p>` +
      `<p style="margin-top:0.5rem">Try different keywords, or check the bill ID and session year.</p>`;
    resultsSection.appendChild(empty);
    return;
  }

  const header = document.createElement('div');
  header.className = 'results-header';
  header.innerHTML = `
    <h2>${escapeHtml(stateName)} Results</h2>
    <span class="results-count">${currentResults.length} result${currentResults.length !== 1 ? 's' : ''} found</span>`;
  resultsSection.appendChild(header);

  currentResults.forEach((bill, i) => resultsSection.appendChild(_makeStateCard(bill, i)));

  _appendShowMoreFooter();
}

function _switchToFederalAndSearch() {
  if (typeof setJurisdiction === 'function') setJurisdiction('federal');
  runSearch();
}

async function openStateBill(bill) {
  const myToken = ++_detailToken;
  previousPage = document.querySelector('.page.active').id;
  _currentBill = { ...bill, is_state_bill: true };

  const stateLabel = stateNameFromCode(bill.state) || bill.state || '';
  const billId = `${bill.identifier}${stateLabel ? ` · ${stateLabel}` : ''}`;
  document.getElementById('detail-bill-id').textContent = billId;
  document.getElementById('detail-bill-title').textContent = bill.title || bill.identifier;
  document.getElementById('detail-loading').style.display = 'block';
  document.getElementById('detail-content').style.display = 'none';
  document.getElementById('votes-section').style.display = 'none';
  showPage('page-detail');

  let revealed = false;
  const ensureRevealed = () => {
    if (revealed) return;
    _revealDetail(bill.title || bill.identifier);
    // OpenStates has no cosponsor/connections/lobbying data — keep those
    // federal-only sections hidden and free of any prior bill's content.
    renderSponsors([], []);
    renderConnections(null);
    revealed = true;
  };

  const handleSection = (msg) => {
    if (myToken !== _detailToken) return;
    switch (msg.section) {
      case 'meta':
        ensureRevealed();
        // Enrich _currentBill so renderFullText can deep-link to the state's
        // own bill page when no text is available.
        _currentBill = { ..._currentBill, source_url: msg.source_url || _currentBill.source_url, sources: msg.sources || [] };
        _initNotifyBtn(billId, null, null, null, bill.ocd_id);
        break;
      case 'translation':
        ensureRevealed();
        renderExplanation(msg.translation || '');
        if (typeof setCurrentBillContext === 'function') {
          setCurrentBillContext({
            bill_id:      billId,
            bill_title:   bill.title || bill.identifier,
            bill_summary: msg.translation || '',
            latest_action: bill.latest_action || '',
            _reopen: { type: 'state', ocd_id: bill.ocd_id, identifier: bill.identifier, title: bill.title, state: bill.state },
          });
        }
        break;
      case 'timeline':
        ensureRevealed();
        renderTimeline(msg.timeline_events, msg.timeline);
        break;
      case 'votes':
        ensureRevealed();
        renderVotes(msg.votes, true);
        break;
      case 'bill_text':
        ensureRevealed();
        renderFullText(msg.bill_text);
        break;
      case 'done':
        break;
    }
  };

  try {
    const response = await fetch('/state/bill', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ocd_id: bill.ocd_id, state_code: bill.state })
    });
    if (!response.ok || !response.body) throw new Error(`Error: ${response.status}`);

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      if (myToken !== _detailToken) { try { await reader.cancel(); } catch (e) {} return; }
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf('\n')) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (!line) continue;
        try { handleSection(JSON.parse(line)); }
        catch (e) { console.error('bad state stream line', e, line); }
      }
    }
    const tail = buffer.trim();
    if (tail) { try { handleSection(JSON.parse(tail)); } catch (e) {} }
    if (myToken === _detailToken) ensureRevealed();

  } catch (err) {
    document.getElementById('detail-loading').innerHTML = `
      <div class="empty-state">
        <p>This bill couldn't be loaded right now.</p>
      </div>`;
    document.getElementById('detail-loading').style.display = 'block';
    document.getElementById('detail-content').style.display = 'none';
  }
}

// ── Main search ──
function searchForAct(actName) {
  if (!actName) return;
  if (currentJurisdiction !== 'federal') setJurisdiction('federal');
  input.value = actName;
  runSearch();
}

async function runSearch() {
  const question = input.value.trim();
  if (!question) return;

  if (currentJurisdiction === 'state' && currentStateCode) {
    return await runStateSearch(question, currentStateCode);
  }

  btn.disabled = true;
  resultsSection.innerHTML = '';
  clearStatus();
  showPage('page-home');

  setStatus('Understanding your question...');

  let searchStatusTimer = null;
  try {
    searchStatusTimer = setTimeout(() => setStatus('Searching federal legislation...'), 500);

    const response = await fetch('/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ question, max_results: 10 })
    });

    if (!response.ok) throw new Error(`Server error: ${response.status}`);
    const data = await response.json();
    clearTimeout(searchStatusTimer);
    clearStatus();

    if (data.confidence < 0.7 && data.ambiguity_reason) {
      showClarificationBar(data.confidence, data.ambiguity_reason, question);
    }

    if (data.query_type === 'member') {
      if (data.found) {
        renderMemberPage(data);
      } else {
        resultsSection.innerHTML = `
          <div class="empty-state">
            <p>Member not found.</p>
            <p style="margin-top:0.5rem">Try using their full name.</p>
          </div>`;
      }
    } else if (data.query_type === 'committee') {
      if (data.found) {
        renderCommitteePage(data);
      } else {
        resultsSection.innerHTML = `
          <div class="empty-state">
            <p>Committee not found.</p>
            <p style="margin-top:0.5rem">Try the full committee name.</p>
          </div>`;
      }
    } else {
      renderResults(data);
      resultsSection.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }

  } catch (err) {
    clearTimeout(searchStatusTimer);
    clearStatus();
    resultsSection.innerHTML = `
      <div class="empty-state">
        <p>Something went wrong. Is the server running?</p>
        <p style="margin-top:0.5rem;font-size:0.6rem">${err.message}</p>
      </div>`;
  } finally {
    btn.disabled = false;
  }
}

// ── Onboarding ──
let onboardingData = { zip: null, state: null, district: null, geoid: null, senators: [], representative: null, interests: [] };

function showStep(n) {
  document.querySelectorAll('.onboarding-step').forEach(s => s.classList.remove('active'));
  document.getElementById(`step-${n}`).classList.add('active');
  if (n === 1) ensureDistrictMap();  // lazy-load the ~290KB boundary file only when needed
}

async function handleZipInput(val) {
  if (val.length === 5 && /^\d{5}$/.test(val)) {
    await lookupZip(val);
  } else {
    document.getElementById('zip-result').classList.remove('visible');
    document.getElementById('step1-btn').disabled = true;
  }
}

// Every resolution path (address, location, map click, zip fallback) funnels
// through here so the reps preview + onboarding state are populated the same
// way and, when we know the exact district, the map highlights it.
function renderResolution(data) {
  onboardingData.state = data.state;
  onboardingData.senators = data.senators || [];
  onboardingData.representative = data.representative;
  onboardingData.district = data.district_label || null;
  onboardingData.geoid = data.geoid || null;

  const badge = document.getElementById('district-badge');
  if (badge) badge.textContent = data.district_label ? `· ${data.district_label}` : '';

  const all = [
    ...(data.senators || []).map(s => ({ ...s, label: 'Senator' })),
    data.representative ? { ...data.representative, label: 'Rep.' } : null
  ].filter(Boolean);
  document.getElementById('zip-reps').innerHTML = all.map(p => `
    <div class="zip-rep-row">
      <div class="zip-rep-chamber">${p.label}${p.district ? ' · Dist. ' + p.district : ''}</div>
      <div class="zip-rep-name">${p.name}</div>
      <div class="zip-rep-party">${p.party}</div>
    </div>`).join('') ||
    '<div class="zip-rep-empty">No representative on file for this district.</div>';

  document.getElementById('zip-result').classList.add('visible');
  document.getElementById('step1-btn').disabled = false;
  if (data.geoid) highlightDistrict(data.geoid, true);
}

function resolveError(msg) {
  document.getElementById('zip-reps').innerHTML =
    `<div class="zip-rep-empty" style="color:var(--accent)">${msg}</div>`;
  document.getElementById('zip-result').classList.add('visible');
}

async function lookupZip(zip) {
  zip = zip || document.getElementById('zip-input').value;
  if (!/^\d{5}$/.test(zip)) return;
  try {
    const res = await fetch('/resolve-zip', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ zip_code: zip }) });
    if (!res.ok) throw new Error('Not found');
    const data = await res.json();
    onboardingData.zip = zip;
    renderResolution(data);
  } catch (e) {
    resolveError('Could not find representatives for this zip code.');
  }
}

async function lookupAddress() {
  const address = document.getElementById('address-input').value.trim();
  if (address.length < 6) return;
  const hint = document.getElementById('addr-hint');
  hint.textContent = 'Locating your district…';
  try {
    const res = await fetch('/resolve-address', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ address }) });
    if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || 'not found');
    const data = await res.json();
    hint.textContent = data.matched_address ? `Matched: ${data.matched_address}` : '';
    renderResolution(data);
  } catch (e) {
    hint.textContent = '';
    resolveError('Could not find that address. Check it, or try the zip fallback below.');
  }
}

function useMyLocation() {
  const hint = document.getElementById('addr-hint');
  if (!navigator.geolocation) { hint.textContent = 'Location is not available in this browser.'; return; }
  hint.textContent = 'Requesting your location…';
  navigator.geolocation.getCurrentPosition(async pos => {
    try {
      const res = await fetch('/resolve-point', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ lat: pos.coords.latitude, lon: pos.coords.longitude }) });
      if (!res.ok) throw new Error('not found');
      hint.textContent = '';
      renderResolution(await res.json());
    } catch (e) { hint.textContent = ''; resolveError('Could not resolve your location to a district.'); }
  }, () => { hint.textContent = 'Location permission denied — type an address instead.'; },
     { enableHighAccuracy: false, timeout: 10000 });
}

async function resolveGeoid(geoid) {
  try {
    const res = await fetch('/resolve-district', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ geoid }) });
    if (!res.ok) throw new Error('not found');
    renderResolution(await res.json());
  } catch (e) { resolveError('Could not load that district.'); }
}

// --- Congressional-district map. Boundaries are the Census 119th-Congress
// cartographic file (vendored TopoJSON); d3-geo projects them. Like the
// Foundry map, the overview is STATES: click a state to zoom into its
// districts, then click a district for its reps. State shapes are merged
// from the district topology so their borders line up exactly.
const CD_NS = 'http://www.w3.org/2000/svg';
let _cdTopo = null, _cdScope = null, _stateFeats = null;

const CD_FIPS_USPS = {
  "01":"AL","02":"AK","04":"AZ","05":"AR","06":"CA","08":"CO","09":"CT","10":"DE",
  "11":"DC","12":"FL","13":"GA","15":"HI","16":"ID","17":"IL","18":"IN","19":"IA",
  "20":"KS","21":"KY","22":"LA","23":"ME","24":"MD","25":"MA","26":"MI","27":"MN",
  "28":"MS","29":"MO","30":"MT","31":"NE","32":"NV","33":"NH","34":"NJ","35":"NM",
  "36":"NY","37":"NC","38":"ND","39":"OH","40":"OK","41":"OR","42":"PA","44":"RI",
  "45":"SC","46":"SD","47":"TN","48":"TX","49":"UT","50":"VT","51":"VA","53":"WA",
  "54":"WV","55":"WI","56":"WY","72":"PR"
};

function cdFeatures() {
  return topojson.feature(_cdTopo, Object.values(_cdTopo.objects)[0]).features;
}

// state polygons, merged once from the district geometries (shared borders)
function stateFeatures() {
  if (_stateFeats) return _stateFeats;
  const geoms = Object.values(_cdTopo.objects)[0].geometries;
  const byState = {};
  for (const gm of geoms) {
    const s = String(gm.id).slice(0, 2);
    (byState[s] = byState[s] || []).push(gm);
  }
  _stateFeats = Object.entries(byState).map(([fips, grp]) =>
    ({ type: "Feature", id: fips, geometry: topojson.merge(_cdTopo, grp) }));
  return _stateFeats;
}

function cdMapShell(title, backTo) {
  const host = document.getElementById('district-map');
  host.innerHTML = `<div class="cd-maphead">
    <span class="cd-maptitle">${title}</span>
    ${backTo ? `<button class="cd-back" onclick="drawStates()">all states</button>` : ''}</div>`;
  const svg = document.createElementNS(CD_NS, 'svg');
  svg.setAttribute('viewBox', '0 0 960 560');
  svg.setAttribute('class', 'cd-svg');
  host.appendChild(svg);
  return svg;
}

function drawStates() {
  const host = document.getElementById('district-map');
  if (!host || typeof d3 === 'undefined' || !_cdTopo) return;
  _cdScope = null;
  const feats = stateFeatures();
  const path = d3.geoPath(d3.geoAlbersUsa().fitSize([960, 560],
    { type: "FeatureCollection", features: feats }));
  const svg = cdMapShell('Click your state', false);
  for (const f of feats) {
    const d = path(f);
    if (!d) continue;
    const p = document.createElementNS(CD_NS, 'path');
    p.setAttribute('d', d);
    p.setAttribute('class', 'cd-area');
    const title = document.createElementNS(CD_NS, 'title');
    title.textContent = CD_FIPS_USPS[f.id] || f.id;
    p.appendChild(title);
    p.addEventListener('click', () => drawDistricts(f.id));
    svg.appendChild(p);
  }
}

function drawDistricts(stateFips) {
  const host = document.getElementById('district-map');
  if (!host || typeof d3 === 'undefined' || !_cdTopo) return;
  _cdScope = stateFips;
  const feats = cdFeatures().filter(f => String(f.id).slice(0, 2) === stateFips);
  const path = d3.geoPath(d3.geoAlbersUsa().fitSize([960, 560],
    { type: "FeatureCollection", features: feats }));
  const svg = cdMapShell(`Click your district — ${CD_FIPS_USPS[stateFips] || stateFips}`, true);
  for (const f of feats) {
    const d = path(f);
    if (!d) continue;
    const p = document.createElementNS(CD_NS, 'path');
    p.setAttribute('d', d);
    p.setAttribute('class', 'cd-area' + (String(f.id) === onboardingData.geoid ? ' selected' : ''));
    p.dataset.geoid = f.id;
    p.addEventListener('click', () => resolveGeoid(String(f.id)));
    svg.appendChild(p);
  }
}

function highlightDistrict(geoid, zoom) {
  const stateFips = String(geoid).slice(0, 2);
  if (zoom && _cdScope !== stateFips) { drawDistricts(stateFips); return; }  // zoom then highlight
  for (const p of document.querySelectorAll('#district-map .cd-area'))
    p.classList.toggle('selected', p.dataset.geoid === String(geoid));
}

async function ensureDistrictMap() {
  if (_cdTopo || typeof d3 === 'undefined' || typeof topojson === 'undefined') {
    if (_cdTopo) return;
  }
  try {
    _cdTopo = await (await fetch('/static/geo/cd119-10m.json')).json();
    if (onboardingData.geoid) drawDistricts(onboardingData.geoid.slice(0, 2));
    else drawStates();
  } catch (e) { /* map is an aid; the address/zip inputs still work without it */ }
}

function goToStep2() {
  showPage('page-onboarding');
  showStep(2);
}

function toggleInterest(el) {
  el.classList.toggle('selected');
  const selected = document.querySelectorAll('.interest-pill.selected');
  document.getElementById('step2-btn').disabled = selected.length === 0;
}

function finishOnboarding() {
  const selected = [...document.querySelectorAll('.interest-pill.selected')]
    .map(el => el.dataset.interest);

  const STATE_NAMES = {
    AL:"Alabama",AK:"Alaska",AZ:"Arizona",AR:"Arkansas",CA:"California",
    CO:"Colorado",CT:"Connecticut",DE:"Delaware",FL:"Florida",GA:"Georgia",
    HI:"Hawaii",ID:"Idaho",IL:"Illinois",IN:"Indiana",IA:"Iowa",KS:"Kansas",
    KY:"Kentucky",LA:"Louisiana",ME:"Maine",MD:"Maryland",MA:"Massachusetts",
    MI:"Michigan",MN:"Minnesota",MS:"Mississippi",MO:"Missouri",MT:"Montana",
    NE:"Nebraska",NV:"Nevada",NH:"New Hampshire",NJ:"New Jersey",NM:"New Mexico",
    NY:"New York",NC:"North Carolina",ND:"North Dakota",OH:"Ohio",OK:"Oklahoma",
    OR:"Oregon",PA:"Pennsylvania",RI:"Rhode Island",SC:"South Carolina",
    SD:"South Dakota",TN:"Tennessee",TX:"Texas",UT:"Utah",VT:"Vermont",
    VA:"Virginia",WA:"Washington",WV:"West Virginia",WI:"Wisconsin",WY:"Wyoming"
  };
  const prefs = {
    zip: onboardingData.zip,
    state: onboardingData.state,
    stateName: STATE_NAMES[onboardingData.state] || onboardingData.state,
    senators: onboardingData.senators,
    representative: onboardingData.representative,
    interests: selected,
    created: new Date().toISOString()
  };

  savePrefs(prefs);
  updateJurisdictionToggle(prefs);
  showPage('page-home');
  _renderSidebar();
  loadFeed();
}

async function checkServer() {
  try {
    const r = await fetch('/health', { signal: AbortSignal.timeout(3000) });
    if (!r.ok) throw new Error();
  } catch {
    document.getElementById('results-section').innerHTML = `
      <div class="empty-state">
        <p>NosPopuli is temporarily unavailable.</p>
        <p style="margin-top:0.5rem">
          We're working on it. Please try again in a moment.
        </p>
        <p style="margin-top:1rem">
          <button onclick="location.reload()" class="btn-ghost" style="font-family:'IBM Plex Mono',monospace;font-size:0.7rem">
            Retry
          </button>
        </p>
      </div>`;
  }
}

// ── Flag system ──
let currentFlagType = null;
let currentFlagContext = {};

const SEARCH_REASONS = [
  "Results not relevant",
  "Missing important bills",
  "Wrong time period",
  "Misunderstood my question",
  "Other"
];

const BILL_REASONS = [
  "Translation is inaccurate",
  "Missing key information",
  "Wrong bill details",
  "Timeline is incorrect",
  "Other"
];

function showSearchFlag() {
  currentFlagType = 'search';
  currentFlagContext = currentSearchContext;
  document.getElementById('flag-modal-title').textContent = 'What was wrong with these results?';
  renderFlagReasons(SEARCH_REASONS);
  openFlagModal();
}

function showBillFlag(section) {
  currentFlagType = 'bill';
  currentFlagContext = { section };
  document.getElementById('flag-modal-title').textContent = 'What is inaccurate?';
  renderFlagReasons(BILL_REASONS);
  openFlagModal();
}

function renderFlagReasons(reasons) {
  const container = document.getElementById('flag-reasons');
  container.innerHTML = reasons.map((r, i) => `
    <label style="display:flex;align-items:center;gap:0.5rem;
                  margin-bottom:0.5rem;cursor:pointer;
                  font-size:0.88rem;font-family:'Source Serif 4',serif">
      <input type="radio" name="flag-reason" value="${r}" ${i === 0 ? 'checked' : ''}>
      ${r}
    </label>
  `).join('');
}

function openFlagModal() {
  document.getElementById('flag-notes').value = '';
  document.getElementById('flag-success').style.display = 'none';
  const modal = document.getElementById('flag-modal');
  modal.style.display = 'flex';
}

function closeFlagModal() {
  document.getElementById('flag-modal').style.display = 'none';
  currentFlagType = null;
  currentFlagContext = {};
}

async function submitFlag() {
  const reason = document.querySelector('input[name="flag-reason"]:checked')?.value;
  const notes = document.getElementById('flag-notes').value.trim();
  if (!reason) return;

  try {
    if (currentFlagType === 'search') {
      await fetch('/flag/search', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          query: currentFlagContext.query,
          results_shown: currentFlagContext.results_shown,
          expanded_terms: currentFlagContext.expanded_terms,
          congress_numbers: currentFlagContext.congress_numbers,
          confidence: currentFlagContext.confidence,
          reason, notes
        })
      });
    } else if (currentFlagType === 'bill') {
      const billId = document.getElementById('detail-bill-id').textContent;
      await fetch('/flag/bill', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          bill_id: billId,
          congress: 0,
          bill_type: currentFlagContext.section || 'translation',
          reason, notes,
          flagged_section: currentFlagContext.section || 'translation'
        })
      });
    }
    document.getElementById('flag-success').style.display = 'block';
    setTimeout(closeFlagModal, 1500);
  } catch(err) {
    console.error('Flag submission failed:', err);
  }
}

document.getElementById('flag-modal').addEventListener('click', function(e) {
  if (e.target === this) closeFlagModal();
});

// ── Init ──
checkServer();
updateJurisdictionToggle(getPrefs());

// ─────────────────────────────────────────────
// ELECTIONS
// ─────────────────────────────────────────────

function _saveTracked() {
  localStorage.setItem('np_tracked_elections', JSON.stringify([..._trackedElections]));
}

function toggleTrackElection(id, btn) {
  if (_trackedElections.has(id)) {
    _trackedElections.delete(id);
    btn.textContent = 'Track';
    btn.classList.remove('tracked');
  } else {
    _trackedElections.add(id);
    btn.textContent = 'Tracking ✓';
    btn.classList.add('tracked');
  }
  _saveTracked();
  // Refresh section visibility
  const trackedSection = document.getElementById('elections-tracked-section');
  if (trackedSection) {
    const anyTracked = document.querySelectorAll('.election-card.tracking').length > 0;
    trackedSection.style.display = _trackedElections.size > 0 ? 'block' : 'none';
  }
}

function _countdownDisplay(days) {
  if (days === null || days === undefined) return { num: '?', label: 'days', cls: 'far' };
  if (days >= 0) {
    const cls = days <= 30 ? 'urgent' : days <= 90 ? 'near' : 'far';
    return { num: days, label: days === 1 ? 'day' : 'days', cls };
  }
  return { num: Math.abs(days), label: Math.abs(days) === 1 ? 'day ago' : 'days ago', cls: 'past' };
}

function _formatElectionDate(dateStr) {
  try {
    const d = new Date(dateStr + 'T12:00:00');
    return d.toLocaleDateString('en-US', { month: 'long', day: 'numeric', year: 'numeric' });
  } catch { return dateStr; }
}

function _candidateInitials(name) {
  return (name || '?').split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase();
}

function _renderCandidate(c) {
  const photoEl = c.photo_url
    ? `<img class="candidate-photo" src="${c.photo_url}" alt="${c.name}" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
    : '';
  const initialsEl = `<div class="candidate-initials" style="${c.photo_url ? 'display:none' : ''}">${_candidateInitials(c.name)}</div>`;

  const links = [];
  if (c.candidate_url) links.push(`<a href="${c.candidate_url}" target="_blank" rel="noopener">Website ↗</a>`);
  if (c.email) links.push(`<a href="mailto:${c.email}">${c.email}</a>`);
  if (c.phone) links.push(`<span>${c.phone}</span>`);
  for (const ch of (c.channels || [])) {
    links.push(`<a href="#" onclick="return false" title="${ch.type}: ${ch.id}">${ch.icon} ${ch.id}</a>`);
  }

  return `
    <div class="candidate-row">
      ${photoEl}${initialsEl}
      <div class="candidate-info">
        <div class="candidate-name">${c.name}</div>
        ${c.party ? `<div class="candidate-party"><span class="party-dot ${c.party_color}"></span>${c.party}</div>` : ''}
        ${links.length ? `<div class="candidate-links">${links.join('')}</div>` : ''}
      </div>
    </div>`;
}

function _renderContests(contests) {
  if (!contests || !contests.length) return '<div class="no-candidates">Contest information not yet available.</div>';
  return contests.map(c => {
    const office = c.office || c.type || 'Contest';
    const district = c.district ? ` — ${c.district}` : '';
    const candidatesHtml = c.candidates && c.candidates.length
      ? `<div class="candidates-grid">${c.candidates.map(_renderCandidate).join('')}</div>`
      : '<div class="no-candidates">Candidate information not yet available.</div>';
    return `
      <div class="contest-block">
        <div class="contest-office"><span>${office}</span>${district}</div>
        ${candidatesHtml}
      </div>`;
  }).join('');
}

function _makeElectionCard(election, isPast) {
  const days = isPast ? -(election.days_ago || 0) : (election.countdown_days ?? null);
  const cd = _countdownDisplay(days);
  const isTracked = _trackedElections.has(election.id);
  const affectsUser = election.affects_user;

  const races = (election.contests || []).map(c => c.office || c.type).filter(Boolean);
  const racesText = races.length ? races.slice(0, 4).join(' · ') + (races.length > 4 ? ' · …' : '') : '';

  const badges = [];
  if (isTracked) badges.push(`<span class="election-badge badge-tracking">Tracking</span>`);
  if (affectsUser && !isPast) badges.push(`<span class="election-badge badge-yours">★ Your election</span>`);

  const deadlineHtml = (!isPast && election.registration_deadline)
    ? `<div class="election-deadline">Reg. deadline: <strong>${election.registration_deadline}</strong></div>`
    : '<div class="election-deadline"></div>';

  const trackBtnHtml = `<button class="election-action-btn election-track-btn ${isTracked ? 'tracked' : ''}"
    onclick="event.stopPropagation();toggleTrackElection('${election.id}',this)">
    ${isTracked ? 'Tracking ✓' : 'Track'}
  </button>`;

  const infoUrl = isPast ? election.ballotpedia_url : (election.voter_info_url || election.ballotpedia_url);
  const infoLabel = isPast ? 'View results →' : 'Voter info →';
  const infoBtn = infoUrl
    ? `<a class="election-action-btn election-info-btn" href="${infoUrl}" target="_blank" rel="noopener">${infoLabel}</a>`
    : '';

  const card = document.createElement('div');
  card.className = `election-card${affectsUser ? ' affects-user' : ''}${isTracked ? ' tracking' : ''}`;
  card.dataset.electionId = election.id;

  card.innerHTML = `
    <div class="election-card-top">
      <div class="election-countdown">
        <span class="election-countdown-num ${cd.cls}">${cd.num}</span>
        <span class="election-countdown-label">${cd.label}</span>
      </div>
      <div class="election-card-main">
        <div class="election-name">${escapeHtml(election.name)}</div>
        <div class="election-date">${_formatElectionDate(election.date)}</div>
        ${racesText ? `<div class="election-races">${racesText}</div>` : ''}
        ${badges.length ? `<div class="election-badges">${badges.join('')}</div>` : ''}
      </div>
    </div>
    <div class="election-card-actions">
      ${deadlineHtml}
      ${trackBtnHtml}
      ${infoBtn}
    </div>
    <div class="election-contests">${_renderContests(election.contests)}</div>`;

  // Open the full detail page (stage, candidates, polling, campaign finance).
  // Pass zip/state so federal races — whose roster comes from the FEC, not the
  // ballot — can resolve; state is parsed from the web_XX_ id when prefs lack it.
  card.querySelector('.election-card-top').addEventListener('click', () => {
    const p = getPrefs() || {};
    let st = p.state || '';
    if (!st) { const m = /^web_([A-Za-z]{2})_/.exec(election.id || ''); if (m) st = m[1].toUpperCase(); }
    const params = new URLSearchParams();
    if (p.zip) params.set('zip', p.zip);
    if (st) params.set('state', st);
    const qs = params.toString();
    window.location = `/elections/${encodeURIComponent(election.id)}${qs ? '?' + qs : ''}`;
  });
  card.querySelector('.election-card-top').style.cursor = 'pointer';

  return card;
}

function _renderElectionSection(sectionId, listId, elections, isPast) {
  const section = document.getElementById(sectionId);
  const list = document.getElementById(listId);
  if (!elections || !elections.length) { section.style.display = 'none'; return; }
  section.style.display = 'block';
  list.innerHTML = '';
  elections.forEach(e => list.appendChild(_makeElectionCard(e, isPast)));
}

let _electionsLoaded = false;

function _renderElectionsPage(data, zip) {
  const subtitle = document.getElementById('elections-subtitle');
  if (subtitle) subtitle.textContent = zip
    ? `Based on zip ${zip}`
    : 'National view — set your zip to personalize';

  document.getElementById('elections-no-zip-banner').style.display = zip ? 'none' : 'block';
  document.getElementById('elections-loading').style.display = 'none';
  document.getElementById('elections-error').style.display = 'none';

  if (data.error) {
    document.getElementById('elections-error').textContent = data.error;
    document.getElementById('elections-error').style.display = 'block';
    return;
  }

  const upcoming = data.upcoming || [];
  const recent   = data.recent   || [];

  const tracked = upcoming.filter(e => _trackedElections.has(e.id));
  const yours   = upcoming.filter(e => e.affects_user && !_trackedElections.has(e.id));
  const other   = upcoming.filter(e => !e.affects_user && !_trackedElections.has(e.id));

  _renderElectionSection('elections-tracked-section', 'elections-tracked-list', tracked, false);
  _renderElectionSection('elections-yours-section',   'elections-yours-list',   yours,   false);
  _renderElectionSection('elections-other-section',   'elections-other-list',   other,   false);
  _renderElectionSection('elections-recent-section',  'elections-recent-list',  recent,  true);

  document.getElementById('elections-content').style.display =
    (upcoming.length || recent.length) ? 'block' : 'none';

  if (!upcoming.length && !recent.length) {
    document.getElementById('elections-error').textContent = 'No election data available right now.';
    document.getElementById('elections-error').style.display = 'block';
  }

  _electionsLoaded = true;
}

async function loadElections() {
  if (_electionsLoaded) return;
  const prefs = getPrefs();
  const zip   = prefs?.zip   || null;
  const state = prefs?.state || null;

  document.getElementById('elections-loading').style.display = 'block';
  document.getElementById('elections-content').style.display = 'none';

  try {
    const params = new URLSearchParams();
    if (zip)   params.set('zip',   zip);
    if (state) params.set('state', state);
    const res  = await fetch(`/api/elections?${params}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    const data = await res.json();
    _renderElectionsPage(data, zip);
  } catch {
    document.getElementById('elections-loading').style.display = 'none';
    document.getElementById('elections-error').textContent = 'Could not load elections. Please try again.';
    document.getElementById('elections-error').style.display = 'block';
  }
}

function openElections() {
  previousPage = document.querySelector('.page.active').id;
  showPage('page-elections');
  loadElections();
}

// ── Sidebar ──
function _renderSidebar() {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  const prefs = getPrefs();
  if (!prefs) { sidebar.innerHTML = ''; return; }

  const senators = (prefs.senators || []).map(r => ({ ...r, _isSen: true }));
  const rep = prefs.representative ? { ...prefs.representative, _isSen: false } : null;
  const reps = [...senators, rep].filter(Boolean);

  const repRows = reps.map(r => {
    const party = r.party || '';
    const cls = party.toLowerCase().includes('dem') ? 'dem'
              : party.toLowerCase().includes('rep') ? 'rep' : 'ind';
    const title = r._isSen ? 'Sen.' : 'Rep.';
    const loc = r.district ? `${r.state}-${r.district}` : (r.state || '');
    return `<div class="sidebar-rep" onclick="openMemberFromVote(${JSON.stringify({ name: r.name }).replace(/"/g, '&quot;')})">
      <span class="party-dot ${cls}" style="margin-top:0.3rem;flex-shrink:0"></span>
      <div class="sidebar-rep-info">
        <div class="sidebar-rep-name">${title} ${escapeHtml(r.name)}</div>
        <div class="sidebar-rep-meta">${party}${loc ? ' · ' + loc : ''}</div>
      </div>
    </div>`;
  }).join('');

  sidebar.innerHTML = `
    <div class="sidebar-section">
      <div class="section-rule"><span>Your Reps</span></div>
      ${repRows || '<div class="sidebar-rep-meta" style="padding:0.5rem 0;color:var(--muted)">Set your zip to see your reps</div>'}
    </div>
    `;
}



if (new URLSearchParams(window.location.search).get('tab') === 'letters') {
  showPage('page-correspondence');
  if (typeof loadCorrespondencePage === 'function') loadCorrespondencePage();
}
_renderSidebar();

// ── Bill subscription / notify button ──

const SUBS_KEY = 'np_subscriptions';

function _getSubs() {
  try { return JSON.parse(localStorage.getItem(SUBS_KEY)) || {}; }
  catch { return {}; }
}

function _saveSubs(subs) {
  localStorage.setItem(SUBS_KEY, JSON.stringify(subs));
}

function _getNotifyEmail() {
  // Prefer logged-in account email; fall back to stored anonymous email
  if (typeof _authUser !== 'undefined' && _authUser?.email) return _authUser.email;
  return localStorage.getItem('np_notify_email') || null;
}

// Called by openDetail / openStateBill after bill loads
function _initNotifyBtn(billId, congress, billType, billNumber, ocdId) {
  const btn   = document.getElementById('notify-btn');
  const cap   = document.getElementById('notify-email-capture');
  if (!btn) return;

  btn._billId     = billId;
  btn._congress   = congress   || null;
  btn._billType   = billType   || null;
  btn._billNumber = billNumber || null;
  btn._ocdId      = ocdId     || null;

  btn.style.display = 'inline-flex';
  cap.style.display = 'none';
  document.getElementById('notify-email-input').value = '';

  const subs = _getSubs();
  _setNotifyBtnState(subs[billId]?.active ? 'subscribed' : 'idle');
}

function _setNotifyBtnState(state) {
  const btn = document.getElementById('notify-btn');
  if (!btn) return;
  if (state === 'subscribed') {
    btn.textContent = 'Notifying you ✓';
    btn.classList.add('subscribed');
    btn.disabled = false;
  } else if (state === 'loading') {
    btn.textContent = '…';
    btn.disabled = true;
    btn.classList.remove('subscribed');
  } else {
    btn.textContent = 'Notify me when this moves';
    btn.classList.remove('subscribed');
    btn.disabled = false;
  }
}

async function toggleSubscribe() {
  const btn   = document.getElementById('notify-btn');
  const cap   = document.getElementById('notify-email-capture');
  const billId = btn._billId;
  if (!billId) return;

  const subs = _getSubs();
  const isSubscribed = subs[billId]?.active;

  if (isSubscribed) {
    // Unsubscribe
    const email = subs[billId].email;
    _setNotifyBtnState('loading');
    try {
      await fetch('/correspondence/unsubscribe', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ bill_id: billId, email })
      });
    } catch {}
    delete subs[billId];
    _saveSubs(subs);
    _setNotifyBtnState('idle');
    cap.style.display = 'none';
    return;
  }

  // Subscribe — check if we already have an email
  const email = _getNotifyEmail();
  if (email) {
    await _doSubscribe(email);
  } else {
    cap.style.display = 'block';
    document.getElementById('notify-email-input').focus();
  }
}

async function submitSubscribeEmail() {
  const input = document.getElementById('notify-email-input');
  const email = input.value.trim();
  if (!email || !email.includes('@')) return;
  localStorage.setItem('np_notify_email', email);
  document.getElementById('notify-email-capture').style.display = 'none';
  await _doSubscribe(email);
}

function loadNotificationsPage() {
  const container = document.getElementById('notifications-list');
  if (!container) return;
  const subs = _getSubs();
  const active = Object.entries(subs).filter(([, v]) => v.active);

  if (!active.length) {
    container.innerHTML = `
      <div class="empty-state" style="margin-top:2rem">
        <p>You haven't subscribed to any bills yet.</p>
        <p style="margin-top:0.5rem;color:var(--muted)">
          Open any bill and click "Notify me when this moves."
        </p>
      </div>`;
    return;
  }

  container.innerHTML = active.map(([billId, sub]) => {
    const safeId  = billId.replace(/\W/g, '_');
    const escaped = billId.replace(/&/g, '&amp;').replace(/"/g, '&quot;');
    return `
      <div class="notif-item" id="notif-${safeId}" data-bill-id="${escaped}">
        <div class="notif-item-info notif-item-link"
          data-bill-id="${escaped}" onclick="reopenBillFromNotif(this.dataset.billId)">
          <div class="notif-bill-id">${billId}</div>
          ${sub.title ? `<div class="notif-bill-title">${escapeHtml(sub.title)}</div>` : ''}
        </div>
        <button class="feed-settings-btn notif-stop-btn"
          data-bill-id="${escaped}" onclick="stopNotifying(this.dataset.billId)">
          Stop notifying me
        </button>
      </div>`;
  }).join('');
}

function reopenBillFromNotif(billId) {
  const sub = _getSubs()[billId];
  if (!sub) return;
  if (sub.ocdId) {
    openStateBill({ ocd_id: sub.ocdId, identifier: billId, title: sub.title });
  } else if (sub.congress && sub.billType && sub.billNumber) {
    openDetail({ congress: sub.congress, type: sub.billType, number: sub.billNumber, title: sub.title });
  }
}

async function stopNotifying(billId) {
  const safeId = billId.replace(/\W/g, '_');
  const row    = document.getElementById(`notif-${safeId}`);
  const email  = _getSubs()[billId]?.email || null;

  if (row) {
    row.style.opacity = '0.4';
    row.style.pointerEvents = 'none';
  }

  try {
    await fetch('/correspondence/unsubscribe', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ bill_id: billId, email })
    });
  } catch {}

  const subs = _getSubs();
  delete subs[billId];
  _saveSubs(subs);

  if (row) {
    row.style.transition = 'opacity 0.2s';
    row.style.opacity = '0';
    setTimeout(() => row.remove(), 200);
  }

  // Check after the fade-out completes
  setTimeout(() => {
    const container = document.getElementById('notifications-list');
    if (container && !container.querySelector('.notif-item')) {
      container.innerHTML = `
        <div class="empty-state" style="margin-top:2rem">
          <p>No active notifications.</p>
        </div>`;
    }
  }, 220);

  // Update notify button if we're on that bill's detail page
  const notifyBtn = document.getElementById('notify-btn');
  if (notifyBtn && notifyBtn._billId === billId) {
    _setNotifyBtnState('idle');
  }
}

async function _doSubscribe(email) {
  const btn    = document.getElementById('notify-btn');
  const billId = btn._billId;
  _setNotifyBtnState('loading');

  const token  = typeof getAuthToken === 'function' ? getAuthToken() : null;
  const headers = { 'Content-Type': 'application/json' };
  if (token) headers['Authorization'] = `Bearer ${token}`;

  try {
    await fetch('/correspondence/subscribe', {
      method: 'POST',
      headers,
      body: JSON.stringify({
        bill_id:    billId,
        bill_title: document.getElementById('detail-bill-title').textContent || billId,
        email,
        congress:    btn._congress,
        bill_type:   btn._billType,
        bill_number: btn._billNumber,
        ocd_id:      btn._ocdId,
      })
    });
    const subs = _getSubs();
    const title = document.getElementById('detail-bill-title')?.textContent || '';
    subs[billId] = {
      email, active: true, title,
      congress:   btn._congress,
      billType:   btn._billType,
      billNumber: btn._billNumber,
      ocdId:      btn._ocdId,
    };
    _saveSubs(subs);
    _setNotifyBtnState('subscribed');
  } catch {
    _setNotifyBtnState('idle');
  }
}
loadFeed();

// ── Masthead easter egg ──
(function() {
  const m = document.querySelector('.masthead');
  if (!m) return;
  m.style.cursor = 'default';
  m.addEventListener('click', () => {
    m.classList.remove('jiggle');
    void m.offsetWidth;
    m.classList.add('jiggle');
    m.addEventListener('animationend', () => m.classList.remove('jiggle'), { once: true });

    const shouts = [
      'Extra! Extra!',
      'Read all about it!',
      'Breaking news!',
      'Hot off the press!',
      'Stop the presses!',
      'Hear ye, hear ye!',
      'Latest dispatch!',
      'Special edition!',
      'This just in!',
      'By order of Congress!',
      'The people demand it!',
      'Democracy in action!',
      'Your reps are watching!',
    ];
    const tag = document.createElement('div');
    tag.className = 'masthead-extra';
    tag.textContent = shouts[Math.floor(Math.random() * shouts.length)];
    // Random horizontal position: anywhere from 5% to 85% of the masthead width
    tag.style.left = (5 + Math.random() * 80) + '%';
    m.appendChild(tag);
    setTimeout(() => tag.remove(), 1150);
  });
})();
