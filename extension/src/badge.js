/*
 * The in-page badge: a pill in the corner that expands to show the evidence.
 *
 * Lives in a closed-off shadow root so the host page's CSS cannot restyle it
 * and ours cannot leak out. Every string from the page (title, evidence) goes
 * in through textContent, never innerHTML: a posting is untrusted input.
 */
(function (root) {
  "use strict";

  const HOST_ID = "jobfilter-badge-host";

  const STYLE = `
    :host { all: initial; }
    .wrap {
      position: fixed; right: 16px; bottom: 16px; z-index: 2147483646;
      font: 13px/1.4 system-ui, -apple-system, "Segoe UI", sans-serif;
      color: #1a202c; max-width: min(380px, calc(100vw - 32px));
    }
    .pill {
      display: flex; align-items: center; gap: 8px; cursor: pointer;
      background: #fff; border: 1px solid #d5dae1; border-radius: 999px;
      padding: 7px 12px 7px 10px; box-shadow: 0 2px 10px rgba(0,0,0,.12);
      margin-left: auto; width: max-content; max-width: 100%;
    }
    .dot { width: 10px; height: 10px; border-radius: 50%; flex: none; }
    .name { font-weight: 600; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .tag { color: #5a6472; font-size: 11px; }
    .card {
      display: none; margin-bottom: 8px; background: #fff; border: 1px solid #d5dae1;
      border-radius: 10px; padding: 12px 14px; box-shadow: 0 4px 18px rgba(0,0,0,.14);
    }
    .open .card { display: block; }
    .row { display: flex; justify-content: space-between; gap: 8px; align-items: start; }
    .job { font-weight: 600; }
    .sub { color: #5a6472; font-size: 12px; }
    .close { cursor: pointer; border: 0; background: none; font-size: 16px; color: #5a6472; padding: 0 2px; }
    blockquote {
      margin: 10px 0 8px; padding: 6px 10px; border-left: 3px solid var(--tone);
      background: #f6f7f9; border-radius: 0 6px 6px 0; white-space: pre-wrap;
    }
    .meta { color: #5a6472; font-size: 11px; margin-top: 4px; }
    .warn { color: #975a16; font-size: 12px; margin-top: 6px; }
    @media (prefers-color-scheme: dark) {
      .wrap { color: #e2e8f0; }
      .pill, .card { background: #1f2430; border-color: #394150; }
      blockquote { background: #272d3a; }
      .tag, .sub, .meta, .close { color: #a0aab8; }
      .warn { color: #f6ad55; }
    }
  `;

  function el(doc, tag, className, text) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (text != null) node.textContent = text;
    return node;
  }

  function mount(doc) {
    const labels = root.JobFilterLabels;
    let host = doc.getElementById(HOST_ID);
    if (host) host.remove();

    host = el(doc, "div");
    host.id = HOST_ID;
    const shadow = host.attachShadow({ mode: "open" });
    const style = el(doc, "style");
    style.textContent = STYLE;
    const wrap = el(doc, "div", "wrap");
    shadow.append(style, wrap);
    (doc.body || doc.documentElement).appendChild(host);

    let dismissedKey = null;
    let currentKey = null;

    function render(view) {
      wrap.replaceChildren();
      if (view.key && view.key === dismissedKey) return;
      wrap.style.setProperty("--tone", view.color);

      const card = el(doc, "div", "card");
      const head = el(doc, "div", "row");
      const who = el(doc, "div");
      who.append(el(doc, "div", "job", view.title || "This posting"));
      if (view.subtitle) who.append(el(doc, "div", "sub", view.subtitle));
      const close = el(doc, "button", "close", "×");
      close.title = "Hide for this posting";
      close.addEventListener("click", (event) => {
        event.stopPropagation();
        dismissedKey = currentKey;
        wrap.replaceChildren();
      });
      head.append(who, close);
      card.append(head);

      if (view.evidence) card.append(el(doc, "blockquote", null, view.evidence));
      for (const line of view.meta || []) card.append(el(doc, "div", "meta", line));
      for (const line of view.warnings || []) card.append(el(doc, "div", "warn", line));

      const pill = el(doc, "div", "pill");
      const dot = el(doc, "span", "dot");
      dot.style.background = view.color;
      pill.append(dot, el(doc, "span", "name", view.text));
      if (view.tag) pill.append(el(doc, "span", "tag", view.tag));
      pill.addEventListener("click", () => wrap.classList.toggle("open"));

      wrap.append(card, pill);
    }

    function subtitle(posting) {
      return [posting.company, posting.location].filter(Boolean).join(" · ") || null;
    }

    return {
      host,
      shadow,

      pending(posting, key) {
        currentKey = key;
        render({
          key,
          color: "#a0aab8",
          text: "Checking…",
          title: posting.title,
          subtitle: subtitle(posting),
        });
      },

      show(posting, state, key) {
        currentKey = key;
        const decision = state.decision;
        const info = labels.labelInfo(decision.label);
        const meta = [
          `rule: ${decision.matchedRule || "—"}`,
          `classifier: ${decision.classifierName} (${decision.source})`,
        ];
        if (state.saved) meta.push("saved to your jobfilter database");
        const warnings = [];
        if (state.pending) warnings.push("Waiting for the server…");
        if (state.serverError) warnings.push(`Server: ${state.serverError}. Showing the local verdict.`);
        if (root.JobFilterEngine && root.JobFilterEngine.disagreement(state)) {
          warnings.push(
            `Local rules said ${labels.labelInfo(state.local.label).text}; the server said ${labels.labelInfo(state.server.label).text}.`
          );
        }
        render({
          key,
          color: info.color,
          text: info.text,
          tag: state.pending ? "…" : null,
          title: posting.title,
          subtitle: subtitle(posting),
          evidence: decision.evidence,
          meta,
          warnings,
        });
      },

      unreadable(posting, key) {
        currentKey = key;
        const info = labels.labelInfo("UNCLEAR");
        render({
          key,
          color: info.color,
          text: "Couldn't read this posting",
          title: posting.title,
          subtitle: subtitle(posting),
          warnings: [
            "The job description was not found on the page, so nothing was checked.",
            "The site may have changed its layout; see the README on refreshing fixtures.",
          ],
        });
      },

      remove() {
        currentKey = null;
        host.remove();
      },
    };
  }

  root.JobFilterBadge = { mount, HOST_ID };
})(typeof globalThis !== "undefined" ? globalThis : this);
