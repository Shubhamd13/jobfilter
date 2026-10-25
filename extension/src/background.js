/*
 * Service worker: owns the classifier, the settings, and per-tab state.
 *
 * Content scripts send it the extracted posting; it answers at once with the
 * local verdict, then (in server mode, or when recording) pushes an "update"
 * to the tab once the server has replied. Requests to the local server go out
 * from here, not from the page, so the host page never sees them and they are
 * covered by the manifest's host_permissions rather than CORS.
 *
 * `createBackground(chrome, fetch)` holds all the logic and takes its
 * dependencies as arguments, so tests run it against a fake `chrome`.
 */
(function (root) {
  "use strict";

  if (typeof importScripts === "function" && !root.JobFilterEngine) {
    importScripts("labels.js", "classifier.js", "transport.js");
  }

  const STATE_PREFIX = "tab:";

  function createBackground(chromeApi, fetchFn) {
    const Engine = root.JobFilterEngine;
    let enginePromise = null;

    function engine() {
      if (!enginePromise) {
        enginePromise = fetchFn(chromeApi.runtime.getURL("rules.json"))
          .then((response) => response.json())
          .then((rules) => Engine.create({ rules, fetch: fetchFn }));
      }
      return enginePromise;
    }

    async function settings() {
      const stored = await chromeApi.storage.sync.get(Engine.DEFAULT_SETTINGS);
      return { ...Engine.DEFAULT_SETTINGS, ...stored };
    }

    async function saveTabState(tabId, state) {
      // Session storage survives the service worker being suspended, which
      // MV3 does after ~30 s idle; a plain variable would not.
      await chromeApi.storage.session.set({ [STATE_PREFIX + tabId]: state });
    }

    async function tabState(tabId) {
      const key = STATE_PREFIX + tabId;
      const stored = await chromeApi.storage.session.get(key);
      return stored[key] || null;
    }

    async function setToolbarBadge(tabId, label) {
      if (tabId == null) return;
      if (!label) {
        await chromeApi.action.setBadgeText({ tabId, text: "" });
        return;
      }
      const info = root.JobFilterLabels.labelInfo(label);
      await chromeApi.action.setBadgeText({ tabId, text: info.short });
      await chromeApi.action.setBadgeBackgroundColor({ tabId, color: info.color });
    }

    /** Strip the description text: tab state is for the popup, not a copy. */
    function summary(posting) {
      const { text, ...rest } = posting;
      return { ...rest, chars: text ? text.length : 0 };
    }

    async function onPosting(posting, key, tabId) {
      const [eng, opts] = await Promise.all([engine(), settings()]);
      let first = null;
      const finalPromise = eng.run(posting, opts, (state) => {
        first = state;
      });

      // The local verdict is synchronous inside run(), so it is ready now.
      const record = (state) => ({
        posting: summary(posting),
        key,
        state,
        rulesVersion: eng.rulesVersion,
        updatedAt: new Date().toISOString(),
      });
      await saveTabState(tabId, record(first));
      await setToolbarBadge(tabId, first.decision.label);

      if (first.pending) {
        finalPromise.then(async (state) => {
          await saveTabState(tabId, record(state));
          await setToolbarBadge(tabId, state.decision.label);
          if (tabId != null) {
            chromeApi.tabs.sendMessage(tabId, { type: "update", key, state }).catch(() => {});
          }
        });
      }
      return first;
    }

    async function health() {
      const opts = await settings();
      if (!Engine.isLocalServer(opts.serverUrl)) {
        return { ok: false, error: "server URL must be http://127.0.0.1 or http://localhost" };
      }
      try {
        const response = await fetchFn(opts.serverUrl.replace(/\/+$/, "") + "/api/health");
        if (!response.ok) return { ok: false, error: `server answered ${response.status}` };
        return { ok: true, ...(await response.json()) };
      } catch (error) {
        return { ok: false, error: `not reachable at ${opts.serverUrl}` };
      }
    }

    /** Every message type, one place. Returns a promise of the reply. */
    async function handle(message, sender) {
      const tabId = sender && sender.tab ? sender.tab.id : message.tabId;
      switch (message && message.type) {
        case "posting":
          return onPosting(message.posting, message.key, tabId);
        case "unreadable":
          await saveTabState(tabId, {
            posting: message.posting,
            state: null,
            updatedAt: new Date().toISOString(),
          });
          await setToolbarBadge(tabId, "UNCLEAR");
          return { ok: true };
        case "clear":
          await chromeApi.storage.session.remove(STATE_PREFIX + tabId);
          await setToolbarBadge(tabId, null);
          return { ok: true };
        case "get-state":
          return tabState(tabId);
        case "health":
          return health();
        default:
          return { error: `unknown message ${message && message.type}` };
      }
    }

    function install() {
      chromeApi.runtime.onMessage.addListener((message, sender, sendResponse) => {
        handle(message, sender).then(sendResponse, (error) =>
          sendResponse({ error: String((error && error.message) || error) })
        );
        return true; // keep the channel open for the async reply
      });
      chromeApi.tabs.onRemoved.addListener((tabId) => {
        chromeApi.storage.session.remove(STATE_PREFIX + tabId);
      });
    }

    return { handle, install, engine, settings };
  }

  root.JobFilterBackground = { createBackground };

  if (typeof chrome !== "undefined" && chrome.runtime && chrome.runtime.id) {
    createBackground(chrome, (...args) => fetch(...args)).install();
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
