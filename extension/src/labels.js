/*
 * How each label is shown: in-page badge, popup, and toolbar badge.
 *
 * Loaded by the content scripts, the popup and the service worker, so the
 * wording and colours cannot drift between them. Classic script for the same
 * reason as classifier.js.
 */
(function (root) {
  "use strict";

  const LABELS = {
    RESTRICTED_CITIZEN: {
      text: "U.S. citizens only",
      short: "CIT",
      tone: "restricted",
      color: "#c0392b",
    },
    RESTRICTED_US_PERSON: {
      text: "U.S. persons only (export control)",
      short: "USP",
      tone: "restricted",
      color: "#c0392b",
    },
    RESTRICTED_CLEARANCE: {
      text: "Security clearance required",
      short: "CLR",
      tone: "restricted",
      color: "#c0392b",
    },
    INFO_NO_SPONSORSHIP: {
      text: "No visa sponsorship",
      short: "VISA",
      tone: "info",
      color: "#2c6fbb",
    },
    UNCLEAR: {
      text: "Unclear: check manually",
      short: "?",
      tone: "unclear",
      color: "#b7791f",
    },
    OPEN: {
      text: "No restriction found",
      short: "OK",
      tone: "open",
      color: "#2f855a",
    },
  };

  /** Never throws on a label we do not know: show it as UNCLEAR. */
  function labelInfo(label) {
    return LABELS[label] || { ...LABELS.UNCLEAR, text: String(label) };
  }

  root.JobFilterLabels = { LABELS, labelInfo };
})(typeof globalThis !== "undefined" ? globalThis : this);
