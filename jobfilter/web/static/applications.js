"use strict";

// The Applications page: every posting you marked applied, and what came of
// each resume. Loaded after app.js and uses its helpers ($, esc, getJSON,
// postJSON, when, stepsOf, loadResumes, openDetail, STEP_NAMES...).

const APPS_LIMIT = 1000;
// Where an application stands; each one is under exactly one of these.
const APPS_TABS = ["all", "waiting", ...STEPS.map((s) => s.stage)];
const APPS_TAB_NAMES = { all: "All", waiting: "Waiting", ...STEP_NAMES };
const NO_RESULTS = { sent: 0, waiting: 0, oa: 0, interview: 0, offer: 0, rejected: 0, replied: 0 };

const apps = {
  tab: "all",
  resume: null, // a resume id, "none" for sent without one, null for every resume
  rows: [],
  counts: {},
};

function shortDate(iso) {
  return String(iso || "").slice(0, 10);
}

function renderAppsTabs() {
  const all = Object.values(apps.counts).reduce((sum, n) => sum + n, 0);
  $("apps-tabs").innerHTML = APPS_TABS.map((tab) => `
    <button class="tab" role="tab" data-tab="${tab}" aria-selected="${tab === apps.tab}">
      ${esc(APPS_TAB_NAMES[tab])} <span class="count">${(tab === "all" ? all : apps.counts[tab] ?? 0).toLocaleString()}</span>
    </button>`).join("");
}

function renderAppsNarrowed() {
  const resume = resumes.find((r) => r.id === apps.resume);
  $("apps-narrowed").hidden = apps.resume === null;
  $("apps-narrowed-text").textContent =
    apps.resume === "none" ? "Sent with no resume attached." : `Sent with ${resume ? resume.name : "this resume"}.`;
}

// "OA Oct 7 → Rejected Oct 20", or how long you have been waiting.
function standingCell(p) {
  const steps = stepsOf(p);
  if (!steps.length) return `<span class="note">waiting · ${esc(when(p.app_updated_at))}</span>`;
  return steps.map((s) => `
    <span class="step-tag" data-stage="${esc(s.stage)}" title="${esc(shortDate(s.at))}">${
      esc(STEP_NAMES[s.stage] || s.stage)} <span class="step-when">${esc(when(s.at))}</span></span>`).join(
    `<span class="step-then" aria-hidden="true">→</span>`);
}

const APPS_EMPTY = {
  all: "Nothing marked Applied yet. Mark a posting Applied in the Inbox or on Explore.",
  waiting: "No application is waiting to hear back.",
  oa: "No application stands at an OA.",
  interview: "No application stands at an interview.",
  offer: "No offers yet.",
  rejected: "No rejections.",
};

function renderAppsRows() {
  $("apps-rows").innerHTML = apps.rows.length
    ? apps.rows.map((p) => `
        <tr class="clickable ${p.closed_at ? "is-closed" : ""}" data-id="${p.posting_id}">
          <td class="company">${esc(p.company_name || p.company_slug)}</td>
          <td class="title">${esc(p.title || "—")}${changeTags({ closed_at: p.closed_at })}</td>
          <td class="when" title="${esc(shortDate(p.app_updated_at))}">${esc(when(p.app_updated_at))}</td>
          <td class="resume">${p.resume_id
            ? `<a href="/api/resumes/${p.resume_id}/file" target="_blank" rel="noopener">${esc(p.resume_name)}</a>`
            : `<span class="note">none attached</span>`}</td>
          <td class="standing">${standingCell(p)}</td>
        </tr>`).join("")
    : `<tr><td colspan="5" class="empty">${apps.resume === null ? APPS_EMPTY[apps.tab] : "No applications here for this resume."}</td></tr>`;
}

function resultCells(found) {
  const r = { ...NO_RESULTS, ...found };
  const cell = (n) => `<td class="num ${n ? "" : "zero"}">${n.toLocaleString()}</td>`;
  return `${cell(r.sent)}${cell(r.waiting)}${cell(r.oa)}${cell(r.interview)}${cell(r.offer)}${cell(r.rejected)}
    <td class="num ${r.replied ? "replied" : "zero"}">${r.replied ? `${r.replied} of ${r.sent}` : "0"}</td>`;
}

