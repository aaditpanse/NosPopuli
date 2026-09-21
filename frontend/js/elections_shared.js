// Shared by elections.html and election_detail.html. One copy of the prefs key,

// the tracked-elections set and the election-card renderer, instead of three.

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
const TRACKED_KEY = 'np_tracked_elections';

function getPrefs() {
  try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || null; }
  catch { return null; }
}

let _trackedElections = new Set();
try { _trackedElections = new Set(JSON.parse(localStorage.getItem(TRACKED_KEY) || '[]')); } catch {}

function _saveTracked() {
  try { localStorage.setItem(TRACKED_KEY, JSON.stringify([..._trackedElections])); } catch {}
}

function toggleTrackElection(id, btn, labels) {
  const L = Object.assign({ on: 'Tracking ✓', off: 'Track' }, labels || {});
  if (_trackedElections.has(id)) {
    _trackedElections.delete(id);
    btn.textContent = L.off;
    btn.classList.remove('tracked');
  } else {
    _trackedElections.add(id);
    btn.textContent = L.on;
    btn.classList.add('tracked');
  }
  _saveTracked();
  const trackedSection = document.getElementById('elections-tracked-section');
  if (trackedSection) trackedSection.style.display = _trackedElections.size > 0 ? 'block' : 'none';
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
    ? `<img class="candidate-photo" src="${c.photo_url}" alt="${escapeHtml(c.name)}" loading="lazy" decoding="async" onerror="this.style.display='none';this.nextElementSibling.style.display='flex'">`
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
  return card;
}

// One delegated click per list (set up in _renderElectionSection) opens the
// detail page; cards carry their id, so no per-card listener.
function _openElectionFromCard(card) {
  const id = card.dataset.electionId;
  const p = getPrefs() || {};
  let st = p.state || '';
  if (!st) { const m = /^web_([A-Za-z]{2})_/.exec(id || ''); if (m) st = m[1].toUpperCase(); }
  const params = new URLSearchParams();
  if (p.zip) params.set('zip', p.zip);
  if (st) params.set('state', st);
  const qs = params.toString();
  window.location = `/elections/${encodeURIComponent(id)}${qs ? '?' + qs : ''}`;
}

function _renderElectionSection(sectionId, listId, elections, isPast) {
  const section = document.getElementById(sectionId);
  const list = document.getElementById(listId);
  if (!section || !list) return;
  if (!elections || !elections.length) { section.style.display = 'none'; return; }
  section.style.display = 'block';
  list.innerHTML = '';
  elections.forEach(e => list.appendChild(_makeElectionCard(e, isPast)));
  if (!list._wired) {
    list._wired = true;
    list.addEventListener('click', e => {
      const top = e.target.closest('.election-card-top');
      const card = top && top.closest('.election-card');
      if (card) _openElectionFromCard(card);
    });
  }
}
