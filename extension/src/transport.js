/*
 * Where a decision comes from: the bundled rule engine, the local server, or
 * both.
 *
 * The extension depends on `classify(posting) -> decision`, not on any one
 * classifier. The local engine (classifier.js + rules.json) answers instantly
 * and offline. The server answers with whatever classifier it is configured
 * with -- today the same rules, in Phase 2b Ollama or zero-shot -- so those
 * reach the extension with no change here.
 *
 * No chrome.* calls: `fetch` is injected, which is what lets the tests drive
 * every server outcome with a fake.
 */
(function (root) {
  "use strict";

  const DEFAULT_SETTINGS = Object.freeze({
    // "local": bundled rules only. "server": ask the server, falling back to
    // the local verdict if it is unreachable.
    mode: "local",
    // Save every posting viewed to the server's database (source "extension").
    record: false,
    serverUrl: "http://127.0.0.1:8000",
  });
  const SERVER_TIMEOUT_MS = 8000;

  /** Only a server on this machine. The manifest grants nothing else. */
  function isLocalServer(serverUrl) {
    try {
      const url = new URL(serverUrl);
      return (
        url.protocol === "http:" &&
        (url.hostname === "127.0.0.1" || url.hostname === "localhost")
      );
    } catch (_) {
      return false;
    }
  }

  /** The server speaks snake_case; the JS engine speaks camelCase. */
  function fromServer(decision) {
    return {
      label: decision.label,
      confidence: decision.confidence,
      evidence: decision.evidence ?? null,
      matchedRule: decision.matched_rule ?? null,
      matchedRules: decision.matched_rules ?? [],
      classifierName: decision.classifier_name ?? "server",
    };
  }

  function browsedBody(posting) {
    return {
      site: posting.site,
      job_id: posting.jobId,
      url: posting.url,
      title: posting.title,
      company: posting.company,
      location: posting.location,
      posted_ago: posting.postedAgo ?? null,
      applicants: posting.applicants ?? null,
      text: posting.text,
      extraction: posting.extraction,
    };
  }

  function create({ rules, fetch }) {
    const local = root.JobFilterRules.RuleClassifier(rules);

    async function post(serverUrl, path, body) {
      const controller = typeof AbortController !== "undefined" ? new AbortController() : null;
      const timer = controller ? setTimeout(() => controller.abort(), SERVER_TIMEOUT_MS) : null;
      try {
        const response = await fetch(serverUrl.replace(/\/+$/, "") + path, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          signal: controller ? controller.signal : undefined,
        });
        let payload = null;
        try {
          payload = await response.json();
        } catch (_) {
          // fall through: reported below as a bad response
        }
        if (!response.ok) {
          throw new Error((payload && payload.error) || `server answered ${response.status}`);
        }
        if (!payload || !payload.decision) throw new Error("server sent no decision");
        return payload;
      } finally {
        if (timer) clearTimeout(timer);
      }
    }

    function classifyLocal(posting) {
      return { ...local.classify(posting.text), source: "local" };
    }

    /**
     * Classify one posting. `onLocal` fires with the instant local verdict;
     * the returned promise resolves with the final state once the server (if
     * any) has answered.
     */
    async function run(posting, settings, onLocal) {
      const options = { ...DEFAULT_SETTINGS, ...(settings || {}) };
      const localDecision = classifyLocal(posting);
      const state = {
        decision: localDecision,
        local: localDecision,
        server: null,
        serverError: null,
        saved: false,
        postingId: null,
        pending: options.mode === "server" || options.record,
      };
      if (onLocal) onLocal({ ...state });
      if (!state.pending) return state;

      if (!isLocalServer(options.serverUrl)) {
        return {
          ...state,
          pending: false,
          serverError: "server URL must be http://127.0.0.1 or http://localhost",
        };
      }

      try {
        // Recording goes through /api/browsed, which classifies too: one
        // request instead of two.
        const payload = options.record
          ? await post(options.serverUrl, "/api/browsed", browsedBody(posting))
          : await post(options.serverUrl, "/api/classify", {
              text: posting.text,
              title: posting.title,
            });
        const server = { ...fromServer(payload.decision), source: "server" };
        return {
          ...state,
          pending: false,
          server,
          saved: Boolean(options.record),
          postingId: payload.posting_id ?? null,
          // Server mode trusts the server; local mode only wanted it saved.
          decision: options.mode === "server" ? server : localDecision,
        };
      } catch (error) {
        const message =
          error && error.name === "AbortError"
            ? "server did not answer in time"
            : String((error && error.message) || error);
        // Never leave the posting without a verdict: keep the local one.
        return { ...state, pending: false, serverError: message };
      }
    }

    return { run, classifyLocal, rulesVersion: rules.version };
  }

  /** Did the two classifiers disagree? Worth showing: it is Phase 2b's data. */
  function disagreement(state) {
    return Boolean(
      state && state.server && state.local && state.server.label !== state.local.label
    );
  }

  root.JobFilterEngine = {
    create,
    disagreement,
    isLocalServer,
    fromServer,
    browsedBody,
    DEFAULT_SETTINGS,
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
