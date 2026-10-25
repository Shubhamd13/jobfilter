"use strict";

// The Calendar page: a month of days, each with how many postings you applied
// to, or how many were added (first seen) that day. Click a day for its jobs.
// Loaded after app.js and uses its helpers ($, esc, getJSON, filterParams,
// activeFilterCount, openDetail, badge, verdictBadge, deadlineTag, postedAgo...).

const CAL_SAVED = "jobfilter.calendar";
const CAL_DAY_LIMIT = 500;
const WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

// What "Added" counts: a fixed set of filters, or whatever Explore is set to.
const CAL_SETS = {
  rules: { name: "Passed the rules", params: () => new URLSearchParams([["stage", "model"]]) },
  fit: {
    name: "Best fit + Fit",
    params: () => new URLSearchParams([["stage", "model"], ["verdict", "best"], ["verdict", "fit"]]),
  },
  explore: { name: "Explore's filters", params: () => filterParams() },
};
const CAL_KINDS = { applied: "Applied", added: "Added" };

const cal = {
  kind: "applied",
  set: "fit",
  month: null, // the first of the month shown, as a local Date
  selected: null, // YYYY-MM-DD
  days: {},
};

try {
  const saved = JSON.parse(localStorage.getItem(CAL_SAVED) || "{}");
  if (saved.kind in CAL_KINDS) cal.kind = saved.kind;
  if (saved.set in CAL_SETS) cal.set = saved.set;
} catch { /* nothing saved */ }

function saveCalendar() {
  try {
    localStorage.setItem(CAL_SAVED, JSON.stringify({ kind: cal.kind, set: cal.set }));
  } catch { /* storage unavailable */ }
}

