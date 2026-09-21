const _monitorSecret = new URLSearchParams(window.location.search).get('secret') || '';
const _mfetch = (url, opts = {}) => fetch(`${url}${url.includes('?') ? '&' : '?'}secret=${encodeURIComponent(_monitorSecret)}`, opts);

const AGENTS = [
  { id: 'router',        label: 'Router',        icon: '⇄' },
  { id: 'search',        label: 'Search',        icon: '⌕' },
  { id: 'orchestrator',  label: 'Orchestrator',  icon: '◈' },
  { id: 'bill_fetcher',  label: 'Bill Fetcher',  icon: '↓' },
  { id: 'translator',    label: 'Translator',    icon: '✦' },
  { id: 'historian',     label: 'Historian',     icon: '◷' },
  { id: 'vote_parser',   label: 'Vote Parser',   icon: '⊙' },
  { id: 'vote_fetcher',  label: 'Vote Fetcher',  icon: '⊕' },
  { id: 'vote_mapper',   label: 'Vote Mapper',   icon: '⊞' },
  { id: 'member_search', label: 'Member Search', icon: '◉' },
  { id: 'title_search',      label: 'Title Search',  icon: '⊛' },
  { id: 'dispatcher',        label: 'Dispatcher',    icon: '⇢' },
  { id: 'result_validator',  label: 'Validator',     icon: '✓' },
  { id: 'api',               label: 'API',           icon: '⊳' },
  { id: 'state_search',      label: 'State Search',  icon: '◎' },
  { id: 'state_fetcher',     label: 'State Fetcher', icon: '↓' },
  { id: 'state_member',      label: 'State Member',  icon: '◉' },
];

let seenCount = 0;
let paused = false;
let activeFilter = null;
let agentCounts = {};
let lastActivity = {};

// Build sidebar
const agentList = document.getElementById('agent-list');
AGENTS.forEach(agent => {
  const pill = document.createElement('div');
  pill.className = 'agent-pill';
  pill.id = `pill-${agent.id}`;
  pill.innerHTML = `
    <div class="agent-dot"></div>
    <div class="agent-name">${agent.label}</div>
    <div class="agent-count" id="cnt-${agent.id}">0</div>
  `;
  pill.onclick = () => filterAgent(agent.id);
  agentList.appendChild(pill);
});

// Build flow nodes
const flowNodes = document.getElementById('flow-nodes');
AGENTS.forEach((agent, i) => {
  const node = document.createElement('div');
  node.className = 'flow-node';
  node.id = `flow-${agent.id}`;
  node.innerHTML = `
    <div class="flow-node-icon">${agent.icon}</div>
    <div class="flow-node-name">${agent.label}</div>
    <div class="flow-node-time" id="ftime-${agent.id}"></div>
  `;
  flowNodes.appendChild(node);

  if (i < AGENTS.length - 1) {
    const arrow = document.createElement('div');
    arrow.className = 'flow-arrow';
    arrow.id = `farrow-${agent.id}`;
    arrow.textContent = '↓';
    flowNodes.appendChild(arrow);
  }
});

// Filtering is a CSS attribute match (see monitor.css [data-filter] rules):
// the feed keeps its DOM, nothing is rebuilt.
function filterAgent(agentId) {
  activeFilter = activeFilter === agentId ? null : agentId;
  document.querySelectorAll('.agent-pill').forEach(p =>
    p.classList.toggle('active', p.id === `pill-${activeFilter}`));
  const feed = document.getElementById('feed');
  if (activeFilter) feed.dataset.filter = activeFilter;
  else delete feed.dataset.filter;
}

// Entries kept in the DOM. Older ones are dropped so a monitor tab left open
// for a day does not grow without bound.
const MAX_ENTRIES = 2000;

function createEntryEl(entry) {
  const agent = entry.agent || 'unknown';
  const colorClass = `c-${agent}`;
  const time = (entry.timestamp || '').slice(11, 19);

  const el = document.createElement('div');
  el.className = 'entry';
  el.dataset.agent = agent;

  const inputKeys = Object.entries(entry.input || {}).filter(([k,v]) => v !== null && v !== '' && JSON.stringify(v) !== '{}');
  const outputKeys = Object.entries(entry.output || {}).filter(([k,v]) => v !== null && v !== '' && JSON.stringify(v) !== '{}');

  el.innerHTML = `
    <div class="entry-header">
      <span class="entry-agent ${colorClass}">${agent}</span>
      <span class="entry-action">${entry.action || ''}</span>
      <span class="entry-time">${time}</span>
    </div>
    ${inputKeys.length || outputKeys.length ? `
      <div class="entry-data">
        ${inputKeys.slice(0,2).map(([k,v]) => `
          <div class="data-block">
            <div class="data-label">IN · ${k}</div>
            <div class="data-value">${String(v).slice(0,120)}</div>
          </div>
        `).join('')}
        ${outputKeys.slice(0,4).map(([k,v]) => `
          <div class="data-block">
            <div class="data-label">OUT · ${k}</div>
            <div class="data-value">${String(v).slice(0,120)}</div>
          </div>
        `).join('')}
      </div>
    ` : ''}
  `;

  return el;
}

