/*
 * Content script: read the posting, ask the service worker for a verdict,
 * show the badge.
 *
 * LinkedIn, Indeed and Workday are single-page apps: clicking another job in a search
 * list swaps the description without a page load. So instead of running once,
 * this watches the DOM (debounced) and re-reads whenever the posting's
 * identity or text changes.
 *
 * Loaded automatically on LinkedIn, Indeed and Workday, and injected on demand into any
 * other page by the popup's "Scan this page" button. Injecting twice must not
 * double up, so the second injection just triggers a rescan.
 */
(function () {
  "use strict";

  if (globalThis.__jobfilterContent) {
    globalThis.__jobfilterContent.scan({ force: true, generic: true });
    return;
  }

  const DEBOUNCE_MS = 700;
  let badge = null;
  let lastKey = null;
  let lastPosting = null;
  // Set by the popup just before it injects this file into an arbitrary page.
  let genericMode = Boolean(globalThis.__jobfilterGeneric);
  let timer = null;

  /** Cheap, stable fingerprint: a re-render with identical text is a no-op. */
  function fingerprint(text) {
    let hash = 0;
    for (let i = 0; i < text.length; i++) hash = (hash * 31 + text.charCodeAt(i)) | 0;
    return (hash >>> 0).toString(36);
  }

  function ensureBadge() {
    if (!badge || !document.contains(badge.host)) badge = JobFilterBadge.mount(document);
    return badge;
  }

  function clear() {
    if (badge) badge.remove();
    badge = null;
    if (lastKey !== null) chrome.runtime.sendMessage({ type: "clear" }).catch(() => {});
    lastKey = null;
    lastPosting = null;
  }

  function scan(options) {
    const force = Boolean(options && options.force);
    if (options && options.generic) genericMode = true;

    const result = JobFilterExtract.extractPosting(document, location.href, {
      generic: genericMode,
    });

    if (!result.ok && result.reason !== "description-not-found") {
      // Not a job page (LinkedIn feed, Indeed search with nothing selected),
      // or a Workday pane still showing the previous job.
      clear();
      return;
    }

    const key = `${result.site}:${result.jobId || result.url}:${result.ok ? fingerprint(result.text) : "unreadable"}`;
    if (!force && key === lastKey && badge && document.contains(badge.host)) return;
    lastKey = key;
    lastPosting = result;

    if (!result.ok) {
      ensureBadge().unreadable(result, key);
      chrome.runtime.sendMessage({ type: "unreadable", posting: result }).catch(() => {});
      return;
    }

    ensureBadge().pending(result, key);
    chrome.runtime
      .sendMessage({ type: "posting", posting: result, key })
      .then((state) => {
        if (state && key === lastKey) ensureBadge().show(result, state, key);
      })
      .catch((error) => {
        // The service worker is gone (extension reloaded): say so on the page.
        if (key === lastKey) {
          ensureBadge().show(
            result,
            {
              decision: { label: "UNCLEAR", classifierName: "none", source: "error" },
              serverError: `extension error: ${error && error.message}`,
            },
            key
          );
        }
      });
  }

  function scheduleScan() {
    clearTimeout(timer);
    timer = setTimeout(() => scan(), DEBOUNCE_MS);
  }

  /**
   * The live DOM as a static file. The site's own scripts are dropped: opened
   * from disk they boot, find no session and re-render an empty page. JSON-LD
   * stays -- it is data, and the extractor reads it. The source URL goes in a
   * comment so a saved page can be turned into a test fixture.
   */
  function snapshotHtml() {
    const copy = document.documentElement.cloneNode(true);
    for (const script of copy.querySelectorAll("script")) {
      if (script.type !== "application/ld+json") script.remove();
    }
    const url = location.href.replace(/--/g, "%2D%2D");
    return `<!-- jobfilter snapshot of ${url} -->\n${copy.outerHTML}`;
  }

  chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
    if (!message) return false;
    if (message.type === "update" && message.key === lastKey && lastPosting) {
      // The server answered after the local verdict was shown.
      ensureBadge().show(lastPosting, message.state, message.key);
    } else if (message.type === "rescan") {
      scan({ force: true, generic: message.generic });
      sendResponse({ ok: true });
    } else if (message.type === "snapshot") {
      sendResponse({ html: snapshotHtml(), url: location.href });
    }
    return false;
  });

  // Known sites re-render constantly; generic pages are scanned once, on
  // request, and then watched the same way in case they load lazily.
  new MutationObserver(scheduleScan).observe(document.documentElement, {
    childList: true,
    subtree: true,
    characterData: true,
  });
  globalThis.__jobfilterContent = { scan };
  scan();
})();
