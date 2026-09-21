/* Ask SPA: /ledger NDJSON, /bill stream, /geo/guess, watching. */
(function () {
  const NS = "http://www.w3.org/2000/svg";
  const STORE = "np_ledger";
  const FIPS = {
    AL: "01", AK: "02", AZ: "04", AR: "05", CA: "06", CO: "08", CT: "09", DE: "10", DC: "11",
    FL: "12", GA: "13", HI: "15", ID: "16", IL: "17", IN: "18", IA: "19", KS: "20", KY: "21",
    LA: "22", ME: "23", MD: "24", MA: "25", MI: "26", MN: "27", MS: "28", MO: "29", MT: "30",
    NE: "31", NV: "32", NH: "33", NJ: "34", NM: "35", NY: "36", NC: "37", ND: "38", OH: "39",
    OK: "40", OR: "41", PA: "42", RI: "44", SC: "45", SD: "46", TN: "47", TX: "48", UT: "49",
    VT: "50", VA: "51", WA: "53", WV: "54", WI: "55", WY: "56",
  };

  const state = {
    view: "home",
    prev: "home",
    query: "",
    stage: "all",
    placeConfirmed: false,
    placeName: "",
    stateCode: "",
    placeSource: "",
    district: "",
    geoid: "",
    rep: "",
    placeBusy: false,
    placeError: "",
    money: null,
    perf: {},
    offTopic: null,
    electionsPage: null,
    watching: [],
    searchCount: 0,
    email: "",
    notifyMoves: true,
    notifyWeekly: false,
    ledger: null,
    bill: null,
    member: null,
    uncharted: null,
    focusMeeting: 0,
    elections: null,
    error: "",
    loading: false,
  };

  let cdFeats = null;  // district GeoJSON, derived once; the TopoJSON is dropped

  function loadStore() {
    try {
      const raw = JSON.parse(localStorage.getItem(STORE) || "{}");
      if (Array.isArray(raw.watching)) state.watching = raw.watching;
      if (typeof raw.email === "string") state.email = raw.email;
      // Only a place the person confirmed survives a reload. Guesses are
      // re-made each visit so a stale or wrong guess never sticks.
      if (raw.placeConfirmed && raw.stateCode) {
        state.placeConfirmed = true;
        state.stateCode = raw.stateCode;
        state.placeName = raw.placeName || "";
        state.placeSource = raw.placeSource || "typed";
        state.district = raw.district || "";
        state.geoid = raw.geoid || "";
        state.rep = raw.rep || "";
      }
      if (typeof raw.notifyMoves === "boolean") state.notifyMoves = raw.notifyMoves;
      if (typeof raw.notifyWeekly === "boolean") state.notifyWeekly = raw.notifyWeekly;
      if (typeof raw.searchCount === "number") state.searchCount = raw.searchCount;
    } catch (e) { /* ignore */ }
  }
  function saveStore() {
    localStorage.setItem(STORE, JSON.stringify({
      watching: state.watching, email: state.email,
      placeName: state.placeName, stateCode: state.stateCode,
      placeConfirmed: state.placeConfirmed, placeSource: state.placeSource,
      district: state.district, geoid: state.geoid, rep: state.rep,
      notifyMoves: state.notifyMoves,
      notifyWeekly: state.notifyWeekly, searchCount: state.searchCount,
    }));
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, c => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[c]));
  }
  function dateLong() {
    return new Date().toLocaleDateString("en-US", {
      weekday: "long", month: "long", day: "numeric", year: "numeric"
    });
  }
  function isWatched(id) { return state.watching.some(w => w.id === id); }
  function watchingTab() { return "Watching (" + state.watching.length + ")"; }
  function watchingLine() {
    const n = state.watching.length;
    if (n === 0) return "You are watching nothing yet. Nothing will be emailed to you.";
    return "You are watching " + n + (n === 1 ? " thing" : " things") + ". We will email you when any of them moves.";
  }
  function placeLabel() {
    if (!state.stateCode) return "";
    const bits = [state.placeName || state.stateCode];
    if (state.district) bits.push(state.district);
    return bits.join(" · ");
  }
  function placeLine() {
    if (!state.stateCode) return "We do not know where you are. National results until you tell us.";
    const name = placeLabel();
    if (state.placeSource === "gps") return "Showing " + name + ", from your device location.";
    if (state.placeSource === "ip") return "Showing " + name + ", guessed from your connection.";
    return "Showing " + name + ", because you set it.";
  }
  function whyPlace() {
    if (!state.stateCode) return "Nothing was guessed. Use your location or type a zip on the Watching page.";
    if (state.placeSource === "gps") return "Your device shared its location. Change it any time.";
    if (state.placeSource === "ip") return "Guessed from your connection. Correct it any time.";
    if (state.placeSource === "map") return "You picked it on the map.";
    return "You set it.";
  }

  function onTestPath() {
    const p = location.pathname;
    return p === "/test" || p.startsWith("/test/");
  }
  function homePath() { return onTestPath() ? "/test" : "/"; }
  function askPath(q) {
    return homePath() + (homePath() === "/" ? "?" : "?") + "q=" + encodeURIComponent(q);
  }
  function watchingPath() {
    return homePath() + (homePath() === "/" ? "?" : "?") + "view=watching";
  }
  function billPath(congress, type, number) {
    return "/bill/" + congress + "/" + type + "/" + number;
  }
  function memberPath(bioguide) {
    return "/member/" + encodeURIComponent(bioguide);
  }

  function go(view) {
    // Scroll only when the reader actually moves to a different view. The
    // stream calls go("ledger") while already on the ledger, and scrolling
    // then yanked the page to the top mid-answer.
    const changed = view !== state.view;
    if (changed) state.prev = state.view;
    state.view = view;
    if (changed) window.scrollTo(0, 0);
    render();
  }
  function goHome() {
    state.query = "";
    history.pushState({}, "", homePath());
    go("home");
  }
  function goBack() { go(state.prev === state.view ? "ledger" : state.prev); }
  function goWatching(opts) {
    if (!opts || !opts.fromHistory) history.pushState({}, "", watchingPath());
    go("watching");
  }

  async function rememberSearch(q) {
    state.searchCount += 1;
    saveStore();
  }

  async function readNdjson(res, onMsg) {
    if (!res.body || !res.body.getReader) {
      const data = await res.json();
      onMsg(data.section ? data : Object.assign({ section: "plate" }, data));
      return;
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const lines = buf.split("\n");
      buf = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        try { onMsg(JSON.parse(line)); } catch (e) { /* skip */ }
      }
    }
    if (buf.trim()) {
      try { onMsg(JSON.parse(buf)); } catch (e) { /* skip */ }
    }
  }

  async function ask(raw, opts) {
    const q = String(raw || "").trim();
    state.query = q;
    state.error = "";
    if (!q) return goHome();
    if (!opts || !opts.fromHistory) history.pushState({}, "", askPath(q));
    state.loading = true;
    state.ledger = {
      question: q,
      pending: true,
      stories: [],
      funnel: [],
      shelves: null,
      member: null,
      legislation: {},
      headline: "Looking that up…",
      deck: "",
      place_name: state.placeName,
      state_code: state.stateCode,
    };
    go("ledger");
    let pendingBill = null;
    let personOnly = false;
    let personHandled = false;
    try {
      const res = await fetch("/ledger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: q, state_code: state.stateCode || null, max_results: 10 }),
      });
      if (!res.ok) throw new Error("Ask failed");
      await readNdjson(res, (msg) => {
        const section = msg.section || "plate";
        if (section === "plate") {
          const plate = msg.plate || "ledger";
          if (plate === "home") { pendingBill = { _home: true }; return; }
          if (plate === "watching") { pendingBill = { _watching: true }; return; }
          if (plate === "bill") { pendingBill = msg; return; }
          if (plate === "uncharted") {
            state.uncharted = msg;
            state.focusMeeting = 0;
            pendingBill = { _uncharted: true };
            return;
          }
          if (plate === "off_topic") { state.offTopic = msg; pendingBill = { _view: "offtopic" }; return; }
          if (plate === "graph") { state.graph = msg; pendingBill = { _view: "graph" }; return; }
          if (plate === "elections") { state.electionsPage = msg; pendingBill = { _view: "elections" }; return; }
          state.ledger = Object.assign({}, state.ledger, msg);
          state.stage = "all";
          // A person-only ask answers with the person's page, not a ledger
          // with one shelf in it. Hold the ledger until the member arrives.
          if (msg.person_only) { personOnly = true; return; }
          state.ledger.pending = false;
          state.loading = false;
          if (state.view !== "ledger") go("ledger");
          else render();
          loadElections(msg.state_code);
        }
        if (personHandled) return;
        if (section === "member" && state.ledger) {
          state.ledger.member = msg.member;
          state.ledger.legislation = msg.legislation || {};
          if (personOnly && msg.member && msg.member.bioguide_id) {
            personHandled = true;
            openMember(msg.member.bioguide_id, { seed: msg });
            return;
          }
          if (state.view === "ledger") scheduleRender();
        }
        if (section === "shelves" && state.ledger) {
          state.ledger.shelves = msg.shelves || [];
          if (state.view === "ledger") scheduleRender();
        }
      });
      await rememberSearch(q);
      if (personHandled) return;
      if (personOnly) {
        // Member lookup came back empty; show the ledger we were holding.
        personOnly = false;
        state.loading = false;
        state.ledger.pending = false;
        go("ledger");
        loadElections(state.ledger && state.ledger.state_code);
        return;
      }
      if (pendingBill && pendingBill._home) return goHome();
      if (pendingBill && pendingBill._watching) return goWatching();
      if (pendingBill && pendingBill._uncharted) { go("uncharted"); return; }
      if (pendingBill && pendingBill._view) { state.loading = false; go(pendingBill._view); return; }
      if (pendingBill && pendingBill.plate === "bill") {
        await openBill(pendingBill.congress, pendingBill.bill_type, pendingBill.number);
        return;
      }
      state.loading = false;
      // Only repaint if this actually flips the page out of its loading
      // state; otherwise the stream already painted the same HTML.
      if (state.ledger && state.ledger.pending) {
        state.ledger.pending = false;
        if (state.view === "ledger") scheduleRender();
      }
    } catch (e) {
      state.error = "We could not assemble that ledger. Try again.";
      state.loading = false;
      go("home");
    }
  }

  async function openBill(congress, type, number, title, opts) {
    state.loading = true;
    state.bill = {
      congress, type, number,
      meta: { title: title || "" },
      plate: {}, translation: "", sponsors: [], cosponsors: [], timeline_events: [],
      votes: null, connections: null, lobbying: null, sponsor_money: null, background: null,
      bill_text: null, text_truncated: false, showAllEvents: false, showText: false,
      pending: true,
    };
    if (!opts || !opts.fromHistory) history.pushState({}, "", billPath(congress, type, number));
    go("bill");
    try {
      const res = await fetch("/bill", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ congress: Number(congress), bill_type: type, number: Number(number) }),
      });
      if (!res.ok) throw new Error("bill");
      await readNdjson(res, (msg) => {
        const B = state.bill;
        if (!B || B.congress !== congress || B.number !== number) return;
        if (msg.section === "meta") {
          B.meta = Object.assign({}, B.meta, msg);
          if (msg.became_law) B.became_law = msg.became_law;
        }
        if (msg.section === "sponsors") {
          B.sponsors = msg.sponsors || [];
          B.cosponsors = msg.cosponsors || [];
        }
        if (msg.section === "translation") {
          B.translation = msg.translation || "";
          B.plate = msg.plate || {};
          if (msg.became_law) B.became_law = msg.became_law;
          B.pending = false;
        }
        if (msg.section === "timeline") B.timeline_events = msg.timeline_events || [];
        if (msg.section === "votes") B.votes = msg.votes || {};
        if (msg.section === "connections") B.connections = msg.connections || {};
        if (msg.section === "lobbying") B.lobbying = msg.entities || [];
        if (msg.section === "sponsor_money") B.sponsor_money = msg.sponsors || [];
        if (msg.section === "background") B.background = msg.items || [];
        if (msg.section === "bill_text") { B.bill_text = msg.bill_text || ""; B.text_truncated = !!msg.truncated; }
        if (state.view === "bill") scheduleRender();
      });
    } catch (e) {
      state.error = "This bill page could not be built.";
      scheduleRender();
    } finally {
      state.loading = false;
      // Ten sections used to force ten full repaints here. Now each one
      // schedules a frame, and this only paints if it changes something.
      if (state.bill && state.bill.pending) {
        state.bill.pending = false;
        scheduleRender();
      }
    }
  }

  async function openMember(bioguide, opts) {
    if (!bioguide) return;
    const seed = opts && opts.seed;
    state.loading = true;
    // A seed (the member block the ask already streamed) paints the full page
    // at once; otherwise a loading page holds until the profile arrives.
    state.member = seed && seed.member
      ? Object.assign({}, seed.member, { legislation: seed.legislation || {}, pending: false })
      : { bioguide_id: bioguide, legislation: {}, pending: true };
    if (seed) loadMemberMoney(state.member);
    if (!opts || !opts.fromHistory) history.pushState({}, "", memberPath(bioguide));
    go("member");
    try {
      const res = await fetch("/api/member/" + encodeURIComponent(bioguide));
      const data = await res.json();
      if (!data.found) {
        if (seed) return;
        state.error = "We could not find that member.";
        state.member = null;
        go("home");
        return;
      }
      state.member = Object.assign({}, data.member, { legislation: data.legislation || {}, pending: false });
      if (!seed) loadMemberMoney(state.member);
    } catch (e) {
      state.error = "We could not load that member.";
    } finally {
      state.loading = false;
      scheduleRender();
    }
  }

  // Money and trades load after the profile so the record paints first.
  // Each piece re-renders when it lands; a stale bioguide is dropped.
  function loadMemberMoney(m) {
    const bg = m.bioguide_id || "";
    state.money = { bioguide: bg, finance: null, pacs: null, industries: null, stocks: null };
    state.perf = {};
    const chamber = (m.chambers && m.chambers.join(" ")) || m.chamber || "";
    const still = () => state.money && state.money.bioguide === bg;
    const paint = () => { if (still() && state.view === "member") scheduleRender(); };
    if (m.name) {
      const q = new URLSearchParams({ name: m.name });
      if (m.state) q.set("state", m.state);
      if (chamber) q.set("chamber", chamber);
      fetch("/member/finance?" + q).then(r => r.json()).then(fin => {
        if (!still()) return;
        state.money.finance = fin || {};
        paint();
        if (fin && fin.candidate_id && fin.cycle) {
          const cq = new URLSearchParams({ cid: fin.candidate_id, cycle: fin.cycle });
          fetch("/member/industries?" + cq).then(r => r.json()).then(d => { if (still()) { state.money.industries = d; paint(); } }).catch(() => {});
          const pq = new URLSearchParams({ cid: fin.candidate_id, cycle: fin.cycle, name: fin.name || "" });
          fetch("/member/pac-interests?" + pq).then(r => r.json()).then(d => { if (still()) { state.money.pacs = d; paint(); } }).catch(() => {});
        }
      }).catch(() => { if (still()) { state.money.finance = {}; paint(); } });
    }
    if (bg) {
      fetch("/member/stocks?bioguide=" + encodeURIComponent(bg)).then(r => r.json()).then(d => {
        if (still()) { state.money.stocks = d || {}; paint(); }
      }).catch(() => { if (still()) { state.money.stocks = {}; paint(); } });
    }
  }

  async function togglePerf(ticker, date) {
    const key = ticker + "|" + date;
    const cur = state.perf[key];
    if (cur && cur.open) { cur.open = false; render(); return; }
    state.perf[key] = { open: true, loading: !cur || !cur.data, data: cur && cur.data };
    const keys = Object.keys(state.perf);
    for (let i = 0; keys.length - i > 100; i++) delete state.perf[keys[i]];  // bounded
    render();
    if (state.perf[key].data) return;
    try {
      const res = await fetch("/stock/perf?ticker=" + encodeURIComponent(ticker) + "&date=" + encodeURIComponent(date));
      state.perf[key].data = await res.json();
    } catch (e) {
      state.perf[key].data = { windows: [] };
    }
    state.perf[key].loading = false;
    render();
  }

  async function loadElections(stateCode) {
    if (!stateCode) { state.elections = null; return; }
    try {
      const res = await fetch("/api/elections?state=" + encodeURIComponent(stateCode));
      if (!res.ok) return;
      const data = await res.json();
      state.elections = (data.upcoming || [])[0] || null;
      if (state.view === "ledger") scheduleRender();
    } catch (e) { /* fail-open */ }
  }

  async function guessPlace() {
    if (state.placeConfirmed && state.stateCode) return;
    try {
      const res = await fetch("/geo/guess");
      if (!res.ok) return;
      const data = await res.json();
      if (data.state_code) {
        state.stateCode = data.state_code;
        state.placeName = data.state || NAME_BY_STATE[data.state_code] || data.state_code;
        state.placeSource = "ip";
        state.district = "";
        state.geoid = "";
        state.rep = "";
        scheduleRender();
      }
    } catch (e) { /* fail-open */ }
  }

  function applyResolved(data, source) {
    const code = (data.state || "").toUpperCase();
    if (!code) return false;
    state.stateCode = code;
    state.placeName = NAME_BY_STATE[code] || code;
    state.district = data.district_label || "";
    state.geoid = data.geoid || "";
    state.rep = (data.representative && data.representative.name) || "";
    state.placeSource = source;
    state.placeConfirmed = true;
    state.placeError = "";
    saveStore();
    return true;
  }

  function useMyLocation() {
    if (!navigator.geolocation) {
      state.placeError = "This browser will not share location. Type a zip or town instead.";
      render();
      return;
    }
    state.placeBusy = true;
    state.placeError = "";
    render();
    navigator.geolocation.getCurrentPosition(async (pos) => {
      try {
        const res = await fetch("/resolve-point", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lat: pos.coords.latitude, lon: pos.coords.longitude }),
        });
        const data = await res.json();
        if (!res.ok || data.error || !applyResolved(data, "gps")) {
          state.placeError = "We could not place that point in a district. Type a zip or town instead.";
        }
      } catch (e) {
        state.placeError = "Location lookup failed. Type a zip or town instead.";
      }
      state.placeBusy = false;
      render();
    }, () => {
      state.placeBusy = false;
      state.placeError = "Location was blocked. Type a zip or town instead.";
      render();
    }, { timeout: 10000, maximumAge: 600000 });
  }

  function clearPlace() {
    state.stateCode = "";
    state.placeName = "";
    state.placeSource = "";
    state.district = "";
    state.geoid = "";
    state.rep = "";
    state.placeConfirmed = false;
    state.placeError = "";
    saveStore();
    render();
  }

  const STATE_BY_NAME = {
    alabama: "AL", alaska: "AK", arizona: "AZ", arkansas: "AR", california: "CA",
    colorado: "CO", connecticut: "CT", delaware: "DE", florida: "FL", georgia: "GA",
    hawaii: "HI", idaho: "ID", illinois: "IL", indiana: "IN", iowa: "IA", kansas: "KS",
    kentucky: "KY", louisiana: "LA", maine: "ME", maryland: "MD", massachusetts: "MA",
    michigan: "MI", minnesota: "MN", mississippi: "MS", missouri: "MO", montana: "MT",
    nebraska: "NE", nevada: "NV", "new hampshire": "NH", "new jersey": "NJ",
    "new mexico": "NM", "new york": "NY", "north carolina": "NC", "north dakota": "ND",
    ohio: "OH", oklahoma: "OK", oregon: "OR", pennsylvania: "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", tennessee: "TN", texas: "TX",
    utah: "UT", vermont: "VT", virginia: "VA", washington: "WA", "west virginia": "WV",
    wisconsin: "WI", wyoming: "WY", "district of columbia": "DC",
  };
  const NAME_BY_STATE = {};
  for (const [name, code] of Object.entries(STATE_BY_NAME)) {
    NAME_BY_STATE[code] = name.replace(/\b\w/g, c => c.toUpperCase()).replace(/\bOf\b/, "of");
  }

  function confirmPlace() {
    if (!state.stateCode) return;
    state.placeConfirmed = true;
    if (!state.placeSource) state.placeSource = "typed";
    saveStore();
    render();
  }
  async function setPlace() {
    const box = document.getElementById("place-box");
    const v = box && box.value.trim();
    if (!v) return;
    state.placeBusy = true;
    state.placeError = "";
    render();
    try {
      const named = STATE_BY_NAME[v.toLowerCase()];
      const abbr = v.length === 2 && FIPS[v.toUpperCase()] ? v.toUpperCase() : null;
      if (named || abbr) {
        applyResolved({ state: named || abbr }, "typed");
      } else if (/^\d{5}(-\d{4})?$/.test(v)) {
        const res = await fetch("/resolve-zip", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ zip_code: v }),
        });
        const data = await res.json();
        if (!res.ok || !applyResolved({ state: data.state }, "typed")) {
          state.placeError = "That zip did not resolve. Try a town and state.";
        }
      } else {
        const res = await fetch("/resolve-address", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ address: v }),
        });
        const data = await res.json();
        if (!res.ok || data.error || !applyResolved(data, "typed")) {
          state.placeError = "We could not find that place. Try a zip, or a town and state.";
        }
      }
    } catch (e) {
      state.placeError = "Lookup failed. Try again.";
    }
    state.placeBusy = false;
    render();
  }
  // The ask box and the place box are <form>s: Enter submits natively.
  function askSubmit(e) {
    e.preventDefault();
    const box = e.currentTarget.querySelector("input");
    ask(box ? box.value : "");
  }
  function placeSubmit(e) { e.preventDefault(); setPlace(); }
  // Stage filter: the shelves render every card with a data-stage; the
  // container's data-stage and CSS in test.html hide the rest. No re-render.
  function setStage(id) {
    state.stage = state.stage === id ? "all" : id;
    const sh = document.getElementById("shelves");
    if (!sh) { render(); return; }
    sh.dataset.stage = state.stage;
    document.querySelectorAll(".frow").forEach(b =>
      b.classList.toggle("on", b.dataset.stage === state.stage || (state.stage === "all" && b.dataset.stage === "all")));
    const head = document.getElementById("funnel-head");
    if (head) head.textContent = state.stage !== "all" ? "Showing one stage only. Click again to clear." : head.dataset.default;
  }

  function watchIdForStory(s) {
    return (s.type || "hr") + s.number;
  }
  function relatedWatches(item) {
    if (!String(item.id || "").startsWith("topic:") || !state.ledger || !state.ledger.stories) return [];
    return state.ledger.stories.map(s => ({
      id: watchIdForStory(s),
      title: s.english_title || s.title,
      sub: s.meta,
      congress: s.congress,
      bill_type: s.type,
      number: s.number,
    }));
  }
  async function persistWatch(item, unsub) {
    if (!state.email) return;
    try {
      await fetch(unsub ? "/correspondence/unsubscribe" : "/correspondence/subscribe", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          email: state.email,
          bill_id: item.id,
          bill_title: item.title || "",
          congress: item.congress || null,
          bill_type: item.bill_type || item.type || null,
          bill_number: item.bill_number || item.number || null,
        }),
      });
    } catch (e) { /* local list still stands */ }
  }
  async function toggleWatch(item) {
    const exists = isWatched(item.id);
    const extras = relatedWatches(item);
    if (exists) {
      const drop = new Set([item.id, ...extras.map(x => x.id)]);
      state.watching = state.watching.filter(w => !drop.has(w.id));
    } else {
      const have = new Set(state.watching.map(w => w.id));
      state.watching = state.watching.concat([item], extras.filter(x => !have.has(x.id)));
    }
    saveStore();
    // Only the "watching" list changes shape; everywhere else the buttons
    // just flip their label and .on class in place.
    if (state.view === "watching") render(); else paintWatchButtons();
    await persistWatch(item, exists);
    for (const extra of extras) await persistWatch(extra, exists);
  }
  function paintWatchButtons() {
    document.querySelectorAll("[data-watch]").forEach(b => {
      const on = isWatched(b.dataset.watch);
      b.classList.toggle("on", on);
      if (b.dataset.on && b.dataset.off) b.textContent = on ? b.dataset.on : b.dataset.off;
    });
  }
  async function saveEmail() {
    const box = document.getElementById("watch-email");
    const v = box && box.value.trim();
    if (!v || !v.includes("@")) return;
    state.email = v;
    saveStore();
    for (const w of state.watching) {
      try {
        await fetch("/correspondence/subscribe", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            email: v, bill_id: w.id, bill_title: w.title || "",
            congress: w.congress || null, bill_type: w.bill_type || w.type || null,
            bill_number: w.number || w.bill_number || null,
          }),
        });
      } catch (e) { /* keep going */ }
    }
    render();
  }

  function chromeBar(opts) {
    const q = esc(state.query);
    const mid = opts.search
      ? `<form class="ask-row" onsubmit="askSubmit(event)">
           <input type="search" placeholder="Ask again" value="${q}" aria-label="Ask again">
           <button class="ask-go" type="submit">Ask</button>
         </form>`
      : (opts.back || "");
    return `<div class="chrome">
      <button class="wordmark" type="button" onclick="goHome()" style="font-size:22px;font-weight:700">Nos<em>Populi</em></button>
      ${mid}
      <button class="act" type="button" onclick="goWatching()">${esc(watchingTab())}</button>
    </div>`;
  }

  function homeView() {
    const err = state.error ? `<p class="meta" style="margin-top:12px">${esc(state.error)}</p>` : "";
    const healthAsk = state.stateCode ? "Healthcare bills in " + (state.placeName || state.stateCode) : "Healthcare bills";
    return `<div class="view" data-screen="home" style="min-height:100vh;display:flex;flex-direction:column">
      <div style="padding:26px 28px 10px">
        <div style="display:flex;justify-content:space-between;align-items:baseline;gap:16px;font-family:var(--fm);font-size:10px;letter-spacing:.18em;text-transform:uppercase;color:var(--muted);max-width:1020px;margin:0 auto;width:100%;border-bottom:1px solid var(--rule);padding-bottom:10px">
          <span>NosPopuli</span>
          <span style="font-family:var(--fb);font-style:italic;font-size:12px;letter-spacing:0;text-transform:none;color:var(--ink)">${esc(dateLong())}</span>
          <button class="act" type="button" onclick="goWatching()">${esc(watchingTab())}</button>
        </div>
      </div>
      <div style="flex:1;display:flex;flex-direction:column;justify-content:center;max-width:720px;width:100%;margin:0 auto;padding:30px 28px">
        <div style="text-align:center;margin-bottom:24px">
          <svg class="home-mark" id="home-mark" viewBox="0 0 800 280" aria-hidden="true"></svg>
          <button class="wordmark hero" type="button" onclick="goHome()" style="font-size:clamp(40px,7vw,58px);display:block;width:100%;text-align:center">Nos<em>Populi</em></button>
          <div style="font-family:var(--fm);font-size:11px;letter-spacing:.2em;text-transform:uppercase;color:var(--muted);margin-top:12px">Laws, in plain English</div>
        </div>
        <form class="ask-row" onsubmit="askSubmit(event)">
          <input id="q" type="search" placeholder="Ask about a law, a place, or a person" aria-label="Ask about a law, a place, or a person" ${state.loading ? "disabled" : ""} style="padding:18px 20px;font-size:18px">
          <button type="submit" style="border:0;background:var(--ink);color:var(--paper);font-family:var(--fm);font-size:11px;letter-spacing:.16em;text-transform:uppercase;padding:0 24px;cursor:pointer">${state.loading ? "…" : "Ask"}</button>
        </form>
        ${err}
        <div style="margin-top:20px;display:flex;flex-direction:column;gap:9px">
          <div class="kick">Things people ask</div>
          <div style="display:flex;flex-wrap:wrap;gap:10px">
            <button class="ex" type="button" onclick="ask(${JSON.stringify(healthAsk)})">${esc(healthAsk)}</button>
            <button class="ex" type="button" onclick="ask('HR 1')">HR 1</button>
            <button class="ex" type="button" onclick="ask('Ted Cruz')">Ted Cruz</button>
          </div>
        </div>
      </div>
      <div style="padding:14px 28px 22px">
        <div style="max-width:1020px;width:100%;margin:0 auto;display:flex;justify-content:space-between;align-items:baseline;gap:16px;flex-wrap:wrap;border-top:1px solid var(--rule);padding-top:14px">
          <span style="font-family:var(--fb);font-size:13px;color:var(--muted)">${esc(watchingLine())}</span>
          <button class="act" type="button" onclick="goWatching()">See them</button>
        </div>
      </div>
    </div>`;
  }

  function compactTitle(raw, limit) {
    if (!raw) return "";
    let t = String(raw).trim();
    t = t.replace(/^(a bill to|an act to|to)\s+/i, "");
    t = t.replace(/[,;]?\s*(and\s+)?for\s+other\s+purposes\.?\s*$/i, "");
    t = t.replace(/[\s.,;:]+$/, "").trim();
    if (t) t = t.charAt(0).toUpperCase() + t.slice(1);
    const cap = limit || 140;
    if (t.length > cap) t = t.slice(0, cap).replace(/\s+\S*$/, "");
    return t;
  }
  function stageFromAction(action, isLaw) {
    if (isLaw) return "law";
    const a = String(action || "").toLowerCase();
    if (/became public law|enacted|signed by president|signed into law|became law/.test(a)) return "law";
    if (/passed house|passed senate|agreed to in house|agreed to in senate/.test(a)) return "passed";
    if (/committee|referred|reported|ordered to be reported/.test(a)) return "committee";
    return "introduced";
  }
  function asStory(s) {
    return {
      congress: s.congress,
      type: (s.type || "").toLowerCase(),
      number: s.number,
      title: s.title,
      english_title: s.english_title || compactTitle(s.title),
      stage: s.stage || stageFromAction(s.latest_action, s.is_law),
    };
  }

  function stageDots(stage) {
    if (stage === "unknown") return "";
    const on = (name) => stage === name || (name === "committee" && (stage === "passed" || stage === "law")) || (name === "passed" && stage === "law") || name === "introduced";
    const fill = (name) => {
      if (name === "law") return stage === "law" ? "var(--ink)" : "var(--paper)";
      if (!on(name)) return "var(--aged)";
      if (name === "passed") return "var(--ink)";
      return "var(--accent)";
    };
    const border = (name) => (name === "law" && stage !== "law") ? "var(--rule)" : fill(name);
    const dot = (name) => `<span class="dot" style="background:${fill(name)};border:1.5px solid ${border(name)}"></span>`;
    return `<div class="dots" aria-hidden="true">${dot("introduced")}${dot("committee")}${dot("passed")}${dot("law")}</div>`;
  }

  // "Referred to the House Committee on Science, Space, and Technology." →
  // { word: "In committee", detail: "Science, Space, and Technology" }
  function plainAction(s) {
    const a = String(s.latest_action || "");
    const stage = s.stage;
    if (stage === "law") return { word: "Became law", detail: s.law_number ? "Public Law " + s.law_number : "" };
    if (stage === "passed") {
      const ch = /senate/i.test(a) ? "Senate" : /house/i.test(a) ? "House" : "";
      return { word: ch ? "Passed the " + ch : "Passed one chamber", detail: "" };
    }
    if (stage === "committee") {
      const sub = a.match(/Subcommittee on ([^.;]+?)(?:\.|,? and| in addition|$)/i);
      const com = a.match(/Committee on ([^.;]+?)(?:\.|,? and in addition|, and in addition| for a period|$)/i);
      if (/reported|ordered to be reported/i.test(a)) return { word: "Cleared committee", detail: com ? com[1].trim() : "" };
      if (sub) return { word: "In subcommittee", detail: sub[1].trim() };
      if (com) return { word: "In committee", detail: com[1].trim() };
      return { word: "In committee", detail: "" };
    }
    if (stage === "unknown") return { word: "", detail: "" };
    return { word: "Introduced", detail: "" };
  }
  const PARTY_COLOR = { D: "#2457a0", R: "var(--accent)", I: "#6b6355", ID: "#6b6355", L: "#6b6355" };
  function shortDate(iso) {
    if (!iso) return "";
    const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : ""));
    if (isNaN(d)) return "";
    const now = new Date();
    return d.toLocaleDateString("en-US", d.getFullYear() === now.getFullYear() ? { month: "short", day: "numeric" } : { month: "short", year: "numeric" });
  }
  // Four segments, filled up to the stage reached. Read with the word beside it.
  function stageTrack(stage) {
    const order = ["introduced", "committee", "passed", "law"];
    const idx = order.indexOf(stage);
    if (idx < 0) return "";
    return `<span class="track" aria-hidden="true">${order.map((_, i) => `<span class="${i <= idx ? "on" : ""}${stage === "law" && i === 3 ? " law" : ""}"></span>`).join("")}</span>`;
  }

  function compactCard(s) {
    const title = s.english_title || s.title || "";
    const id = ((s.type || "").toUpperCase() + " " + s.number).trim();
    const chamber = /^s/.test(s.type || "") ? "Senate" : "House";
    const act = plainAction(s);
    const when = shortDate(s.latest_action_date || s.introduced || s.date);
    const party = (s.sponsor_party || "").toUpperCase();
    const text = (s.english_text || "").trim();
    const summary = text && text !== s.latest_action ? `<p class="scard-text">${esc(text.length > 150 ? text.slice(0, 149).replace(/\s+\S*$/, "") + "…" : text)}</p>` : "";
    const who = s.sponsor
      ? `<div class="scard-who"><span class="pmark" style="background:${PARTY_COLOR[party] || "var(--rule)"}"></span>${esc(s.sponsor)}${party || s.sponsor_state ? ` <span class="meta" style="margin:0">${esc([party, s.sponsor_state].filter(Boolean).join("-"))}</span>` : ""}${s.cosponsors ? ` <span class="meta" style="margin:0">· +${s.cosponsors}</span>` : ""}</div>`
      : "";
    const status = act.word
      ? `<div class="scard-status">
           <div style="display:flex;align-items:center;gap:7px">${stageTrack(s.stage)}<span class="scard-stage">${esc(act.word)}</span>${when ? `<span class="meta" style="margin:0 0 0 auto;flex:none">${esc(when)}</span>` : ""}</div>
           ${act.detail ? `<div class="scard-detail">${esc(act.detail)}</div>` : ""}
         </div>`
      : "";
    return `<article class="scard" data-stage="${esc(s.stage || "unknown")}" onclick='event.stopPropagation();openBill(${Number(s.congress)},${JSON.stringify(s.type)},${Number(s.number)},${JSON.stringify(title)})'>
      <div class="scard-kick"><span style="color:var(--accent)">${esc(id)}</span><span>${chamber}</span>${s.policy_area ? `<span class="scard-area">${esc(s.policy_area)}</span>` : ""}</div>
      <h2 class="h2 scard-title">${esc(title)}</h2>
      ${summary}
      <div class="scard-foot">${who}${status}</div>
    </article>`;
  }

  function policyChips(areas) {
    const rows = Object.entries(areas || {}).sort((a, b) => b[1] - a[1]).slice(0, 4);
    if (!rows.length) return "";
    const max = rows[0][1] || 1;
    return `<div class="chips">${rows.map(([label, n]) =>
      `<span class="chip">${esc(label)} <span style="display:inline-block;width:28px;height:6px;background:var(--aged);border:1px solid var(--rule);vertical-align:middle;margin-left:4px"><span style="display:block;height:100%;width:${Math.round(100 * n / max)}%;background:var(--accent)"></span></span></span>`
    ).join("")}</div>`;
  }

  function memberShelf(m, legislation) {
    if (!m) return "";
    const bg = m.bioguide_id || "";
    const photo = bg ? `/member/photo/${encodeURIComponent(bg)}` : "";
    const sub = [m.party, m.state].filter(Boolean).join(" · ");
    const sponsored = (legislation && legislation.sponsored || []).filter(s => s.congress && s.type && s.number).slice(0, 3).map(asStory);
    const click = bg ? `onclick="event.stopPropagation();openMember(${JSON.stringify(bg)})"` : "";
    return `<div class="shelf">
      <div class="kick" style="margin-bottom:8px">Person</div>
      <div class="member-shelf" ${click}>
        ${photo ? `<img src="${esc(photo)}" alt="" onerror="this.style.display='none'">` : `<div style="width:88px;height:108px;background:var(--aged)"></div>`}
        <div>
          <div class="h2" style="margin:0 0 4px">${esc(m.name || "This member")}</div>
          <div class="meta">${esc(sub)}</div>
          ${policyChips((legislation && legislation.policy_areas) || {})}
          ${sponsored.length ? `<div class="shelf-row" style="margin-top:12px">${sponsored.map(compactCard).join("")}</div>` : ""}
        </div>
      </div>
    </div>`;
  }

  function renderShelves(L) {
    const byId = {};
    for (const s of L.stories || []) byId[s.id] = s;
    const shelves = L.shelves;
    const hasStage = (L.stories || []).some(s => s.stage && s.stage !== "unknown");
    const legend = hasStage ? `<div class="legend"><span>How far it got</span>${["introduced", "committee", "passed", "law"].map(st => `<span style="display:inline-flex;align-items:center;gap:5px">${stageTrack(st)}${esc(STAGE_WORDS[st])}</span>`).join("")}</div>` : "";
    const empty = `<p class="body shelves-empty">Nothing at that stage.</p>`;
    const wrap = inner => `<div class="shelves" id="shelves" data-stage="${esc(state.stage)}">${inner}${empty}</div>`;
    if (!shelves || !shelves.length) {
      const all = L.stories || [];
      if (!all.length) return state.loading ? `<p class="meta">Searching…</p>` : `<p class="body">Nothing at that stage.</p>`;
      return wrap(`${legend}<div class="shelf"><div class="shelf-row">${all.map(compactCard).join("")}</div></div>`);
    }
    const blocks = [legend];
    for (const sh of shelves) {
      if (sh.kind === "member") {
        if (L.member) blocks.push(memberShelf(L.member, L.legislation || {}));
        continue;
      }
      const items = (sh.item_ids || []).map(id => byId[id]).filter(Boolean);
      if (!items.length) continue;
      blocks.push(`<div class="shelf">
        <div class="lbl" style="margin-bottom:10px">${esc(sh.label)} · ${items.length}</div>
        <div class="shelf-row">${items.map(compactCard).join("")}</div>
      </div>`);
    }
    if (blocks.length < 2) {
      return state.loading ? `<p class="meta">Searching…</p>` : `<p class="body">Nothing at that stage.</p>`;
    }
    return wrap(blocks.join(""));
  }

  function placeStrip() {
    let actions;
    if (state.placeBusy) {
      actions = `<span class="meta">Finding you…</span>`;
    } else if (!state.stateCode) {
      actions = `<button class="sbtn" type="button" onclick="useMyLocation()">Use my location</button>
        <button class="sbtn" type="button" onclick="goWatching()">Type a place</button>`;
    } else if (!state.placeConfirmed) {
      actions = `<button class="sbtn" type="button" onclick="confirmPlace()">Right</button>
        <button class="sbtn" type="button" onclick="useMyLocation()">Use my location</button>
        <button class="sbtn" type="button" onclick="goWatching()">Somewhere else</button>`;
    } else {
      actions = `<button class="sbtn" type="button" onclick="goWatching()">Change</button>`;
    }
    const err = state.placeError ? `<div class="meta" style="width:100%;color:var(--accent)">${esc(state.placeError)}</div>` : "";
    return `<div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap;border:1px solid var(--rule);background:var(--card);padding:11px 14px;margin-bottom:26px">
      <span style="font-family:var(--fb);font-size:14px;color:var(--ink)">${esc(placeLine())}</span>
      ${actions}
      ${err}
    </div>`;
  }

  function ledgerView() {
    const L = state.ledger || { stories: [], funnel: [], headline: "", deck: "" };
    const personOnly = !!L.person_only;
    const defaultHead = personOnly ? "How far their recent bills got" : "How far they got";
    const funnelHead = state.stage !== "all" ? "Showing one stage only. Click again to clear." : defaultHead;
    const funnel = (L.funnel || []).map(r => {
      const on = state.stage === r.id || (state.stage === "all" && r.id === "all");
      return `<button class="frow ${on ? "on" : ""}" type="button" data-stage="${esc(r.id)}" onclick="setStage('${r.id}')">
        <div class="flabel">${esc(r.label)}</div>
        <div class="ftrack"><div style="height:100%;background:var(--accent);width:${esc(r.width)}"></div></div>
        <div class="fnum">${r.n}</div>
      </button>`;
    }).join("");
    const topicId = "topic:" + (L.question || "ask") + ":" + (L.state_code || "US");
    const watchAll = isWatched(topicId) ? "Watching this topic ✓" : "Watch all of these";
    const el = state.elections;
    const ballot = el
      ? `<button class="abtn" type="button" onclick="goElections()">Check my ballot${el.countdown_days != null ? " · " + el.countdown_days + " days" : ""}</button>
         <div style="font-family:var(--fb);font-size:12.5px;line-height:1.45;color:var(--muted)">${esc(el.name || "Next election")}${el.date ? " · " + esc(fmtDate(el.date)) : ""}</div>`
      : `<button class="abtn" type="button" onclick="goElections()">Check my ballot</button>
         <div style="font-family:var(--fb);font-size:12.5px;line-height:1.45;color:var(--muted)">Upcoming elections for this place.</div>`;
    const where = L.place_name || state.placeName || "Congress";
    const deck = L.deck
      ? `<div class="meta" style="max-width:38em;margin-bottom:16px">${esc(L.deck)}</div>`
      : "";
    const committee = L.committee;
    const q = L.question || state.query;
    const kick = committee
      ? `Committee${committee.chamber ? " · " + esc(committee.chamber) : ""}`
      : `You asked about ${esc(q)}${personOnly || q.toLowerCase().includes(where.toLowerCase()) ? "" : " in " + esc(where)}`;
    const funnelBlock = (L.funnel || []).length
      ? `<div class="lbl" id="funnel-head" data-default="${esc(defaultHead)}" style="border-bottom:1px solid var(--ink);padding-bottom:7px;margin-bottom:14px">${esc(funnelHead)}</div>
         <div style="display:grid;gap:9px;margin-bottom:18px">${funnel}</div>`
      : "";
    const watchId = committee ? "committee:" + (committee.system_code || committee.name) : topicId;
    const watchLabel = isWatched(watchId) ? (committee ? "Watching this committee ✓" : "Watching this topic ✓") : (committee ? "Watch this committee" : "Watch all of these");
    const watchSub = committee ? "A committee · " + esc(committee.chamber || "") : "A topic · " + (L.stories || []).length + " bills";
    const rail = committee
      ? `<div class="rail">
          <div class="lbl" style="border-bottom:1px solid var(--rule);padding-bottom:7px;margin-bottom:13px">Do something about it</div>
          <button class="abtn ${isWatched(watchId) ? "on" : ""}" type="button" data-watch="${esc(watchId)}" data-on="Watching this committee ✓" data-off="Watch this committee" onclick='toggleWatch(${JSON.stringify({ id: watchId, title: committee.name || "This committee", sub: watchSub }).replace(/'/g, "&#39;")})'>${watchLabel}</button>
          <div style="font-family:var(--fb);font-size:12.5px;line-height:1.45;color:var(--muted);margin-bottom:14px">One email when this committee reports a bill out. Nothing otherwise.</div>
        </div>
        <div class="railq">
          <div class="lbl" style="border-bottom:1px solid var(--rule);padding-bottom:7px;margin-bottom:10px">What a committee does</div>
          <p class="read" style="font-size:14px;margin:0">Every bill is sent to a committee first. Most never come back out. A committee can hold hearings, rewrite the bill, or vote to send it to the full chamber.</p>
          ${committee.url ? `<div class="meta" style="margin-top:10px"><a href="${esc(committee.url.replace(/\?.*$/, "").replace(/api\.congress\.gov\/v3\/committee\/(house|senate)\/(\w+)/, "www.congress.gov/committee/$1-committee/$2"))}" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:none">On Congress.gov ↗</a></div>` : ""}
        </div>`
      : `<div class="rail">
          <div class="lbl" style="border-bottom:1px solid var(--rule);padding-bottom:7px;margin-bottom:13px">Do something about it</div>
          ${(L.stories || []).length ? `<button class="abtn ${isWatched(watchId) ? "on" : ""}" type="button" data-watch="${esc(watchId)}" data-on="Watching this topic ✓" data-off="Watch all of these" onclick='toggleWatch(${JSON.stringify({ id: watchId, title: q || "This topic", sub: watchSub }).replace(/'/g, "&#39;")})'>${watchLabel}</button>
          <div style="font-family:var(--fb);font-size:12.5px;line-height:1.45;color:var(--muted);margin-bottom:14px">One email when any of them moves. Nothing otherwise.</div>` : ""}
          ${ballot}
        </div>
        <div style="border:1px solid var(--rule);background:var(--card);margin-bottom:16px">
          <div class="lbl" style="padding:14px 16px 9px">Where these bills apply</div>
          <div id="ledger-map" class="plc" style="height:170px;margin:0 16px"><span class="plct">District map</span></div>
          <div id="ledger-map-cap" style="font-family:var(--fb);font-size:12.5px;color:var(--muted);padding:9px 16px 15px">${state.rep && state.district ? esc(state.rep + " · " + state.district) : "Click a district to see who represents it."}</div>
        </div>
        <div style="border-top:1px solid var(--rule);padding-top:12px">
          <div style="font-family:var(--fm);font-size:9.5px;letter-spacing:.16em;text-transform:uppercase;color:var(--muted);margin-bottom:6px">Why you are seeing ${esc(where)}</div>
          <div style="font-family:var(--fb);font-size:13px;line-height:1.5">${esc(L.state_code && L.state_code !== state.stateCode ? "You asked about it." : whyPlace())}</div>
        </div>`;
    return `<div class="view wrap">
      ${chromeBar({ search: true })}
      <div class="kick" style="margin-bottom:9px">${kick}</div>
      <div class="h1" style="max-width:20em;margin-bottom:10px">${esc(L.headline)}</div>
      ${deck}
      ${committee ? "" : placeStrip()}
      <div class="cols">
        <div>
          ${funnelBlock}
          ${renderShelves(L)}
        </div>
        <div>${rail}</div>
      </div>
      <div class="folio"><b>Sheet two</b> · ${committee ? "one committee, newest first" : "the answer, as a ledger"}</div>
    </div>`;
  }

  function goElections() {
    ask(state.placeName ? "Elections in " + state.placeName : "Upcoming elections");
  }

  function offTopicView() {
    const o = state.offTopic || {};
    const isCommittee = o.kind === "committee";
    const healthAsk = state.stateCode ? "Healthcare bills in " + (state.placeName || state.stateCode) : "Healthcare bills";
    const tries = isCommittee
      ? ["Senate Finance Committee", "House Judiciary Committee", "Armed Services Committee"]
      : [healthAsk, "HR 1", "Ted Cruz", state.stateCode ? "Elections in " + (state.placeName || state.stateCode) : "Upcoming elections"];
    return `<div class="view wrap">
      ${chromeBar({ search: true })}
      <div class="loading-page" style="padding-top:36px">
        <div class="kick" style="margin-bottom:10px">You asked about ${esc(o.question || state.query)}</div>
        <div class="h1" style="max-width:20em;margin-bottom:14px">${isCommittee ? "We could not find that committee." : "That is not something Congress votes on."}</div>
        <p class="read" style="max-width:34em">${esc(o.reason || "This site covers federal bills, the people who write them, the committees they sit in, and the elections that put them there.")}</p>
        <div class="lbl" style="margin:26px 0 10px">Try one of these</div>
        <div style="display:flex;flex-wrap:wrap;gap:10px">${tries.map(t => `<button class="ex" type="button" onclick="ask(${JSON.stringify(t)})">${esc(t)}</button>`).join("")}</div>
      </div>
      <div class="folio"><b>Sheet seven</b> · an honest no</div>
    </div>`;
  }

  // The graph plate: one traversal, answered. Three asks — a person's votes,
  // who voted on an instrument, who held a seat on a date. Every answer
  // names the weakest hop it crossed, because nothing in the graph is
  // certified yet and the reader must not mistake it for the clerk's word.
  function graphView() {
    const G = state.graph || {};
    const stamp = (c) => `<span style="font-family:var(--fm);font-size:10px;letter-spacing:.16em;text-transform:uppercase;padding:1px 5px;border:1px solid currentColor;color:${c === "certified" ? "#2f5d2a" : "var(--accent)"}">${esc(c || "ingested")}</span>`;
    const pos = (p) => `<span style="font-family:var(--fm);font-size:11px;font-weight:500;letter-spacing:.1em;text-transform:uppercase;color:${p === "aye" ? "#2f5d2a" : p === "no" ? "var(--accent)" : "var(--muted)"}">${esc(p || "")}</span>`;
    const layer = (j) => (j || "").endsWith("country:us") ? "Congress" : "Fairfax County";
    const weak = (G.weak_hops || []).map(h => `${h.predicate} is ${h.weakest}`).join(", ");
    const askBtn = (t) => `<button class="ex" type="button" onclick="ask(${JSON.stringify(t)})">${esc(t)}</button>`;
    let head = "", deck = "", body = "";
    if (G.ask === "holder") {
      const hs = G.holders || [];
      head = hs.length ? hs.map(h => h.name).join(", ") : "Nobody on record.";
      deck = `${(G.seats || []).join("; ") || esc(G.seat)} · as of ${esc(G.as_of)}`;
      body = hs.map(h => `<div style="display:flex;flex-wrap:wrap;gap:6px 14px;align-items:baseline;padding:8px 0;border-bottom:1px solid var(--rule)">
          <span class="read" style="margin:0">${esc(h.name)}</span>${stamp(h.certification)}
          <span class="meta" style="margin:0">${esc(h.valid_from || "before our records")} → ${esc(h.valid_to || "open")}${h.bound_from && h.bound_from !== "exact" ? " · start " + esc(h.bound_from) : ""}${h.bound_to && h.bound_to !== "exact" ? " · end " + esc(h.bound_to) : ""}${h.inferred_from ? " (" + esc(h.inferred_from) + ")" : ""}</span>
        </div>`).join("");
    } else {
      const rows = G.rows || [];
      const who = G.ask === "voters" ? (G.persons || []).join(", ") : (G.persons || []).map(p => p.name).join(", ");
      const noun = (G.ask === "sponsors" || G.ask === "sponsored") ? "bill" : "recorded vote";
      head = rows.length ? `${rows.length}${G.truncated ? "+" : ""} ${noun}${rows.length === 1 ? "" : "s"}.` : `No ${noun}s.`;
      deck = G.ask === "voters" || G.ask === "sponsors"
        ? `${G.ask === "sponsors" ? "Sponsored: " : G.position ? esc(G.position) + " on " : "On "}${esc(G.topic)} · ${who || "nobody"}`
        : `${who || esc(G.query)}${G.topic ? " · on " + esc(G.topic) : ""}${G.ask === "sponsored" ? " · bills they put their name on" : ""}`;
      body = rows.map(r => `<div style="display:grid;grid-template-columns:1fr auto;gap:2px 12px;align-items:baseline;padding:8px 0;border-bottom:1px solid var(--rule)">
          <span class="read" style="margin:0">${G.ask === "voters" ? esc(r.person) + " · " : ""}${esc(r.title)}</span>${pos(r.position)}
          <span class="meta" style="grid-column:1/-1;margin:0">${esc(r.date)} · ${layer(r.jurisdiction)}${r.question ? " · " + esc(r.question) : ""}${r.topic ? " · topic: " + esc(r.topic) : ""} ${stamp(r.certification)}</span>
        </div>`).join("");
    }
    const tries = ["how did Herrity vote on zoning", "who voted no on the Affordable HOMES Act", "who held the Braddock seat on 2025-11-18", "who sponsored the Affordable HOMES Act", "what did Kaine sponsor"];
    return `<div class="view wrap">
      ${chromeBar({ search: true })}
      <div style="padding-top:28px;max-width:44em">
        <div class="kick" style="margin-bottom:10px">You asked ${esc(G.question || state.query)}</div>
        <div class="h1" style="margin-bottom:6px">${esc(head)}</div>
        <p class="read" style="margin:0 0 6px">${deck}</p>
        ${G.empty_reason ? `<p class="read" style="color:var(--muted)">${esc(G.empty_reason)}</p>` : ""}
        ${weak ? `<div class="meta" style="color:var(--accent);margin:0 0 4px">Weakest hop: ${esc(weak)}. Nothing in the graph is affirmed by a second source yet.</div>` : ""}
        ${(G.advisory_fields || []).length ? `<div class="meta" style="margin:0 0 4px">The topic filter matched a model's reading of each title, not the clerk's classification.</div>` : ""}
        ${G.place_ignored ? `<div class="meta" style="margin:0 0 4px">Ignored “${esc(G.place_ignored)}” in the topic: it is a place, and the graph already knows where these votes are.</div>` : ""}
        <div style="margin-top:14px">${body}</div>
        <div class="lbl" style="margin:26px 0 10px">Ask the graph something else</div>
        <div style="display:flex;flex-wrap:wrap;gap:10px">${tries.map(askBtn).join("")}</div>
      </div>
      <div class="folio"><b>The graph</b> · walked, not classified</div>
    </div>`;
  }

  function electionsView() {
    const E = state.electionsPage || {};
    const up = E.upcoming || [];
    const recent = E.recent || [];
    const where = E.place_name || state.placeName || "";
    const mine = E.state_code ? up.filter(e => e.affects_user) : up;
    const elsewhere = E.state_code ? up.filter(e => !e.affects_user) : [];
    const next = mine[0] || up[0];
    const headline = next
      ? `${next.name || "Next election"}${next.countdown_days != null ? ", in " + next.countdown_days + " day" + (next.countdown_days === 1 ? "" : "s") : ""}.`
      : (where ? `No upcoming elections listed for ${where}.` : "Set a place to see your elections.");
    const card = (e, past) => {
      const id = "election:" + (e.id || e.name);
      const deadline = e.registration_deadline ? `<div class="meta" style="margin:0">Register by ${esc(fmtDate(e.registration_deadline))}</div>` : "";
      const contests = (e.contests || []).slice(0, 4).map(c => `<span class="chip">${esc(c.name || c.office || c)}</span>`).join("");
      const links = [
        e.voter_info_url ? `<a class="act" href="${esc(e.voter_info_url)}" target="_blank" rel="noopener" style="text-decoration:none">Voter info ↗</a>` : "",
        e.ballotpedia_url ? `<a class="act" href="${esc(e.ballotpedia_url)}" target="_blank" rel="noopener" style="text-decoration:none">Ballotpedia ↗</a>` : "",
      ].filter(Boolean).join("");
      return `<article class="elect ${past ? "past" : ""}">
        <div class="elect-date">
          <div style="font-family:var(--fd);font-size:34px;font-weight:700;line-height:1">${e.date ? new Date(e.date + "T00:00:00").getDate() : "n/a"}</div>
          <div class="lbl" style="font-size:9px">${e.date ? new Date(e.date + "T00:00:00").toLocaleDateString("en-US", { month: "short", year: "numeric" }) : ""}</div>
        </div>
        <div style="min-width:0">
          <div style="font-family:var(--fd);font-size:20px;font-weight:700;line-height:1.2;margin-bottom:4px">${esc(e.name || "Election")}</div>
          <div class="meta" style="margin:0 0 6px">${past ? "Held" : (e.countdown_days == null ? "Upcoming" : e.countdown_days === 0 ? "Today" : e.countdown_days === 1 ? "Tomorrow" : e.countdown_days + " days away")}${e.affects_user ? " · on your ballot" : ""}</div>
          ${deadline}
          ${contests ? `<div class="chips">${contests}</div>` : ""}
          <div style="display:flex;gap:12px;flex-wrap:wrap;margin-top:10px;align-items:center">
            ${past ? "" : `<button class="sbtn ${isWatched(id) ? "on" : ""}" type="button" data-watch="${esc(id)}" data-on="Tracking ✓" data-off="Track" onclick='toggleWatch(${JSON.stringify({ id, title: e.name || "Election", sub: "An election · " + (e.date || "") }).replace(/'/g, "&#39;")})'>${isWatched(id) ? "Tracking ✓" : "Track"}</button>`}
            ${links}
          </div>
        </div>
      </article>`;
    };
    return `<div class="view wrap">
      ${chromeBar({ search: true })}
      <div class="kick" style="margin-bottom:9px">Elections${where ? " · " + esc(where) : ""}</div>
      <div class="h1" style="max-width:20em;margin-bottom:10px">${esc(headline)}</div>
      <div class="meta" style="max-width:38em;margin-bottom:16px">Dates and deadlines for the place you set. Track one and we email you before registration closes.</div>
      ${placeStrip()}
      <div class="cols">
        <div>
          ${mine.length ? `${sectionLbl(where ? "Coming up in " + where : "Coming up", mine.length + " election" + (mine.length === 1 ? "" : "s"))}<div style="display:grid;gap:12px;margin-bottom:26px">${mine.map(e => card(e, false)).join("")}</div>` : ""}
          ${elsewhere.length ? `${sectionLbl("Elsewhere in the country")}<div style="display:grid;gap:12px;margin-bottom:26px">${elsewhere.map(e => card(e, false)).join("")}</div>` : ""}
          ${recent.length ? `${sectionLbl("Recently held")}<div style="display:grid;gap:12px">${recent.slice(0, 4).map(e => card(e, true)).join("")}</div>` : ""}
          ${!up.length && !recent.length ? `<p class="read">Nothing listed yet. ${state.stateCode ? "Check back closer to the date." : "Use your location or type a place above and we will look again."}</p>` : ""}
        </div>
        <div>
          <div class="railq">
            <div class="lbl" style="border-bottom:1px solid var(--rule);padding-bottom:7px;margin-bottom:10px">Who represents this place</div>
            ${state.rep && state.district ? `<div style="font-family:var(--fb);font-size:14.5px">${esc(state.rep)} <span class="meta" style="margin:0">· ${esc(state.district)}</span></div>` : `<p class="read" style="font-size:14px;margin:0">Set an exact place and we name your representative here.</p>`}
          </div>
          <div style="border:1px solid var(--rule);background:var(--card);margin-bottom:16px">
            <div class="lbl" style="padding:14px 16px 9px">Your districts</div>
            <div id="ledger-map" class="plc" style="height:170px;margin:0 16px"><span class="plct">District map</span></div>
            <div id="ledger-map-cap" style="font-family:var(--fb);font-size:12.5px;color:var(--muted);padding:9px 16px 15px">Click a district to see who represents it.</div>
          </div>
        </div>
      </div>
      <div class="folio"><b>Sheet eight</b> · the calendar</div>
    </div>`;
  }

  function money(n) {
    n = Number(n) || 0;
    if (n >= 1e6) return "$" + (n / 1e6).toFixed(n >= 1e7 ? 0 : 1) + "M";
    if (n >= 1e3) return "$" + Math.round(n / 1e3) + "K";
    return "$" + Math.round(n);
  }
  function sectionLbl(text, sub) {
    return `<div class="lbl" style="border-bottom:1px solid var(--ink);padding-bottom:7px;margin:0 0 12px;display:flex;justify-content:space-between;gap:10px;align-items:baseline">
      <span>${esc(text)}</span>${sub ? `<span class="meta" style="margin:0">${esc(sub)}</span>` : ""}
    </div>`;
  }
  function note(text) {
    return `<p class="meta" style="margin:10px 0 0;line-height:1.5;font-style:italic">${text}</p>`;
  }
  // Compact share rows: name · bar · pct · amount. `other` rows read as leftovers.
  function shareRows(items, nameKey, color, scaleToMax) {
    const max = Math.max(...items.map(i => i.share || 0), 0.0001);
    return items.map(i => {
      const other = /^(Other|Unclassified employers)$/.test(i[nameKey]);
      const w = scaleToMax ? 100 * (i.share || 0) / max : 100 * (i.share || 0);
      return `<div class="mrow" style="grid-template-columns:minmax(0,9rem) 1fr 2.4rem 3.4rem" title="${esc((i.top || []).join(", "))}">
        <span style="font-family:var(--fb);font-size:13.5px;${other ? "color:var(--muted);font-style:italic" : ""}">${esc(i[nameKey])}</span>
        <span class="policy-bar"><span style="display:block;height:100%;width:${Math.min(100, Math.max(2, w)).toFixed(1)}%;background:${other ? "var(--rule)" : color}"></span></span>
        <span class="fnum" style="font-size:12px;color:var(--muted)">${Math.round((i.share || 0) * 100)}%</span>
        <span class="fnum" style="font-size:12px">${money(i.total)}</span>
      </div>`;
    }).join("");
  }

  function memberFinanceBlock() {
    const M = state.money || {};
    const fin = M.finance;
    if (fin === null || fin === undefined) return `${sectionLbl("Campaign money")}<p class="meta">Loading FEC filings…</p>`;
    const R = fin.receipts || 0;
    if (!R && !fin.disbursements) return "";
    const segs = [
      { label: "Small-dollar donors", sub: "under $200", val: fin.indiv_unitemized || 0, c: "#4a7c59" },
      { label: "Larger individuals", sub: "$200 and up", val: fin.indiv_itemized || 0, c: "#2457a0" },
      { label: "PACs", sub: "", val: fin.from_pacs || 0, c: "var(--accent)" },
      { label: "Party committees", sub: "", val: fin.from_party || 0, c: "#8b6a1a" },
      { label: "Self-funded", sub: "own money", val: fin.self_funding || 0, c: "var(--ink)" },
    ].filter(s => s.val > 0);
    const known = segs.reduce((a, s) => a + s.val, 0);
    const other = Math.max(0, R - known);
    if (other > R * 0.02) segs.push({ label: "Other", sub: "transfers, loans", val: other, c: "var(--rule)" });
    const bar = segs.map(s => `<span title="${esc(s.label)}: ${money(s.val)}" style="display:block;height:100%;width:${(100 * s.val / R).toFixed(1)}%;background:${s.c}"></span>`).join("");
    const rows = segs.map(s => `<div class="mrow" style="grid-template-columns:10px 1fr 2.4rem 3.4rem">
        <span style="width:10px;height:10px;background:${s.c};display:block"></span>
        <span style="font-family:var(--fb);font-size:13.5px">${esc(s.label)}${s.sub ? ` <span class="meta" style="margin:0 0 0 6px;font-size:10px">${esc(s.sub)}</span>` : ""}</span>
        <span class="fnum" style="font-size:12px;color:var(--muted)">${Math.round(100 * s.val / R)}%</span>
        <span class="fnum" style="font-size:12px">${money(s.val)}</span>
      </div>`).join("");
    const cyc = fin.cycle ? fin.cycle + " cycle" : "latest filing";
    const pacCyc = fin.cycle ? `${fin.cycle - 3}–${String(fin.cycle).slice(-2)}` : "recent cycles";
    const pacs = (fin.top_pacs || []).filter(p => p.amount > 0);
    const pacList = pacs.length ? `<div class="sub-lbl">Top PAC contributors <span>${esc(pacCyc)}</span></div>
      ${pacs.map(p => `<div class="mrow" style="grid-template-columns:1fr auto"><span style="font-family:var(--fb);font-size:13.5px">${esc(p.name)}</span><span class="fnum" style="font-size:12px;color:var(--accent)">${money(p.amount)}</span></div>`).join("")}` : "";
    const pi = (M.pacs && M.pacs.interests || []).filter(i => i.total > 0);
    const piList = pi.length ? `<div class="sub-lbl">Funded by these interests <span>PAC money · recent cycles</span></div>
      ${shareRows(pi, "interest", "#8b6a1a", true)}
      ${note("Each PAC grouped by what it <em>is</em>: an industry or a cause. Hover a row for examples. This is where the money came from, not what anyone believes.")}` : "";
    const inds = (M.industries && M.industries.industries || []).filter(i => i.total > 0);
    const indCyc = M.industries && M.industries.cycle ? `estimated · ${M.industries.cycle - 1}–${String(M.industries.cycle).slice(-2)}` : "estimated";
    const indList = inds.length ? `<div class="sub-lbl">Individual donors by industry <span>${esc(indCyc)}</span></div>
      ${shareRows(inds, "industry", "#2457a0", false)}
      ${note("Itemized individual donors ($200+) grouped by their employer's industry. Shares of that classified money, not of the total. Approximate.")}` : "";
    return `${sectionLbl("Campaign money", cyc)}
      <div style="font-family:var(--fd);font-size:22px;font-weight:700;line-height:1.2;margin-bottom:4px">${money(R)} raised <span style="color:var(--muted);font-weight:400">·</span> ${money(fin.cash_on_hand || 0)} on hand</div>
      ${fin.fec_url ? `<div class="meta" style="margin-bottom:12px"><a href="${esc(fin.fec_url)}" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:none">FEC filing ↗</a></div>` : ""}
      <div class="policy-bar" style="display:flex;height:14px;margin-bottom:10px">${bar}</div>
      ${rows}
      ${pacList}
      ${piList}
      ${indList}
      ${note("Shares of <strong>total money raised</strong> this cycle, from FEC filings, with the member's own joint-fundraising committees and pass-throughs (ActBlue, WinRed) removed.")}`;
  }

  function memberStocksBlock(isSenator) {
    const M = state.money || {};
    const d = M.stocks;
    if (d === null || d === undefined) return `${sectionLbl("Stock trades")}<p class="meta">Checking STOCK Act filings…</p>`;
    const trades = d.trades || [];
    if (!trades.length) {
      const yrs = (d.cycles || []).join("–");
      if (isSenator) return `${sectionLbl("Stock trades")}${note("Senate disclosures sit in a system that is not machine-readable the way the House's is, so we cannot show a senator's trades yet. That is our gap, not a sign they do not trade.")}`;
      if (d.filed === false) return `${sectionLbl("Stock trades")}${note(`No trades disclosed${yrs ? " in " + esc(yrs) : ""}. This member filed no Periodic Transaction Reports. Trades over $1,000 must be reported under the STOCK Act.`)}`;
      return "";
    }
    const chips = (d.top_tickers || []).map(t => `<span class="chip">${esc(t.ticker)} <span style="color:var(--muted)">${t.count}</span></span>`).join("");
    const rows = trades.map(t => {
      const dir = (t.type || "").startsWith("buy") ? "buy" : (t.type || "").startsWith("sell") ? "sell" : "exch";
      const color = dir === "buy" ? "#2a6e2a" : dir === "sell" ? "var(--accent)" : "var(--muted)";
      const label = t.ticker || (t.asset || "").slice(0, 32);
      const key = (t.ticker || "") + "|" + (t.date || "");
      const p = state.perf[key];
      const click = t.ticker ? ` onclick="togglePerf(${JSON.stringify(t.ticker)},${JSON.stringify(t.date || "")})" style="cursor:pointer"` : "";
      let perf = "";
      if (p && p.open) {
        if (p.loading) perf = `<div class="meta" style="padding:6px 0 10px">Loading price history…</div>`;
        else {
          const w = (p.data && p.data.windows) || [];
          perf = w.length
            ? `<div style="display:flex;gap:18px;flex-wrap:wrap;padding:6px 0 4px">${w.map(x => `<span><span class="meta" style="margin:0;display:block;font-size:9.5px">${esc(x.label)} later</span><span class="fnum" style="font-size:16px;color:${x.pct >= 0 ? "#2a6e2a" : "var(--accent)"}">${x.pct >= 0 ? "+" : ""}${x.pct}%</span></span>`).join("")}</div>
               ${note(`${esc(p.data.ticker || "")} closed at $${esc(String(p.data.base_price || ""))} on ${esc(p.data.base_date || "")}. The stock's move after the trade, not the member's gain, and not an accusation.`)}`
            : `<div class="meta" style="padding:6px 0 10px">No price history for this ticker.</div>`;
          perf = `<div style="padding:0 0 8px;border-bottom:1px solid var(--rule)">${perf}</div>`;
        }
      }
      return `<div class="mrow" style="grid-template-columns:5.4rem 3.6rem 1fr auto;${p && p.open ? "border-bottom:0" : ""}"${click}>
          <span class="fnum" style="font-size:11px;color:var(--muted);text-align:left">${esc(t.date || "")}</span>
          <span class="dotl" style="color:${color}">${esc(t.type || "")}</span>
          <span class="fnum" style="font-size:13px;text-align:left" title="${esc(t.asset || "")}">${esc(label)}${t.ticker ? ` <span style="color:var(--muted);font-size:10px">${p && p.open ? "▾" : "▸"}</span>` : ""}</span>
          <span class="meta" style="margin:0;white-space:nowrap">${esc(t.amount || "")}${t.owner ? " · " + esc(t.owner) : ""}</span>
        </div>${perf}`;
    }).join("");
    return `${sectionLbl("Stock trades", (d.cycles || []).join(", "))}
      <div style="font-family:var(--fd);font-size:22px;font-weight:700;line-height:1.2;margin-bottom:10px">${d.trade_count} disclosed trades <span style="color:var(--muted);font-weight:400">·</span> <span style="color:#2a6e2a">${d.buys} buys</span> <span style="color:var(--muted);font-weight:400">/</span> <span style="color:var(--accent)">${d.sells} sells</span></div>
      ${chips ? `<div class="chips" style="margin:0 0 12px">${chips}</div>` : ""}
      ${rows}
      ${note("Trades the member, spouse, or dependent disclosed under the STOCK Act, from House filings. Amounts are reported ranges with a ~45-day lag. Click a ticker to see how the stock moved afterward.")}`;
  }

  function memberView() {
    const m = state.member || {};
    const legis = m.legislation || {};
    const bg = m.bioguide_id || "";
    const photo = bg ? `/member/photo/${encodeURIComponent(bg.toLowerCase())}` : (m.photo_url || "");
    const chamber = (m.chambers && m.chambers.join(" & ")) || m.chamber || "";
    const isSenator = /sen/i.test(chamber);
    const kick = [m.party || "Independent", chamber, m.current === false ? "Former member" : "Currently serving"].filter(Boolean).join(" · ");
    const metaLine = [
      m.state,
      m.district ? "District " + m.district : "",
      m.start_year ? `${m.start_year}–${m.end_year || "present"}` : "",
      m.birth_year ? "b. " + m.birth_year : "",
    ].filter(Boolean).join(" · ");
    const sponsored = (legis.sponsored || []).filter(s => s.title && s.number && s.type);
    const areas = Object.entries(legis.policy_areas || {})
      .filter(([k]) => k && !/^(None|Other|null)$/.test(k))
      .sort((a, b) => b[1] - a[1]);
    const stats = [
      { n: m.years_served || "n/a", l: "Years served" },
      { n: Number(legis.sponsored_count || 0).toLocaleString(), l: "Bills sponsored" },
      { n: Number(legis.cosponsored_count || 0).toLocaleString(), l: "Cosponsored" },
      { n: areas.length, l: "Policy areas" },
    ].map(s => `<div class="stat"><div class="stat-n">${esc(String(s.n))}</div><div class="lbl" style="color:var(--muted);font-size:9px">${esc(s.l)}</div></div>`).join("");
    const max = (areas[0] && areas[0][1]) || 1;
    const bars = areas.slice(0, 12).map(([label, n]) =>
      `<div class="mrow" style="grid-template-columns:minmax(0,11rem) 1fr 2rem;border:0;padding:4px 0">
        <span style="font-family:var(--fb);font-size:13.5px">${esc(label)}</span>
        <span class="policy-bar"><span style="display:block;height:100%;width:${(100 * n / max).toFixed(1)}%;background:var(--ink)"></span></span>
        <span class="fnum" style="font-size:12px">${n}</span>
      </div>`).join("");
    const bills = sponsored.map(b => {
      const s = asStory(b);
      const title = compactTitle(b.title);
      return `<div class="mrow" style="grid-template-columns:5.2rem 1fr auto;cursor:pointer" onclick='openBill(${Number(s.congress)},${JSON.stringify(s.type)},${Number(s.number)},${JSON.stringify(title)})'>
        <span class="fnum" style="font-size:11px;color:var(--accent);text-align:left">${esc(((b.type || "").toUpperCase() + " " + b.number).trim())}</span>
        <span style="font-family:var(--fb);font-size:14px;line-height:1.4">${esc(title)}</span>
        <span class="meta" style="margin:0;white-space:nowrap">${esc(b.date || "")}</span>
      </div>`;
    }).join("");
    const links = [
      m.official_url ? `<a href="${esc(m.official_url)}" target="_blank" rel="noopener" class="act" style="text-decoration:none">Official site ↗</a>` : "",
      m.congress_url ? `<a href="${esc(m.congress_url)}" target="_blank" rel="noopener" class="act" style="text-decoration:none">Congress.gov ↗</a>` : "",
    ].filter(Boolean).join("");
    return `<div class="view wrap">
      ${chromeBar({ search: true })}
      <div style="display:grid;grid-template-columns:120px minmax(0,1fr);gap:22px;align-items:start;margin-bottom:22px">
        ${photo ? `<img src="${esc(photo)}" alt="" style="width:120px;height:150px;object-fit:cover;background:var(--aged);border:1px solid var(--rule)" onerror="this.style.visibility='hidden'">` : `<div style="width:120px;height:150px;background:var(--aged)"></div>`}
        <div>
          <div class="kick" style="margin-bottom:8px">${esc(kick)}</div>
          <div class="h1" style="margin-bottom:6px">${esc(m.name || "This member")}</div>
          <div class="meta" style="font-size:12px;margin-bottom:10px">${esc(metaLine)}</div>
          <div style="display:flex;gap:14px;flex-wrap:wrap">${links}</div>
        </div>
      </div>
      <div class="stats">${stats}</div>
      <div class="cols" style="margin-top:26px">
        <div>
          ${bars ? `${sectionLbl("Legislation by topic", "sponsored bills")}${bars}${note("Based on sponsored bills only, up to the 250 most recent. Cosponsored legislation is not counted.")}` : ""}
          <div style="margin-top:26px">${sectionLbl("Recent sponsored bills", sponsored.length ? sponsored.length + " shown" : "")}</div>
          ${bills || (state.loading ? `<p class="meta">Loading…</p>` : `<p class="body">No recent bills found.</p>`)}
        </div>
        <div>
          ${memberFinanceBlock()}
          <div style="margin-top:26px">${memberStocksBlock(isSenator)}</div>
        </div>
      </div>
      <div class="folio"><b>Sheet six</b> · the record beside the money</div>
    </div>`;
  }

  // The translator writes light markdown: "# Heading", "**bold**", "- item".
  // Render just that, on escaped text, so the explanation reads as sections
  // instead of a pre-wrapped wall.
  function mdLite(md) {
    const lines = String(md || "").split(/\r?\n/);
    const out = [];
    let para = [];
    let list = [];
    const inline = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
    const flush = () => {
      if (para.length) { out.push(`<p class="read">${inline(para.join(" "))}</p>`); para = []; }
      if (list.length) { out.push(`<ul class="read-list">${list.map(i => `<li>${inline(i)}</li>`).join("")}</ul>`); list = []; }
    };
    for (const raw of lines) {
      const line = raw.trim();
      if (!line) { flush(); continue; }
      const h = line.match(/^#{1,3}\s+(.*)$/);
      if (h) { flush(); out.push(`<div class="read-h">${inline(h[1])}</div>`); continue; }
      const li = line.match(/^[-*•]\s+(.*)$/);
      if (li) { if (para.length) flush(); list.push(li[1]); continue; }
      if (list.length) flush();
      para.push(line);
    }
    flush();
    return out.join("");
  }

  // Split the translation on its headings: [{title, body(html), plain}].
  // A leading untitled chunk gets the title "What this bill does".
  function mdSections(md) {
    const lines = String(md || "").split(/\r?\n/);
    const out = [];
    let cur = null;
    for (const raw of lines) {
      const h = raw.trim().match(/^#{1,3}\s+(.*)$/);
      if (h) { cur = { title: h[1].trim(), lines: [] }; out.push(cur); continue; }
      if (!cur) { if (!raw.trim()) continue; cur = { title: "What this bill does", lines: [] }; out.push(cur); }
      cur.lines.push(raw);
    }
    return out.filter(s => s.lines.some(l => l.trim())).map(s => {
      const text = s.lines.join("\n");
      return { title: s.title, body: mdLite(text), plain: text.replace(/^[-*•]\s+/gm, "").replace(/\s+/g, " ").trim() };
    });
  }

  // One collapsible section: label, a one-line fact readable while closed,
  // and the body. Open state survives re-renders via state.bill.open.
  function fold(id, label, fact, body) {
    const open = !!((state.bill || {}).open || {})[id];
    return `<details class="fold" ${open ? "open" : ""} ontoggle="foldToggle('${esc(id)}', this.open)">
      <summary>
        <span class="fold-lbl">${esc(label)}</span>
        <span class="fold-fact">${esc(fact || "")}</span>
        <span class="fold-chev">${open ? "−" : "+"}</span>
      </summary>
      <div class="fold-body">${body}</div>
    </details>`;
  }
  function foldToggle(id, open) {
    if (!state.bill) return;
    state.bill.open = state.bill.open || {};
    if (!!state.bill.open[id] === !!open) return;
    state.bill.open[id] = !!open;
    const d = document.querySelector(`details.fold[ontoggle*="'${id}'"] .fold-chev`);
    if (d) d.textContent = open ? "−" : "+";
  }

  // Congress.gov ships bill text as a fixed-width typescript: hard wraps at
  // ~70 columns, centering by leading spaces, ``TeX quotes'', <DOC>/<all>
  // markers. Rebuild it into blocks: a new block starts at a blank line, an
  // enumerator like (a) (1) (A) (i), a SEC. heading, or a centered line.
  // Everything else is a continuation of the block above. The first line's
  // indent becomes the block's left padding, so the outline survives.
  function formatBillText(txt) {
    let s = String(txt || "")
      .replace(/<\/?(DOC|all|html|body|pre)[^>]*>/gi, "")
      .replace(/\[\[Page [^\]]*\]\]/g, "")
      .replace(/<<NOTE:[^>]*>>[ \t]*/g, "")
      .replace(/``/g, "\u201C").replace(/''/g, "\u201D")
      .replace(/\r/g, "");
    const lines = s.split("\n");
    const blocks = [];
    let cur = null;
    const ENUM = /^\s*[\u201C\u2018"']*\(([a-z]{1,3}|[A-Z]{1,3}|\d{1,3}|[ivxlc]{1,5}|[IVXLC]{1,5})\)/;
    const SEC = /^\s*[\u201C]*(SEC(TION)?\.?\s+\d+[A-Z]?\.|TITLE\s+[IVXLC]+|Subtitle\s+[A-Z]|CHAPTER\s+\d+|PART\s+[A-Z0-9]+)(?=[\s\u2014-]|$)/;
    const TOC = /^\s*Sec\.\s+\d+[A-Z]?\./;
    const HEAD = /^\s*(\d+(st|nd|rd|th) (CONGRESS|Congress)|\d+(st|nd) Session|(H\. ?R\.|S\.|H\. ?J\. ?Res\.|S\. ?J\. ?Res\.|H\. ?Res\.|S\. ?Res\.|H\. ?Con\. ?Res\.|S\. ?Con\. ?Res\.) \d+|Public Law \d+-\d+|An Act|A BILL|A RESOLUTION|A JOINT RESOLUTION|A CONCURRENT RESOLUTION|IN THE (HOUSE OF REPRESENTATIVES|SENATE)( OF THE UNITED STATES)?)\s*$/;
    const RULE = /^\s*_{10,}\s*$/;
    const push = (kind, text, lead) => { cur = { kind, text, lead }; blocks.push(cur); };
    for (const raw of lines) {
      const line = raw.replace(/\s+$/, "");
      if (!line.trim()) { cur = null; continue; }
      if (RULE.test(line)) { push("rule", "", 0); cur = null; continue; }
      const lead = line.match(/^\s*/)[0].length;
      const text = line.trim();
      // Enumerators are always indented in this typescript; a "(d)" at column 0
      // or one followed by punctuation is a wrapped line, not a new item.
      // Items sit at columns 4, 12, 20…; their wrapped lines at 0, 8, 16….
      const isEnum = ENUM.test(line) && lead % 8 === 4 && !/^\s*[\u201C\u2018"']*\([^)]{1,5}\)\s*[,;:)(]/.test(line);
      const centered = lead >= 14 && text.length <= 72 - lead + 8 && !isEnum && !/[.;:]$/.test(text) && /[A-Za-z]/.test(text);
      const allCaps = /^[A-Z0-9 .,;:'\u2019\u201C\u201D()-]+$/.test(text) && /[A-Z]{3}/.test(text);
      const curDone = !cur || cur.kind !== "p" || /[.;:\u201D"]$/.test(cur.text);
      if (HEAD.test(line)) { push("head", text, lead); continue; }
      if (SEC.test(line)) { push("sec", text, 0); continue; }
      if (TOC.test(line)) { push("toc", text, lead); continue; }
      if (cur && cur.kind === "sec" && (allCaps || lead >= 10) && !isEnum) { cur.text += " " + text; continue; }
      if (!cur && allCaps && text.length < 40 && !/[.;:,]$/.test(text)) { push("head", text, lead); continue; }
      if (centered && (allCaps || lead >= 20) && curDone && (!cur || cur.kind !== "p" || cur.lead < 8)) { push("head", text, lead); continue; }
      if (isEnum || !cur || cur.kind === "head" || cur.kind === "rule") { push("p", text, lead); continue; }
      cur.text += (cur.text.endsWith("-") && !cur.text.endsWith("--") ? "" : " ") + text;
    }
    for (const b of blocks) b.text = b.text.replace(/--/g, "\u2014").replace(/`([^`'\n]{1,80})'/g, "\u2018$1\u2019");
    return blocks.map(b => {
      if (b.kind === "rule") return `<hr class="bt-rule">`;
      if (b.kind === "head") return `<div class="bt-head">${esc(b.text)}</div>`;
      if (b.kind === "sec") {
        const m = b.text.match(/^([\u201C]*(?:SEC(?:TION)?\.?\s+\d+[A-Z]?\.|TITLE\s+[IVXLC]+|Subtitle\s+[A-Z]|CHAPTER\s+\d+|PART\s+[A-Z0-9]+))\s*[\u2014]?\s*(.*)$/);
        const big = /^(TITLE|Subtitle|CHAPTER|PART)/.test(b.text);
        const rest = m ? m[2] : b.text;
        return `<div class="bt-sec ${big ? "bt-title" : ""}"><span class="bt-secno">${esc(m ? m[1] : "")}</span>${big && rest ? " \u2014 " : " "}${esc(rest)}</div>`;
      }
      if (b.kind === "toc") return `<p class="bt-p bt-toc">${esc(b.text)}</p>`;
      const pad = Math.min(Math.max(0, b.lead - 4), 40) * 0.22;
      const em = b.text.match(/^([\u201C]*\([^)]{1,5}\))\s*(.*)$/);
      const body = em ? `<span class="bt-enum">${esc(em[1])}</span> ${esc(em[2])}` : esc(b.text);
      return `<p class="bt-p" style="padding-left:${pad.toFixed(2)}em">${body}</p>`;
    }).join("");
  }

  const CG_TYPES = { hr: "house-bill", s: "senate-bill", hres: "house-resolution", sres: "senate-resolution", hjres: "house-joint-resolution", sjres: "senate-joint-resolution", hconres: "house-concurrent-resolution", sconres: "senate-concurrent-resolution" };
  function ordinal(n) { n = Number(n); const s = ["th", "st", "nd", "rd"], v = n % 100; return n + (s[(v - 20) % 10] || s[v] || s[0]); }
  function congressGovText(congress, type, number) {
    const t = CG_TYPES[(type || "").toLowerCase()];
    return t ? `https://www.congress.gov/bill/${ordinal(congress)}-congress/${t}/${number}/text` : `https://www.congress.gov/search?q=${encodeURIComponent((type || "").toUpperCase() + " " + number)}`;
  }

  async function loadFullText() {
    const B = state.bill;
    if (!B || B.textLoading) return;
    B.textLoading = true; render();
    try {
      const r = await fetch(`/api/bill/${B.congress}/${B.type}/${B.number}/text`);
      const d = await r.json();
      if (state.bill === B && d && d.text) { B.bill_text = d.text; B.text_truncated = false; }
    } catch (e) { /* keep the preview */ }
    finally { B.textLoading = false; render(); }
  }

  function billStage(b) {
    if (b.became_law) return "law";
    const texts = (b.timeline_events || []).map(e => (e.text || "").toLowerCase());
    if (texts.some(t => /became public law|signed by president|enacted/.test(t))) return "law";
    if (texts.some(t => /passed (house|senate)|agreed to in (house|senate)/.test(t))) return "passed";
    if (texts.some(t => /committee|referred|reported/.test(t))) return "committee";
    return "introduced";
  }
  const STAGE_WORDS = { introduced: "Introduced", committee: "In committee", passed: "Passed one chamber", law: "Became law" };

  function fmtDate(iso) {
    if (!iso) return "";
    const d = new Date(iso + (iso.length === 10 ? "T00:00:00" : ""));
    if (isNaN(d)) return iso;
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  }

  // Seat map: one dot per member, colored by vote, arranged by the server.
  function seatMap(label, data, w, h) {
    if (!data || !data.seats) return "";
    const s = data.summary || {};
    const dots = data.seats.map(x => `<circle cx="${x.x}" cy="${x.y}" r="${w > 400 ? 3.6 : 4.6}" fill="${esc(x.color)}"><title>${esc(x.name)} (${esc(x.party)}-${esc(x.state)}): ${esc(x.vote)}</title></circle>`).join("");
    const total = (s.yea || 0) + (s.nay || 0) || 1;
    return `<div class="vote-block">
      <div class="sub-lbl" style="margin-top:0;border:0;padding:0">${esc(label)}</div>
      <svg viewBox="0 0 ${w} ${h}" style="width:100%;height:auto;display:block">${dots}</svg>
      <div class="vote-bar"><span style="width:${(100 * (s.yea || 0) / total).toFixed(1)}%;background:#2a6e2a"></span><span style="width:${(100 * (s.nay || 0) / total).toFixed(1)}%;background:var(--accent)"></span></div>
      <div style="display:flex;justify-content:space-between;gap:10px">
        <span class="fnum" style="font-size:15px;color:#2a6e2a;text-align:left">${s.yea || 0} yes</span>
        <span class="meta" style="margin:0">${s.not_voting ? s.not_voting + " did not vote" : ""}</span>
        <span class="fnum" style="font-size:15px;color:var(--accent)">${s.nay || 0} no</span>
      </div>
    </div>`;
  }

  function billRow(r) {
    if (!r || !r.number) return "";
    const type = (r.type || "").toLowerCase();
    const title = compactTitle(r.title || "");
    const click = r.congress ? ` onclick='openBill(${Number(r.congress)},${JSON.stringify(type)},${Number(r.number)},${JSON.stringify(title)})' style="cursor:pointer"` : "";
    return `<div class="mrow" style="grid-template-columns:6rem 1fr"${click}>
      <span class="fnum" style="font-size:11px;color:var(--accent);text-align:left">${esc(type.toUpperCase() + " " + r.number)}</span>
      <span style="font-family:var(--fb);font-size:14px;line-height:1.4">${esc(title)}${r.latest_action ? `<span class="meta" style="display:block;margin:2px 0 0">${esc(compactTitle(r.latest_action).slice(0, 90))}</span>` : ""}</span>
    </div>`;
  }

  function billView() {
    const b = state.bill || {};
    const p = b.plate || {};
    const meta = b.meta || {};
    const sponsor = (b.sponsors || meta.sponsors || [])[0] || {};
    const cos = b.cosponsors || [];
    const nCo = cos.length;
    const dCount = cos.filter(c => (c.party || "").startsWith("D")).length;
    const rCount = cos.filter(c => (c.party || "").startsWith("R")).length;
    const bid = (b.type || "hr") + b.number;
    const idLabel = ((b.type || "").toUpperCase() + " " + b.number).trim();
    const watchBill = isWatched(bid) ? "Watching this bill ✓" : "Watch this bill";
    const reader = `/bill/${b.congress}/${b.type}/${b.number}/text`;
    const stage = billStage(b);
    const law = b.became_law;
    const title = p.headline || compactTitle(meta.title) || "This bill";

    // Where it is: one strip, dots plus a sentence.
    const status = `<div class="status-strip ${stage === "law" ? "is-law" : ""}">
      ${stageDots(stage)}
      <div>
        <div style="font-family:var(--fd);font-size:17px;font-weight:700;line-height:1.2">${esc(law ? "Became law" + (law.number ? " · Public Law " + law.number : "") : STAGE_WORDS[stage])}</div>
        ${p.status_plain ? `<div style="font-family:var(--fb);font-size:14px;line-height:1.55;margin-top:3px">${esc(p.status_plain)}</div>` : ""}
      </div>
    </div>`;

    // The explanation. The first section ("What this bill does") is the one
    // thing everyone reads, so it is open. The rest fold with a one-line fact.
    const secs = mdSections(b.translation);
    const lead = secs.length
      ? secs[0].body
      : (b.translation ? mdLite(b.translation) : `<p class="meta">Writing the plain English…</p>`);
    // The status strip already says where it is; do not fold the same sentence.
    const restSecs = secs.slice(1).filter(s => !/status|where it is|where it stands/i.test(s.title));
    const clip = (s, n) => { s = String(s || "").replace(/\*\*/g, ""); return s.length > n ? s.slice(0, n - 1).trimEnd() + "…" : s; };
    const explainFolds = restSecs.map((s, i) => {
      const t = s.title.toLowerCase();
      let fact = "";
      if (/who it affects/.test(t) && (p.who || []).length) fact = clip(p.who[0], 70);
      else if (/cost|money|pay/.test(t) && (p.cost || []).length) fact = clip(p.cost[0], 70);
      else fact = clip(s.plain, 70);
      return fold("x" + i, s.title, fact, s.body);
    }).join("");

    const costBody = (p.cost || []).length
      ? `<ul class="read-list">${p.cost.map(x => `<li>${esc(x)}</li>`).join("")}</ul>${p.cost_honesty ? note(esc(p.cost_honesty)) : ""}`
      : "";
    const hasCostSec = restSecs.some(s => /cost|money|pay/i.test(s.title));
    const cost = costBody && !hasCostSec ? fold("cost", "What it costs, and who pays", clip(p.cost[0], 70), costBody) : "";

    // Milestones only, newest first. Every procedural line lives behind a toggle.
    const events = b.timeline_events || [];
    const isMilestone = (e) => /introduced|referred|reported|passed|agreed to|became public law|signed by president|presented to president|vetoed|yea-nay|roll no|record vote/i.test(e.text || "") || /signed|vote|passed|introduced/.test(e.event_type || "");
    const shown = b.showAllEvents ? events : events.filter(isMilestone).slice(0, 7);
    const latest = events[0] || {};
    const history = events.length ? fold("history", "How it got here", `${events.length} action${events.length === 1 ? "" : "s"} · last ${fmtDate(latest.date)}`, `
      <div class="tl">${shown.map(ev => `<div class="tl-row">
          <span class="tl-date">${esc(fmtDate(ev.date))}</span>
          <span class="tl-dot ${/signed|law/i.test(ev.event_type + ev.text) ? "law" : /passed|agreed to/i.test(ev.text || "") ? "passed" : ""}"></span>
          <span class="tl-text">${esc(compactTitle(ev.text || ""))}${ev.chamber ? ` <span class="meta" style="margin:0">· ${esc(ev.chamber)}</span>` : ""}${ev.yea != null && !String(ev.text || "").includes(String(ev.yea)) ? ` <span class="meta" style="margin:0">· ${ev.yea}–${ev.nay}</span>` : ""}</span>
        </div>`).join("")}</div>
      ${events.length > shown.length || b.showAllEvents ? `<button class="act" type="button" onclick="state.bill.showAllEvents=!state.bill.showAllEvents;render()" style="margin-top:8px">${b.showAllEvents ? "Show milestones only" : "Show all " + events.length + " actions"}</button>` : ""}`) : "";

    // Votes as seat maps.
    const v = b.votes || {};
    const tally = (d) => d && d.summary ? `${d.summary.yea}–${d.summary.nay}` : "";
    const voteFact = [v.house ? "House " + tally(v.house) : "", v.senate ? "Senate " + tally(v.senate) : ""].filter(Boolean).join(" · ");
    const votes = (v.house || v.senate)
      ? fold("votes", "How they voted", voteFact, `
         <div class="vote-grid">${seatMap("House", v.house, 500, 260)}${seatMap("Senate", v.senate, 300, 200)}</div>
         ${note("Each dot is one member, seated by party. Green voted yes, red voted no, grey did not vote. Hover a dot for the name.")}`)
      : (b.votes && stage !== "introduced" && stage !== "committee" ? fold("votes", "How they voted", "No roll-call vote recorded", note("Bills can pass by voice vote or unanimous consent, which leaves no individual record.")) : "");

    // Who is pushing it: lobbying entities, bar by money on this bill.
    const lob = b.lobbying || [];
    const lobMax = Math.max(...lob.map(e => e.bill_spend || e.spend || 0), 1);
    const lobTotal = lob.reduce((a, e) => a + (e.bill_spend || e.spend || 0), 0);
    const lobbying = lob.length ? fold("lobbying", "Who is lobbying on it", `${lob.length} filer${lob.length === 1 ? "" : "s"} · ${money(lobTotal)} on filings naming it`, `
      ${lob.map(e => `<div class="mrow" style="grid-template-columns:minmax(0,12rem) 1fr auto">
        <span style="font-family:var(--fb);font-size:13.5px">${esc(e.name)}<span class="meta" style="display:block;margin:0">${esc(e.kind === "client" ? "hired lobbyists" : e.kind)} · ${e.mentions} filing${e.mentions === 1 ? "" : "s"}</span></span>
        <span class="policy-bar"><span style="display:block;height:100%;width:${Math.max(2, 100 * (e.bill_spend || e.spend || 0) / lobMax).toFixed(1)}%;background:#8b6a1a"></span></span>
        <span class="fnum" style="font-size:12px">${money(e.bill_spend || e.spend || 0)}</span>
      </div>`).join("")}
      ${note("Organizations whose lobbying disclosures name this bill, from Senate LDA filings. The amount is what they reported spending on filings that mention it, not on this bill alone.")}`) : "";

    // Money behind the sponsors.
    const sm = b.sponsor_money || [];
    const f0 = (sm[0] || {}).finance || {};
    const moneyFact = sm.length ? `${money(f0.receipts || 0)} raised · ${Math.round(100 * (f0.from_pacs || 0) / (f0.receipts || 1))}% from PACs${sm.length > 1 ? " · " + sm.length + " sponsors" : ""}` : "";
    const sponsorMoney = sm.length ? fold("money", "Money behind the sponsor" + (sm.length > 1 ? "s" : ""), moneyFact, `
      ${sm.map(s => {
        const f = s.finance || {};
        const R = f.receipts || 1;
        const pac = f.from_pacs || 0;
        const small = f.indiv_unitemized || 0;
        return `<div style="padding:8px 0 12px;border-bottom:1px solid var(--rule)">
          <div style="display:flex;justify-content:space-between;gap:10px;align-items:baseline">
            <span style="font-family:var(--fb);font-size:14.5px">${esc(compactName(s.name))} <span class="meta" style="margin:0">${esc([s.party, s.state].filter(Boolean).join("-"))}</span></span>
            <span class="fnum" style="font-size:14px">${money(f.receipts || 0)} raised</span>
          </div>
          <div class="policy-bar" style="display:flex;height:10px;margin:8px 0 6px"><span title="PACs" style="display:block;width:${(100 * pac / R).toFixed(1)}%;background:var(--accent)"></span><span title="Small-dollar donors" style="display:block;width:${(100 * small / R).toFixed(1)}%;background:#4a7c59"></span></div>
          <div class="meta" style="margin:0;display:flex;gap:14px;flex-wrap:wrap"><span><span style="color:var(--accent)">■</span> ${Math.round(100 * pac / R)}% from PACs</span><span><span style="color:#4a7c59">■</span> ${Math.round(100 * small / R)}% small-dollar</span><span>${money(f.cash_on_hand || 0)} on hand</span>${f.fec_url ? `<a href="${esc(f.fec_url)}" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:none">FEC ↗</a>` : ""}</div>
        </div>`;
      }).join("")}
      ${note("FEC totals for the people who introduced it, this cycle. Shown beside the lobbying above as two facts, not as a claim that one caused the other.")}`) : "";

    // Connected bills and reports.
    const c = b.connections || {};
    const conn = [];
    if (c.amends) conn.push(`<div class="sub-lbl">Amends</div><p class="read" style="font-size:14.5px">${esc(typeof c.amends === "string" ? c.amends : JSON.stringify(c.amends))}</p>`);
    if ((c.identical || []).length) conn.push(`<div class="sub-lbl">Same bill in the other chamber</div>${c.identical.map(billRow).join("")}`);
    if ((c.related || []).length) conn.push(`<div class="sub-lbl">Related bills</div>${c.related.slice(0, 6).map(billRow).join("")}`);
    if ((c.amended_by || []).length) conn.push(`<div class="sub-lbl">Amendments offered · ${c.amended_by.length}</div>${c.amended_by.slice(0, 5).map(a => `<div class="mrow" style="grid-template-columns:6rem 1fr"><span class="fnum" style="font-size:11px;color:var(--muted);text-align:left">${esc((a.type || "").toUpperCase() + " " + a.number)}</span><span style="font-family:var(--fb);font-size:14px;line-height:1.4">${esc(compactTitle(a.title || ""))}${a.latest_action ? `<span class="meta" style="display:block;margin:2px 0 0">${esc(a.latest_action.slice(0, 90))}</span>` : ""}</span></div>`).join("")}`);
    if ((c.committee_reports || []).length) conn.push(`<div class="sub-lbl">Committee reports</div>${c.committee_reports.map(r => `<div class="mrow" style="grid-template-columns:1fr auto"><span style="font-family:var(--fb);font-size:14px">${esc(r.committee || "")}${r.chamber ? ` <span class="meta" style="margin:0">· ${esc(r.chamber)}</span>` : ""}<span class="meta" style="display:block;margin:0">${esc(r.citation || "")}${r.issue_date ? " · " + esc(fmtDate(r.issue_date)) : ""}</span></span>${r.full_url ? `<a class="act" href="${esc(r.full_url)}" target="_blank" rel="noopener" style="text-decoration:none">Read ↗</a>` : ""}</div>`).join("")}`);
    if ((c.superseded || []).length) conn.push(`<div class="sub-lbl">Superseded by</div>${c.superseded.map(billRow).join("")}`);
    const connFact = [
      (c.identical || []).length ? "same bill in other chamber" : "",
      (c.related || []).length ? c.related.length + " related" : "",
      (c.amended_by || []).length ? c.amended_by.length + " amendment" + (c.amended_by.length === 1 ? "" : "s") : "",
      (c.committee_reports || []).length ? c.committee_reports.length + " report" + (c.committee_reports.length === 1 ? "" : "s") : "",
    ].filter(Boolean).join(" · ");
    const connections = conn.length ? fold("conn", "Connected to it", connFact, `<div class="conn">${conn.join("")}</div>`) : "";

    // Background: things the bill references, resolved.
    const bg = b.background || [];
    const background = bg.length ? fold("bg", "Things it refers to", clip(bg.map(i => i.term).join(", "), 70), `
      ${bg.map(i => `<div style="padding:0 0 12px;margin-bottom:12px;border-bottom:1px solid var(--rule)">
        <div style="font-family:var(--fd);font-size:18px;font-weight:700;margin-bottom:4px">${esc(i.term)}</div>
        <p class="read" style="font-size:15px;margin:0">${esc(i.summary)}</p>
        ${i.source ? `<div class="meta" style="margin-top:4px">${/^https?:/.test(i.source) ? `<a href="${esc(i.source)}" target="_blank" rel="noopener" style="color:var(--accent);text-decoration:none">Source ↗</a>` : esc(i.source)}</div>` : ""}
      </div>`).join("")}`) : "";

    // Glossary as a two-column definition grid.
    const gloss = (p.glossary || []).length ? fold("gloss", "Words it uses", clip(p.glossary.map(g => g.term).join(", "), 70), `
      <div class="gloss">${p.glossary.map(g => `<div><div style="font-family:var(--fd);font-size:16.5px;font-weight:700;margin-bottom:2px">${esc(g.term)}</div><div style="font-family:var(--fb);font-size:14px;line-height:1.55">${esc(g.meaning)}</div></div>`).join("")}</div>`) : "";

    // Full text, folded.
    const cgText = congressGovText(b.congress, b.type, b.number);
    const pages = b.bill_text ? Math.max(1, Math.round(b.bill_text.length / 3200)) : 0;
    const text = b.bill_text != null ? fold("text", "The actual text", b.bill_text ? `About ${pages} page${pages === 1 ? "" : "s"}${b.text_truncated ? ", first part loaded" : ""}` : "Not published yet", `
      <div style="display:flex;gap:12px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
        ${b.text_truncated ? `<button class="act" type="button" onclick="loadFullText()" ${b.textLoading ? "disabled" : ""}>${b.textLoading ? "Loading the rest…" : "Load the whole bill here"}</button>` : ""}
        <a class="act" href="${esc(reader)}" target="_blank" rel="noopener" style="text-decoration:none">Read the whole bill in a new tab ↗</a>
        <a class="act" href="${esc(cgText)}" target="_blank" rel="noopener" style="text-decoration:none;color:var(--muted)">Congress.gov ↗</a>
      </div>
      <div class="billtext">${b.bill_text ? formatBillText(b.bill_text) : `<p class="bt-p">No text is available yet.</p>`}</div>
      ${b.text_truncated ? note("This is the first part of a long bill. Load the rest above, or open the whole thing in a new tab.") : ""}`) : "";

    // The rail keeps the scannable version of "who": short lines, always visible.
    const who = (p.who || []).length
      ? `<div class="railq"><div class="lbl" style="border-bottom:1px solid var(--rule);padding-bottom:7px;margin-bottom:10px">Who it changes things for</div><ul class="read-list" style="font-size:13.5px;margin:0;line-height:1.5">${p.who.slice(0, 5).map(x => `<li>${esc(clip(x, 90))}</li>`).join("")}</ul></div>`
      : "";

    // At a glance: four numbers before any reading.
    const glance = `<div class="stats" style="margin:18px 0 0">
      <div class="stat"><div class="stat-n" style="font-size:22px">${esc(law ? "Law" : STAGE_WORDS[stage])}</div><div class="lbl">Where it is</div></div>
      <div class="stat"><div class="stat-n">${nCo}</div><div class="lbl">Cosponsors${nCo ? ` · ${dCount}D ${rCount}R` : ""}</div></div>
      <div class="stat"><div class="stat-n" style="font-size:${v.house || v.senate ? 26 : 16}px">${esc(v.house ? tally(v.house) : v.senate ? tally(v.senate) : (b.votes ? "No roll call" : "…"))}</div><div class="lbl">${v.house ? "House vote" + (v.senate ? " · Senate " + tally(v.senate) : "") : v.senate ? "Senate vote" : "Recorded vote"}</div></div>
      <div class="stat"><div class="stat-n" style="font-size:22px">${b.lobbying ? (lob.length ? money(lobTotal) : "None") : "…"}</div><div class="lbl">Lobbying${lob.length ? ` · ${lob.length} filer${lob.length === 1 ? "" : "s"}` : ""}</div></div>
    </div>`;

    const split = nCo ? `<div class="policy-bar" style="display:flex;height:8px;margin:8px 0 4px"><span style="display:block;width:${(100 * dCount / nCo).toFixed(1)}%;background:#2457a0"></span><span style="display:block;width:${(100 * rCount / nCo).toFixed(1)}%;background:var(--accent)"></span></div>
      <div class="meta" style="margin:0">${dCount} Democrats · ${rCount} Republicans${nCo - dCount - rCount ? " · " + (nCo - dCount - rCount) + " other" : ""}</div>` : "";

    return `<div class="view wrap">
      ${chromeBar({ search: true })}
      <div class="cols">
        <div class="read-col">
          <div class="kick" style="margin-bottom:10px">${esc(idLabel)} · ${esc(b.congress)}th Congress${sponsor.name ? " · " + esc(compactName(sponsor.name)) : ""}</div>
          <div class="h1" style="margin-bottom:12px">${esc(title)}</div>
          ${p.formal_name || meta.title ? `<div class="meta" style="margin-bottom:18px;font-style:italic;font-family:var(--fb);font-size:13px;text-transform:none;letter-spacing:0">${esc(p.formal_name || meta.title)}</div>` : ""}
          ${status}
          ${glance}
          <div style="margin-top:22px">${lead}</div>
          <div class="folds">
            ${explainFolds}${cost}${history}${votes}${lobbying}${sponsorMoney}${connections}${background}${gloss}${text}
          </div>
        </div>
        <div>
          <div class="rail">
            <div class="lbl" style="border-bottom:1px solid var(--rule);padding-bottom:7px;margin-bottom:13px">Do something about it</div>
            <button class="abtn ${isWatched(bid) ? "on" : ""}" type="button" data-watch="${esc(bid)}" data-on="Watching this bill ✓" data-off="Watch this bill" onclick='toggleWatch(${JSON.stringify({ id: bid, title: title, sub: idLabel, congress: Number(b.congress) || null, bill_type: b.type || "", number: Number(b.number) || null }).replace(/'/g, "&#39;")})'>${watchBill}</button>
            <div style="font-family:var(--fb);font-size:12.5px;line-height:1.45;color:var(--muted)">One email if this bill moves. Nothing otherwise.</div>
          </div>
          <div class="railq">
            <div class="lbl" style="border-bottom:1px solid var(--rule);padding-bottom:7px;margin-bottom:12px">Who is behind it</div>
            ${sponsor.name ? `<div style="font-family:var(--fb);font-size:14.5px;line-height:1.5;${sponsor.bioguide_id ? "cursor:pointer" : ""}" ${sponsor.bioguide_id ? `onclick="openMember('${esc(sponsor.bioguide_id)}')"` : ""}>${esc(compactName(sponsor.name))} <span style="color:var(--muted)">wrote it</span>${sponsor.bioguide_id ? ` <span class="meta" style="margin:0">›</span>` : ""}</div>
            <div class="meta" style="margin:0 0 10px">${esc([sponsor.party, sponsor.state].filter(Boolean).join(" · "))}</div>` : `<div class="meta">Sponsor not listed</div>`}
            <div style="font-family:var(--fb);font-size:14px;line-height:1.5">${nCo ? nCo + " others signed on" : "No cosponsors"}</div>
            ${split}
          </div>
          ${who}
          <div style="border-top:1px solid var(--rule);padding-top:13px;margin-bottom:14px">
            <div class="meta" style="line-height:1.6">This page is our English. <a href="${esc(reader)}" target="_blank" rel="noopener">The original</a> is one click away. A person wrote none of this; <a href="mailto:flags@nospopuli.org?subject=${esc(bid)}">tell us if it is wrong</a>.</div>
          </div>
        </div>
      </div>
      <div class="folio"><b>Sheet three</b> · one bill, in order</div>
    </div>`;
  }

  // "Rep. Arrington, Jodey C. [R-TX-19]" → "Jodey C. Arrington"
  function compactName(n) {
    const s = String(n || "").replace(/\s*\[[^\]]*\]\s*$/, "").replace(/^(Rep|Sen|Del|Del\.|Sen\.|Rep\.)\.?\s+/, "");
    const m = s.match(/^([^,]+),\s*(.+)$/);
    return m ? `${m[2].trim()} ${m[1].trim()}` : s;
  }

  // Latin stands in for the parts of a county page we hold no data for:
  // district geography, supervisor terms and biographies, video, transcripts.
  // It is Latin, it sits inside a hatched plate or the member's own window,
  // and it is labelled, because the one rule this product cannot break is
  // letting a reader mistake something we invented for something a county
  // published. Set PLACEHOLDERS to false and every plate disappears.
  const PLACEHOLDERS = true;
  const LATIN = {
    districts: "Lorem ipsum dolor sit amet, consectetur adipiscing elit. Sed do eiusmod tempor incididunt ut labore et dolore magna aliqua.",
    bio: "Lorem ipsum dolor sit amet, consectetur adipiscing elit, sed do eiusmod tempor incididunt ut labore et dolore magna aliqua. Ut enim ad minim veniam, quis nostrud exercitation ullamco laboris nisi ut aliquip ex ea commodo consequat.",
    elected: "MMXXIII", termEnds: "MMXXVII", ballot: "Novembris",
    margin: "Duis aute irure dolor in reprehenderit",
    turnout: "XLI percent", predecessor: "Quis nostrud",
  };

  function placePlate(lines, height) {
    return `<div class="plc plc-sheet" style="height:${height}px"><span class="plct">${lines.join("<br>")}</span></div>`;
  }

  function unchartedView() {
    const u = state.uncharted || {};
    const place = u.place || {};
    const cov = u.coverage || {};
    const recs = cov.records || [];
    const pct = Math.round(cov.certified_pct || 0);
    const shortName = place.name ? place.name.split(",")[0] : "This board";
    if (!recs.length) return unchartedEmptyView(u, place, cov, shortName);

    const idx = Math.min(state.focusMeeting || 0, recs.length - 1);
    const focus = recs[idx];
    const st = cov.stats || {};
    const watchLabel = isWatched("place:" + (place.slug || "x"))
      ? "We will email you" : "Tell me when this fills in";

    const deck = pct === 0
      ? `Every vote below is copied from the record this government publishes itself. We have not been able to check any of it against a second, independently written source, so read it as their account and not as ours.`
      : `Every vote below comes from the board's own record, and ${pct}% of it is checked against a second, independently written source. Where the two disagree we say so rather than pick one.`;

    const warn = cov.public_note ? `
      <div style="border:1px solid var(--accent);padding:13px 15px;margin-bottom:24px;max-width:46em">
        <div class="lbl" style="color:var(--accent);margin-bottom:5px">${pct === 0 ? "Not confirmed" : "Partly confirmed"}</div>
        <div style="font-family:var(--fb);font-size:14.5px;line-height:1.6">${esc(cov.public_note)}</div>
      </div>` : "";

    return `<div class="view wrap">
      ${chromeBar({ search: true })}
      <div class="kick" style="margin-bottom:9px">${esc(place.name || "This place")} · ${esc(place.body || "Board of Supervisors")}</div>
      <div class="h1" style="max-width:20em;margin-bottom:12px">What ${esc(shortName)}'s board decided, meeting by meeting.</div>
      <div class="dropcap" style="font-family:var(--fb);font-size:16px;line-height:1.55;max-width:38em;margin-bottom:22px">${deck}</div>
      ${warn}
      <div class="cols">
        <div>
          <div id="focus-block">${focusBlock(recs, idx)}</div>
          ${otherMeetingsBlock(recs, idx, cov)}
          ${comingUpBlock(cov, place, watchLabel)}
          ${capitalBlock(cov)}
        </div>
        <div>
          ${districtsBlock(shortName, cov)}
          ${boardBlock(cov)}
          ${figuresBlock(st)}
        </div>
      </div>
      <div class="folio"><b>Sheet four</b> · a county, charted</div>
      <div id="sup-modal"></div>
    </div>`;
  }

  const longDate = d => new Date(d + "T12:00:00").toLocaleDateString("en-US",
    { month: "long", day: "numeric", year: "numeric" });
  const tallyOf = v => Object.entries(v.counts || {}).map(([k, n]) => `${n} ${k}`).join(" · ");
  const moneyOf = n => n >= 1e9 ? `$${(n / 1e9).toFixed(1)}B`
                     : n >= 1e6 ? `$${(n / 1e6).toFixed(0)}M` : `$${Math.round(n / 1e3)}K`;

  // The focus list: one meeting's decisions, scrollable, re-rendered in place
  // when another meeting is chosen so the swap animates instead of reloading
  // the page under the reader.
  function focusBlock(recs, idx) {
    const focus = recs[idx];
    const rows = (focus.votes || []).map(v => `
      <div class="mtg-focus">
        <div>
          <div style="font-family:var(--fb);font-size:15px;line-height:1.5;font-weight:600">${esc(v.plain || v.title || "Motion recorded without a readable description")}</div>
          <div class="meta">${esc([v.topic, (v.against || []).length ? "Against: " + v.against.join(", ") : "",
                                   (v.abstain || []).length ? "Abstained: " + v.abstain.join(", ") : ""]
                                  .filter(Boolean).join(" · "))}</div>
        </div>
        <div style="text-align:right;white-space:nowrap">
          <div style="font-family:var(--fm);font-size:11px;letter-spacing:.09em;text-transform:uppercase">${esc(v.result === "fail" ? "failed" : v.result || "")}</div>
          <div class="meta">${esc(tallyOf(v))}</div>
          <div class="meta">${v.certified ? '<span style="color:var(--accent)">confirmed</span>' : "unconfirmed"}</div>
        </div>
      </div>`).join("");
    return `
      <div class="lbl" style="border-bottom:1px solid var(--ink);padding-bottom:7px;margin-bottom:4px">
        Decisions in focus <span style="float:right;color:var(--muted)">Meeting of ${esc(longDate(focus.date))}</span>
      </div>
      <div class="focus-scroll" onscroll="onFocusScroll(this)"><div class="focus-inner">${rows}</div></div>
      <div class="scroll-hint">↓ more decisions below</div>
      <div class="meta" style="display:flex;justify-content:space-between;gap:10px;align-items:baseline;padding:7px 0 0;margin-bottom:6px;border-top:1px solid var(--rule)">
        <span>${focus.votes.length} of ${focus.vote_total} decisions this meeting</span>
        <span>${focus.present.length} present${focus.absent.length ? ", " + focus.absent.length + " absent" : ""}</span>
      </div>
      ${focus.source_url ? `<a class="sbtn" style="border-color:var(--accent);color:var(--accent);text-decoration:none;display:inline-block;margin-bottom:24px" href="${esc(focus.source_url)}" target="_blank" rel="noopener">The whole meeting, on their site</a>` : ""}`;
  }

  // Bring a meeting into focus: swap only the focus block so the CSS
  // animation runs and the rest of the page stays put.
  function focusMeeting(i) {
    const recs = ((state.uncharted || {}).coverage || {}).records || [];
    if (!recs[i]) return;
    state.focusMeeting = i;
    const host = document.getElementById("focus-block");
    if (host) {
      host.innerHTML = focusBlock(recs, i);
      const sc = host.querySelector(".focus-scroll");
      if (sc) { sc.scrollTop = 0; onFocusScroll(sc); }
    }
    document.querySelectorAll(".mtg-row").forEach((row, n) =>
      row.setAttribute("aria-current", String(n === i)));
  }

  // The bottom fade is a scroll affordance, so it must go once you reach the end.
  function onFocusScroll(el) {
    const more = el.scrollHeight - el.scrollTop - el.clientHeight > 8;
    el.classList.toggle("more", more);
  }

  function otherMeetingsBlock(recs, idx, cov) {
    const rows = recs.map((m, i) => `
      <div class="mtg-row" aria-current="${i === idx}" onclick="focusMeeting(${i})" title="Bring this meeting into focus">
        <div style="display:flex;justify-content:space-between;gap:14px;align-items:baseline;font-family:var(--fb);font-size:15px">
          <span style="white-space:nowrap">${esc(longDate(m.date))}</span>
          <span class="meta" style="flex:1">${m.vote_total} votes${m.present.length ? ", " + m.present.length + " present" : ""}</span>
          <span class="mtg-open">${i === idx ? "in focus" : "open"}</span>
        </div>
      </div>`).join("");
    return `
      <div class="lbl" style="border-bottom:1px solid var(--ink);padding-bottom:7px;margin-bottom:2px">
        Other meetings <span style="float:right;color:var(--muted)">${esc((recs[0].date || "").slice(0, 4))}</span>
      </div>
      <div style="margin-bottom:24px">${rows}
        <div class="meta" style="padding:11px 0">All ${cov.meetings} meetings ingested for this board.</div>
      </div>`;
  }

  function comingUpBlock(cov, place, watchLabel) {
    const nxt = (cov.next_meeting_list || [])[0];
    if (!nxt) return "";
    const when = new Date(nxt.date + "T12:00:00").toLocaleDateString("en-US",
      { weekday: "long", month: "long", day: "numeric" });
    return `
      <div class="lbl" style="border-bottom:1px solid var(--ink);padding-bottom:7px;margin-bottom:10px">What is coming up</div>
      <div style="font-family:var(--fb);font-size:15.5px;line-height:1.6;margin-bottom:6px">
        The next meeting is ${esc(when)}${nxt.time ? ` at ${esc(nxt.time)}` : ""}. Public comment is usually at the start and you do not have to sign up in advance.
      </div>
      <div class="meta" style="margin-bottom:12px">Read from the board's posted schedule by machine.</div>
      <div style="display:flex;gap:9px;flex-wrap:wrap;margin-bottom:24px">
        ${cov.source_url ? `<a class="sbtn" style="border-color:var(--accent);color:var(--accent);text-decoration:none" href="${esc(cov.source_url)}" target="_blank" rel="noopener">Their own records</a>` : ""}
        <button class="sbtn" type="button" onclick="toggleWatch({id:'place:${esc(place.slug || "x")}',title:'${esc(place.name || "This place")}',sub:'A place we are still checking'})">${watchLabel}</button>
      </div>`;
  }

  function districtsBlock(shortName, cov) {
    if (!PLACEHOLDERS) return "";
    const seats = (cov.board || []).filter(b => b.district).length;
    return `
      <div style="border:1px solid var(--rule);background:var(--card);margin-bottom:16px">
        <div class="lbl" style="padding:14px 16px 9px">The districts</div>
        ${placePlate([esc(shortName) + " magisterial districts",
                      "d3-geo and county subdivisions",
                      '<span style="text-transform:none;letter-spacing:0;font-style:italic;font-size:11px">shaded by turnout</span>'], 170)}
        <div style="font-family:var(--fb);font-size:13px;line-height:1.55;padding:10px 16px 15px;color:var(--muted);font-style:italic">${esc(LATIN.districts)}</div>
        <div class="meta" style="padding:0 16px 14px">Placeholder. We hold ${seats ? seats + " district names from the election results" : "no district geography"}, but no boundaries or turnout yet.</div>
      </div>`;
  }

  function boardBlock(cov) {
    const board = cov.board || [];
    if (!board.length) return "";
    const rows = board.map((b, i) => `
      <div class="sup-row" onclick="openSup(${i})" title="Open their record">
        <div>
          <div style="font-family:var(--fb);font-size:14.5px">${esc(b.name)}</div>
          <div class="meta">${esc(b.district || "District not matched")}</div>
        </div>
        <div style="text-align:right;white-space:nowrap">
          <div style="font-family:var(--fd);font-size:16px;font-weight:700">${b.with_majority !== null ? b.with_majority + "%" : "n/a"}</div>
          <div class="meta">${b.attendance !== null ? b.attendance + "% present" : ""}</div>
        </div>
      </div>`).join("");
    return `
      <div class="railq" style="margin-bottom:16px">
        <div class="lbl" style="border-bottom:1px solid var(--rule);padding-bottom:7px;margin-bottom:6px">
          The board <span style="float:right;color:var(--muted)">with maj.</span>
        </div>
        ${rows}
        <div class="meta" style="padding-top:8px">Click a name for their record. Voting with the majority is not a measure of anything on its own: a board that agrees can be right.</div>
      </div>`;
  }

  function figuresBlock(st) {
    if (!st || !st.meetings) return "";
    const cells = [[st.meetings, "meetings held"], [st.votes, "recorded votes"],
                   [st.unanimous_pct === null ? "n/a" : st.unanimous_pct + "%", "unanimous"],
                   [st.failed, "motions that failed"]];
    return `
      <div class="railq" style="margin-bottom:16px">
        <div class="lbl" style="border-bottom:1px solid var(--rule);padding-bottom:7px;margin-bottom:10px">${esc(st.year)} so far</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:14px">
          ${cells.map(([n, l]) => `<div><div style="font-family:var(--fd);font-size:26px;font-weight:700;line-height:1.1">${esc(n)}</div><div class="meta">${esc(l)}</div></div>`).join("")}
        </div>
      </div>`;
  }

  function capitalBlock(cov) {
    const cap = cov.capital;
    if (!cap) return "";
    return `
      <div class="lbl" style="border-bottom:1px solid var(--ink);padding-bottom:7px;margin-bottom:8px">
        What is being built <span style="float:right;color:var(--muted)">${cap.count} projects · ${esc(moneyOf(cap.total_usd))}</span>
      </div>
      <div class="meta" style="margin-bottom:8px">${esc(cap.edition)}${cap.source_url ? ` · <a href="${esc(cap.source_url)}" target="_blank" rel="noopener">the published capital plan</a>` : ""}</div>
      ${cap.projects.map(pr => `<div style="padding:9px 0;border-top:1px solid var(--rule);display:flex;gap:12px;justify-content:space-between;align-items:baseline;flex-wrap:wrap">
          <div style="flex:1;min-width:16em">
            <div style="font-family:var(--fb);font-size:14.5px;line-height:1.45">${esc(pr.title || "Untitled project")}</div>
            <div class="meta">${esc([pr.function, (pr.districts || []).join(", "), pr.work_type].filter(Boolean).join(" · "))}</div>
          </div>
          <div style="font-family:var(--fm);font-size:12px;white-space:nowrap">${esc(moneyOf(pr.total_usd))}</div>
        </div>`).join("")}
      <div class="meta" style="padding:9px 0 24px">Largest ${cap.projects.length} of ${cap.count}. Figures as published; no second source for capital plans, so none of this is confirmed.</div>`;
  }

  // The member's floating window. Everything we actually know is real and
  // counted; everything we do not is Latin, inside this window, labelled.
  function openSup(i) {
    const cov = (state.uncharted || {}).coverage || {};
    const b = (cov.board || [])[i];
    if (!b) return;
    const host = document.getElementById("sup-modal");
    if (!host) return;
    const cell = (label, value, big) =>
      `<div><div class="meta">${esc(label)}</div><div style="font-family:${big ? "var(--fd);font-size:19px;font-weight:700" : "var(--fb);font-size:15px"}">${value}</div></div>`;
    const latin = v => `<span style="color:var(--muted);font-style:italic">${esc(v)}</span>`;
    host.innerHTML = `
      <div class="sup-shade" onclick="closeSup()">
        <div class="sup-card" onclick="event.stopPropagation()">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:16px;padding:20px 24px 14px;border-bottom:1px solid var(--ink)">
            <div>
              <div class="kick" style="margin-bottom:6px">${esc(b.district || "District not matched")} · ${esc(b.office || "Board member")}</div>
              <div style="font-family:var(--fd);font-size:32px;font-weight:700;line-height:1.05">${esc(b.name)}</div>
            </div>
            <button class="sbtn" type="button" onclick="closeSup()">Close</button>
          </div>
          <div class="sup-grid" style="display:grid;grid-template-columns:repeat(5,1fr);gap:12px;padding:14px 24px;border-bottom:1px solid var(--rule)">
            ${cell("Elected", PLACEHOLDERS ? latin(LATIN.elected) : "n/a")}
            ${cell("Term ends", PLACEHOLDERS ? latin(LATIN.termEnds) : "n/a")}
            ${cell("Next on the ballot", PLACEHOLDERS ? latin(LATIN.ballot) : "n/a")}
            ${cell("With majority", b.with_majority !== null ? b.with_majority + "%" : "n/a", true)}
            ${cell("Attendance", b.attendance !== null ? b.attendance + "%" : "n/a", true)}
          </div>
          <div style="padding:16px 24px 20px">
            <p style="font-family:var(--fb);font-size:15.5px;line-height:1.7;margin:0 0 6px;color:var(--muted);font-style:italic">${esc(LATIN.bio)}</p>
            <div class="meta" style="margin-bottom:16px">Placeholder. We hold no biography for this member.</div>
            <div class="lbl" style="border-bottom:1px solid var(--rule);padding-bottom:6px;margin-bottom:6px">How they got the seat</div>
            <div style="display:flex;justify-content:space-between;gap:12px;padding:7px 0;border-bottom:1px solid var(--rule)">
              <span style="font-family:var(--fb);font-size:14px">${latin(LATIN.margin)}</span><span class="meta">${esc(LATIN.turnout)} turnout</span>
            </div>
            <div style="display:flex;justify-content:space-between;gap:12px;padding:7px 0;margin-bottom:6px">
              <span style="font-family:var(--fb);font-size:14px">Succeeded ${latin(LATIN.predecessor)}</span><span class="meta">Placeholder</span>
            </div>
            <div class="meta" style="margin-bottom:16px">Margins, turnout and predecessors are not in our data yet.</div>
            <div class="lbl" style="border-bottom:1px solid var(--rule);padding-bottom:6px;margin-bottom:6px">What we did count</div>
            <div style="font-family:var(--fb);font-size:14.5px;line-height:1.6">
              ${b.votes_cast} recorded votes cast${b.with_majority !== null ? `, ${b.with_majority}% of them with the majority` : ""}. Counted from this board's own record, not from any outside tally.
            </div>
          </div>
        </div>
      </div>`;
    document.addEventListener("keydown", supEsc);
  }
  function closeSup() {
    const host = document.getElementById("sup-modal");
    if (host) host.innerHTML = "";
    document.removeEventListener("keydown", supEsc);
  }
  function supEsc(e) { if (e.key === "Escape") closeSup(); }

  // The genuinely empty case: we hold nothing for this place. Kept separate
  // so the sheet above never has to pretend.
  function unchartedEmptyView(u, place, cov, shortName) {
    const watchLabel = isWatched("place:" + (place.slug || "x"))
      ? "We will email you" : "Email me when we finish this";
    const src = cov.source_url
      ? `<a class="sbtn" style="border-color:var(--accent);color:var(--accent);display:inline-block;text-decoration:none" href="${esc(cov.source_url)}" target="_blank" rel="noopener">Their own records</a>` : "";
    return `<div class="view wrap">
      ${chromeBar({ search: true })}
      <div class="kick" style="margin-bottom:9px">${esc(place.name || "This place")} · ${esc(place.body || "Local government")}</div>
      <div class="h1" style="max-width:22em;margin-bottom:12px">${esc(shortName)} meets in public. We cannot yet tell you what it decides.</div>
      <div class="dropcap" style="font-family:var(--fb);font-size:16px;line-height:1.55;max-width:38em;margin-bottom:26px">They publish their records. We have not built a reliable way to read them, and we will not print numbers we cannot vouch for. Here is exactly where we are.</div>
      <div class="cols">
        <div>
          <div class="lbl" style="border-bottom:1px solid var(--ink);padding-bottom:7px;margin-bottom:16px">Where this place stands with us</div>
          <div style="margin-bottom:24px">
            ${step(!!cov.found, "We found where the records live", cov.found ? ("Ingested " + (cov.meetings || 0) + " meetings from the published source.") : "We do not have a store for this place yet.")}
            ${step(false, "We can read them", "Nothing has come out in a usable shape yet.")}
            ${step(false, "We can prove they are right", cov.public_note || cov.note || "A second record, written by someone else, is required before we vouch for a vote.")}
          </div>
          <div style="border:1px solid var(--ink);padding:16px 18px 18px;background:var(--card)">
            <div style="font-family:var(--fd);font-size:22px;font-weight:700;margin-bottom:7px">What you can do today, without us</div>
            <p style="font-family:var(--fb);font-size:15px;line-height:1.65;margin:0 0 12px">Go to the source. Public comment is usually at the start of the meeting.</p>
            <div style="display:flex;gap:9px;flex-wrap:wrap">${src}
              <button class="sbtn" type="button" onclick="toggleWatch({id:'place:${esc(place.slug || "x")}',title:'${esc(place.name || "This place")}',sub:'A place we have not charted'})">${watchLabel}</button>
            </div>
          </div>
        </div>
        <div>
          <div class="railq">
            <div class="lbl" style="border-bottom:1px solid var(--rule);padding-bottom:7px;margin-bottom:12px">What we do know</div>
            <div style="padding:8px 0;border-bottom:1px solid var(--rule)"><div style="font-family:var(--fb);font-size:14px">Federal and state layers, in full</div><div class="meta">Congress and the statehouse are charted everywhere.</div></div>
            <div style="padding:8px 0"><div style="font-family:var(--fb);font-size:14px">Local coverage is incomplete</div><div class="meta">We would rather show you an empty page than a confident wrong one.</div></div>
          </div>
        </div>
      </div>
      <div class="folio"><b>Sheet four</b> · the honest half</div>
    </div>`;
  }
  function step(done, title, sub) {
    const bg = done ? "var(--accent)" : "var(--paper)";
    const bd = done ? "var(--accent)" : "var(--rule)";
    return `<div style="display:grid;grid-template-columns:1.4rem 1fr;gap:12px;padding:12px 0;border-bottom:1px solid var(--rule);align-items:start">
      <span style="width:10px;height:10px;background:${bg};border:1.5px solid ${bd};margin-top:6px"></span>
      <div><div style="font-family:var(--fd);font-size:19px;font-weight:700">${esc(title)}</div><div style="font-family:var(--fb);font-size:14px;line-height:1.55;color:var(--muted)">${esc(sub)}</div></div>
    </div>`;
  }

  function watchingView() {
    const watching = state.watching.map(w => {
      const sub = (w.sub || "").length > 70 ? (w.sub || "").split(" · ")[0] : (w.sub || "");
      return `<div style="display:flex;justify-content:space-between;gap:12px;padding:12px 0;border-top:1px solid var(--rule)">
        <div><div style="font-family:var(--fb);font-size:14px;line-height:1.4">${esc(w.title)}</div><div class="meta">${esc(sub)}</div></div>
        <button class="act" type="button" onclick='toggleWatch(${JSON.stringify(w).replace(/'/g, "&#39;")})' style="white-space:nowrap">Stop</button>
      </div>`;
    }).join("");
    const empty = state.watching.length === 0
      ? `<div class="body" style="color:var(--muted);border-top:1px solid var(--rule);padding-top:12px">Nothing yet. Press Watch on a bill or a topic and it shows up here.</div>`
      : "";
    const placeErr = state.placeError ? `<div class="meta" style="color:var(--accent);margin-top:8px">${esc(state.placeError)}</div>` : "";
    const placeNow = state.stateCode
      ? `<div style="font-family:var(--fd);font-size:22px;font-weight:700;line-height:1.15">${esc(placeLabel())}</div>
         ${state.rep ? `<div class="meta">Represented by ${esc(state.rep)}</div>` : ""}
         <div class="meta" style="margin-top:4px">${esc(whyPlace())}</div>`
      : `<div style="font-family:var(--fd);font-size:22px;font-weight:700;line-height:1.15">Not set</div>
         <div class="meta" style="margin-top:4px">National results until you pick a place.</div>`;
    const busy = state.placeBusy;
    return `<div class="view wrap" style="max-width:640px">
      <div class="chrome">
        <button class="wordmark" type="button" onclick="goHome()" style="font-size:22px;font-weight:700">Nos<em>Populi</em></button>
        <button class="act" type="button" onclick="goBack()">Back</button>
      </div>

      <div class="lbl" style="border-bottom:1px solid var(--ink);padding-bottom:7px;margin-bottom:14px">Where you are</div>
      <div class="railq">
        ${placeNow}
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:14px">
          <button class="sbtn" type="button" onclick="useMyLocation()" ${busy ? "disabled" : ""}>${busy ? "Finding you…" : "Use my location"}</button>
          ${state.stateCode ? `<button class="sbtn" type="button" onclick="clearPlace()">Clear</button>` : ""}
        </div>
        <div class="ask-row" style="border-width:1px;margin-top:12px">
          <input id="place-box" type="text" placeholder="Zip, town and state, or a state" style="padding:11px 12px;font-size:14px" ${busy ? "disabled" : ""}>
          <button type="submit" style="border:0;background:var(--ink);color:var(--paper);font-family:var(--fm);font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;padding:0 14px;cursor:pointer">Set</button>
        </div>
        ${placeErr}
        <div class="meta" style="margin-top:9px">Kept in this browser. A town or address finds your exact district; a zip or state sets the state.</div>
      </div>

      <div class="lbl" style="border-bottom:1px solid var(--ink);padding-bottom:7px;margin:28px 0 4px">Watching · ${state.watching.length}</div>
      ${watching}
      ${empty}

      <div class="lbl" style="border-bottom:1px solid var(--ink);padding-bottom:7px;margin:28px 0 14px">Email</div>
      <div class="ask-row" style="border-width:1px;margin-bottom:10px">
        <input id="watch-email" type="email" placeholder="you@example.com" value="${esc(state.email)}" style="padding:11px 12px;font-size:14px">
        <button type="button" onclick="saveEmail()" style="border:0;background:var(--ink);color:var(--paper);font-family:var(--fm);font-size:9.5px;letter-spacing:.14em;text-transform:uppercase;padding:0 14px;cursor:pointer">Save</button>
      </div>
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0"><span style="font-family:var(--fb);font-size:13.5px">Email me when something I watch moves</span><button class="tog ${state.notifyMoves ? "on" : ""}" type="button" onclick="state.notifyMoves=!state.notifyMoves;saveStore();render()"><span class="knob"></span></button></div>
      <div style="display:flex;justify-content:space-between;align-items:center;padding:8px 0"><span style="font-family:var(--fb);font-size:13.5px">Weekly summary, even if nothing moved</span><button class="tog ${state.notifyWeekly ? "on" : ""}" type="button" onclick="state.notifyWeekly=!state.notifyWeekly;saveStore();render()"><span class="knob"></span></button></div>
      <div class="meta" style="margin-top:12px">${state.email ? "Saved. " : "No email saved, so nothing is sent. "}Email is the only thing we ask for.</div>

      <div class="folio"><b>Sheet five</b> · place, watching, email</div>
    </div>`;
  }

  // The 156 seat rects never change, so build them once and re-attach the
  // group on later renders. Rebuilding them was a visible teardown.
  let markSeats = null;

  function drawHomeMark() {
    const host = document.getElementById("home-mark");
    if (!host) return;
    if (markSeats) { host.replaceChildren(markSeats); return; }
    const seats = document.createElementNS(NS, "g");
    const cx = 400, cy = 272;
    const rings = [{ r: 252, n: 51, s: 11 }, { r: 220, n: 43, s: 10 }, { r: 188, n: 35, s: 9 }, { r: 156, n: 27, s: 8 }];
    let k = 0;
    for (const ring of rings) {
      for (let i = 0; i < ring.n; i++) {
        const a = Math.PI * (1 - i / (ring.n - 1));
        const x = cx + ring.r * Math.cos(a);
        const y = cy - ring.r * Math.sin(a);
        const deg = 90 - (a * 180 / Math.PI);
        const g = document.createElementNS(NS, "g");
        g.setAttribute("transform", `rotate(${deg} ${x} ${y})`);
        const rect = document.createElementNS(NS, "rect");
        rect.setAttribute("x", String(x - ring.s / 2));
        rect.setAttribute("y", String(y - ring.s / 2));
        rect.setAttribute("width", String(ring.s));
        rect.setAttribute("height", String(ring.s * 0.72));
        rect.setAttribute("fill", i % 8 === 0 ? "var(--accent)" : i % 3 === 0 ? "var(--ink)" : "var(--aged)");
        rect.style.animationDelay = (0.12 + k * 0.008) + "s";
        g.appendChild(rect);
        seats.appendChild(g);
        k += 1;
      }
    }
    markSeats = seats;
    host.replaceChildren(seats);
  }

  // The map is expensive to build and was being torn down and rebuilt on
  // every ledger render, which meant it blinked several times per answer.
  // Now the built SVG is cached and simply re-attached: moving a node keeps
  // its click handlers, so district selection survives a re-render too.
  let featsPromise = null;
  let mapSvg = null;
  let mapSvgKey = null;

  function ensureFeats() {
    if (cdFeats) return null;
    if (!featsPromise) {
      featsPromise = fetch("/static/geo/cd119-10m.json")
        .then(r => r.json())
        .then(topo => {
          cdFeats = topojson.feature(topo, Object.values(topo.objects)[0]).features;
        })
        .catch(() => { /* placeholder stays */ })
        .finally(() => { featsPromise = null; mountLedgerMap(); });
    }
    return featsPromise;
  }

  function mountLedgerMap() {
    const host = document.getElementById("ledger-map");
    if (!host || typeof d3 === "undefined" || typeof topojson === "undefined") return;
    const asked = state.view === "elections" ? (state.electionsPage || {}).state_code : (state.ledger && state.ledger.state_code);
    const code = asked || state.stateCode;
    const fips = FIPS[code];
    if (!fips) {
      // Only write the placeholder if it is not already what is there.
      if (!host.querySelector(".plct")) {
        host.innerHTML = `<span class="plct">Set a place to see its districts</span>`;
      }
      mapSvg = mapSvgKey = null;
      return;
    }
    const key = [state.view, code, state.geoid || ""].join("|");
    if (mapSvgKey === key && mapSvg) {
      if (mapSvg.parentNode !== host) host.replaceChildren(mapSvg);
      host.style.background = "var(--card)";
      return;
    }
    // Leave whatever is on screen alone while the geography loads.
    if (!cdFeats) { ensureFeats(); return; }
    const feats = cdFeats.filter(f => String(f.id).slice(0, 2) === fips);
    if (!feats.length) return;
    const path = d3.geoPath(d3.geoAlbersUsa().fitSize([320, 170], { type: "FeatureCollection", features: feats }));
    // Build detached, then swap in one go, so no frame shows an empty box.
    const svg = document.createElementNS(NS, "svg");
    svg.setAttribute("viewBox", "0 0 320 170");
    svg.setAttribute("class", "geo-svg");
    svg.style.width = "100%";
    svg.style.height = "170px";
    svg.style.display = "block";
    for (const f of feats) {
      const d = path(f);
      if (!d) continue;
      const pth = document.createElementNS(NS, "path");
      pth.setAttribute("d", d);
      const id = String(f.id);
      const picked = state.pickedGeoid === id;
      pth.setAttribute("fill", picked ? "var(--ink)" : (state.geoid && id === state.geoid ? "var(--accent)" : "var(--aged)"));
      pth.setAttribute("stroke", "var(--ink)");
      pth.setAttribute("stroke-width", picked ? "1.2" : "0.6");
      pth.setAttribute("data-geoid", id);
      pth.style.cursor = "pointer";
      pth.addEventListener("click", (ev) => { ev.stopPropagation(); openDistrict(id); });
      svg.appendChild(pth);
    }
    mapSvg = svg;
    mapSvgKey = key;
    host.style.background = "var(--card)";
    host.replaceChildren(svg);
  }

  // Clicking a district opens a floating card over the map: who represents
  // it, then what the bills on this page would mean there. The rep is a local
  // lookup and paints at once; the paragraph is one Haiku call that fills in.
  async function openDistrict(geoid) {
    state.pickedGeoid = geoid;
    const svg = document.querySelector("#ledger-map svg");
    if (svg) for (const p of svg.querySelectorAll("path")) {
      const on = p.getAttribute("data-geoid") === geoid;
      p.setAttribute("fill", on ? "var(--ink)" : (state.geoid && p.getAttribute("data-geoid") === state.geoid ? "var(--accent)" : "var(--aged)"));
      p.setAttribute("stroke-width", on ? "1.2" : "0.6");
    }
    const question = state.view === "ledger" ? ((state.ledger || {}).question || state.query || "") : "";
    const stories = state.view === "ledger" ? ((state.ledger || {}).stories || []).slice(0, 10).map(s => ({
      id: s.id, title: s.title, english_title: s.english_title, stage: s.stage,
      sponsor: s.sponsor, sponsor_bioguide: s.sponsor_bioguide, policy_area: s.policy_area,
    })) : [];
    state.districtPop = { geoid, question, data: null, impact: null, pending: true };
    paintDistrictPop();
    try {
      // Fast local lookup first so the card is never blank.
      const r1 = await fetch("/resolve-district", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ geoid }) });
      if (r1.ok && state.districtPop && state.districtPop.geoid === geoid) { state.districtPop.data = await r1.json(); paintDistrictPop(); }
      if (!question || !stories.length) { if (state.districtPop) state.districtPop.pending = false; paintDistrictPop(); return; }
      const r2 = await fetch("/ledger/district", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ geoid, question, stories }) });
      if (r2.ok && state.districtPop && state.districtPop.geoid === geoid) {
        const d = await r2.json();
        state.districtPop.data = state.districtPop.data || d;
        state.districtPop.impact = d.impact || null;
      }
    } catch (e) { /* fail-open: the card keeps what it has */ }
    if (state.districtPop && state.districtPop.geoid === geoid) { state.districtPop.pending = false; paintDistrictPop(); }
  }

  function closeDistrict() {
    state.districtPop = null;
    state.pickedGeoid = null;
    const el = document.getElementById("dpop");
    if (el) el.remove();
    const svg = document.querySelector("#ledger-map svg");
    if (svg) for (const p of svg.querySelectorAll("path")) {
      p.setAttribute("fill", state.geoid && p.getAttribute("data-geoid") === state.geoid ? "var(--accent)" : "var(--aged)");
      p.setAttribute("stroke-width", "0.6");
    }
  }

  function districtPopHtml() {
    const P = state.districtPop || {};
    const d = P.data || {};
    const rep = d.representative || null;
    const label = d.district_label || "";
    const stateName = NAME_BY_STATE[d.state] || d.state || "";
    const impact = P.impact;
    const q = P.question;
    const photo = rep && rep.bioguide_id ? `/member/photo/${encodeURIComponent(rep.bioguide_id)}` : "";
    const party = rep ? PARTY_COLOR[(rep.party || "").slice(0, 1).toUpperCase()] || "var(--rule)" : "var(--rule)";
    const who = rep
      ? `<div class="dpop-rep" ${rep.bioguide_id ? `onclick="openMember('${esc(rep.bioguide_id)}')" style="cursor:pointer"` : ""}>
           ${photo ? `<img src="${esc(photo)}" alt="" onerror="this.style.display='none'">` : ""}
           <div>
             <div style="font-family:var(--fd);font-size:18px;font-weight:700;line-height:1.15">${esc(rep.name)}</div>
             <div class="meta" style="margin:2px 0 0;display:flex;align-items:center;gap:6px"><span class="pmark" style="background:${party}"></span>${esc([rep.party, "U.S. House", rep.term_end ? "term ends " + rep.term_end : ""].filter(Boolean).join(" · "))}</div>
           </div>
         </div>`
      : (P.pending && !P.data ? `<div class="meta">Looking up who represents it…</div>` : `<div class="meta">No current representative on file.</div>`);
    const known = impact && impact.known_for ? `<div class="dpop-known">Roughly: ${esc(impact.known_for)}</div>` : "";
    const here = label ? `in ${esc(label)}` : "here";
    let body = "";
    if (q) {
      if (impact) {
        body = `<div class="lbl" style="margin:12px 0 6px">What this means ${here}</div>
          <p class="read" style="font-size:14.5px;line-height:1.55;margin:0 0 8px">${esc(impact.impact)}</p>
          ${impact.rep_line ? `<p class="read" style="font-size:13.5px;line-height:1.5;margin:0;color:var(--muted)">${esc(impact.rep_line)}</p>` : ""}`;
      } else if (P.pending) {
        body = `<div class="lbl" style="margin:12px 0 6px">What this means ${here}</div><div class="loading-rule" aria-hidden="true" style="max-width:200px"><span></span></div><div class="meta" style="margin-top:6px">Writing it for this district…</div>`;
      } else {
        body = `<div class="meta" style="margin-top:12px">We could not write a district note this time. The representative's page is the next best place to look.</div>`;
      }
    }
    const links = rep && rep.bioguide_id
      ? `<button class="abtn" type="button" onclick="openMember('${esc(rep.bioguide_id)}')" style="margin-top:12px">See ${esc((rep.name || "").split(" ").slice(-1)[0])}'s record on this →</button>`
      : "";
    const mine = state.geoid === P.geoid
      ? `<div class="meta" style="margin-top:8px">This is your district.</div>`
      : (d.state ? `<button class="act" type="button" onclick="adoptDistrict()" style="margin-top:8px">Make this my place</button>` : "");
    return `<div class="dpop-head">
        <div class="kick">${esc(label)}${stateName ? " · " + esc(stateName) : ""}</div>
        <button class="dpop-x" type="button" onclick="closeDistrict()" aria-label="Close">×</button>
      </div>
      ${who}
      ${known}
      ${body}
      ${links}
      ${mine}`;
  }

  function paintDistrictPop() {
    if (!state.districtPop) return;
    const host = document.getElementById("ledger-map");
    if (!host) return;
    const box = host.parentElement;
    let el = document.getElementById("dpop");
    if (!el) {
      el = document.createElement("div");
      el.id = "dpop";
      el.className = "dpop";
      el.addEventListener("click", ev => ev.stopPropagation());
      box.style.position = "relative";
      box.appendChild(el);
    }
    el.innerHTML = districtPopHtml();
  }

  // "Make this my place": adopt the clicked district as the user's location.
  function adoptDistrict() {
    const P = state.districtPop;
    if (!P || !P.data) return;
    applyResolved(Object.assign({}, P.data, { place_name: null }), "map");
    state.placeConfirmed = true;
    saveStore();
    closeDistrict();
    render();
  }

  // One quiet page while an answer is assembled: the question, a moving rule,
  // nothing else. Replaces the half-empty ledger that used to flash first.
  function loadingView(what, sub) {
    return `<div class="view wrap">
      ${chromeBar({ search: true })}
      <div class="loading-page">
        <div class="kick" style="margin-bottom:10px">${esc(sub || "Looking that up")}</div>
        <div class="h1" style="max-width:20em;margin-bottom:22px">${esc(what || state.query || "")}</div>
        <div class="loading-rule" aria-hidden="true"><span></span></div>
        <div class="meta" style="margin-top:14px">Reading Congress. A few seconds.</div>
      </div>
    </div>`;
  }

  // A paint is "fresh" when the reader has genuinely arrived somewhere new,
  // not when a streamed section has filled part of the page they are already
  // looking at. Only a fresh paint replays the entrance animation. Excluded
  // on purpose: money, perf, elections, stage and districtPop, because those
  // are updates and they are exactly what used to make the page flash.
  let lastKey = null;

  function renderKey() {
    const v = state.view;
    if (v === "ledger") {
      const l = state.ledger || {};
      return `ledger:${l.pending ? "pending" : "ready"}:${l.question || state.query || ""}`;
    }
    if (v === "bill") {
      const b = state.bill || {};
      return `bill:${b.congress}/${b.type}/${b.number}:${b.pending ? "pending" : "ready"}`;
    }
    if (v === "member") {
      const m = state.member || {};
      return `member:${m.bioguide_id || m.name || ""}:${m.pending ? "pending" : "ready"}`;
    }
    if (v === "uncharted") return `uncharted:${((state.uncharted || {}).place || {}).name || ""}`;
    if (v === "elections") return `elections:${(state.electionsPage || {}).state_code || ""}`;
    if (v === "offtopic") return `offtopic:${state.query || ""}`;
    if (v === "graph") return `graph:${(state.graph || {}).question || state.query || ""}`;
    return v;
  }

  // Several stream sections can land in one network chunk, and each used to
  // force its own synchronous repaint. Coalescing on a frame turns that burst
  // into one write.
  let renderQueued = false;
  const _raf = typeof requestAnimationFrame === "function"
    ? requestAnimationFrame : (fn) => setTimeout(fn, 16);
  function scheduleRender() {
    if (renderQueued) return;
    renderQueued = true;
    _raf(() => { renderQueued = false; render(); });
  }

  function render() {
    const views = { home: homeView, ledger: ledgerView, bill: billView, uncharted: unchartedView, watching: watchingView, member: memberView, offtopic: offTopicView, elections: electionsView, graph: graphView };
    const key = renderKey();
    const fresh = key !== lastKey;
    lastKey = key;
    if (state.view !== "home") document.body.classList.remove("is-entering");
    let html;
    if (state.view === "ledger" && state.ledger && state.ledger.pending) html = loadingView(state.ledger.question, "You asked about");
    else if (state.view === "member" && state.member && state.member.pending) html = loadingView(state.member.name || "A member of Congress", "Opening");
    else if (state.view === "bill" && state.bill && state.bill.pending) html = loadingView(compactTitle((state.bill.meta || {}).title) || ((state.bill.type || "").toUpperCase() + " " + state.bill.number), "Reading the bill");
    else html = (views[state.view] || homeView)();
    const app = document.getElementById("app");
    app.innerHTML = html;
    const root = app.firstElementChild;
    if (fresh && root && root.classList && root.classList.contains("view")) {
      root.classList.add("enter");
    }
    if (state.view === "home") {
      drawHomeMark();
      // Replay the seat animation only on arrival. Re-running it on every
      // home render is what made the mark blink.
      if (fresh) {
        document.body.classList.remove("is-entering");
        void document.body.offsetWidth;
        document.body.classList.add("is-entering");
      }
      const q = document.getElementById("q");
      if (q) q.focus();
    }
    if (state.view === "uncharted") {
      // measure once painted so the bottom fade only shows when there is
      // genuinely more list below the fold
      setTimeout(() => {
        const sc = document.querySelector(".focus-scroll");
        if (sc) onFocusScroll(sc);
      }, 0);
    }
    if (state.view === "ledger" || state.view === "elections") {
      // Synchronous, in the same task as the innerHTML write, so the browser
      // never paints a frame with an empty map panel.
      mountLedgerMap();
      if (state.districtPop) paintDistrictPop();
    } else if (state.districtPop) {
      state.districtPop = null;
      state.pickedGeoid = null;
    }
  }

  async function bootFromUrl() {
    const path = (location.pathname || "/").replace(/\/+$/, "") || "/";
    const segs = path.split("/").filter(Boolean);
    if (segs[0] === "test") segs.shift();
    const params = new URLSearchParams(location.search);
    if (params.get("view") === "watching") { goWatching({ fromHistory: true }); return; }
    if (segs[0] === "bill" && segs.length === 4) {
      await openBill(segs[1], segs[2], segs[3], "", { fromHistory: true });
      return;
    }
    if (segs[0] === "member" && segs.length === 2) {
      await openMember(segs[1], { fromHistory: true });
      return;
    }
    const congress = params.get("congress");
    const type = params.get("type");
    const number = params.get("number");
    if (congress && type && number) {
      await openBill(congress, type, number, "", { fromHistory: true });
      return;
    }
    const q = params.get("q");
    if (q) await ask(q, { fromHistory: true });
    else render();
  }

  window.ask = ask;
  window.askSubmit = askSubmit;
  window.placeSubmit = placeSubmit;
  window.goHome = goHome;
  window.goBack = goBack;
  window.goWatching = goWatching;
  window.confirmPlace = confirmPlace;
  window.setPlace = setPlace;
  window.useMyLocation = useMyLocation;
  window.clearPlace = clearPlace;
  window.setStage = setStage;
  window.openBill = openBill;
  window.openMember = openMember;
  window.togglePerf = togglePerf;
  window.goElections = goElections;
  window.foldToggle = foldToggle;
  window.loadFullText = loadFullText;
  window.openDistrict = openDistrict;
  window.closeDistrict = closeDistrict;
  window.adoptDistrict = adoptDistrict;
  // A click anywhere off the card or the map puts the card away.
  document.addEventListener("click", (ev) => {
    if (!state.districtPop) return;
    const t = ev.target;
    if (t && t.closest && (t.closest("#dpop") || t.closest("#ledger-map"))) return;
    closeDistrict();
  });
  document.addEventListener("keydown", (ev) => { if (ev.key === "Escape" && state.districtPop) closeDistrict(); });
  window.toggleWatch = toggleWatch;
  window.focusMeeting = focusMeeting;
  window.onFocusScroll = onFocusScroll;
  window.openSup = openSup;
  window.closeSup = closeSup;
  window.saveEmail = saveEmail;
  window.state = state;
  window.render = render;
  window.saveStore = saveStore;

  loadStore();
  render();
  window.addEventListener("popstate", () => { bootFromUrl(); });
  guessPlace().then(bootFromUrl);
})();