// The flash/lit looks are CSS animations; the class is dropped on
// animationend (one delegated listener below) instead of a timer per flash.
function _restart(el, cls) {
  if (!el) return;
  el.classList.remove(cls);
  void el.offsetWidth;
  el.classList.add(cls);
}
function flashAgent(agentId) {
  _restart(document.getElementById(`pill-${agentId}`), 'firing');
  _restart(document.getElementById(`flow-${agentId}`), 'lit');
  _restart(document.getElementById(`farrow-${agentId}`), 'lit');
}
document.addEventListener('animationend', e => {
  if (e.animationName === 'flash') e.target.classList.remove('firing');
  else if (e.animationName === 'lit' || e.animationName === 'lit-arrow') e.target.classList.remove('lit');
});

let autoScroll = true;
let logOffset = 0;  // byte offset into the server's JSONL log

async function poll() {
  if (paused || document.hidden) return;

  try {
    const res = await _mfetch(`/monitor/stream?after=${logOffset}`);
    const data = await res.json();
    const entries = data.entries || [];
    logOffset = data.offset || logOffset;
    document.body.classList.remove('disconnected');
    if (!entries.length) return;

    const empty = document.getElementById('empty-state');
    if (empty) empty.remove();
    const feed = document.getElementById('feed');

    entries.forEach(entry => {
      const agent = entry.agent || 'unknown';

      agentCounts[agent] = (agentCounts[agent] || 0) + 1;
      const cntEl = document.getElementById(`cnt-${agent}`);
      if (cntEl) cntEl.textContent = agentCounts[agent];

      flashAgent(agent);
      feed.appendChild(createEntryEl(entry));

      const ftimeEl = document.getElementById(`ftime-${agent}`);
      if (ftimeEl) ftimeEl.textContent = (entry.timestamp || '').slice(11, 19);
    });
    while (feed.children.length > MAX_ENTRIES) feed.removeChild(feed.firstChild);
    if (autoScroll) feed.scrollTop = feed.scrollHeight;

    seenCount += entries.length;
    document.getElementById('count-badge').textContent = `${seenCount} events`;
    document.getElementById('status-label').textContent = paused ? 'Paused' : 'Monitoring';
  } catch(e) {
    document.getElementById('status-label').textContent = 'Disconnected';
    document.body.classList.add('disconnected');
    return;
  }
}

function togglePause() {
  paused = !paused;
  const btn = document.getElementById('pause-btn');
  btn.textContent = paused ? 'Resume' : 'Pause';
  btn.classList.toggle('active', paused);
  document.getElementById('status-label').textContent = paused ? 'Paused' : 'Monitoring';
  document.body.classList.toggle('paused', paused);
}

function clearLog() {
  seenCount = 0;
  agentCounts = {};
  document.getElementById('feed').innerHTML = `
    <div class="empty" id="empty-state">
      <div class="empty-icon">⬡</div>
      <div class="empty-text">Waiting for activity</div>
      <div class="empty-sub">Make a search in NosPopuli to see agents fire</div>
    </div>`;
  AGENTS.forEach(a => {
    const el = document.getElementById(`cnt-${a.id}`);
    if (el) el.textContent = '0';
  });
  document.getElementById('count-badge').textContent = '0 events';
}

function scrollToBottom() {
  const feed = document.getElementById('feed');
  feed.scrollTop = feed.scrollHeight;
}

document.getElementById('feed').addEventListener('scroll', function() {
  const feed = this;
  autoScroll = feed.scrollTop + feed.clientHeight >= feed.scrollHeight - 50;
});

// 2 s is plenty for a human-readable feed; each poll carries only new lines.
// Paused while the tab is hidden; catches up as soon as it is visible again.
setInterval(poll, 2000);
document.addEventListener('visibilitychange', () => {
  document.body.classList.toggle('bg', document.hidden);  // CSS pauses the live dot while hidden
  if (!document.hidden) poll();
});
poll();

