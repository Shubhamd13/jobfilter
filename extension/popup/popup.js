/*
 * Popup: the verdict for this tab, "Scan this page", and settings.
 *
 * Reads tab state from the service worker and re-renders whenever it changes
 * (chrome.storage.onChanged), so a server verdict arriving while the popup is
 * open shows up without reopening it.
 */
(async function () {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const { labelInfo } = JobFilterLabels;
  const Engine = JobFilterEngine;

  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const tabId = tab ? tab.id : null;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  // --- verdict --------------------------------------------------------------

  function renderVerdict(record) {
    const box = $("verdict");
    box.replaceChildren();

    if (!record) {
      box.append(
        el("p", null, "No job posting detected on this page."),
        el("p", "muted", "LinkedIn and Indeed postings are checked automatically. Anywhere else, press Scan this page.")
      );
      $("scan").textContent = "Scan this page";
      return;
    }
    $("scan").textContent = "Re-check";

    const posting = record.posting || {};
    if (!record.state) {
      const info = labelInfo("UNCLEAR");
      box.append(labelLine(info.color, "Couldn't read this posting"));
      if (posting.title) box.append(el("div", "job", posting.title));
      box.append(el("p", "warn", "The description wasn't found on the page, so nothing was checked."));
      return;
    }

    const { state } = record;
    const decision = state.decision;
    const info = labelInfo(decision.label);
    box.style.setProperty("--tone", info.color);
    box.append(labelLine(info.color, info.text));

    if (posting.title) box.append(el("div", "job", posting.title));
    const sub = [posting.company, posting.location].filter(Boolean).join(" · ");
    if (sub) box.append(el("div", "muted", sub));
    if (decision.evidence) box.append(el("blockquote", null, decision.evidence));

    box.append(el("div", "meta", `rule: ${decision.matchedRule || "—"}`));
    box.append(el("div", "meta", `classifier: ${decision.classifierName} (${decision.source})`));
    box.append(el("div", "meta", `read from: ${posting.extraction ? posting.extraction.description : "?"} · ${posting.chars || 0} chars`));
    if (state.saved) box.append(el("div", "meta", `saved to database (posting #${state.postingId})`));

    if (state.pending) box.append(el("p", "warn", "Waiting for the server…"));
    if (state.serverError) box.append(el("p", "warn", `Server: ${state.serverError}. Showing the local verdict.`));
    if (Engine.disagreement(state)) {
      box.append(
        el(
          "p",
          "warn",
          `Local rules: ${labelInfo(state.local.label).text}. Server: ${labelInfo(state.server.label).text}.`
        )
      );
    }
  }

  function labelLine(color, text) {
    const line = el("div", "label");
    const dot = el("span", "dot");
    dot.style.background = color;
    line.append(dot, el("span", null, text));
    return line;
  }

  async function refresh() {
    if (tabId == null) return renderVerdict(null);
    const record = await chrome.runtime.sendMessage({ type: "get-state", tabId });
    renderVerdict(record && !record.error ? record : null);
    if (record && record.rulesVersion != null) $("rules-version").textContent = `rules v${record.rulesVersion}`;
  }

  chrome.storage.onChanged.addListener((changes, area) => {
    if (area === "session" && `tab:${tabId}` in changes) refresh();
  });

  // --- scan -------------------------------------------------------------------

  $("scan").addEventListener("click", async () => {
    if (tabId == null) return;
    const button = $("scan");
    button.disabled = true;
    try {
      // Tell the content script this is an explicit request, so it reads any
      // page, not only LinkedIn and Indeed. activeTab grants this tab only.
      await chrome.scripting.executeScript({
        target: { tabId },
        func: () => {
          globalThis.__jobfilterGeneric = true;
        },
      });
      await chrome.scripting.executeScript({
        target: { tabId },
        files: [
          "src/labels.js",
          "src/extract.js",
          "src/transport.js",
          "src/badge.js",
          "src/content.js",
        ],
      });
    } catch (error) {
      $("verdict").replaceChildren(
        el("p", "warn", `Can't scan this page: ${error.message}`),
        el("p", "muted", "Chrome doesn't let extensions read its own pages (chrome://, the Web Store).")
      );
    } finally {
      button.disabled = false;
    }
  });

  // --- settings -------------------------------------------------------------

  async function loadSettings() {
    const settings = { ...Engine.DEFAULT_SETTINGS, ...(await chrome.storage.sync.get(Engine.DEFAULT_SETTINGS)) };
    for (const radio of document.querySelectorAll('input[name="mode"]')) {
      radio.checked = radio.value === settings.mode;
    }
    $("record").checked = Boolean(settings.record);
    $("server-url").value = settings.serverUrl;
  }

  async function saveSettings() {
    const mode = document.querySelector('input[name="mode"]:checked');
    const serverUrl = $("server-url").value.trim() || Engine.DEFAULT_SETTINGS.serverUrl;
    await chrome.storage.sync.set({
      mode: mode ? mode.value : "local",
      record: $("record").checked,
      serverUrl,
    });
    await checkServer();
    // Re-run on the page so the new setting takes effect right away.
    if (tabId != null) chrome.tabs.sendMessage(tabId, { type: "rescan" }).catch(() => {});
  }

  async function checkServer() {
    const status = $("server-status");
    const needed =
      document.querySelector('input[name="mode"]:checked')?.value === "server" || $("record").checked;
    const health = await chrome.runtime.sendMessage({ type: "health" });
    status.className = health && health.ok ? "ok" : needed ? "bad" : "muted";
    status.textContent =
      health && health.ok
        ? `Connected · classifier: ${health.classifier}${health.rules_version != null ? ` · rules v${health.rules_version}` : ""}`
        : `Not connected: ${(health && health.error) || "unknown error"}. Run python -m jobfilter serve`;
  }

  for (const input of document.querySelectorAll('input[name="mode"], #record')) {
    input.addEventListener("change", saveSettings);
  }
  $("server-url").addEventListener("change", saveSettings);

  // --- fixture snapshot -----------------------------------------------------

  $("snapshot").addEventListener("click", async () => {
    try {
      const snap = await chrome.tabs.sendMessage(tabId, { type: "snapshot" });
      const host = new URL(snap.url).hostname.replace(/^www\./, "").split(".")[0];
      const blob = new Blob([snap.html], { type: "text/html" });
      const link = el("a");
      link.href = URL.createObjectURL(blob);
      link.download = `${host}_${new Date().toISOString().slice(0, 10)}.html`;
      link.click();
      setTimeout(() => URL.revokeObjectURL(link.href), 10000);
    } catch (error) {
      $("verdict").append(el("p", "warn", "Scan the page first, then download its HTML."));
    }
  });

  await loadSettings();
  await refresh();
  await checkServer();
})();
