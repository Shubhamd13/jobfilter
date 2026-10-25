"use strict";

// Label -> status role. The badge always carries its text, so the color
// reinforces the label rather than carrying it.
const SEVERITY = {
  RESTRICTED_CLEARANCE: "critical",
  RESTRICTED_CITIZEN: "critical",
  RESTRICTED_US_PERSON: "serious",
  UNCLEAR: "warning",
  INFO_NO_SPONSORSHIP: "",
  OPEN: "good",
};

const $ = (id) => document.getElementById(id);

const PAGE_SIZES = [50, 100, 200, 500];
const SORTS = {
  newest: "Last checked",
  posted: "Newest posted",
  posted_asc: "Oldest posted",
  deadline: "Deadline soonest",
};
// Clicking the Posted header: newest first, then oldest first, then back to
// the order postings were checked in.
const POSTED_CYCLE = { posted: "posted_asc", posted_asc: "newest" };
const POSTED_WITHIN = [3, 7, 14, 30];

// Company filters hold slugs (what the API filters on); place filters hold
// text matched anywhere in a location, case-insensitively.
const state = {
  q: "",
  labels: new Set(),
  regions: new Set(),
  only: [],
  hidden: [],
  places: [],
  hiddenPlaces: [],
  changed: false, // only postings their company's latest board refresh changed
  closing: false, // only open postings whose apply-by date is near
  posted: null, // only postings that went up in the last this many days
  sort: "newest", // newest (last checked), posted (newest posting) or deadline
  statuses: new Set(), // your marks: applied / skipped / maybe, "none" for unmarked
  stages: new Set(), // where the role funnel stopped a posting
  verdicts: new Set(), // what the model made of a posting that passed the rules
  offset: 0,
  limit: 100,
  companySort: [], // set with its defaults under "company table sort"
};
let labelOrder = [];
// How near an apply-by date makes a posting "closing soon"; the server says.
let closingSoonDays = 7;
let regionOrder = [];
let statusOrder = [];
let stageOrder = [];
let verdictOrder = [];
let companies = new Map();
let places = [];
let resumes = []; // every resume you uploaded, newest first
let resumeResults = {}; // resume id ("none": sent without one) -> what came of its applications

const REGION_NAMES = {
  us: "United States",
  outside_us: "Outside the US",
  unknown: "Unknown / remote only",
};

// Your marks. "none" is a posting you have not marked: the ones left to read.
const STATUS_NAMES = { none: "To review", maybe: "Maybe", applied: "Applied", skipped: "Skipped" };
const DEGREE_NAMES = { none: "none", bachelors: "BS", masters: "MS", phd: "PhD" };
const MARKS = [
  { status: "applied", text: "Applied", title: "I applied to this one" },
  { status: "maybe", text: "Maybe", title: "Come back to this one" },
  { status: "skipped", text: "Skip", title: "Read it, not applying" },
];
// What came of an application, in the order the steps usually come.
const STEPS = [
  { stage: "oa", text: "OA", title: "They sent an online assessment" },
  { stage: "interview", text: "Interview", title: "They asked for an interview" },
  { stage: "offer", text: "Offer", title: "They made an offer" },
  { stage: "rejected", text: "Rejected", title: "They said no" },
];
const STEP_NAMES = Object.fromEntries(STEPS.map((s) => [s.stage, s.text]));

// Where the role funnel (jobfilter.roles) stopped a posting.
const STAGE_NAMES = {
  model: "Passed the rules",
  mech_elec: "Mechanical / electrical",
  off_focus: "Not robotics",
  seniority: "Seniority",
  non_engineering: "Non-engineering",
  restricted: "Restricted label",
  location: "Outside your locations",
  unscreened: "Not screened yet",
};

// What the model made of a posting that passed the rules. "unread": not read yet.
const VERDICT_NAMES = {
  best: "Best fit",
  fit: "Fit",
  stretch: "Stretch",
  unknown: "Needs a look",
  unread: "Not read yet",
  skip: "Skip",
};
const VERDICT_TONE = { best: "best", fit: "good", stretch: "warning", unknown: "serious", skip: "" };
// Everything but what the model skipped: what "For me" and the Inbox show.
const WORTH_READING = ["best", "fit", "stretch", "unknown", "unread"];

// A model verdict as a badge; nothing for a posting no model has read.
function verdictBadge(verdict) {
  if (!verdict) return "";
  return `<span class="badge verdict ${VERDICT_TONE[verdict] ?? ""}">${esc(VERDICT_NAMES[verdict] || verdict)}</span>`;
}

// Your own skills a posting mentions (roles.match), heaviest first.
function skillsOf(posting) {
  try {
    const skills = JSON.parse(posting.match_skills || "[]");
    return Array.isArray(skills) ? skills : [];
  } catch {
    return [];
  }
}

// What a model read in the posting, or null.
function factsOf(posting) {
  try {
    return JSON.parse(posting.facts_json || "null");
  } catch {
    return null;
  }
}

// Sets that outlive a reload, saved under their own state key.
const SAVED_SETS = ["regions", "statuses", "stages", "verdicts"];

// Hidden companies, location choices, page size and the company table's sort
// outlive a reload: they are standing preferences, not one-off searches.
// Storage can be missing or throw (private windows, blocked site data), so
// every access is guarded.
const SAVED = "jobfilter.filters";
const SAVED_LISTS = ["hidden", "places", "hiddenPlaces"];

function loadSaved() {
  try {
    const saved = JSON.parse(localStorage.getItem(SAVED) || "{}");
    for (const key of SAVED_LISTS) {
      if (Array.isArray(saved[key])) state[key] = saved[key].filter((s) => typeof s === "string");
    }
    // Refill the Sets rather than replacing them: state's Sets keep one identity.
    for (const key of SAVED_SETS) {
      if (!Array.isArray(saved[key])) continue;
      for (const value of saved[key]) if (typeof value === "string") state[key].add(value);
    }
    if (PAGE_SIZES.includes(saved.limit)) state.limit = saved.limit;
    if (saved.sort in SORTS) state.sort = saved.sort;
    if (validCompanySort(saved.companySort)) state.companySort = saved.companySort;
  } catch { /* nothing saved */ }
}

function save() {
  try {
    const saved = { limit: state.limit, sort: state.sort, companySort: state.companySort };
    for (const key of SAVED_SETS) saved[key] = [...state[key]];
    for (const key of SAVED_LISTS) saved[key] = state[key];
    localStorage.setItem(SAVED, JSON.stringify(saved));
  } catch { /* not persisted this time */ }
}