function renderResumeRows() {
  const rows = resumes.map((r) => `
    <tr class="clickable" data-resume="${r.id}" aria-selected="${apps.resume === r.id}">
      <td class="resume">
        <a href="/api/resumes/${r.id}/file" target="_blank" rel="noopener" title="${esc(r.filename)} · uploaded ${esc(shortDate(r.uploaded_at))}">${esc(r.name)}</a>
      </td>
      ${resultCells(resumeResults[r.id])}
      <td class="resume-actions">
        <button class="link resume-rename" data-id="${r.id}">Rename</button>
        ${r.used ? "" : `<button class="link resume-delete" data-id="${r.id}">Delete</button>`}
      </td>
    </tr>`);
  if (resumeResults.none) {
    rows.push(`
      <tr class="clickable" data-resume="none" aria-selected="${apps.resume === "none"}">
        <td class="resume"><span class="note">No resume attached</span></td>
        ${resultCells(resumeResults.none)}
        <td></td>
      </tr>`);
  }
  $("resume-rows").innerHTML = rows.length
    ? rows.join("")
    : `<tr><td colspan="9" class="empty">No resumes yet. Open an applied posting and use Upload… to add the one you sent.</td></tr>`;
}

// Responses can land out of order; only the latest one renders.
let appsRequest = 0;

async function loadApplications() {
  const ticket = ++appsRequest;
  const params = new URLSearchParams({ app: "applied", limit: APPS_LIMIT });
  if (apps.tab !== "all") params.set("outcome", apps.tab);
  if (apps.resume !== null) params.set("resume", apps.resume);
  const [page] = await Promise.all([getJSON(`/api/postings?${params}`), loadResumes()]);
  if (ticket !== appsRequest) return;

  // A log of what you sent: the latest application on top.
  apps.rows = page.postings
    .map((p, at) => ({ p, at }))
    .sort((a, b) => (b.p.app_updated_at || "").localeCompare(a.p.app_updated_at || "") || a.at - b.at)
    .map(({ p }) => p);
  apps.counts = page.outcome_counts || {};
  // The sidebar counts every application, whatever this page is narrowed to.
  if (apps.resume === null) {
    $("nav-applications-count").textContent =
      Object.values(apps.counts).reduce((sum, n) => sum + n, 0).toLocaleString();
  }

  renderAppsTabs();
  renderAppsNarrowed();
  renderAppsRows();
  renderResumeRows();
}

$("apps-tabs").addEventListener("click", (event) => {
  const tab = event.target.closest(".tab");
  if (!tab || tab.dataset.tab === apps.tab) return;
  apps.tab = tab.dataset.tab;
  loadApplications().catch(showError);
});

$("apps-rows").addEventListener("click", (event) => {
  if (event.target.closest("a")) return; // the resume link opens the file
  const row = event.target.closest("tr.clickable");
  if (row) openDetail(row.dataset.id).catch(showError);
});

$("resume-rows").addEventListener("click", (event) => {
  if (event.target.closest("a")) return;
  const rename = event.target.closest(".resume-rename");
  const remove = event.target.closest(".resume-delete");
  if (rename || remove) {
    const id = Number((rename || remove).dataset.id);
    const resume = resumes.find((r) => r.id === id);
    let change;
    if (rename) {
      const name = prompt("Name this resume", resume.name);
      if (!name || !name.trim() || name.trim() === resume.name) return;
      change = { name };
    } else {
      if (!confirm(`Delete ${resume.name}? The stored file goes with it.`)) return;
      change = { delete: true };
    }
    postJSON(`/api/resumes/${id}`, change).then(loadApplications).catch(showError);
    return;
  }
  const row = event.target.closest("tr.clickable");
  if (!row) return;
  const picked = row.dataset.resume === "none" ? "none" : Number(row.dataset.resume);
  apps.resume = apps.resume === picked ? null : picked; // a second click shows every resume again
  loadApplications().catch(showError);
});

$("apps-resume-clear").addEventListener("click", () => {
  apps.resume = null;
  loadApplications().catch(showError);
});

// A step, a resume or a mark changed in the dialog: this page shows all three.
$("detail").addEventListener("close", () => {
  if (currentPage() === "applications") loadApplications().catch(showError);
});