// ── Tab switching: body[data-tab] drives visibility and the active tab in CSS ──
function switchTab(tab) {
  document.body.dataset.tab = tab;
  if (tab === 'analytics') loadAnalytics();
  else if (tab === 'flags') loadFlags();
}

// ── Flags loading ──
async function loadFlags() {
  try {
    const res = await _mfetch('/monitor/flags');
    const flags = await res.json();

    const searchFlags = flags.filter(f => f.event === 'search_flag');
    const billFlags   = flags.filter(f => f.event === 'bill_flag');

    const searchEl = document.getElementById('search-flags-list');
    searchEl.innerHTML = searchFlags.length
      ? searchFlags.slice().reverse().map(f => `
          <div class="query-row">
            <div class="query-text">
              <div style="color:var(--text)">"${f.query}"</div>
              <div style="color:var(--red);font-size:0.6rem">${f.reason}</div>
              ${f.notes ? `<div style="color:var(--muted);font-size:0.6rem">${f.notes}</div>` : ''}
            </div>
            <div class="query-count">${(f.timestamp || '').slice(11,19)}</div>
          </div>
        `).join('')
      : '<div style="color:var(--muted);font-size:0.7rem">No search flags yet</div>';

    const billEl = document.getElementById('bill-flags-list');
    billEl.innerHTML = billFlags.length
      ? billFlags.slice().reverse().map(f => `
          <div class="query-row">
            <div class="query-text">
              <div style="color:var(--text)">${f.bill_id} · ${f.flagged_section}</div>
              <div style="color:var(--red);font-size:0.6rem">${f.reason}</div>
              ${f.notes ? `<div style="color:var(--muted);font-size:0.6rem">${f.notes}</div>` : ''}
            </div>
            <div class="query-count">${(f.timestamp || '').slice(11,19)}</div>
          </div>
        `).join('')
      : '<div style="color:var(--muted);font-size:0.7rem">No bill flags yet</div>';

  } catch(e) {
    console.error('Failed to load flags:', e);
  }
}

// ── Analytics loading ──
async function loadAnalytics() {
  document.getElementById('analysis-report').textContent = 'Analyzing...';

  try {
    const res = await _mfetch('/monitor/analysis');
    const data = await res.json();
    const stats = data.stats || {};

    document.getElementById('stat-searches').textContent = stats.total_searches || 0;
    document.getElementById('stat-bills').textContent = stats.total_bill_opens || 0;
    document.getElementById('stat-members').textContent = stats.total_member_opens || 0;

    document.getElementById('analysis-report').textContent = data.report || 'No report generated.';

    const topQ = document.getElementById('top-queries');
    topQ.innerHTML = (stats.top_queries || []).map(([q, count]) => `
      <div class="query-row">
        <div class="query-text">${q}</div>
        <div class="query-count">${count}x</div>
      </div>
    `).join('') || '<div style="color:var(--muted);font-size:0.7rem">No searches yet</div>';

    const zeroEl = document.getElementById('zero-results');
    const zeros = stats.zero_result_searches || [];
    zeroEl.innerHTML = zeros.length
      ? zeros.map(q => `
          <div class="query-row">
            <div class="query-text zero-result">${q}</div>
            <div class="query-count" style="color:var(--red)">0 results</div>
          </div>
        `).join('')
      : '<div style="color:var(--muted);font-size:0.7rem">No zero-result searches</div>';

    const topB = document.getElementById('top-bills');
    topB.innerHTML = (stats.top_bills || []).map(([bill, count]) => `
      <div class="query-row">
        <div class="query-text">${bill}</div>
        <div class="query-count">${count}x</div>
      </div>
    `).join('') || '<div style="color:var(--muted);font-size:0.7rem">No bills opened yet</div>';

  } catch(e) {
    document.getElementById('analysis-report').textContent = 'Failed to load analysis. Is the server running?';
  }
}

async function clearSearchLog() {
  if (!confirm('Clear all search log data? This cannot be undone.')) return;
  try {
    await _mfetch('/monitor/clear-search-log', { method: 'POST' });
    document.getElementById('stat-searches').textContent = '0';
    document.getElementById('stat-bills').textContent = '0';
    document.getElementById('stat-members').textContent = '0';
    document.getElementById('analysis-report').textContent = 'Search log cleared.';
    document.getElementById('top-queries').innerHTML = '';
    document.getElementById('zero-results').innerHTML = '';
    document.getElementById('top-bills').innerHTML = '';
  } catch(e) {
    alert('Failed to clear search log.');
  }
}