function esc(value) {
  return String(value ?? "").replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

// A URL that never reached a classification has a status, not a label.
const STATUS_TONE = { unsupported: "warning", fetch_error: "critical" };

function badge(label) {
  if (!label) return '<span class="badge">unclassified</span>';
  return `<span class="badge ${SEVERITY[label] ?? ""}">${esc(label)}</span>`;
}

function statusBadge(status) {
  return `<span class="badge ${STATUS_TONE[status] ?? ""}">${esc(status)}</span>`;
}

function when(iso) {
  if (!iso) return "—";
  const then = new Date(iso);
  if (Number.isNaN(then.getTime())) return "—";
  const mins = Math.round((Date.now() - then.getTime()) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins}m ago`;
  if (mins < 1440) return `${Math.round(mins / 60)}h ago`;
  if (mins < 43200) return `${Math.round(mins / 1440)}d ago`;
  return then.toISOString().slice(0, 10);
}

async function getJSON(url) {
  const response = await fetch(url);
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  return payload;
}

// ---- rendering -----------------------------------------------------------

function renderKpis(stats) {
  const tiles = [
    { label: "Postings", value: stats.postings },
    { label: "Restricted", value: stats.restricted, tone: "is-critical" },
    { label: "Needs review", value: stats.labels.UNCLEAR },
    { label: "Open", value: stats.labels.OPEN },
    { label: "Companies", value: stats.companies },
  ];
  $("kpis").innerHTML = tiles.map((tile) => `
    <div class="tile ${tile.tone || ""}">
      <div class="label">${esc(tile.label)}</div>
      <div class="value">${esc(tile.value)}</div>
    </div>`).join("");
}

// ---- company table sort ----------------------------------------------------

// Column key -> how it compares. Numbers start descending (biggest first),
// text ascending, on the first click.
const COMPANY_COLUMNS = {
  company_slug: "text",
  platform: "text",
  total: "num",
  restricted: "num",
  unclear: "num",
  info: "num",
  open_count: "num",
  changed: "num",
  refreshed: "text",
};
// The order the server sends, spelled out so the headers can show it.
const DEFAULT_COMPANY_SORT = [{ key: "restricted", dir: "desc" }, { key: "total", dir: "desc" }];

const flip = (dir) => (dir === "asc" ? "desc" : "asc");
const firstDir = (key) => (COMPANY_COLUMNS[key] === "num" ? "desc" : "asc");
const byText = new Intl.Collator(undefined, { sensitivity: "base", numeric: true });

state.companySort = DEFAULT_COMPANY_SORT;

// A saved sort comes from storage, so it is checked before it is trusted.
function validCompanySort(sort) {
  return Array.isArray(sort) && sort.length > 0 &&
    sort.every((s) => s && s.key in COMPANY_COLUMNS && (s.dir === "asc" || s.dir === "desc")) &&
    new Set(sort.map((s) => s.key)).size === sort.length;
}

function isDefaultSort(sort) {
  return JSON.stringify(sort) === JSON.stringify(DEFAULT_COMPANY_SORT);
}

// Plain click: sort by this column alone, or flip it if it already leads.
// Shift-click: add it as the next tie-breaker; again flips it; a third time
// drops it.
function nextCompanySort(sort, key, add) {
  const at = sort.findIndex((s) => s.key === key);
  if (!add) {
    if (at === 0) return [{ key, dir: flip(sort[0].dir) }, ...sort.slice(1)];
    return [{ key, dir: firstDir(key) }];
  }
  if (at < 0) return [...sort, { key, dir: firstDir(key) }];
  if (sort[at].dir === firstDir(key)) {
    return sort.map((s, i) => (i === at ? { key, dir: flip(s.dir) } : s));
  }
  const rest = sort.filter((_, i) => i !== at);
  return rest.length ? rest : DEFAULT_COMPANY_SORT;
}

// Slug last, so equal rows keep a stable order across re-renders.
function sortCompanies(rows, sort) {
  const keys = [...sort, { key: "company_slug", dir: "asc" }];
  return [...rows].sort((a, b) => {
    for (const { key, dir } of keys) {
      const order = COMPANY_COLUMNS[key] === "num"
        ? (a[key] ?? 0) - (b[key] ?? 0)
        : byText.compare(String(a[key] ?? ""), String(b[key] ?? ""));
      if (order) return dir === "asc" ? order : -order;
    }
    return 0;
  });
}

function renderCompanySortMarks() {
  const sort = state.companySort;
  for (const th of $("company-head").querySelectorAll("th[data-key]")) {
    const at = sort.findIndex((s) => s.key === th.dataset.key);
    const mark = th.querySelector(".sort-mark");
    if (at < 0) {
      delete th.dataset.dir;
      th.removeAttribute("aria-sort");
      mark.innerHTML = "";
      continue;
    }
    const { dir } = sort[at];
    th.dataset.dir = dir;
    // aria-sort belongs on one column only: the one that leads.
    if (at === 0) th.setAttribute("aria-sort", dir === "asc" ? "ascending" : "descending");
    else th.removeAttribute("aria-sort");
    mark.innerHTML = (dir === "asc" ? "▲" : "▼") + (sort.length > 1 ? `<sup>${at + 1}</sup>` : "");
  }
  $("company-sort-reset").hidden = isDefaultSort(sort);
}

let companyRows = [];
// True while a board refresh runs: its buttons are disabled meanwhile.
let refreshBusy = false;

// What a refresh can find, in the order it is read out.
const CHANGE_KINDS = ["new", "closed", "reopened", "edited", "relabeled"];

// {new: 3, closed: 2} -> "+3 new · 2 closed"
function changeSummary(changes, separator = " · ") {
  return CHANGE_KINDS
    .filter((kind) => changes[kind])
    .map((kind) => `${kind === "new" ? "+" : ""}${changes[kind].toLocaleString()} ${kind}`)
    .join(separator);
}

// The company table sorts on flat keys: lift them out of `refresh`.
function withRefreshKeys(rows) {
  return rows.map((c) => ({
    ...c,
    changed: c.refresh?.changed ?? 0,
    refreshed: c.refresh?.at ?? "",
  }));
}

function changesCell(c) {
  if (!c.refreshable) return `<span class="none" title="No job board to re-read">—</span>`;
  if (!c.refresh) return `<span class="none">not refreshed yet</span>`;
  if (c.refresh.status !== "ok") {
    return `<span class="badge critical" title="${esc(c.refresh.error)}">could not be read</span>`;
  }
  if (!c.refresh.changed) return `<span class="none">no change</span>`;
  return `<button class="changes-link" data-slug="${esc(c.company_slug)}"
                  title="Show these postings">${esc(changeSummary(c.refresh.changes))}</button>`;
}

function refreshCell(c) {
  if (!c.refreshable) return "";
  return `<button class="ghost refresh-one" data-platform="${esc(c.platform)}" data-slug="${esc(c.company_slug)}"
                  title="Refresh this company's board" aria-label="Refresh ${esc(c.company_name || c.company_slug)}"
                  ${refreshBusy ? "disabled" : ""}>↻</button>`;
}

function renderCompanies(rows = companyRows) {
  companyRows = rows;
  renderCompanySortMarks();
  const companies = sortCompanies(rows, state.companySort);
  $("company-rows").innerHTML = companies.length
    ? companies.map((c) => `
        <tr>
          <td class="company">${esc(c.company_slug)}</td>
          <td class="company">${esc(c.platform)}</td>
          <td class="num">${c.total}</td>
          <td class="num">${c.restricted}</td>
          <td class="num">${c.unclear}</td>
          <td class="num">${c.info}</td>
          <td class="num">${c.open_count}</td>
          <td class="changes">${changesCell(c)}</td>
          <td class="when">${esc(when(c.refresh?.at))}</td>
          <td class="refresh-cell">${refreshCell(c)}</td>
        </tr>`).join("")
    : `<tr><td colspan="10" class="empty">Nothing checked yet.</td></tr>`;
}

function renderFailures(failures) {
  $("failure-rows").innerHTML = failures.length
    ? failures.map((f) => `
        <tr>
          <td class="evidence">${esc(f.url_as_given)}</td>
          <td>${statusBadge(f.status)}</td>
          <td class="evidence">${esc(f.error_type)}: ${esc(f.error)}</td>
          <td class="when">${esc(when(f.created_at))}</td>
        </tr>`).join("")
    : `<tr><td colspan="4" class="empty">Every URL checked so far was fetched successfully.</td></tr>`;
}

// How a posting reached the database: you pasted it, board expansion found
// it, or the browser extension saw it while you were browsing.
const ORIGINS = { requested: "you", discovered: "auto", browsed: "browser" };

// "Austin, TX; South San Francisco, CA; London" -> "Austin, TX +2".
function locationCell(location) {
  const parts = String(location || "").split(";").map((s) => s.trim()).filter(Boolean);
  if (!parts.length) return "—";
  const more = parts.length > 1 ? ` <span class="more">+${parts.length - 1}</span>` : "";
  return `<span title="${esc(parts.join("\n"))}">${esc(parts[0])}</span>${more}`;
}

// What the company's latest refresh found on a posting, plus "closed" for
// as long as its board does not list it.
function changeTags(p) {
  const kinds = String(p.changes || "").split(",").filter(Boolean);
  if (p.closed_at && !kinds.includes("closed")) kinds.push("closed");
  return kinds
    .map((kind) => `<span class="change-tag ${kind === "closed" ? "closed" : ""}">${esc(kind)}</span>`)
    .join("");
}

// The steps an application reached, oldest first: [{ stage, at }].
function stepsOf(posting) {
  return String(posting.outcome_steps || "").split(",").filter(Boolean).map((step) => {
    const cut = step.indexOf(":");
    return { stage: step.slice(0, cut), at: step.slice(cut + 1) };
  });
}

// An applied posting with steps or a resume: clearing its mark loses them.
function hasTrail(posting) {
  return posting.app_status === "applied" && Boolean(posting.outcome_steps || posting.resume_id);
}

function confirmClear() {
  return confirm("Clearing Applied also forgets its steps and resume. Clear it?");
}

// Applied / Maybe / Skip. The one that is set is pressed; pressing it again clears it.
function markButtons(posting) {
  const { posting_id: id, app_status: status } = posting;
  return `<span class="marks" role="group" aria-label="Mark this posting">${MARKS.map((m) => `
    <button class="mark ${status === m.status ? "on" : ""}" data-id="${id}" data-status="${m.status}"
            ${status === m.status && hasTrail(posting) ? "data-trail" : ""}
            title="${m.title}" aria-pressed="${status === m.status}">${m.text}</button>`).join("")}</span>`;
}

// OA / Interview / Offer / Rejected, for a posting marked applied. A step that
// was reached is pressed and dated; pressing it again takes it back.
function stepButtons(posting) {
  const reached = new Map(stepsOf(posting).map((s) => [s.stage, s.at]));
  return `<span class="marks steps" role="group" aria-label="What came of it">${STEPS.map((s) => `
    <button class="mark step ${reached.has(s.stage) ? "on" : ""}" data-id="${posting.posting_id}" data-stage="${s.stage}"
            title="${s.title}" aria-pressed="${reached.has(s.stage)}">${s.text}${
              reached.has(s.stage) ? ` <span class="step-when">${esc(when(reached.get(s.stage)))}</span>` : ""}</button>`).join("")}</span>`;
}

async function setStep(id, stage, on) {
  await postJSON(`/api/postings/${id}/outcome`, { stage, on });
  await loadPostings();
}

// ---- resumes -------------------------------------------------------------

const MAX_RESUME_BYTES = 5_000_000; // the server's limit, checked here first

async function loadResumes() {
  ({ resumes, results: resumeResults = {} } = await getJSON("/api/resumes"));
}

// The file goes up as the request body itself. The same file uploaded twice
// is one resume: the server answers with the one it already has.
async function uploadResume(file) {
  if (file.size > MAX_RESUME_BYTES) throw new Error(`${file.name} is larger than 5 MB.`);
  const response = await fetch(`/api/resumes?${new URLSearchParams({ name: file.name })}`, {
    method: "POST",
    headers: { "Content-Type": "application/octet-stream" },
    body: file,
  });
  const payload = await response.json();
  if (!response.ok) throw new Error(payload.error || response.statusText);
  await loadResumes();
  return payload.resume;
}

// Attach a resume to an applied posting, or detach it with null.
async function setResume(id, resumeId) {
  await postJSON(`/api/postings/${id}/application`, { status: "applied", resume_id: resumeId });
  await loadPostings();
}

// Which resume you sent, for a posting marked applied: pick one you uploaded
// before, or upload the file.
function resumeRow(posting) {
  // The attached one is listed even before the list of resumes has loaded.
  const listed = resumes.some((r) => r.id === posting.resume_id) || !posting.resume_id
    ? resumes
    : [{ id: posting.resume_id, name: posting.resume_name }, ...resumes];
  return `
    <div class="row resume-row">
      <label class="note">Resume
        <select class="resume-pick" data-id="${posting.posting_id}">
          <option value="">none attached</option>
          ${listed.map((r) => `<option value="${r.id}" ${r.id === posting.resume_id ? "selected" : ""}>${esc(r.name)}</option>`).join("")}
        </select>
      </label>
      <button class="ghost resume-upload" title="Upload the PDF or DOCX you sent">Upload…</button>
      <input type="file" class="resume-file" accept=".pdf,.docx" hidden>
      ${posting.resume_id ? `<a class="resume-open" href="/api/resumes/${posting.resume_id}/file" target="_blank" rel="noopener">Open ↗</a>` : ""}
    </div>
    <p class="note error mine-error" role="alert" hidden></p>`;
}

// `note` is sent only when given, so marking from the table keeps a note.
async function setMark(id, status, note) {
  await postJSON(`/api/postings/${id}/application`, note === undefined ? { status } : { status, note });
  await loadPostings();
}

// One handler for the table and the detail dialog: a pressed mark clears.
function onMarkClick(event, after) {
  const button = event.target.closest("button.mark:not(.step)");
  if (!button) return false;
  event.stopPropagation();
  const status = button.classList.contains("on") ? null : button.dataset.status;
  if (status === null && "trail" in button.dataset && !confirmClear()) return true;
  setMark(button.dataset.id, status).then(after).catch(showError);
  return true;
}

// ---- apply-by dates ------------------------------------------------------

const DAY_MS = 86400000;

// Days from today (local) to a YYYY-MM-DD day: negative in the past.
function daysFromToday(day) {
  const [y, m, d] = day.split("-").map(Number);
  const now = new Date();
  const today = new Date(now.getFullYear(), now.getMonth(), now.getDate());
  return Math.round((new Date(y, m - 1, d) - today) / DAY_MS);
}

function formatDay(day, options = { month: "short", day: "numeric" }) {
  const [y, m, d] = day.split("-").map(Number);
  return new Date(y, m - 1, d).toLocaleDateString(undefined, options);
}

// Days from today to a posting's apply-by date, or null without one.
function daysLeft(p) {
  return p.apply_by ? daysFromToday(p.apply_by) : null;
}

function applyByDate(p, options) {
  return formatDay(p.apply_by, options);
}

// ---- posting dates ---------------------------------------------------------

// "today", "3 days ago", "Aug 5"; "~" when counted back from "2 weeks ago".
function postedAgo(p) {
  if (!p.posted_on) return null;
  const ago = -daysFromToday(p.posted_on);
  const text = ago <= 0 ? "today" : ago === 1 ? "yesterday" : ago < 60 ? `${ago} days ago`
    : formatDay(p.posted_on, { month: "short", day: "numeric", year: "numeric" });
  return (p.posted_on_approx ? "~" : "") + text;
}

// The header line's "Posted Sep 30 (16 days ago)", or the board's own text.
function postedLine(p) {
  if (!p.posted_on) return p.posted_ago || null;
  const day = formatDay(p.posted_on, { month: "short", day: "numeric", year: "numeric" });
  return `Posted ${p.posted_on_approx ? "~" : ""}${day} (${postedAgo(p).replace("~", "")})`;
}

// What to say about a posting's apply-by date in a list, or null. A firm date
// counts down; a minimum ("accepted at least until") also warns for a while
// after, since the posting can come down any day once it is past. Matches the
// server's closing-soon filter (store.closing_soon_sql).
function deadlineNote(p) {
  const days = daysLeft(p);
  if (days == null || p.closed_at) return null;
  if (days < 0) {
    if (p.apply_by_firm !== 0) return { text: `deadline passed ${applyByDate(p)}`, tone: "past" };
    if (days >= -closingSoonDays) {
      return { text: `past ${applyByDate(p)} · may close any day`, tone: "critical", soon: true };
    }
    return null;
  }
  if (days > closingSoonDays) return null;
  const left = days === 0 ? "today" : days === 1 ? "tomorrow" : `${applyByDate(p)} · ${days} days`;
  return { text: `apply by ${left}`, tone: days <= 2 ? "critical" : "warning", soon: true };
}

function closingSoon(p) {
  return Boolean(deadlineNote(p)?.soon);
}

function deadlineTag(p) {
  const note = deadlineNote(p);
  if (!note) return "";
  return `<span class="deadline-tag ${note.tone}" title="${esc(p.apply_by_text || "")}">${esc(note.text)}</span>`;
}

// The detail pane's line: the date, how far off, and the sentence it rests on.
function deadlineDetail(p) {
  if (!p.apply_by) return "";
  const days = daysLeft(p);
  const relative = days === 0 ? "today" : days === 1 ? "tomorrow" : days === -1 ? "yesterday"
    : days > 0 ? `in ${days} days` : `${-days} days ago`;
  const minimum = p.apply_by_firm === 0
    ? " — a minimum: it may stay up longer, or come down any day after" : "";
  return `<dt>Apply by</dt><dd>${esc(applyByDate(p, { month: "short", day: "numeric", year: "numeric" }))}
    (${esc(relative)})${esc(minimum)} ${deadlineTag(p)}
    <span class="note">“${esc(p.apply_by_text)}”</span></dd>`;
}

function renderPostings(postings) {
  $("rows").innerHTML = postings.length
    ? postings.map((p) => `
        <tr class="clickable ${p.closed_at ? "is-closed" : ""} ${p.app_status ? `is-${esc(p.app_status)}` : ""} ${closingSoon(p) ? "is-due" : ""}" data-id="${p.posting_id}">
          <td class="company">
            <span class="company-cell">
              ${esc(p.company_name || p.company_slug)}
              <button class="hide-company" data-slug="${esc(p.company_slug)}"
                      title="Hide this company" aria-label="Hide ${esc(p.company_name || p.company_slug)}">×</button>
            </span>
          </td>
          <td class="title">${esc(p.title || "—")}${changeTags(p)} ${verdictBadge(p.verdict)}${deadlineTag(p)}</td>
          <td class="location">${locationCell(p.location)}</td>
          <td>${badge(p.label)}</td>
          <td class="origin">${ORIGINS[p.origin] || "you"}</td>
          <td class="evidence">${esc(p.evidence || "—")}</td>
          <td class="when" title="${esc(p.posted_on || "")}">${esc(postedAgo(p) || "—")}</td>
          <td class="when">${esc(when(p.last_fetched))}</td>
          <td class="mine">${markButtons(p)}</td>
        </tr>`).join("")
    : `<tr><td colspan="9" class="empty">No postings match these filters.</td></tr>`;

  for (const row of $("rows").querySelectorAll("tr.clickable")) {
    row.addEventListener("click", (event) => {
      if (!onMarkClick(event)) openDetail(row.dataset.id);
    });
  }
  for (const button of $("rows").querySelectorAll(".hide-company")) {
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      addFilter("hidden", button.dataset.slug);
    });
  }
}

// ---- filter pane ---------------------------------------------------------

function companyName(slug) {
  return companies.get(slug)?.name || slug;
}

// The summary has one row per (company, platform); the pane wants one per
// company slug, since that is what the filters match on.
function indexCompanies(rows) {
  const index = new Map();
  for (const row of rows) {
    const entry = index.get(row.company_slug) ||
      { slug: row.company_slug, name: "", total: 0, restricted: 0 };
    entry.name ||= row.company_name || "";
    entry.total += row.total;
    entry.restricted += row.restricted;
    index.set(row.company_slug, entry);
  }
  return index;
}

// Built once, then updated in place: rebuilding on every load would drop
// keyboard focus and could swallow a click landing mid-rebuild.
function renderChecks(listId, order, picked, counts, labelOf) {
  const list = $(listId);
  if (list.dataset.order !== order.join(",")) {
    list.dataset.order = order.join(",");
    list.innerHTML = order.map((value) => `
      <label class="check-option">
        <input type="checkbox" value="${esc(value)}">
        ${labelOf(value)}
        <span class="count"></span>
      </label>`).join("");
  }
  for (const option of list.querySelectorAll(".check-option")) {
    const box = option.querySelector("input");
    box.checked = picked.has(box.value);
    option.querySelector(".count").textContent = (counts[box.value] ?? 0).toLocaleString();
  }
}

function renderFacets(page) {
  renderChecks("label-list", labelOrder, state.labels, page.label_counts, badge);
  renderChecks("region-list", regionOrder, state.regions, page.region_counts,
    (r) => `<span class="region-name">${esc(REGION_NAMES[r] || r)}</span>`);
  $("labels-clear").hidden = state.labels.size === 0;
  $("changed-only").checked = state.changed;
  $("closing-only").checked = state.closing;
  for (const input of document.querySelectorAll('input[name="posted-within"]')) {
    input.checked = input.value === String(state.posted ?? "");
  }
  $("closing-days").textContent = closingSoonDays;

  renderChecks("status-list", statusOrder, state.statuses, page.app_counts,
    (s) => `<span class="stage-name">${esc(STATUS_NAMES[s] || s)}</span>`);
  renderChecks("stage-list", stageOrder, state.stages, page.stage_counts,
    (s) => `<span class="stage-name">${esc(STAGE_NAMES[s] || s)}</span>`);
  renderChecks("verdict-list", verdictOrder, state.verdicts, page.verdict_counts,
    (v) => `<span class="stage-name">${esc(VERDICT_NAMES[v] || v)}</span>`);
  $("mine-clear").hidden = state.statuses.size + state.stages.size + state.verdicts.size === 0;
  if (!screenBusy) {
    const waiting = page.stage_counts.unscreened || 0;
    $("screen-status").className = "note hint";
    $("screen-status").textContent = waiting
      ? `${jobs(waiting)} not screened yet. Re-screen to place them.`
      : "Where the role rules stopped each posting. Edit profile.json, then re-screen.";
  }
}

// Each chip list: its state key, where it renders, and how a value reads.
const CHIP_LISTS = {
  only: { group: "only-group", chips: "only-chips", name: companyName },
  hidden: { group: "hidden-group", chips: "hidden-chips", name: companyName },
  places: { group: "place-group", chips: "place-chips", name: (v) => v },
  hiddenPlaces: { group: "hidden-place-group", chips: "hidden-place-chips", name: (v) => v },
};
// Adding a value to one list takes it out of its opposite.
const OPPOSITE = { only: "hidden", hidden: "only", places: "hiddenPlaces", hiddenPlaces: "places" };
const EXCLUDING = new Set(["hidden", "hiddenPlaces"]);

function renderChips() {
  for (const [key, list] of Object.entries(CHIP_LISTS)) {
    $(list.chips).innerHTML = state[key].map((value) => {
      const name = list.name(value);
      return `
        <span class="tag ${EXCLUDING.has(key) ? "excluded" : "included"}">
          <span class="tag-text" title="${esc(value)}">${esc(name)}</span>
          <button data-value="${esc(value)}" data-list="${key}" aria-label="Remove ${esc(name)}">×</button>
        </span>`;
    }).join("");
    $(list.group).hidden = state[key].length === 0;
  }

  const hiddenJobs = state.hidden.reduce((n, slug) => n + (companies.get(slug)?.total ?? 0), 0);
  $("hidden-count").textContent = hiddenJobs ? `· ${jobs(hiddenJobs)}` : "";
  $("companies-clear").hidden = state.only.length + state.hidden.length === 0;
  $("locations-clear").hidden =
    state.places.length + state.hiddenPlaces.length + state.regions.size === 0;
}

function addFilter(key, value) {
  value = String(value || "").trim();
  if (!value) return;
  state[OPPOSITE[key]] = state[OPPOSITE[key]].filter((v) => v !== value);
  if (!state[key].includes(value)) state[key].push(value);
  save();
  renderChips();
  filtersChanged();
}

function removeFilter(key, value) {
  state[key] = state[key].filter((v) => v !== value);
  save();
  renderChips();
  filtersChanged();
}

function jobs(n) {
  return `${n.toLocaleString()} job${n === 1 ? "" : "s"}`;
}

// Prefix matches first, then the most-listed.
function rankMatches(items, text, haystack) {
  const needle = text.trim().toLowerCase();
  return items
    .map((item) => {
      const hay = haystack(item).toLowerCase();
      const at = needle ? hay.indexOf(needle) : 0;
      return { item, at, rank: needle && at !== 0 && !hay.includes(` ${needle}`) ? 1 : 0 };
    })
    .filter((m) => m.at >= 0)
    .sort((a, b) => a.rank - b.rank || b.item.total - a.item.total)
    .map((m) => m.item);
}

// Typed place -> "3,004 jobs · every US location". Cached until the next refresh.
const placeInfo = new Map();

function describePlace(text) {
  const key = text.toLowerCase();
  if (!placeInfo.has(key)) {
    placeInfo.set(key, getJSON(`/api/place?${new URLSearchParams({ q: text })}`)
      .then((info) => `${jobs(info.total)} · ${info.meaning}`)
      .catch(() => ""));
  }
  return placeInfo.get(key);
}

// A searchable list under an input. `suggest(text)` returns options as
// { value, name, meta, lateMeta? }; lateMeta() resolves to a meta that
// replaces the first once it arrives. Picking one runs `pick(value, action)` where action
// is "include" or "exclude". `enterAction` is what plain Enter does.
function makeFinder({ input, list, suggest, pick, enterAction }) {
  let options = [];
  let active = -1;
  const other = enterAction === "include" ? "exclude" : "include";

  function render() {
    options = suggest(input.value).slice(0, 8);
    active = Math.min(active, options.length - 1);
    if (document.activeElement !== input) return close();

    list.innerHTML = options.length
      ? options.map((o, i) => `
          <li role="option" id="${list.id}-${i}" data-index="${i}"
              class="${i === active ? "active" : ""}" aria-selected="${i === active}">
            <span class="sugg-name">${esc(o.name)}</span>
            <span class="sugg-meta">${esc(o.meta)}</span>
            <span class="sugg-actions">
              <button class="mini" data-index="${i}" data-action="include">only</button>
              <button class="mini" data-index="${i}" data-action="exclude">hide</button>
            </span>
          </li>`).join("")
      : `<li class="sugg-empty">No matches</li>`;
    list.hidden = false;
    input.setAttribute("aria-expanded", "true");
    options.forEach((o, i) => {
      if (!o.lateMeta) return;
      o.lateMeta().then((meta) => {
        // Only if the list still shows this option.
        const row = document.getElementById(`${list.id}-${i}`);
        if (meta && row && options[i] === o) row.querySelector(".sugg-meta").textContent = meta;
      });
    });
    if (active >= 0) input.setAttribute("aria-activedescendant", `${list.id}-${active}`);
    else input.removeAttribute("aria-activedescendant");
  }

  function close() {
    list.hidden = true;
    input.setAttribute("aria-expanded", "false");
    active = -1;
  }

  function choose(index, action) {
    const option = options[index];
    if (!option) return;
    input.value = "";
    active = -1;
    pick(option.value, action);
    render();
  }

  input.addEventListener("input", () => { active = -1; render(); });
  input.addEventListener("focus", render);
  input.addEventListener("blur", () => setTimeout(close, 120));
  input.addEventListener("keydown", (event) => {
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!options.length) return;
      const step = event.key === "ArrowDown" ? 1 : -1;
      active = (active + step + options.length) % options.length;
      render();
    } else if (event.key === "Enter") {
      event.preventDefault();
      choose(Math.max(active, 0), event.shiftKey ? other : enterAction);
    } else if (event.key === "Escape") {
      close();
    }
  });
  // mousedown, not click: it fires before the input's blur closes the list.
  list.addEventListener("mousedown", (event) => {
    const target = event.target.closest("[data-index]");
    if (!target) return;
    event.preventDefault();
    choose(Number(target.dataset.index), target.dataset.action || enterAction);
  });
}

makeFinder({
  input: $("company-search"),
  list: $("company-suggestions"),
  enterAction: "exclude",
  suggest(text) {
    const taken = new Set([...state.only, ...state.hidden]);
    const free = [...companies.values()].filter((c) => !taken.has(c.slug));
    return rankMatches(free, text, (c) => `${c.name} ${c.slug}`).map((c) => ({
      value: c.slug,
      name: c.name || c.slug,
      meta: `${jobs(c.total)} · ${c.restricted.toLocaleString()} restricted`,
    }));
  },
  pick: (slug, action) => addFilter(action === "include" ? "only" : "hidden", slug),
});

makeFinder({
  input: $("place-search"),
  list: $("place-suggestions"),
  enterAction: "include",
  suggest(text) {
    const typed = text.trim();
    const needle = typed.toLowerCase();
    const taken = new Set([...state.places, ...state.hiddenPlaces].map((v) => v.toLowerCase()));
    // A place filter matches its text anywhere in a location, so the exact
    // place is the same filter as the typed text: offer it once, as that.
    const free = places.filter((p) => !taken.has(p.place.toLowerCase()) && p.place.toLowerCase() !== needle);
    const matches = rankMatches(free, typed, (p) => p.place).map((p) => ({
      value: p.place,
      name: p.place,
      meta: jobs(p.total),
    }));
    // The typed text first: the server decides what it means ("usa" is every
    // US location, "california" includes "San Francisco Bay Area", anything
    // else matches as text) and counts it.
    if (typed && !taken.has(needle)) {
      matches.unshift({
        value: typed,
        name: `“${typed}”`,
        meta: "counting…",
        lateMeta: () => describePlace(typed),
      });
    }
    return matches;
  },
  pick: (place, action) => addFilter(action === "include" ? "places" : "hiddenPlaces", place),
});

// ---- pager ---------------------------------------------------------------

// 1 … 4 5 [6] 7 8 … 20
function pageNumbers(current, last) {
  const pages = new Set([1, last]);
  for (let p = current - 2; p <= current + 2; p++) if (p >= 1 && p <= last) pages.add(p);
  const sorted = [...pages].sort((a, b) => a - b);
  const out = [];
  sorted.forEach((p, i) => {
    if (i && p - sorted[i - 1] > 1) out.push(null);
    out.push(p);
  });
  return out;
}

function renderPager(total) {
  const { offset, limit } = state;
  const last = Math.max(1, Math.ceil(total / limit));
  const current = Math.floor(offset / limit) + 1;
  const range = total
    ? `${(offset + 1).toLocaleString()}–${Math.min(offset + limit, total).toLocaleString()} of ${total.toLocaleString()}`
    : "No postings";

  const pageButtons = last > 1
    ? `<nav class="pages" aria-label="Pages">
         <button class="ghost page" data-page="${current - 1}" ${current === 1 ? "disabled" : ""} aria-label="Previous page">‹</button>
         ${pageNumbers(current, last).map((p) => p === null
           ? `<span class="gap">…</span>`
           : `<button class="ghost page ${p === current ? "current" : ""}" data-page="${p}"
                      ${p === current ? 'aria-current="page"' : ""}>${p}</button>`).join("")}
         <button class="ghost page" data-page="${current + 1}" ${current === last ? "disabled" : ""} aria-label="Next page">›</button>
       </nav>`
    : "";

  const html = `
    <span class="note range">${range}</span>
    ${pageButtons}
    <label class="note per-page">
      Sort
      <select data-role="sort">
        ${Object.entries(SORTS).map(([key, name]) => `<option value="${key}" ${key === state.sort ? "selected" : ""}>${name}</option>`).join("")}
      </select>
    </label>
    <label class="note per-page">
      Per page
      <select data-role="limit">
        ${PAGE_SIZES.map((n) => `<option value="${n}" ${n === limit ? "selected" : ""}>${n}</option>`).join("")}
      </select>
    </label>`;
  $("pager-top").innerHTML = html;
  $("pager-bottom").innerHTML = total > limit ? html : "";
}

// ---- detail --------------------------------------------------------------

// Keep the open posting in the query string so a detail view can be linked
// to and survives a reload.
function syncUrl(id) {
  const url = new URL(location.href);
  if (id) url.searchParams.set("posting", id);
  else url.searchParams.delete("posting");
  history.replaceState(null, "", url);
}

// Everything about one posting below its title, in three parts: `mine` (your
// mark, what came of it, the resume you sent and your note), `facts` (where the role funnel stopped it, the restriction
// verdict and its evidence) and `description`. The detail dialog stacks them;
// the Inbox reader puts the description beside the other two.
function detailParts(posting) {
  const rules = JSON.parse(posting.matched_rules_json || "[]");
  const history = posting.history.length > 1
    ? `<h2>Classification history</h2>
       <div class="table-scroll"><table><thead><tr>
         <th>When</th><th>Classifier</th><th>Label</th><th>Rule</th>
       </tr></thead><tbody>${posting.history.map((h) => `
         <tr>
           <td class="when">${esc(h.created_at.slice(0, 19).replace("T", " "))}</td>
           <td>${esc(h.classifier_name)}</td>
           <td>${badge(h.label)}</td>
           <td><span class="chip">${esc(h.matched_rule || "—")}</span></td>
         </tr>`).join("")}</tbody></table></div>`
    : "";

  const stage = posting.screen_stage || "unscreened";
  const screenWhy = [posting.screen_reason, posting.screen_evidence, posting.screen_focus_evidence]
    .filter(Boolean).join(" · ");

  const mine = `
    <div class="mine-box" data-id="${posting.posting_id}">
      <div class="row">
        ${markButtons(posting)}
        <span class="note">${posting.app_status ? `marked ${esc(when(posting.app_updated_at))}` : "not marked yet"}</span>
      </div>
      ${posting.app_status === "applied" ? `<div class="row">
        ${stepButtons(posting)}
        ${posting.outcome_steps ? "" : `<span class="note">waiting to hear back</span>`}
      </div>${resumeRow(posting)}` : ""}
      <textarea class="mine-note" placeholder="${posting.app_status ? "A note for yourself…" : "Mark it first to add a note"}"
                ${posting.app_status ? "" : "disabled"}>${esc(posting.app_note)}</textarea>
      <div class="row">
        <button class="ghost mine-note-save" data-status="${esc(posting.app_status)}" ${posting.app_status ? "" : "disabled"}>Save note</button>
      </div>
    </div>`;

  // What the model read, each fact with the sentence it rests on.
  const read = factsOf(posting);
  const degrees = (read?.degrees_accepted || []).map((d) => DEGREE_NAMES[d] || d).join(" / ");
  const model = posting.verdict || read ? `
      <dt>Model verdict</dt><dd>${posting.verdict ? `${verdictBadge(posting.verdict)} ${esc(posting.verdict_reason)}` : "read, no verdict: the profile has no “fit” section"}</dd>
      ${read?.min_years != null ? `<dt>Years asked</dt><dd>${read.min_years}+ <span class="note">“${esc(read.years_quote)}”</span></dd>` : ""}
      ${degrees ? `<dt>Degree</dt><dd>${esc(degrees)} <span class="note">“${esc(read.degree_quote)}”</span></dd>` : ""}
      ${read?.flags?.length ? `<dt>Unchecked</dt><dd class="note">${esc(read.flags.join(", "))}</dd>` : ""}
      <dt>Read by</dt><dd>${esc(posting.facts_reader || "—")}</dd>` : "";

  const mySkills = skillsOf(posting);
  const skillRow = mySkills.length ? `
      <dt>Your skills</dt><dd><div class="skills">${mySkills.map((s) => `<span class="chip">${esc(s)}</span>`).join("")}</div>
        <span class="note">match score ${esc(posting.match_score)}</span></dd>` : "";

  const facts = `
    <dl class="kv">
      ${deadlineDetail(posting)}
      <dt>Role screening</dt><dd>${esc(STAGE_NAMES[stage] || stage)}${screenWhy ? ` — ${esc(screenWhy)}` : ""}</dd>${model}${skillRow}
      <dt>Label</dt><dd>${badge(posting.label)}</dd>
      <dt>Confidence</dt><dd>${posting.confidence == null ? "—" : Number(posting.confidence).toFixed(2)}</dd>
      <dt>Classifier</dt><dd>${esc(posting.classifier_name || "—")}</dd>
      <dt>URL</dt><dd><a href="${esc(posting.url)}" target="_blank" rel="noopener">${esc(posting.url)}</a></dd>
      ${posting.posted_on ? `<dt>Posted</dt><dd>${esc(postedLine(posting).replace(/^Posted /, ""))}${posting.posted_on_approx ? ` <span class="note">counted back from “${esc(posting.posted_ago)}”</span>` : ""}</dd>` : ""}
      <dt>First seen</dt><dd>${esc(posting.first_seen.slice(0, 19).replace("T", " "))}</dd>
      <dt>Last fetched</dt><dd>${esc(posting.last_fetched.slice(0, 19).replace("T", " "))}</dd>
      ${posting.closed_at ? `<dt>Closed</dt><dd>${esc(posting.closed_at.slice(0, 19).replace("T", " "))} — no longer on the company's board</dd>` : ""}
      ${posting.changes ? `<dt>Last refresh</dt><dd>${changeTags({ changes: posting.changes })}</dd>` : ""}
    </dl>
    ${posting.evidence ? `<h2>Evidence</h2><blockquote class="quote">${esc(posting.evidence)}</blockquote>` : ""}
    ${rules.length ? `<h2>Rules that fired</h2><div class="rules">${rules.map((r) => `<span class="chip">${esc(r)}</span>`).join("")}</div>` : ""}
    ${history}`;

  const description = `<pre class="description">${esc(posting.description_text)}</pre>`;
  return { mine, facts, description };
}

function detailHtml(posting) {
  const { mine, facts, description } = detailParts(posting);
  return `${mine}${facts}<h2>Full description</h2>${description}`;
}

async function openDetail(id) {
  const { posting } = await getJSON(`/api/postings/${id}`);

  $("d-title").textContent = posting.title || "(untitled)";
  $("d-company").textContent =
    [
      posting.company_name || posting.company_slug,
      posting.platform,
      posting.location || "location unknown",
      postedLine(posting),
      posting.applicants,
    ].filter(Boolean).join(" · ");

  $("d-body").innerHTML = detailHtml(posting);

  $("detail").showModal();
  syncUrl(id);
}

// ---- loading -------------------------------------------------------------

// The Explore filters as query parameters: what narrows the postings table
// and, with it, the counts on the Companies page.
function filterParams() {
  const params = new URLSearchParams();
  if (state.q) params.set("q", state.q);
  const lists = {
    label: [...state.labels],
    region: [...state.regions],
    app: [...state.statuses],
    stage: [...state.stages],
    verdict: [...state.verdicts],
    company: state.only,
    exclude: state.hidden,
    place: state.places,
    exclude_place: state.hiddenPlaces,
  };
  for (const [key, values] of Object.entries(lists)) {
    for (const value of values) params.append(key, value);
  }
  if (state.changed) params.set("changed", "1");
  if (state.closing) params.set("closing", "1");
  if (state.posted != null) params.set("posted", state.posted);
  return params;
}

function postingsQuery() {
  const params = filterParams();
  params.set("limit", state.limit);
  if (state.sort !== "newest") params.set("order", state.sort);
  params.set("offset", state.offset);
  return params;
}

// Filters that narrow postings, leaving out the company ones: the Companies
// table lists every company whatever you hid or picked on Explore.
function activeFilterCount() {
  return (state.q ? 1 : 0) + state.labels.size + state.regions.size + state.places.length +
    state.hiddenPlaces.length + (state.changed ? 1 : 0) + (state.closing ? 1 : 0) + (state.posted != null ? 1 : 0) +
    state.statuses.size + state.stages.size +
    state.verdicts.size;
}

let companiesRequest = 0;

// The Companies table, counted under the Explore filters.
async function loadCompanies() {
  const ticket = ++companiesRequest;
  const { companies: rows } = await getJSON(`/api/companies?${filterParams()}`);
  if (ticket !== companiesRequest) return;
  renderCompanies(withRefreshKeys(rows));

  const active = activeFilterCount();
  $("company-filter-note").hidden = active === 0;
  if (active) {
    const matching = rows.reduce((n, c) => n + c.total, 0);
    const all = rows.reduce((n, c) => n + c.all_total, 0);
    $("company-filter-text").textContent =
      `Counting only the ${matching.toLocaleString()} of ${jobs(all)} that match your Explore filters.`;
  }
}

// Responses can land out of order while typing; only the latest one renders.
let postingsRequest = 0;

async function loadPostings() {
  const ticket = ++postingsRequest;
  const page = await getJSON(`/api/postings?${postingsQuery()}`);
  if (ticket !== postingsRequest) return;

  // Filtering can leave the current page past the end: step back to the last one.
  if (page.total && state.offset >= page.total) {
    state.offset = Math.floor((page.total - 1) / state.limit) * state.limit;
    return loadPostings();
  }
  renderPostings(page.postings);
  renderPager(page.total);
  renderPostedSortMark();
  renderFacets(page);
}

function renderPostedSortMark() {
  const th = $("posted-head");
  const dir = { posted: "desc", posted_asc: "asc" }[state.sort];
  th.querySelector(".sort-mark").textContent = dir ? (dir === "asc" ? "▲" : "▼") : "";
  if (dir) {
    th.dataset.dir = dir;
    th.setAttribute("aria-sort", dir === "asc" ? "ascending" : "descending");
  } else {
    delete th.dataset.dir;
    th.removeAttribute("aria-sort");
  }
}

function setSort(sort) {
  state.sort = sort;
  state.offset = 0;
  save();
  loadPostings().catch(showError);
}

$("posted-head").addEventListener("click", () => setSort(POSTED_CYCLE[state.sort] || "posted"));

function filtersChanged() {
  state.offset = 0;
  loadPostings().catch(showError);
  loadCompanies().catch(showError);
}

async function refresh() {
  const [summary, failures] = await Promise.all([
    getJSON("/api/stats"),
    getJSON("/api/failures"),
    loadResumes(),
  ]);

  placeInfo.clear();
  labelOrder = summary.labels;
  regionOrder = summary.regions;
  statusOrder = summary.app_statuses;
  stageOrder = summary.stages;
  verdictOrder = summary.verdicts;
  closingSoonDays = summary.closing_soon_days ?? closingSoonDays;
  places = summary.places;
  companies = indexCompanies(summary.companies);
  renderKpis(summary.stats);
  $("nav-explore-count").textContent = summary.stats.postings.toLocaleString();
  $("nav-companies-count").textContent = summary.stats.companies.toLocaleString();
  renderFailures(failures.failures);
  renderChips();
  await Promise.all([loadPostings(), loadCompanies()]);
}

// Shown on whichever page is open, and beside the Check button.
function showError(error) {
  $("check-status").className = "note error";
  $("check-status").textContent = error.message;
  $("app-error").textContent = error.message;
  $("app-error").hidden = false;
}

async function check() {
  const button = $("check");
  const status = $("check-status");
  button.disabled = true;
  status.className = "note";
  status.textContent = "Fetching and classifying…";

  try {
    const result = await postJSON("/api/check", {
      urls: $("urls").value,
      expand: $("expand").checked,
    });
    const failed = result.results.filter((r) => r.status !== "ok").length;
    const restricted = result.results.filter((r) => r.label.startsWith("RESTRICTED")).length;
    status.textContent =
      `Checked ${result.checked}` +
      (result.discovered ? ` (${result.discovered} found automatically)` : "") +
      `: ${restricted} restricted` +
      (failed ? `, ${failed} could not be fetched.` : ".");
    $("urls").value = "";
    await refresh();
    // The new postings have labels; now place them and have the model read them.
    processNewPostings().catch(showError);
  } catch (error) {
    status.className = "note error";
    status.textContent = error.message;
  } finally {
    button.disabled = false;
  }
}

// ---- board refresh ---------------------------------------------------------

// The server runs a refresh in the background; the page starts it and polls.
const REFRESH_POLL_MS = 1000;
let refreshTimer;

function setRefreshBusy(busy) {
  refreshBusy = busy;
  $("refresh-all").disabled = busy;
  $("refresh-deep").disabled = busy;
  for (const button of $("company-rows").querySelectorAll(".refresh-one")) button.disabled = busy;
}

function refreshStatus(text, isError = false) {
  $("refresh-status").className = isError ? "note error" : "note";
  $("refresh-status").textContent = text;
}

// "Refreshing 7/29 · nvidia…" while it runs; what it found once it is done.
function describeRefresh(run) {
  if (run.running) {
    const current = run.current ? ` · ${run.current.name || run.current.slug}` : "";
    return `Refreshing ${run.done}/${run.total}${current}…`;
  }
  if (run.error) return `Refresh stopped: ${run.error}`;
  if (!run.finished_at) return "";

  const totals = {};
  for (const result of run.results) {
    for (const [kind, n] of Object.entries(result.changes)) totals[kind] = (totals[kind] || 0) + n;
  }
  const changed = run.results.filter((r) => r.changed).length;
  const unread = run.results.filter((r) => r.status !== "ok").length;
  const jobsFailed = run.results.reduce((n, r) => n + r.failed, 0);
  const boards = `${run.results.length} board${run.results.length === 1 ? "" : "s"}`;
  return `Refreshed ${boards} ${when(run.finished_at)}: ` +
    (changed ? `${changed} changed (${changeSummary(totals, ", ")})` : "no changes") +
    (unread ? `, ${unread} could not be read` : "") +
    (jobsFailed ? `, ${jobsFailed} job${jobsFailed === 1 ? "" : "s"} could not be fetched` : "") +
    ".";
}

// `reload`: the tables are stale once the refresh being watched finishes.
async function pollBoardRefresh(reload = false) {
  clearTimeout(refreshTimer);
  let run;
  try {
    ({ refresh: run } = await getJSON("/api/refresh"));
  } catch (error) {
    refreshStatus(error.message, true);
    setRefreshBusy(false);
    return;
  }
  refreshStatus(describeRefresh(run), Boolean(run.error) && !run.running);
  if (run.running) {
    setRefreshBusy(true);
    refreshTimer = setTimeout(() => pollBoardRefresh(true), REFRESH_POLL_MS);
    return;
  }
  if (reload) {
    await refresh().catch(showError);
    // New and edited jobs from the boards: place them and have the model read them.
    processNewPostings().catch(showError);
  }
  setRefreshBusy(false);
}

// `companies`: [{platform, slug}] for some, or nothing for every board.
async function startBoardRefresh(companies) {
  setRefreshBusy(true);
  refreshStatus("Starting…");
  try {
    await postJSON("/api/refresh", {
      deep: $("refresh-deep").checked,
      ...(companies ? { companies } : {}),
    });
  } catch (error) {
    // Most likely one is already running (another tab): show it, then follow it.
    refreshStatus(error.message, true);
  }
  await pollBoardRefresh(true);
}

$("refresh-all").addEventListener("click", () => startBoardRefresh());

$("company-rows").addEventListener("click", (event) => {
  const one = event.target.closest(".refresh-one");
  if (one) {
    startBoardRefresh([{ platform: one.dataset.platform, slug: one.dataset.slug }]);
    return;
  }
  // The postings this company's refresh changed, in the table above.
  const link = event.target.closest(".changes-link");
  if (!link) return;
  state.changed = true;
  state.hidden = state.hidden.filter((slug) => slug !== link.dataset.slug);
  state.only = [link.dataset.slug];
  save();
  renderChips();
  filtersChanged();
  location.hash = "#explore";
});

$("changed-only").addEventListener("change", (event) => {
  state.changed = event.target.checked;
  filtersChanged();
});

$("closing-only").addEventListener("change", (event) => {
  state.closing = event.target.checked;
  filtersChanged();
});

$("posted-list").innerHTML = [null, ...POSTED_WITHIN].map((days) => `
  <label class="check-option">
    <input type="radio" name="posted-within" value="${days ?? ""}" ${days == null ? "checked" : ""}>
    <span class="region-name">${days == null ? "Any time" : `Last ${days} days`}</span>
  </label>`).join("");
$("posted-list").addEventListener("change", (event) => {
  state.posted = event.target.value === "" ? null : Number(event.target.value);
  filtersChanged();
});

// ---- wiring --------------------------------------------------------------

let searchTimer;
$("q").addEventListener("input", (event) => {
  clearTimeout(searchTimer);
  state.q = event.target.value.trim();
  searchTimer = setTimeout(filtersChanged, 200);
});

// Takes the state key, not the Set, and looks it up per change, so the
// handler can never be left holding a Set that state no longer uses.
function wireChecks(listId, key, persist) {
  $(listId).addEventListener("change", (event) => {
    const box = event.target;
    if (box.checked) state[key].add(box.value);
    else state[key].delete(box.value);
    if (persist) { save(); renderChips(); }
    filtersChanged();
  });
}
wireChecks("label-list", "labels", false);
wireChecks("region-list", "regions", true);
wireChecks("status-list", "statuses", true);
wireChecks("stage-list", "stages", true);
wireChecks("verdict-list", "verdicts", true);

// ---- your list and the role screening ----------------------------------------

function setOnly(set, values) {
  set.clear();
  for (const value of values) set.add(value);
}

// What to read next: passed the role rules, not skipped by the model, not marked yet.
$("for-me").addEventListener("click", () => {
  setOnly(state.stages, ["model"]);
  setOnly(state.statuses, ["none"]);
  setOnly(state.verdicts, WORTH_READING);
  save();
  filtersChanged();
});

$("mine-clear").addEventListener("click", () => {
  state.statuses.clear();
  state.stages.clear();
  state.verdicts.clear();
  save();
  filtersChanged();
});

// True while the server re-screens: the facet note is the progress line then.
let screenBusy = false;

// Runs the role funnel on the server. Resolves once the tables are current.
async function rescreen() {
  const button = $("screen-run");
  const status = $("screen-status");
  screenBusy = true;
  button.disabled = true;
  status.className = "note hint";
  status.textContent = "Screening every posting…";
  try {
    const result = await postJSON("/api/screen", {});
    await loadPostings();
    status.textContent =
      `Screened ${jobs(result.screened)}: ${(result.stages.model || 0).toLocaleString()} passed the rules.`;
  } catch (error) {
    status.className = "note hint error";
    status.textContent = error.message;
    throw error;
  } finally {
    screenBusy = false;
    button.disabled = false;
  }
}

$("screen-run").addEventListener("click", () => rescreen().catch(() => {}));

// Marks and the note inside a rendered detail. `redraw(id)` shows it again
// once the server has the change.
function wireMineBox(container, redraw) {
  container.addEventListener("click", (event) => {
    const box = event.target.closest(".mine-box");
    if (!box) return;
    if (onMarkClick(event, () => redraw(box.dataset.id))) return;
    const step = event.target.closest("button.step");
    if (step) {
      setStep(step.dataset.id, step.dataset.stage, !step.classList.contains("on"))
        .then(() => redraw(box.dataset.id))
        .catch(showError);
      return;
    }
    if (event.target.closest(".resume-upload")) {
      box.querySelector(".resume-file").click();
      return;
    }
    const save = event.target.closest(".mine-note-save");
    if (!save) return;
    setMark(box.dataset.id, save.dataset.status, box.querySelector(".mine-note").value)
      .then(() => redraw(box.dataset.id))
      .catch(showError);
  });
  container.addEventListener("change", (event) => {
    const box = event.target.closest(".mine-box");
    if (!box) return;
    // Said in the box itself: the page's own error line sits behind the dialog.
    const failed = (error) => {
      box.querySelector(".mine-error").textContent = error.message;
      box.querySelector(".mine-error").hidden = false;
    };
    if (event.target.matches(".resume-pick")) {
      setResume(box.dataset.id, event.target.value ? Number(event.target.value) : null)
        .then(() => redraw(box.dataset.id))
        .catch(failed);
    } else if (event.target.matches(".resume-file") && event.target.files.length) {
      const file = event.target.files[0];
      event.target.value = ""; // so choosing the same file again is still a change
      uploadResume(file)
        .then((resume) => setResume(box.dataset.id, resume.id))
        .then(() => redraw(box.dataset.id))
        .catch(failed);
    }
  });
}
wireMineBox($("d-body"), openDetail);

$("labels-clear").addEventListener("click", () => {
  state.labels.clear();
  filtersChanged();
});

$("browse-pane").addEventListener("click", (event) => {
  const button = event.target.closest(".tag button[data-list]");
  if (button) removeFilter(button.dataset.list, button.dataset.value);
});

function clearLists(keys, alsoRegions = false) {
  for (const key of keys) state[key] = [];
  if (alsoRegions) state.regions.clear();
  save();
  renderChips();
  filtersChanged();
}
$("companies-clear").addEventListener("click", () => clearLists(["only", "hidden"]));
$("company-filter-clear").addEventListener("click", () => $("reset").click());
$("locations-clear").addEventListener("click", () => clearLists(["places", "hiddenPlaces"], true));

for (const id of ["pager-top", "pager-bottom"]) {
  $(id).addEventListener("click", (event) => {
    const button = event.target.closest("button[data-page]");
    if (!button || button.disabled) return;
    state.offset = (Number(button.dataset.page) - 1) * state.limit;
    loadPostings().catch(showError);
    if (id === "pager-bottom") $("pager-top").scrollIntoView({ block: "start" });
  });
  $(id).addEventListener("change", (event) => {
    if (event.target.dataset.role === "sort") {
      setSort(event.target.value);
      return;
    }
    if (event.target.dataset.role !== "limit") return;
    // Keep the first visible row on screen across a page-size change.
    state.limit = Number(event.target.value);
    state.offset = Math.floor(state.offset / state.limit) * state.limit;
    save();
    loadPostings().catch(showError);
  });
}

// Reset clears the view, not the saved hide list: that is cleared on purpose.
$("reset").addEventListener("click", () => {
  state.q = "";
  state.labels.clear();
  state.regions.clear();
  state.only = [];
  state.places = [];
  state.hiddenPlaces = [];
  state.changed = false;
  state.closing = false;
  state.posted = null;
  state.statuses.clear();
  state.stages.clear();
  state.verdicts.clear();
  $("q").value = "";
  save();
  renderChips();
  filtersChanged();
});
$("company-head").addEventListener("click", (event) => {
  const th = event.target.closest("th[data-key]");
  if (!th) return;
  state.companySort = nextCompanySort(state.companySort, th.dataset.key, event.shiftKey);
  save();
  renderCompanies();
});
// Shift-click would otherwise select the header text.
$("company-head").addEventListener("mousedown", (event) => {
  if (event.shiftKey) event.preventDefault();
});
$("company-sort-reset").addEventListener("click", () => {
  state.companySort = DEFAULT_COMPANY_SORT;
  save();
  renderCompanies();
});
$("check").addEventListener("click", check);
$("d-close").addEventListener("click", () => $("detail").close());
$("detail").addEventListener("close", () => syncUrl(null));
$("urls").addEventListener("keydown", (event) => {
  if ((event.metaKey || event.ctrlKey) && event.key === "Enter") check();
});

loadSaved();
refresh()
  .then(() => {
    // A refresh may be running already (started in another tab, or before a reload).
    pollBoardRefresh();
    const deepLink = new URLSearchParams(location.search).get("posting");
    if (deepLink) return openDetail(deepLink);
  })
  .catch(showError);