function isoDay(d) {
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function thisMonth() {
  const now = new Date();
  return new Date(now.getFullYear(), now.getMonth(), 1);
}

// The filters for the counts and a day's list: Applied counts every
// application; Added counts the chosen set.
function calendarParams() {
  return cal.kind === "applied" ? new URLSearchParams() : CAL_SETS[cal.set].params();
}

function renderCalendarControls() {
  $("cal-kinds").innerHTML = Object.entries(CAL_KINDS).map(([kind, name]) => `
    <button class="tab" role="tab" data-kind="${kind}" aria-selected="${kind === cal.kind}">${esc(name)}</button>`).join("");
  $("cal-sets").hidden = cal.kind !== "added";
  $("cal-sets").innerHTML = Object.entries(CAL_SETS).map(([set, { name }]) => {
    const n = activeFilterCount();
    const extra = set === "explore" ? ` <span class="count">${n ? `${n} set` : "none set"}</span>` : "";
    return `<button class="tab" role="tab" data-set="${set}" aria-selected="${set === cal.set}">${esc(name)}${extra}</button>`;
  }).join("");
  $("cal-month").textContent = cal.month.toLocaleDateString(undefined, { month: "long", year: "numeric" });
  $("cal-today").disabled = cal.month.getTime() === thisMonth().getTime();
}

function renderCalendarGrid() {
  const year = cal.month.getFullYear();
  const month = cal.month.getMonth();
  const daysIn = new Date(year, month + 1, 0).getDate();
  const today = isoDay(new Date());
  const counts = Object.values(cal.days);
  const most = Math.max(1, ...counts);
  const total = counts.reduce((sum, n) => sum + n, 0);

  const cells = [];
  for (let blank = 0; blank < cal.month.getDay(); blank += 1) cells.push(`<div class="cal-cell blank"></div>`);
  for (let date = 1; date <= daysIn; date += 1) {
    const day = isoDay(new Date(year, month, date));
    const n = cal.days[day] || 0;
    // The tint grows with the count; the number itself always says it.
    const tint = n ? Math.round(12 + 48 * (n / most)) : 0;
    cells.push(`
      <button class="cal-cell ${n ? "has" : ""} ${day === today ? "today" : ""} ${day > today ? "future" : ""}"
              data-day="${day}" aria-pressed="${day === cal.selected}" ${n ? "" : "disabled"}
              style="--tint: ${tint}%" aria-label="${esc(`${day}: ${n} ${cal.kind}`)}">
        <span class="cal-date">${date}</span>
        ${n ? `<span class="cal-count">${n}</span>` : ""}
      </button>`);
  }
  $("cal-grid").innerHTML =
    WEEKDAYS.map((d) => `<div class="cal-weekday">${d}</div>`).join("") + cells.join("");

  const what = cal.kind === "applied" ? "applied" : `added · ${CAL_SETS[cal.set].name}`;
  const busiest = Object.entries(cal.days).sort((a, b) => b[1] - a[1])[0];
  $("cal-summary").textContent = total
    ? `${total.toLocaleString()} ${what} in ${cal.month.toLocaleDateString(undefined, { month: "long" })}` +
      ` · busiest ${formatDay(busiest[0])} (${busiest[1]})`
    : `Nothing ${what} this month.`;
}

// Only the latest request draws: clicking through months fires many.
let calendarRequest = 0;

async function loadCalendar() {
  if (!cal.month) cal.month = thisMonth();
  renderCalendarControls();
  const ticket = ++calendarRequest;
  const params = calendarParams();
  params.set("kind", cal.kind);
  params.set("from", isoDay(cal.month));
  params.set("to", isoDay(new Date(cal.month.getFullYear(), cal.month.getMonth() + 1, 0)));
  const page = await getJSON(`/api/calendar?${params}`);
  if (ticket !== calendarRequest) return;
  cal.days = page.days;
  if (cal.selected && !cal.days[cal.selected]) cal.selected = null;
  renderCalendarGrid();
  await loadCalendarDay();
}

let calendarDayRequest = 0;

async function loadCalendarDay() {
  const ticket = ++calendarDayRequest;
  if (!cal.selected) {
    $("cal-day-title").textContent = "";
    $("cal-day-rows").innerHTML = `<p class="empty">Pick a day to see its jobs.</p>`;
    return;
  }
  const params = calendarParams();
  params.set(cal.kind === "applied" ? "applied_on" : "added_on", cal.selected);
  params.set("limit", CAL_DAY_LIMIT);
  if (cal.kind === "added") params.set("order", "posted");
  const page = await getJSON(`/api/postings?${params}`);
  if (ticket !== calendarDayRequest) return;

  const day = formatDay(cal.selected, { weekday: "long", month: "long", day: "numeric" });
  $("cal-day-title").textContent =
    `${day} · ${page.total.toLocaleString()} ${cal.kind === "applied" ? "applied" : "added"}`;
  const more = page.total > page.postings.length
    ? `<p class="note hint">Showing the first ${page.postings.length.toLocaleString()}.</p>` : "";
  $("cal-day-rows").innerHTML = page.postings.map((p) => `
    <button class="cal-job" data-id="${p.posting_id}">
      <span class="cal-job-title">${esc(p.title || "(untitled)")}${deadlineTag(p)}</span>
      <span class="note">${esc(p.company_name || p.company_slug)} · ${esc(firstPlace(p.location))}${
        postedAgo(p) ? ` · posted ${esc(postedAgo(p))}` : ""}${
        cal.kind === "applied" ? ` · applied ${esc(new Date(p.app_updated_at).toLocaleTimeString(undefined, { hour: "numeric", minute: "2-digit" }))}` : ""}</span>
      <span class="cal-job-tags">${verdictBadge(p.verdict)}${p.label && p.label !== "OPEN" ? badge(p.label) : ""}${
        p.app_status && cal.kind === "added" ? `<span class="chip">${esc(STATUS_NAMES[p.app_status] || p.app_status)}</span>` : ""}</span>
    </button>`).join("") + more;
}

function shiftMonth(by) {
  cal.month = new Date(cal.month.getFullYear(), cal.month.getMonth() + by, 1);
  cal.selected = null;
  loadCalendar().catch(showError);
}

$("cal-kinds").addEventListener("click", (event) => {
  const tab = event.target.closest("[data-kind]");
  if (!tab || tab.dataset.kind === cal.kind) return;
  cal.kind = tab.dataset.kind;
  cal.selected = null;
  saveCalendar();
  loadCalendar().catch(showError);
});
$("cal-sets").addEventListener("click", (event) => {
  const tab = event.target.closest("[data-set]");
  if (!tab || tab.dataset.set === cal.set) return;
  cal.set = tab.dataset.set;
  cal.selected = null;
  saveCalendar();
  loadCalendar().catch(showError);
});
$("cal-prev").addEventListener("click", () => shiftMonth(-1));
$("cal-next").addEventListener("click", () => shiftMonth(1));
$("cal-today").addEventListener("click", () => {
  cal.month = thisMonth();
  cal.selected = null;
  loadCalendar().catch(showError);
});
$("cal-grid").addEventListener("click", (event) => {
  const cell = event.target.closest("[data-day]");
  if (!cell || cell.disabled) return;
  cal.selected = cal.selected === cell.dataset.day ? null : cell.dataset.day;
  for (const c of $("cal-grid").querySelectorAll("[data-day]")) {
    c.setAttribute("aria-pressed", c.dataset.day === cal.selected);
  }
  loadCalendarDay().catch(showError);
});
$("cal-day-rows").addEventListener("click", (event) => {
  const job = event.target.closest("[data-id]");
  if (job) openDetail(job.dataset.id).catch(showError);
});

// A mark changed in the dialog can move a day's count.
$("detail").addEventListener("close", () => {
  if (currentPage() === "calendar") loadCalendar().catch(showError);
});
