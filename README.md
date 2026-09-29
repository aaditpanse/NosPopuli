# NosPopuli — Law for the People

> *Nos populus — Latin: "we the people"*

Live at **[nospopuli.org](https://nospopuli.org)**

I'm building a way to make American government legible to ordinary people. Not a
summarizer, not a news feed — a map of who governs you, what they decided, and what
it cost, that you can actually walk.

---

## Where this actually is

I want to be precise about the current state, because I've been burned by my own
optimistic documentation.

**What works well.** Federal legislation is fully wired: type a bill ID, a topic, a
named act, or a member's name and you get a real answer. Bill detail streams in
section-by-section — plain-English translation, timeline, roll-call vote maps, sponsors,
lobbying, sponsor money. Elections work. And Foundry — the self-building local-government pipeline — is the part
I'm proudest of: nine municipal sources ingested, certified against independent second
sources, with the uncertified records visibly quarantined rather than quietly published.

**Where it runs (since 2026-09-26).** One Hetzner server behind Cloudflare holds the
app, Postgres 17 (with pgvector and PostGIS) and the federal bulk data under
`/srv/bulk`. A sync at 11:30 UTC each day downloads what changed, reloads the graph and
commits the small tracked files back to `main`; a timer deploys `main` every five
minutes. The bill page, member pages, campaign money, member photos and the feed read
those files and the graph, not Congress.gov: a bill page's data is ready in 160–320 ms.
Search answers from this server too (since 2026-09-29): federal and state bills from the
same hybrid index, with no GovInfo or Congress.gov search call.
Railway and Supabase are paused, and are deleted after 2026-10-03.

**The state layer is all 50 legislatures, from Open States.** Every session since 2017
(1.2 million bills): bills, sponsors, actions, roll calls and the text versions each
legislature publishes. Virginia alone has an independent check, its own daily files
(Virginia LIS); every other state's terms and votes are `ingested`, and each state's
certification gap line says why. `/ledger` sends a state question there, and a state
bill or legislator has its own page. A state is one entry under `"legislatures"` in the
sidecar. LegiScan is gone: it never issued a key.

Money, lobbying and stock trades are the same story: implemented, live endpoints, and
unreachable by typing a question. Member finance and stocks at least open if you click
into a member; the lobbying directory and the stock explorer exist only on
`/newspaper`. Same for the geo resolvers, which are driven by map clicks and the place
picker but never by text.

Letter-writing is built and shipped *disabled*, because I don't have reliable email
addresses for representatives. And local search throws away your topic: "zoning in
Fairfax" and "Fairfax" return the identical page, because there's no index over the
local corpus yet.

**What's messy.** There is now one frontend: `frontend/test.html` + `js/ledger.js`,
served at `/`. Despite the filename, that is production.

The old tabbed app at `/newspaper` is deleted. It was the only route to state
legislation search, the lobbying directory, the stock explorer, public-law detail, the
personalized feed and structured feedback — so **those are offline until I rebuild
them.** Their endpoints all still work; only the views are gone. The full list, with
endpoints and the order I'll do them in, is under "Rebuilding the newspaper
capabilities" below. I chose to rebuild rather than port because I didn't want to carry
that UI forward.

The ask SPA has eight views: home, ledger, bill, uncharted, watching, member, offtopic,
elections.

---

## Where it's going

### What the map is for

A search engine can find you a document. It cannot tell you how the person you elected
voted, because that answer doesn't live in a document — it lives in a **join** between
a county vote record, a board roster, an election result and your address. Nobody has
made that join, so nobody can answer it, so people stop asking.

That is the end goal: **questions whose answers require crossing layers of government
become a walk across the map, and every step can show its source.**

Two things follow, and they're worth separating because they come from different places:

- **Specificity comes from the joins, not from volume.** More rows in more silos buys
  nothing. "Which supervisors approved the rezoning and who funded them" needs
  vote → person → seat → contest → money to be one connected path. That is what mapping
  buys, and it's the only thing that buys it.
- **Accuracy comes from certification, not from mapping.** A complete map can be
  confidently wrong — this app currently answers "LA County" as off-topic with 0.95
  confidence. So every edge carries whether an independent source affirms it, and every
  answer reports the weakest hop it crossed.

And note what is *not* the goal: anticipating the questions. I can't enumerate what
people will ask, and trying to is the mistake that produced the classifier. The
entity types and edge types are finite — a body, a seat, a person, an instrument, a
vote, a contest, about a dozen relations — while their **combinations** are unbounded.
Model the domain, and the question space takes care of itself.

The reason this is worth the effort is in the paragraph below: the local layer is the
one that touches people most and the one nobody has charted. A map of it does something
a search box can't — search answers the question you asked, a map shows you the
question you didn't know to ask, which is usually that the rezoning passed 7–3 and two
of the ayes were seated by 900 votes.

The original framing was "make an API wrapper, then escape the wrapper by generating my
own pipelines." Foundry did that. But building it taught me the wrapper was never the
real limit — **classification** was.

Every question used to get sorted into one bucket: legislation, or member, or place.
That's a projection, and it throws away the half of the question that made it
interesting. "Which supervisors approved the rezoning and who funded them" has no
single correct bucket. Adding more buckets doesn't fix it; it just makes the projection
finer.

So the direction now is to **model government as a temporal property graph with
provenance**, and answer questions by traversing it instead of classifying them.

Government is self-similar. The Fairfax Board of Supervisors is to Fairfax County what
Congress is to the United States: a body, in a jurisdiction, with seats, held by people,
who vote on instruments, at meetings, put there by elections. That repeats at every
layer, which means one ontology covers all three. Foundry's extraction schema converged
on that shape on its own, which is decent evidence the shape is real and not something
I'm imposing.

Three things make it tractable:

- **The skeleton is small.** Every jurisdiction, body, seat and officeholder in the
  United States is on the order of one to two million nodes. That's two Postgres tables,
  not a graph database.
- **Events stay where they are.** Individual votes and contributions run to hundreds of
  millions. Those don't go in the graph — the graph holds topology and identity, and
  dereferences facts at the leaves. A new source adds edges to an existing skeleton
  rather than inflating it.
- **Time is a column, not an afterthought.** Almost every edge is bounded. "Who voted
  for this" has to resolve seat-holders *as of the vote*. A snapshot model silently goes
  wrong, and I'd rather pay for that up front.

**The honest hard part is identity, and I've measured it.** Matching Fairfax supervisors
to the contests that elected them: exact string comparison gets 3 of 12. A ten-line
normalizer that strips nicknames and suffixes gets 9 of 12. The remaining three are one
genuine coverage gap, one duplicate of the same person inside one source, and one
extraction bug — a sentence fragment currently sitting in the board roster as if it were
a person. That's the work: mostly mechanical, with a residue that deserves a human.

I'm scoping v1 to Virginia, top to bottom — federal, the General Assembly, and the four
counties I already have — because all of that data is already on disk and it proves
cross-layer traversal for real people in one place before I scale sideways.

**Certification extends to edges.** Foundry certifies records today. A graph's claims
are edges, and a wrong edge is worse than a wrong record because everything traversing
it inherits the error. An edge nobody independently affirms is still traversable, but
every answer that crosses one says so and says which hop was weak.

### The whole map

Legislation is where this starts, because it was the easiest record to reach. It is not
the product. The product is **every record American power leaves behind, in one
place, joined**: every election, every NGO, every lobbying filing, every town-hall
meeting. Almost all of it is public, and almost none of it is centralized. Each piece
lives with its own publisher, in its own format, so the connections between them, which
is where accountability lives, exist nowhere.

What that covers, by what it tells you about power:

- **Who holds it.** All ~520,000 elected offices, from Congress to soil and water
  districts; appointed boards and commissions (zoning, parole, utilities, transit,
  ports); agency heads, inspectors general, superintendents, police chiefs; judges and
  who put them there; party officials; the ~40,000 special districts nobody can name the
  board of; public payrolls.
- **What they decided.** Laws and local ordinances; regulations and every public
  comment on them; executive orders and emergency declarations; council and board
  votes, agendas, minutes and meeting video; zoning and development approvals; school
  board policies; pardons; court opinions and consent decrees.
- **Money in.** Campaign finance at every level, super PACs and dark money, ballot
  measure committees, inaugural and legal defence funds, bundlers followed across every
  race they fund.
- **Influence.** Federal, state and city lobbying; foreign agents (FARA); officials'
  calendars; the revolving door; model legislation traced into the statutes that copy
  it.
- **Officials' own money.** Financial disclosures, stock trades, property, gifts and
  paid travel, and votes on matters touching their own holdings.
- **Money out.** Federal grants and contracts; state and municipal budgets line by line,
  proposed against adopted against spent; procurement and the vendors who keep winning;
  capital projects, promised against built; tax-break deals and the jobs they promised;
  bonds, pensions, settlements, earmarks.
- **Elections.** Every contest down to the precinct, candidate filings, ballot
  questions, redistricting and who drew the lines, turnout, certification disputes.
- **Organizations.** Nonprofits and their 990s, foundations and their grants,
  corporations and their officers, shell companies where ownership is published,
  unions, government contractors and what they give the officials who hire them.
- **Oversight.** Inspector general and audit findings, ethics complaints, FOIA logs,
  suits against governments, police misconduct and decertifications, recalls and
  impeachments.
- **Outcomes: what the decisions did.** School funding and results, 311 response times,
  crime and clearance rates, bridges and water systems, permits and evictions,
  affordable housing promised against built.
- **Place.** Property and assessments, government land, zoning maps over time, and every
  district boundary joined to every address.
- **Words.** Floor speeches, statements, campaign promises checked against later votes.

The joins no one has made are the point:

- **Donor → vote → contract.** A company gives to a council member, the member votes on
  a contract, the company wins it. Every step is public; the chain is visible nowhere.
- **Promise → budget → outcome.** A candidate promises housing, votes a budget line;
  were the units built?
- **One person, one career.** School board, state house, Congress, lobbying firm: every
  vote, donor and disclosure along the way, as one person.
- **One address, everything.** Every official, every decision touching that parcel,
  every dollar spent nearby.
- **One template, twenty statutes.** Model legislation traced state by state.

None of this needs a new shape. A town-hall vote, a state bill and a federal law are the
same thing: a body, with seats, held by people, voting on an item on a date. An NGO is
an organization; a grant or a contract is an edge. A new domain adds sources and edges,
not a redesign. What limits the pace is sources and certification, not structure. So
the order follows the joins: a domain comes in when it connects to what is already
there (state money beside state legislators, contracts beside the councils that award
them), and one region goes deep at every level before the map goes wide.

### Making it legible

Having the data is half the problem. A million correct facts can still make a page
nobody can read. These are the rules the interface follows:

- **Four doors, not a graph.** People arrive with a **place** ("what's going on where I
  live"), a **person** ("who is this, what have they done"), a **decision** ("what is
  this, who's behind it") or a **question** ("did my representative vote for it"). Those
  are the entrances. The graph stays behind them; no one should need to know it exists.
- **Answers are sentences; the path is a trail you can open.** "Your state senator voted
  yes on SB 1 on 11 June. He took $12,000 from the teachers' union in 2024." Every
  clause links to its record. Nodes and edges appear only when someone asks how the
  answer was found.
- **Trust is visible and quiet.** One mark per claim, certified or ingested or disputed,
  with the sources a tap away. An answer carries the mark of its weakest step. A page
  covered in badges is as unreadable as one with none.
- **Time is in everything.** "Who represents me" has one answer today and another in
  2019. A person's page is a career; a decision's page is its path through committees,
  votes and signature.
- **Disclose in layers.** The headline first ("passed 43–2, signed in May"), then who and
  how, then money and influence, then the raw records. Most readers stop at the first
  layer; a reporter goes to the fourth. One page serves both.
- **Empty states explain themselves.** "No bill text for Nebraska yet: the legislature's
  site blocks our server" earns more trust than a blank. A gap should look deliberate
  and informative, never broken.
- **Compare, so numbers mean something.** $12,000 beside "the median state senator took
  $3,000"; "voted with her party 94% of the time" instead of 900 rows.
- **Plain words, exact record one tap away.** Say what a motion, a budget code or an
  agency does; keep the official term in reach.
- **The newspaper look is the right one** (Styleguide.md): it reads as a record, not an
  app. It has to work on a phone first, because that is where a resident arrives.
- **Tested on people, not on me.** Five non-experts, a phone, one question each ("who is
  your council member and how did they vote on the budget?"), and watch where they get
  lost. A few rounds of that beat any principle on this list.

### Prior art I'm deliberately not reinventing

Popolo and Open Civic Data already published essentially this ontology, and I'm already
carrying `ocd_id` in the state layer. I'd rather adopt their division IDs and naming than
invent my own.

Worth knowing why those projects went quiet: Sunlight Foundation, EveryPolitician, the
OCD scrapers. None of them died of a bad schema — the schemas still read correctly. They
died of ingestion maintenance. That's exactly the ceiling Foundry attacks, which is why
I think the graph is table stakes and the self-healing pipeline is the part that's
actually mine.

---

## What's next

In order. Each step is independently useful, so none of it is wasted if I stop partway.

**1. Unify the two routers.** *Done 2026-09-29.* `router_agent.route` is the one
routing decision for `/ledger`, `/search` and `/state/search`: the free checks first
(on `/ledger`), then the fast paths, then Haiku. The six federal `handle_*` search
handlers became one, `handle_bill_search`, over the index. What the classifier keeps is
a smaller job — finding the node a graph walk starts from (a bill ID, a named act, a
place).

**2. Give the ask SPA a state plate.** *Done 2026-09-27 for Virginia, 2026-09-28 for all
50.* `/ledger` sends a state question to the state layer; `ledger.js` has a state bill
page and a state legislator page. The data is Open States (monthly dump and people repo),
checked by Virginia LIS in Virginia only, loaded into the graph like Congress. Next: an
independent check for more states, and a state-shaped map under a state answer.

**3. Index the local corpus.** Postgres FTS over instrument titles and the 4,384 item
summaries, scoped by jurisdiction. This is what makes "zoning in Fairfax" stop
discarding the topic.

**4. The graph, Virginia first.** Two tables — `graph_node` and `graph_edge`, the latter
with `valid_from`/`valid_to` and a certification column. Federal, the General Assembly,
and the four counties already on disk. Person resolution via the normalizer that takes
supervisor↔contest matching from 3/12 to 9/12. Uncertified edges stay traversable and
badged.

*Slice one exists:* `graph.py` builds Fairfax BOS top to bottom — state → county →
districts → board → seats → people → agenda items, plus the 2023 contests that seated
them — and answers `person → voted_on → agenda_item` filtered by topic, at
`GET /api/graph/votes?person=&topic=`. `python graph.py build fairfax-bos` runs the
whole thing in memory with no database and prints the gaps; `load` writes it. What it
proved against real data: every Fairfax edge is `ingested` (single-source), so the
weak-hop badge is on every answer; edge identity has to include the asserting record
because one item carried three roll calls; and Braddock changed hands inside the window
with no contest on disk, which is where the time bounds earned their keep — the
displaced holder's `valid_to` is inferred from the successor's first appearance and
says so. Meetings are not nodes (they're events); topic is Haiku's reading of the title
and is labelled as such.

*Slice two is Congress, same two tables, same predicates.* `build_congress` reads
`data/legislators-current.json` (every current member, every term, exact dates, bioguide
ids) and a roll-call snapshot that `python graph.py snapshot 119 2 2026` writes to
`data/congress-votes-119-2.json` from the House clerk's and the Senate's XML — the
loader never touches the network. `load us-congress --state VA` loads one delegation;
without `--state` it loads all 535. What it proved: the ontology really is
self-similar — a chamber is an organization, a district or Senate class is a post, a
bill is an instrument, and a county agenda item is the same kind — so a federal and a
county vote answer with the same words. Federal people are keyed by bioguide id; local
people by county + surname + first initial (never by seat, so a person who changes
seats stays one node; a surname-and-initial collision with a different given name
stays two and is reported). The hand-asserted bridge between the two is `identities`
in `foundry/data/store/_graph-sources.json`, and Walkinshaw (Braddock supervisor, then
VA-11) is one node with a `holds` edge into each layer.

*Certification is real now.* Loudoun is the third source: its meetings are certified
against the clerk's minutes, so a Loudoun answer crosses certified and ingested hops in
one list and names the weakest. Loudoun records motions rather than agenda items, so
the motion is the instrument. Federal `holds` are certified by the clerks' roll calls
— a member recorded voting inside the term affirms the term from a source independent
of the legislators file; all 13 Virginia terms with a 2026 vote are certified, the
earlier ones are not. The bounds keep their own precision either way: certification
says the term is real, not that its dates are.

*Time, across sources.* A person cannot hold two of these seats at once. After every
load, `close_holds_across` looks at each person's holds from every source loaded and
closes an open or inferred hold the day before an exact-started hold on another post
begins. That is how the county loader, which can only see a successor's first
meeting, learns that Walkinshaw left Braddock on 2025-09-09 rather than in January.
Both bounds stay tagged `inferred`.

*All four counties are in.* Prince William's roster is surnames only ("Gordy"), so a
bare surname is a name, and a contest winner ("Thomas T. \"Tom\" Gordy") matches it when
the surname is unique on that board; two members whose special elections are not on
disk vote but hold no seat, and the answer says so. Stafford is the honest thin case:
it staggers its terms, the 2023 results cover three of its seven seats, and its roster
carries three different Allens, so a vote by "Allen" is refused as ambiguous rather
than guessed — 249 positions dropped and counted, not silently assigned.

*`sponsored` is the ninth predicate.* The snapshot now asks Congress.gov for every
bill the session voted on: title, policy area, sponsor, and each cosponsor with the
date they signed (`CONGRESS_API_KEY`; without it the snapshot carries votes only and
the loader names the gap). Sponsorship is an instantaneous edge on that date, with
the role on it. Titles now come from the record, and the policy area is the
instrument's topic — Congress.gov's own subject, so a topic filter that matched it
is not flagged advisory; only Haiku's county topics are. Two more question shapes:
"who sponsored the Affordable HOMES Act" and "what did Kaine sponsor". Sponsors
outside the loaded delegation are not loaded, and only bills with a recorded vote
this session are on disk, so "what did X sponsor" is a floor, not a count.

*A county enters the graph by an entry in `_graph-sources.json`* — its OCD slugs, the
elections store that seats its members, the statutory term — not by editing code.
`python graph.py load all` loads every entry and every state the sidecar names for
Congress; the refresh workflow runs a snapshot of the current session's roll calls and
`load all` daily when the database secret is set.

*The graph answers typed questions* at `GET /api/graph/search?q=` and
`python graph.py ask "…"` (`--memory` builds every source in memory, no database). Same
contract as `fast_route`: a regex answers three shapes for $0 and returns "not mine"
otherwise, so a caller can fall through. The shapes are the three traversals that
exist — "how did Herrity vote on zoning", "who voted no on the Affordable HOMES Act",
"who held the Braddock seat on 2025-11-18" (a bare year means the end of it; no date
means today). A county or state name inside a topic is treated as scope and dropped —
"Fairfax zoning" searches "zoning" and the answer says what it ignored. `parse_question` and `answer` are pure; `memory_backend` runs the same
five lookups over the lists `build` returns that `pg_backend` runs as SQL, which is
the harness `tests/test_graph.py` asks its questions through.

`/ledger` routes into it: `route` runs `classify_question`, which tries `graph.parse_question` before the
place and topic guesses, so "how did Herrity vote on zoning" streams a `graph` plate
instead of going to Congress. The graph declines every other shape, and the caller
falls back with `allow_graph=False` when it parses a shape but knows neither the
person nor the seat — so nothing the ledger answered before is lost.

*The federal layer is in* (2026-09-25). `load us-congress` now loads every member
of Congress since 1789 (the current and historical legislators files, joined by
bioguide id), the presidency (`executive.json`), 49 committees and 181 subcommittees
with their members, and for each bill the 119th Congress voted on: its sponsors,
referrals, committee reports, related bills, the President who signed or vetoed it
(resolved by date), and the public law it became. Campaign committees come from the
FEC with totals on the edge; the top PACs stay in `data/fec-<cycle>.json` and are
read at answer time. `python graph.py fetch` downloads the public files, `enrich`
re-fetches a snapshot's bill records, `snapshot-fec <cycle>` walks the FEC. There
are eighteen predicates now, pinned by a test.

What it proved:
- **Only the current Congress's votes are rows.** The 118th's roll calls stay in
  their snapshot files. They certify the terms they fall inside (1,400 terms are
  certified) and answer "how did X vote … in 2023" from the file. That is the events
  rule, with one exception that I took on purpose.
- **A name that matches several people is a question, not an answer.** "Warner"
  returns the 13 Warners who served, with their seats and years. It does not merge
  their votes. A full name settles it.
- **On handover day the incoming holder has the seat.** The legislators and
  executive files end a term on the day the next one starts, so 3 January had two
  holders of every seat that changed hands, and 20 January had two Presidents.
- **A whole state counts, not a substring.** "The senator for Virginia" no longer
  returns West Virginia's.

In production (measured 2026-09-25, after one full load): 454,740 edges in 364 MB including indexes, about 800 bytes an edge; 17,906 nodes in 10 MB; the whole database 392 MB. `voted_on` is 379,014 of the edges, and the load takes about two minutes.

*Every bill, every district, nominations and lobbying* (2026-09-26, on the Hetzner
server). The graph loads by **scope**, each replaced whole in one transaction and
skipped when its inputs did not change (`graph_scope`): `us/skeleton` (people, seats,
the presidency, committees, money), `us/districts`, `us/bills/<108…118>`,
`us/nominations/<97…118>`, `us/current` (the 119th's bills, roll calls and
nominations), `us/lobbying/orgs` and `us/lobbying/<year>`, and `local/<county>`.

- Every bill since 2003 is a node, with its sponsors, cosponsors, referrals, reports and
  what the President did; each Congress states "N of M bills" against Congress.gov's own
  count (0–10 short, GovInfo's publishing lag). A bare bill number is the newest
  Congress with it; "HR 1 in the 110th" picks one.
- Nominations since 1981 are instruments; the nominee is data on the node (Congress.gov
  gives no id), and `nominated` comes from the President in office the day it arrived.
- District shapes since 1789 (Lewis et al., UCLA) are nodes with PostGIS geometry,
  represented by the House seat once per Congress: `/resolve-point` with a past `date`
  answers who represented a point then (a Fairfax point on 1995-06-01: VA-11, Thomas
  Davis).
- Lobbying: organizations are exact normalized LDA names; `lobbied_for` (firm → client,
  income) is ingested, `lobbied_on` (client → bill, read from free text) advisory, and a
  PAC links to an organization only by an identical name (advisory).

Measured on the fresh load: 42 scopes in 780 s; 269,951 nodes and 3,202,580 edges;
9,413 district shapes (913 MB of geometry); the database 4.4 GB. A fresh load writes
about 6.8 GB of WAL, all of it archived. The daily load replaces the skeleton, the
current Congress and whatever changed: about 75 s.

*The pages read files* (2026-09-27). From the 108th Congress (2003) on, a bill page
reads the record, actions, cosponsors, related bills, amendments and committee reports
from the BILLSTATUS zips, the text from the stored typescript (every version GovInfo
published), and the roll calls from the vote files: the clerks' from the 118th,
Voteview's before, found by the clerk's roll number. A bill not in the last sync is a
404 that names the GovInfo file it read; there is no live fallback. Members come from
the legislators files and the graph's `sponsored` edges (counted from 2003, and the page
says so); campaign money from FEC bulk, including each committee's sources from the
committee summary file; the feed from `bill_doc` and the graph; photos from a mirror of
unitedstates/images. Parity was checked against the live APIs: 118 HR 815's record,
Tammy Baldwin's 2026 totals to the cent, and the NDAA and HR 1 roll calls.

Still live, on purpose: anything before the 108th Congress, donor industries (they need
FEC's multi-GB `indiv` file), geocoding, Google Civic, market prices, and the
models.

*Search.* Every bill since 2003 has a search document (`bill_doc`: titles, subjects,
policy area, the latest CRS summary) and one vector in Voyage 4's shared space:
documents embedded by `voyage-4-large` through the API, questions by `voyage-4-nano` on
the server's CPU, so a question never leaves the machine. `search.bill_index.search`
fuses full-text and nearest-vector ranks (RRF, k=60), and every search uses it since
2026-09-29: `api.handle_bill_search` for Congress, `handle_state_search` for the states,
each with one Haiku relevance check after. A named act is searched by the router's
canonical name, in any Congress ("obamacare" searches "Affordable Care Act"), and the
law of that name goes first; "give me a bill" reads
the newest rows (`bill_index.recent`); a question wholly before 2003 gets an honest
empty (`empty_reason: "before_index"`), and one reaching back past it a note. It
replaced GovInfo search without the planned blind evaluation, by my choice: the
question set is kept in `scripts/search_smoketest.py` (`EVAL_QUERIES`) for the eval
that measures it.

*Next for the graph:* the General Assembly.

**5. Rebuild what `/newspaper` did.** See the next section — I deleted the old tabbed
app rather than porting it, so these are rebuilds in `ledger.js`, not migrations. The
backends all still work; only the views are gone.

Staying in Python. I looked hard at porting the backend to Rust and decided against it:
no official Anthropic SDK, no comfortable BeautifulSoup equivalent, the synthesized
extractors have to stay Python anyway, and nothing a user can see gets better. Same
answer for a Leptos frontend — a WASM compile costs me edit-and-reload, and the one real
prize, shared types with a Rust backend, doesn't exist if the backend is Python.

---

## Rebuilding the newspaper capabilities

I deleted `frontend/index.html`, `js/index.js`, `js/lobbying.js` and
`js/correspondence.js` — the old tabbed app at `/newspaper`. I'd rather rebuild these
properly in the ask SPA than port UI I didn't like.

**Every backend endpoint below still exists and works.** Nothing server-side was
removed. What's gone is the only UI that reached them, so each item is a view to build
in `ledger.js` against a known, working API. The old implementation is in git history
if I ever want to look.

### Offline until rebuilt

| # | Capability | Endpoints | Notes |
|---|---|---|---|
| 1 | **State legislation search** | `POST /state/search`, `/ledger` | *Rebuilt 2026-09-27.* `/ledger` routes a state question here; no Federal/State picker, the router decides. |
| 2 | **State bill detail** | `POST /state/bill` | *Rebuilt 2026-09-27:* `stateBillView`, at `/state/<st>/<session>/<type>/<n>`. |
| 3 | **State member lookup** | `POST /state/member/search` | *Rebuilt 2026-09-27:* `stateMemberView`, at `/state/<st>/member/<id>`. |
| 4 | **Public law detail** | `POST /law` + `/law/{congress}/{number}` routes | Same generator as `/bill`; the old app just picked the endpoint off `is_law`. Small. |
| 5 | **Lobbying directory** | `GET /lobbying/search`, `GET /lobbying/entity` | Type-ahead over ~14k entities → entity profile: spend, issues, lobbyists, bills pushed. Per-bill lobbying already renders inside `billView`; this is the standalone directory. |
| 6 | **Stock explorer** | `GET /stocks/notable`, `/stocks/all`, `/api/stocks/traded`, `/api/stock/{ticker}/timeline` | Member finance and per-member trades already exist in `memberView`. This is the cross-member browse and the ticker timeline. |
| 7 | **Personalized feed** | `POST /feed` | "What's Moving." The scoring is pure and unit-tested (`tests/test_feed_rank.py`, 40 cases) — only the rendering is gone. The home page is currently just a search box. |
| 8 | **Structured feedback** | `POST /flag/search`, `POST /flag/bill` | The flag modal with canned reasons. `ledger.js` has only a `mailto:` link, so the loop into `analyst_agent` and `/monitor` is dead. Cheap to rebuild, and it's how I learn what's broken. |
| 9 | **Notifications page** | `GET /correspondence`, `/correspondence/{id}/replies`, `POST /correspondence/unsubscribe` | Subscribe already works in `ledger.js`; listing, replies and unsubscribe don't. |
| 10 | **Letter writing** | `POST /correspondence/draft`, `/send`, `/followup/draft` | Was already shipped disabled — no reliable rep email addresses. Rebuild only if that's solved. |
| 11 | **Onboarding flow** | — | Multi-step intro on the old front page. Probably don't rebuild; the ask box is the onboarding now. |

### Order I'd do them in

1 → 2 → 3 (the state layer — done 2026-09-27)
7 → 8 (feed and flags — both cheap, both restore signal I use)
4 (public law — small)
5 → 6 (lobbying and stocks — the money layer)
9 (notifications)
10, 11 (only if the underlying problem changes)

### Still live, don't re-delete

`elections_shared.js` is used by `elections.html` and `election_detail.html`.
`d3-geo`, `d3-array` and `topojson-client` are used by `ledger.js` and `foundry.js`.
`leaflet.js` is used by `foundry.js`.

---

## Architecture

No framework, no bundler, no ORM, no build step. FastAPI serving vanilla HTML/CSS/JS.
Edit a file, hit save, reload the browser.

```
Ask (production)     POST /ledger → NDJSON stream
                     route: classify → plate → stream plate, member, shelves
                     frontend/test.html + js/ledger.js

Search               POST /search, /state/search → JSON
                     route: fast_route_state → fast_route → route_query (Haiku)
                     → the hybrid index (bill_doc) → one Haiku relevance check
                     the $0 regex paths answer first; the model is last resort

Bill detail          POST /bill, /law → NDJSON
                     ~10 sections computed concurrently, flushed as each finishes
                     so the page paints progressively

Foundry              discover (Sonnet) → synthesize (Opus) → gate (deterministic)
                     → certify against an independent oracle → refresh ($0)
                     certified and uncertified data are never interchangeable

Logging              every agent action → agent_log.json
                     searches + confidence → search_log.jsonl
                     user "this is wrong" → flags
                     all three feed the analyst at /monitor
```

Routing is one function, `router_agent.route`. On `/ledger` it runs the free checks
(`classify_question` in `agents/ledger_agent.py`: watch, graph, elections, place, bill
ID, local) and only then structures the question (fast paths, then the LLM).
`/search` and `/state/search` skip the free checks (`plates=False`) and keep their JSON
shape, which is why `/search` still answers "LA County" as off-topic. That gap is left
on purpose: `/search` has no user, and the graph replaces both steps.

---

## The rules I hold myself to

These aren't preferences. When I break one I say so in the commit message.

1. **I should be able to fit this in my head.** No framework, no bundler, no ORM.
2. **Edit-and-reload is sacred.** Anything that adds a build step is the wrong tool,
   even if it's individually better.
3. **Plain data structures unless I can't.** Dicts for config, tuples for ordered
   priority, sets for dedup. A class earns its place at the third method, not the first.
4. **One dispatch entrypoint per concept.** One `/search`, one `/bill`. Not five variants.
5. **Comments explain why, never what.** Hidden invariants, upstream bugs, deliberate
   trade-offs. Never a restatement of the line below.
6. **Three similar lines beat a premature helper.** Extract at the third real use case.
7. **Delete cleanly.** No backwards-compat shims for code only this app calls.
8. **Fail-open vs fail-closed is a decision, made explicitly, per function.**
9. **Degrade gracefully.** Circuit breakers, fallback sources, stale-while-revalidate.
10. **Wall-clock budgets, not per-call timeouts.** One slow call must not burn the wait.
11. **Namespace every cache** (`search:v6:`) so a version bump invalidates without a purge.
12. **Honest empty states.** When there's nothing, say why. Never a silent zero.
13. **Validate at the boundary.** Pydantic at the perimeter, loose dicts inside.
14. **DOM is a render target, not state.** Module variables hold the truth. Clear stale
    DOM on context switch.
15. **Cache-bust CSS** with `?v=` whenever styles change.
16. **No new files unless unavoidable.** Existing files grow; the codebase doesn't sprawl.
    (`api.py` is past 3,700 lines and has genuinely earned a split — extracting the search
    dispatcher is overdue.)

And the ones I won't negotiate on: **no silent failures**, **every external call wrapped
in cache + timeout + fallback + breaker**, and **certified data is never mixed with
uncertified data**. Missing data is my gap, not the jurisdiction's, and the UI says so.

Visual design is a separate contract — see `Styleguide.md`. Short version: it should look
like a 1940s legal newspaper, never a SaaS dashboard. No border-radius, no box-shadow,
no purple.

---

## Stack

```
Backend       Python · FastAPI · uvicorn · slowapi rate limiting
Models        Haiku 4.5 — routing, validation, translation (most calls)
              Sonnet 5.5 — web search: bill background, upcoming elections, polling
              Opus      — Foundry extractor synthesis only
Search        Voyage 4 — voyage-4-large for documents (API), voyage-4-nano for
              questions (on the server CPU, requirements-embed.txt)
Federal       GovInfo bulk (BILLSTATUS, bill text, CRPT) · Voteview · clerk.house.gov +
              senate.gov XML · FEC bulk · lda.gov · Congress.gov (nominations; bills
              before 2003)
State         Open States (monthly Postgres dump, people repo) for all 50 · bill text
              from each version's own link · Virginia LIS daily files (the one
              independent check)
Local         Foundry — my own synthesized extractors, 9 sources
Civic         Google Civic (elections) · Census geocoder (districts) · FEC · Senate LDA
Storage       Postgres 17 on the server (psycopg3 pool, pgvector, PostGIS) — the graph,
              bill_doc and bill_embedding, subscriptions, mail, disk_cache
              Foundry stores are JSON on disk under foundry/data/store/
Email         Gmail OAuth for user letters · SMTP for system notifications
Frontend      Vanilla HTML/CSS/JS. Playfair Display · Source Serif 4 · IBM Plex Mono
Deploy        One Hetzner CX53 behind Cloudflare; the server pulls main every 5 minutes
Backups       WAL-G continuous archive + nightly restic pg_dump to a Hetzner Storage Box;
              a weekly restore check compares exact row counts
```

### Where the data lives

`graph.DATASETS` names every dataset once: its path, source, licence and the code
that writes it. Everything reads and writes through `graph.data_path` / `data_glob`.
The root is `NOSPOPULI_DATA_DIR` (`/srv/bulk` on the server, `data/` in a checkout):

```
raw/<source>/…            downloads exactly as published, each with its manifest
                          (govinfo/, voteview/, fec/, districts/)
derived/bills/            bills-<congress>.json        GovInfo BILLSTATUS + CRPT
derived/votes/            congress-votes-<c>-<n>.json  clerks (118th on), Voteview (1st–117th)
derived/fec/              fec-<cycle>.json             FEC bulk (the graph's members)
                          candidates-<cycle>.json      every candidate's committee money
                                                       and each member's PACs (untracked)
derived/nominations/      nominations-<congress>.json  Congress.gov
derived/lobbying/         lobbying-<year>.json         lda.gov
derived/states/<st>/      people.json                  Open States people repo
                          sessions.json                the session index: order, years
                                                       (ids like "88" carry neither)
                          bills-<session>.json         Open States dump: bills, actions,
                          votes-<session>.json         sponsors, versions; roll calls
                          lis-<session>.json           Virginia LIS: history, roll calls
                          member-session.json          certification from LIS roll calls
raw/openstates/           the monthly dump (one kept, ~11 GB) and its manifest
raw/openstates-people/    a sparse clone of the people repo
raw/lis/<session>/        Virginia LIS CSVs
raw/states/<st>/text/<session>/  each text version, gzipped, with its manifest; a PDF
                          also has its text (.txt.gz). One fetch per state at a time
                          (.lock). np-ca-bundle.pem: Python's roots plus the two
                          intermediates Illinois' and Connecticut's servers omit
derived/certification/    member-congress.json         from the older roll calls
public/                   the unitedstates project's legislators, executive, committees
raw/unitedstates-images/  member photos (a sparse git clone, pulled Mondays)
datasets.json             written by `graph.py manifest` after each sync
```

The app's own files (`app/`: house_stocks, known_elections, notable_trades,
zip3_to_state) live only in the checkout's `data/app/`. The checkout's `data/` mirrors
the relative paths above, so the sync copies the tracked files back by path. Only the
small ones are tracked: `public/`, the clerks' roll calls and `fec-<cycle>.json`.
`python graph.py migrate-layout` moves a flat data dir into this layout once.

---

## Running it

```bash
pip install -r requirements.txt
uvicorn api:app --reload        # → http://localhost:8000
python -m unittest discover tests   # pure-logic tests, no network, no pytest
```

Required in `.env`:

```
ANTHROPIC_API_KEY       CONGRESS_API_KEY        GovInfo_API_KEY
GOOGLE_CIVIC_API_KEY    DATABASE_URL
```

Optional, per feature: `FEC_API_KEY` and `LDA_API_KEY` (money and lobbying),
`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` (letter writing), `SMTP_*` and
`NOTIFY_FROM_EMAIL` and `WATCHER_SECRET` (notification emails), `JWT_SECRET` (auth),
`MONITOR_SECRET` (the `/monitor` console), and the `FOUNDRY_*` set (pipeline
budgets and switches).

```bash
python -m scripts.event_watcher        # daily bill-state watcher
python -m scripts.clear_search_cache   # --all to include feed/elections
```

A `.env` on a laptop reads no database unless `DATABASE_URL` is set; point it at an SSH
tunnel to the server, never at Supabase (the old `SUPABASE_DB_URL` name is ignored).
Graph loads and bulk syncs belong on the server.

### On the server

`nospopuli-1` (`/srv/nospopuli/app` is the checkout, `/srv/nospopuli/venv` the
virtualenv with `requirements.txt` and `requirements-embed.txt`, `/srv/bulk` the data,
`/srv/nospopuli/models` the query model, `/etc/nospopuli/env` the secrets). Run a module
as the app: `sudo -u nospopuli` from the checkout, with that env file loaded. Caddy
terminates TLS for Cloudflare, which is the only source the firewall lets in on 80/443.

The scripts are `/usr/local/sbin/nospopuli-{deploy,sync,openstates,backup,restore-check}`.
The sync stops the deploy timer while it runs, so a deploy cannot reset the checkout between
the Foundry refresh and its commit. The daily sync pulls the state legislators, the LIS
files and the latest sessions' bill text before the graph load. `nospopuli-openstates`
runs on the first Sunday of each month at 04:00 UTC: the dump, a restore into a scratch
database as `postgres`, the extract, the drop, then the graph load and search documents;
it stops early when every loaded state is already extracted from the current dump. The
units, verbatim:

```ini
# nospopuli.service
[Unit]
Description=NosPopuli API (uvicorn)
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
User=nospopuli
Group=nospopuli
WorkingDirectory=/srv/nospopuli/app
EnvironmentFile=/etc/nospopuli/env
# One worker: the rate limiter and the TTL caches live in process memory.
ExecStart=/srv/nospopuli/venv/bin/uvicorn api:app --host 127.0.0.1 --port 8000 --workers 1 --proxy-headers --forwarded-allow-ips 127.0.0.1
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target

# nospopuli-deploy.service
[Unit]
Description=Deploy NosPopuli from origin/main if it moved
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
ExecStart=/usr/local/sbin/nospopuli-deploy

# nospopuli-deploy.timer
[Unit]
Description=Check origin/main every 5 minutes

[Timer]
OnBootSec=2min
OnUnitActiveSec=5min

[Install]
WantedBy=timers.target

# nospopuli-sync.service
[Unit]
Description=NosPopuli daily sync: Foundry, roll calls, federal bulk data, graph load
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/nospopuli/env
ExecStart=/usr/local/sbin/nospopuli-sync
TimeoutStartSec=6h

# nospopuli-sync.timer
[Unit]
Description=Daily NosPopuli sync at 11:30 UTC

[Timer]
OnCalendar=*-*-* 11:30:00 UTC
Persistent=true

[Install]
WantedBy=timers.target

# nospopuli-openstates.service
[Unit]
Description=NosPopuli monthly: Open States dump, state files, graph load
After=network-online.target postgresql.service
Wants=network-online.target

[Service]
Type=oneshot
EnvironmentFile=/etc/nospopuli/env
ExecStart=/usr/local/sbin/nospopuli-openstates
TimeoutStartSec=4h

# nospopuli-openstates.timer
[Unit]
Description=Monthly Open States dump, the first Sunday at 04:00 UTC

[Timer]
OnCalendar=Sun *-*-01..07 04:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target

# nospopuli-backup.service
[Unit]
Description=NosPopuli backup
After=postgresql.service

[Service]
Type=oneshot
User=postgres
ExecStart=/usr/local/sbin/nospopuli-backup

# nospopuli-backup.timer
[Unit]
Description=Nightly NosPopuli backup

[Timer]
OnCalendar=*-*-* 03:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target

# nospopuli-restore-check.service
[Unit]
Description=NosPopuli restore-check
After=postgresql.service

[Service]
Type=oneshot
User=postgres
ExecStart=/usr/local/sbin/nospopuli-restore-check

# nospopuli-restore-check.timer
[Unit]
Description=Weekly NosPopuli restore check

[Timer]
OnCalendar=Sun *-*-* 05:00:00 UTC
Persistent=true

[Install]
WantedBy=timers.target
```

The sync, in order: Foundry refresh; the clerks' roll calls; on Mondays the public
legislators and committee files, district shapes and member photos; GovInfo BILLSTATUS
and bill text; Voteview; nominations; lobbying filings; FEC bulk for the current cycle;
the certification index; `graph.py load all`; the search documents and any new
embeddings; `graph.py manifest`; then a commit of the tracked data to `main` with a
deploy key.

---

## Tests

Two tiers, kept apart on purpose.

**Tier 1 — regression.** Free, deterministic, and the only thing CI gates on
(`.github/workflows/tests.yml`). 301 tests in ~3s with no network, no LLM and no
database: the pure-logic tests, plus 56 golden fixtures over the route surface —
request → exact response JSON, captured once and committed under `tests/golden/`.

**Tier 2 — quality eval.** Measures whether search is any *good*, not whether it
changed. Costs money (cents), opt-in, never in CI. `scripts/search_eval.json` holds 218
questions, each labelled with its intent, the graph nodes it names and which bills
answer it. The questions:
- the 72 saved eval questions;
- the 15 in the search log (the server was new);
- the smoke cases;
- 120 written for every intent the graph search answers.

Haiku labelled them once; each row says who labelled it, and owner corrections change
that to `owner`. One question in five is held out, scored and never tuned on.

```bash
python -m scripts.search_smoketest --baseline scripts/search_eval.json preds.json --base URL  # run a system
python -m scripts.search_smoketest --judge scripts/search_eval.json preds.json   # rate what it found
python -m scripts.search_smoketest --score scripts/search_eval.json preds.json   # the numbers
```

`--judge` rates every returned bill no label covers, so no system is scored only on the
bills the first pool held. Baseline (2026-09-29, /ledger before the graph search;
held-out, then all 218):

| intent | entity links | recall@10 | nDCG@10 |
|---|---|---|---|
| 0.38 / 0.49 | 0.19 / 0.11 | 0.14 / 0.18 | 0.23 / 0.30 |

Every phase of the graph search (Phases 1–6) must not lower the held-out numbers.

```bash
pytest tests/ -q                     # Tier 1, the whole thing
pytest tests/ -q -k ledger           # while iterating
python -m unittest discover tests    # the pure-logic tests, no pytest needed
```

`pytest` is in `requirements.txt` for the golden suite, which needs strict xfail and
parametrize. The eight pure-logic files are stdlib `unittest` and run under either
runner. That split is deliberate, not drift.

### Philosophy

This is **characterization testing**. A test here proves behaviour did not change; it
does not prove the behaviour is right. Tests are scaffolding for change — they are
what let me refactor `api.py` without guessing. Six principles follow from that:

1. **Routes are pinned by golden fixtures.** Request → whole response, diffed. A fixture
   pins today's answer, wrong or not; two of them pin a wrong answer on purpose.
2. **Replay is hermetic, and a miss raises.** Every boundary replays from a recording.
   A call with no recording is a named failure, never a live call, so a green run
   cannot be green by accident or cost money.
3. **A test controls every input it reads.** Not only the network, the LLM and the
   database — also the clock and the data files in the repo. The daily Foundry refresh
   commits a new `foundry/data/store` to `main`; a route that read it live went red on
   2026-09-21 with no code change. An input the test does not control is a test that
   fails for someone else's reason.
4. **Unit tests go where the risk is.** Logic that is non-trivial, that branches, or
   that has already broken in production. Not thin wrappers over a library, and not a
   restatement of the implementation.
5. **Known defects are pinned, not hidden.** A strict expected-failure records the bug
   and keeps the suite green; the day it is fixed, the test fails and asks to be
   promoted to a plain assertion. A permanently red suite teaches everyone to ignore red.
6. **Correctness is Tier 2's job.** Tier 1 answers "did it change?" Asking it "is it
   right?" is how a regression suite turns into something people skip.

A failing test is a finding. Read the code, decide whether the test or the code is
wrong, and fix that one — never change application code just to turn a test green, and
never loosen a test to get there. It happened twice while writing the first batch (a
year-validation bug in the state regex, a participation gap in the vote mapper), and
both times the test was right.

### Replay, and why a green run is trustworthy

`tests/replay.py` makes `api` importable without a network, an LLM bill or a Postgres
connection, so whole responses can be diffed against fixtures. The shape is lifted from
`foundry/sandbox2.py`, including the property that matters most: **a boundary with no
fixture raises, it never falls through to a live call.** A socket guard backs that up, so
a missed seam is a named failure rather than a surprise invoice.

What it controls: the Anthropic clients, `requests`, async `httpx`, `urllib`, Postgres
(through `correspondence.db._cursor`), the logs that would otherwise dirty the tree, the
clock where a pinned route reads it (`api._dt.date.today()` is frozen to
`replay.TODAY`), and the Foundry store — routes read a
frozen subset under `tests/golden/_store/` instead of the live `foundry/data/store/`.
The subset is whole files copied unchanged, never hand-edited.

Note it blanks `DATABASE_URL` before importing `api` — `correspondence/router.py`
calls `init_db()` at import time, so without that, merely importing the app runs
`CREATE TABLE IF NOT EXISTS` against production.

```bash
NOSPOPULI_REPLAY=record python -m tests.replay record          # re-capture, live
NOSPOPULI_REPLAY=record python -m tests.replay record ledger   # only matching cases
python -m tests.replay list                                    # what's pinned
```

Re-capture only for an *intended* behaviour change, and read the fixture diff before
committing it — that diff is the whole point. Record mode makes live calls and spends
money; never use it to debug.

### Adding a test

No new test files — each kind already has a home.

**Pure logic.**
1. Add `test_*` methods to the matching `tests/test_<area>.py` (a `unittest.TestCase`).
2. Keep it free of HTTP, LLM and database; those files declare none.
3. Run `pytest tests/ -q -k <area>` until it passes.
4. If it caught a real bug, say what the bug was in a comment — that is the test's
   strongest justification.

**A route.**
1. Add a case to `CASES` in `tests/replay.py`, with a note that says what it pins.
2. Record it: `NOSPOPULI_REPLAY=record python -m tests.replay record <substring>`.
3. Read the new fixture before you commit it. If it reads the repo's data or the clock,
   make sure the harness controls that input first (principle 3).
4. A new route without a fixture is unverifiable, so add it in the same change.

**A known defect.**
1. Write the test that should pass, and mark it `@pytest.mark.xfail(strict=True)` or
   `@unittest.expectedFailure`.
2. Name the defect and its file:line in the docstring.
3. Add it to "Known defects" below in the same change.

Then update the table below if the test guards something new.

### What's covered

| File | What it guards | Why |
|---|---|---|
| `test_router_fast_paths.py` | `fast_route` (federal) + `fast_route_state` (state) regex paths | Highest-traffic query type; breaks silently turn every "HB 1234" into a slow LLM round-trip |
| `test_state_vote_mapper.py` | Committee-vs-floor vote disambiguation, participation threshold | Two real bugs caught in production this month (VA HB 191 committee tally, CA SB 1407 fake Assembly vote) |
| `test_parse_amends.py` | "To amend the X Act of YYYY" extraction in the Connections panel | Pure regex; if it silently degrades, every bill detail page loses its primary law reference |
| `test_foundry_health.py` | `foundry/health.py` — the scraper-health status vocabulary (`summarize`) and the ledger's bounds | The console at `/admin/foundry` reads nothing else; if `summarize` mislabels a source, the operator is told a scraper is healthy when it is not. Also pins the rule that a quarantine caused by publication lag is never reported as a failure |
| `test_endpoints.py` | 56 fixtures over the 88 routes, request → exact response, via `tests/replay.py` | The oracle the suite didn't have. Turns "read api.py and reason about equivalence" into "make this JSON match", which is what makes a refactor of `api.py` verifiable instead of hopeful |
| `test_ledger.py` | `classify_question`, the funnel, compact titles, shelves — and `KnownDefects` | The defects below are pinned here as strict expected-failures, so they're recorded without leaving the suite red. Includes the control that `classify_question` gets "LA County" *right*, which is what makes `/search` answering `off_topic` a bug rather than an opinion |
| `test_feed_rank.py` | Feed scoring and the interest/blocklist gates | 40 cases; the scoring is pure and the rendering is currently offline, so this is all that holds it |
| `test_search_rank.py` | `rank_by_relevance` determinism and order stability | Ranking is re-sorted by the validator downstream, so a silent change here is invisible in the UI |
| `test_graph.py` | `graph.py` — identity resolution, node/edge building, the as-of seat resolver, and the answer shape | Fixtures replay the real Fairfax defects (duplicate spelling, sentence fragment as a member, three roll calls on one item, a seat that changed hands with no contest on disk, a person who won two bodies' seats in one district). Pins that certification follows the asserting record, that `elected_in` is scoped to the seat and never to the surname, and that an empty answer always says why |

### What's NOT covered (and why)

- **Search *quality*** — Tier 2 measures it (218 labelled questions, above), but the
  labels are mostly Haiku's, and a bill no pool held cannot count until `--judge` rates it.
- **Routes without a fixture** — mostly the ones needing a seeded database, a real
  secret, or a per-route request body worth hand-writing. Auth-gated routes are pinned
  at their refusal contract only; a fabricated 200 would be worse than nothing.
- **The graph's SQL.** `test_graph.py` asks its questions through `memory_backend`;
  nothing runs `pg_backend`, so a wrong query passes. The graph routes are pinned only at
  their no-database empty state.
- **Part of `/api/foundry/data`.** The frozen store holds one source (`loudoun-bos`),
  one capital-projects store and `upcoming.json`. No elections store and no item-facts,
  item-summaries or meeting-digests sidecars (~2.5 MB together), so those four keys are
  pinned empty.
- **The data files outside the Foundry store.** `/stocks/notable` and `/member/stocks`
  still read their files in `data/` live. The daily refresh does not rewrite those two
  files (it writes only `data/congress-votes-*.json`), so they have not drifted yet; they
  are the next input to freeze if they do.
- **The rest of the clock.** Only `api._dt.date.today()` is frozen. `datetime.now()` in
  `api.py` and the "no date means today" rule in `graph.parse_question` still read the
  real clock. No pinned route reaches them today; a fixture that does needs that seam
  first.
- **`/api/stocks/traded`** — deliberately not pinned. It looks pure but classifies 1,249
  tickers through Haiku in batches built from a set comprehension
  (`money/bill_market.py:150`), so the batches, and every cache key derived from them, differ
  per process. One cold request cost 44 Haiku calls while recording.
- **Foundry's own LLM clients** — ~10 separate seams. No route under test calls them
  synchronously; the four that touch foundry spawn threads and correctly refuse a
  TestClient host.
- **Frontend JS** — would need a separate JS test runner; out of scope for now.

---

## Known defects

Live problems I know about and haven't fixed. Listed so nobody has to rediscover them.

- `"McKay extended thoughts and prayers to the Fairfax family"` is stored as a board
  member in `foundry/data/store/fairfax-bos.json`. A sentence fragment parsed as a
  person. A gate floor should have caught it.
- `Pat Herrity` and `Patrick S. Herrity` are duplicate members in that same store.
- `topic:` subscriptions are written by the frontend but nothing on the server ever
  polls them. People can subscribe to a topic and will never hear anything.
- The watchlist regex is anchored, so "show me what I'm watching" misses and falls
  through to federal bill search.
- Local search discards the topic (above).
- **The Virginia record is up to a month old.** Open States publishes its dump monthly.
  During a session (January to March) the legislature's newer actions show on the bill
  page as advisory rows from LIS, and move the funnel stage, but they are not the record.
- **Virginia's 2023 redistricting is one post per district number.** A delegate's
  District 87 before and after 2024 is the same post node, told apart only by the dated
  holds. Per-plan post keys would fix it.
- **Some sessions have thin sponsor records in Open States.** 2020 Special Session I has
  no sponsors at all (488 bills); 2023, 2021S2, 2022S1, 2023S1 and 2024S1 list only the
  primary patron. A sponsor named without a person id is matched by name among the
  legislators serving then (3,716 links; ties refused, and the edge says how it was
  matched). About 440 stay unmatched, mostly 2017–2020 legislators missing from the
  roster; each session's gap line counts them.
- **Every state but Virginia is `ingested` only.** No second publisher confirms a term
  or a vote; the certification gap line says so. Open States itself is never used to
  certify Open States (the same publisher for roster and votes).
- **Term dates are sometimes inferred.** The people repo often gives a term's end and no
  start (a start two years before the end is used, `bound_from: inferred`), or a sitting
  member with no dates at all (the chamber's current term start is used: 13 in
  Alabama's House, 32 in South Carolina's). For name matching an inferred start counts as
  unknown. An inferred bound never closes anyone's term.
- **Multi-member districts are one post with a seat count** (New Hampshire up to 14 over
  the history of a reused district name,
  Arizona, Maryland, New Jersey, North Dakota, South Dakota, Vermont, Washington, West
  Virginia). A stale open record in such a district is not detected unless the person is
  in the people repo's retired folder.
- **Nebraska has one chamber**, `legislature`, shown on the seat map as the Legislature.
- **Open States' own faults, handled and counted:** roll calls filed under the wrong
  chamber (Colorado; read from the voters), tallies larger than the chamber (Texas
  Senate; never the seat map), actions tagged as a signature that are not (New Hampshire
  "Not Signed Off", Texas "Transmitted"), signatures recorded twice (West Virginia),
  names as Python bytes ("b'Elliott, Josh'", Connecticut) or with a town ("of Saco",
  Maine), one-day duplicate terms (Louisiana). Missouri's and Alaska's roll calls carry no
  individual votes.
- **One legislator can be several Open States records.** The sidecar's `identities`
  joins the known ones (New Hampshire's Keith Murphy, Vermont's Kesha Ram Hinsdale);
  others show as namesakes, and a lookup answers with the one who sits.
- **Sponsor links are thin in some sessions** (below 90%: Mississippi's specials, West
  Virginia, North Dakota, Maine, Florida, Iowa, Nevada, Maryland and a few more);
  Georgia refuses 12% of its current voters as shared surnames. Each session's gap line
  counts what was not linked.
- **Some states' text is not on this server.** California's PDF links are HTML pages
  that load the PDF by script; Indiana's are an app shell and Open States' Indiana proxy
  refuses everyone. Nebraska, Idaho and Illinois refuse this server's address: their
  text is fetched from another machine and copied in. A bill page says when its text is
  missing.
- **A carried-over bill appears in two sessions.** Open States lists a bill continued to
  the next session in both (2024 and 2025 HB 1122), so a topic search can show it twice.
- **The current session moves in January.** Only the latest year with roll calls is
  loaded as `voted_on` edges; when 2027 starts voting, the 2026 votes leave the graph and
  are answered from their file. The answers stay the same; the edges do not.
- **The ledger map under a state answer shows congressional districts.** It is the
  federal map; a state answer should draw the state's legislative districts.
- Braddock District resolves to nobody from 2025-09-10 to 2026-01-12. Walkinshaw's hold
  now closes the day before his federal term (right), but Sizemore Heizer's begins at
  her first observed meeting because the special election that seated her is not on
  disk. The gap is real and the answer says "vacant or not on disk"; the fix is the
  2025 special-election results in `va-elections.json`.
- **At-large seats beside numbered districts are merged into cd:1.** The seat keys an
  at-large member as `…/cd:1` (`graph.py` `post_for`), so in a state that elected some
  members at large and others by district in the same Congress (common from the 1910s
  to the 1960s), the at-large and district-1 holders share one seat. Their district
  shapes are left out of `us/districts` (a counted gap) rather than given to the wrong
  seat. The fix is an at-large post of its own.
- **Voteview collapses split nomination numbers.** `PN78-10` arrives as `PN7810`, so an
  older roll call on a nomination in parts cannot be tied to it, and one that a split
  citation could collapse to (PN78-1 → PN781) is never linked at all. The clerks'
  files, from the 118th on, keep the hyphen.
- **A lobbying organization is a spelling, not a registry entry.** Organizations are
  one node per exact normalized LDA name (`graph.lobby_key`): "Boeing Co" and "The
  Boeing Company" are one, a subsidiary or a misspelling is another. Bill links are
  read from the filings' free text and their Congress inferred from the filing year;
  both are reported as advisory. A PAC is linked to an organization only when its
  connected-organization name has the same key.
- **A member name that fits several people gets no page.** `/member/search` and the
  `/search` member branch return `candidates` for "Johnson" rather than guessing, but
  no view reads that list, so the page says "not found".
- **`GET /api/graph/votes` still merges people who share a name.** It reads
  `graph.votes`, which does not go through `answer`'s ambiguity check, so
  `?person=Warner` returns every Warner's votes together. `/api/graph/search` and
  `/ledger` ask which one.

Found while building the golden fixtures, pinned as expected-failures:

- **Confidence is inverted, not merely useless.** Across the 73 logged searches,
  `conf=0.95` is 9-of-11 zero-result while `conf=0.6` and `0.75` are 0-for-3. The most
  confident bucket is the least correct one. `/search "LA County"` returns
  `off_topic` at 0.95 while `classify_question` resolves it to `lacounty-bos`.

---

## Legal

Public law is not copyrightable in the United States. All legislative text displayed
here is in the public domain. The plain-English translations are original works created
by the system. This displays information only — it is not legal advice, and every page
says so.

---

*NosPopuli — Law for the People*
