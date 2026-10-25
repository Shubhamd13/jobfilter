"use strict";

// Page navigation and the Inbox. Loaded after app.js and applications.js and
// uses their helpers ($, esc, getJSON, badge, detailParts, setMark,
// wireMineBox, rescreen, loadApplications, loadCalendar...).

// ---- pages -----------------------------------------------------------------

// The page is the URL's hash, so a page can be linked to and survives a reload.
const PAGES = ["inbox", "applications", "calendar", "explore", "companies", "add"];

function currentPage() {
  const name = location.hash.replace(/^#/, "");
  return PAGES.includes(name) ? name : "inbox";
}

// The sections are "views" in the markup: `.page` is already the pager's buttons.
function showPage() {
  const page = currentPage();
  for (const section of document.querySelectorAll(".view")) {
    section.hidden = section.dataset.view !== page;
  }
  for (const link of document.querySelectorAll(".nav-link")) {
    if (link.dataset.view === page) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  }
  $("app-error").hidden = true;
  // Marks made on Explore, or postings added since, change what the Inbox holds.
  if (page === "inbox") loadInbox().catch(showError);
  if (page === "applications") loadApplications().catch(showError);
  // Explore's filters may have changed since: its set is read fresh.
  if (page === "calendar") loadCalendar().catch(showError);
}

window.addEventListener("hashchange", showPage);

// ---- inbox -----------------------------------------------------------------

// What passed your role rules, read one posting at a time: a list on the left,
// the posting on the right, and a mark moves you to the next one.
const INBOX_LIMIT = 1000;
const INBOX_TABS = Object.keys(STATUS_NAMES); // none (to review), maybe, applied, skipped
const INBOX_SAVED = "jobfilter.inbox";
const KEY_MARKS = { a: "applied", m: "maybe", s: "skipped" };

const inbox = {
  tab: "none",
  rows: [],
  counts: {},
  total: 0,
  selected: null, // posting id
  index: 0, // where the selected posting sat, to pick its neighbour when it leaves
  shown: null, // posting id the reader last drew
};

try {
  const saved = JSON.parse(localStorage.getItem(INBOX_SAVED) || "{}");
  if (INBOX_TABS.includes(saved.tab)) inbox.tab = saved.tab;
} catch { /* nothing saved */ }

function firstPlace(location) {
  const parts = String(location || "").split(";").map((s) => s.trim()).filter(Boolean);
  if (!parts.length) return "location unknown";
  return parts.length > 1 ? `${parts[0]} +${parts.length - 1}` : parts[0];
}

function renderInboxTabs() {
  $("inbox-tabs").innerHTML = INBOX_TABS.map((tab) => `
    <button class="tab" role="tab" data-tab="${tab}" aria-selected="${tab === inbox.tab}">
      ${esc(STATUS_NAMES[tab])} <span class="count">${(inbox.counts[tab] ?? 0).toLocaleString()}</span>
    </button>`).join("");
  $("nav-inbox-count").textContent = (inbox.counts.none ?? 0).toLocaleString();
}

// "Fit 120 · Stretch 80 · Not read yet 286 · 40 skipped by the model"
function renderInboxVerdicts(counts) {
  const shown = WORTH_READING
    .filter((v) => counts[v])
    .map((v) => `${VERDICT_NAMES[v]} ${counts[v].toLocaleString()}`);
  if (counts.skip) shown.push(`${counts.skip.toLocaleString()} skipped by the model (see Explore)`);
  $("inbox-verdicts").textContent = shown.join(" · ");
}

const INBOX_EMPTY = {
  none: "Nothing left to review. Add jobs, or look through Explore for what the rules removed.",
  maybe: "Nothing marked Maybe.",
  applied: "Nothing marked Applied yet.",
  skipped: "Nothing skipped yet.",
};

function renderInboxList() {
  const more = inbox.total > inbox.rows.length
    ? `<p class="note hint inbox-more">Showing the first ${inbox.rows.length.toLocaleString()} of ${inbox.total.toLocaleString()}.</p>`
    : "";
  $("inbox-list").innerHTML = inbox.rows.length
    ? inbox.rows.map((p) => `
        <button class="inbox-item ${p.closed_at ? "is-closed" : ""}" role="option" data-id="${p.posting_id}"
                aria-selected="${p.posting_id === inbox.selected}">
          <span class="inbox-title">${esc(p.title || "(untitled)")}${deadlineTag(p)}</span>
          <span class="inbox-meta">${esc(p.company_name || p.company_slug)} · ${esc(firstPlace(p.location))}${
            postedAgo(p) ? ` · posted ${esc(postedAgo(p))}` : ""}${
            inbox.tab === "applied" ? ` · applied ${esc(when(p.app_updated_at))}${p.outcome ? ` · ${STEP_NAMES[p.outcome]}` : ""}` : ""}</span>
          ${p.verdict || (p.label && p.label !== "OPEN") ? `<span class="inbox-tags">
            ${verdictBadge(p.verdict)}${p.label && p.label !== "OPEN" ? badge(p.label) : ""}
            ${p.verdict_reason ? `<span class="inbox-why">${esc(p.verdict_reason)}</span>` : ""}
          </span>` : ""}
          ${skillsOf(p).length ? `<span class="inbox-skills">${esc(skillsOf(p).slice(0, 6).join(" · "))}</span>` : ""}
        </button>`).join("") + more
    : `<p class="empty">${INBOX_EMPTY[inbox.tab]}</p>`;
}

function markSelection() {
  for (const item of $("inbox-list").querySelectorAll(".inbox-item")) {
    const selected = Number(item.dataset.id) === inbox.selected;
    item.setAttribute("aria-selected", selected);
    if (selected) item.scrollIntoView({ block: "nearest" });
  }
}

// Only the latest request draws: arrowing down the list fires many.
let readerRequest = 0;

async function showInReader(id) {
  const reader = $("reader");
  if (id == null) {
    inbox.shown = null;
    reader.innerHTML = `<p class="empty">Select a posting to read it here.</p>`;
    return;
  }
  const ticket = ++readerRequest;
  const { posting } = await getJSON(`/api/postings/${id}`);
  if (ticket !== readerRequest) return;

  const { mine, facts, description } = detailParts(posting);
  const meta = [
    posting.company_name || posting.company_slug,
    posting.location || "location unknown",
    postedLine(posting),
    posting.applicants,
  ].filter(Boolean).join(" · ");
  reader.innerHTML = `
    <header class="reader-head">
      <div>
        <h3>${esc(posting.title || "(untitled)")}</h3>
        <p class="note">${esc(meta)}</p>
      </div>
      <a class="open-posting" href="${esc(posting.url)}" target="_blank" rel="noopener">Open posting ↗</a>
    </header>
    <div class="reader-body">
      <div class="reader-text">${description}</div>
      <aside class="reader-side">${mine}${facts}</aside>
    </div>`;
  // A redraw of the same posting (a saved note) keeps your place in it.
  if (inbox.shown !== id) reader.scrollTop = 0;
  inbox.shown = id;
}

function selectPosting(id) {
  inbox.selected = id;
  inbox.index = Math.max(0, inbox.rows.findIndex((p) => p.posting_id === id));
  markSelection();
  return showInReader(id);
}

// Responses can land out of order; only the latest one renders.
let inboxRequest = 0;

async function loadInbox() {
  const ticket = ++inboxRequest;
  const params = new URLSearchParams({ stage: "model", app: inbox.tab, limit: INBOX_LIMIT });
  // Still to act on: what is about to close comes first.
  if (inbox.tab === "none" || inbox.tab === "maybe") params.set("order", "deadline");
  // Everything the model did not skip. What it skipped is on Explore.
  for (const verdict of WORTH_READING) params.append("verdict", verdict);
  const page = await getJSON(`/api/postings?${params}`);
  if (ticket !== inboxRequest) return;

  // Closing soon first, then best fits, fits, stretches and what still needs
  // reading. Within each, the postings that name more of your own skills lead;
  // then the order the server sent (soonest deadline, then newest).
  const urgent = (p) => (inbox.tab !== "applied" && closingSoon(p) ? 0 : 1);
  const rank = (p) => WORTH_READING.indexOf(p.verdict || "unread");
  const byFit = (a, b) => urgent(a.p) - urgent(b.p) || rank(a.p) - rank(b.p) ||
    (b.p.match_score || 0) - (a.p.match_score || 0) || a.at - b.at;
  // Applied is a log of what you sent: the one you applied to last is on top.
  const byMark = (a, b) => (b.p.app_updated_at || "").localeCompare(a.p.app_updated_at || "") || a.at - b.at;
  inbox.rows = page.postings
    .map((p, at) => ({ p, at }))
    .sort(inbox.tab === "applied" ? byMark : byFit)
    .map(({ p }) => p);
  renderInboxVerdicts(page.verdict_counts);
  inbox.counts = page.app_counts;
  inbox.total = page.total;

  // The selected posting may have left this tab (you just marked it): take
  // the one that moved into its place, so marking walks down the list.
  if (!inbox.rows.some((p) => p.posting_id === inbox.selected)) {
    const next = inbox.rows[Math.min(inbox.index, inbox.rows.length - 1)];
    inbox.selected = next ? next.posting_id : null;
  }

  renderInboxTabs();
  renderInboxList();
  await selectPosting(inbox.selected);
}

function moveSelection(step) {
  if (!inbox.rows.length) return;
  const at = inbox.rows.findIndex((p) => p.posting_id === inbox.selected);
  const next = inbox.rows[Math.min(Math.max(at + step, 0), inbox.rows.length - 1)];
  if (next.posting_id !== inbox.selected) selectPosting(next.posting_id).catch(showError);
}

// Pressing the mark a posting already has clears it, like the buttons.
function markSelected(status) {
  const row = inbox.rows.find((p) => p.posting_id === inbox.selected);
  if (!row) return;
  const clearing = row.app_status === status;
  if (clearing && hasTrail(row) && !confirmClear()) return;
  setMark(row.posting_id, clearing ? null : status)
    .then(loadInbox)
    .catch(showError);
}

$("inbox-tabs").addEventListener("click", (event) => {
  const tab = event.target.closest(".tab");
  if (!tab || tab.dataset.tab === inbox.tab) return;
  inbox.tab = tab.dataset.tab;
  inbox.index = 0;
  try {
    localStorage.setItem(INBOX_SAVED, JSON.stringify({ tab: inbox.tab }));
  } catch { /* not persisted this time */ }
  loadInbox().catch(showError);
});

$("inbox-list").addEventListener("click", (event) => {
  const item = event.target.closest(".inbox-item");
  if (item) selectPosting(Number(item.dataset.id)).catch(showError);
});

wireMineBox($("reader"), loadInbox);

document.addEventListener("keydown", (event) => {
  if (currentPage() !== "inbox" || $("detail").open) return;
  if (event.metaKey || event.ctrlKey || event.altKey) return;
  if (["INPUT", "TEXTAREA", "SELECT"].includes(event.target.tagName)) return;

  const key = event.key.toLowerCase();
  if (key === "j" || key === "arrowdown") moveSelection(1);
  else if (key === "k" || key === "arrowup") moveSelection(-1);
  else if (KEY_MARKS[key]) markSelected(KEY_MARKS[key]);
  else if (key === "o") {
    const row = inbox.rows.find((p) => p.posting_id === inbox.selected);
    if (row) window.open(row.url, "_blank", "noopener");
  } else return;
  event.preventDefault();
});

// ---- new postings: screen them, then have the model read them -----------------

// The server does both in the background; the page starts it and polls. The
// bar shows on every page whenever postings are waiting or a run is under way.
const PROCESS_POLL_MS = 1000;
let processTimer;

function postingsWord(n) {
  return `${n.toLocaleString()} posting${n === 1 ? "" : "s"}`;
}

function describeProcess({ process: run, waiting, can_read: canRead }) {
  if (run.running) {
    if (run.phase !== "reading") return "Screening postings against your role rules…";
    if (!run.total) return "Screened. The model is starting to read…";
    const each = run.seconds_each ? ` · ${run.seconds_each}s each` : "";
    return `The model is reading ${run.done} of ${run.total}${each}…`;
  }
  const parts = [];
  if (run.error) parts.push(`Could not finish: ${run.error}.`);
  else if (run.stopped) parts.push(`The model stopped answering (${run.stopped}). Is Ollama running?`);
  else if (run.finished_at && run.read) parts.push(`The model read ${postingsWord(run.read)}.`);
  if (waiting.unscreened) parts.push(`${postingsWord(waiting.unscreened)} not screened yet.`);
  if (waiting.unread && canRead) {
    parts.push(`${postingsWord(waiting.unread)} passed the rules and ${waiting.unread === 1 ? "is" : "are"} waiting for the model.`);
  }
  return parts.join(" ");
}

function renderProcess(state) {
  const { process: run, waiting, can_read: canRead } = state;
  const text = describeProcess(state);
  const pending = waiting.unscreened > 0 || (canRead && waiting.unread > 0);
  $("process-bar").hidden = !text;
  $("process-bar").classList.toggle("is-error", Boolean(run.error || run.stopped) && !run.running);
  $("process-text").textContent = text;
  $("process-run").hidden = run.running || !pending;
  $("process-run").textContent = canRead ? "Screen and read" : "Screen";
  return pending;
}

// `reload`: the tables are stale once the run being watched finishes.
// Resolves to whether postings are still waiting.
async function pollProcess(reload = false) {
  clearTimeout(processTimer);
  const state = await getJSON("/api/process");
  const pending = renderProcess(state);
  if (state.process.running) {
    processTimer = setTimeout(() => pollProcess(true).catch(showError), PROCESS_POLL_MS);
    return pending;
  }
  if (reload) {
    await refresh();
    if (currentPage() === "inbox") await loadInbox();
  }
  return pending;
}

async function startProcess() {
  $("process-run").hidden = true;
  try {
    renderProcess(await postJSON("/api/process", {}));
  } catch {
    // Most likely one is already running (another tab): follow it.
  }
  await pollProcess(true);
}

// After adding jobs or refreshing a board: screen and read whatever is new.
async function processNewPostings() {
  if (await pollProcess()) await startProcess();
}

$("process-run").addEventListener("click", () => startProcess().catch(showError));

showPage();
// The sidebar's count of applications, whichever page opens first.
if (currentPage() !== "applications") loadApplications().catch(showError);
// A run may be under way already (another tab, or before a reload).
pollProcess().catch(showError);
