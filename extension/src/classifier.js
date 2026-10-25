/*
 * jobfilter rule classifier -- JavaScript port.
 *
 * A faithful mirror of jobfilter/classifiers/rules.py and sentences.py. The
 * PATTERNS are not duplicated: both engines load the same rules.json. The
 * PIPELINE is, because Python cannot run in a content script -- so
 * tests/cases.json is a golden corpus both implementations must agree on, and
 * tests/test_js_parity.py diffs them. Change the algorithm here and you change
 * it there, or the parity test fails.
 *
 * Classic script, not an ES module: MV3 content scripts load plain scripts,
 * and the test harness loads this over file:// where module imports are
 * blocked by CORS.
 */
(function (root) {
  "use strict";

  // --- sentence splitting (mirror of sentences.py) ------------------------

  const BOUNDARY = /([.!?]+)(["'\)\]]*)(\s+)/g;
  const SENTENCE_START = /[A-Z0-9"'(\[‘“•\-]/;

  function splitSentences(text, protect) {
    if (!text) return [];
    const out = [];
    for (const rawLine of text.split("\n")) {
      const line = rawLine.trim();
      if (line) out.push(...splitLine(line, protect));
    }
    return out;
  }

  function splitLine(line, protect) {
    const pieces = [];
    let start = 0;
    BOUNDARY.lastIndex = 0;

    let match;
    while ((match = BOUNDARY.exec(line)) !== null) {
      // End of the punctuation plus any closing quotes: group 1 + group 2.
      const cut = match.index + match[1].length + match[2].length;
      const left = line.slice(start, cut);

      // "...a U.S." / "...(i.e." / "...8 U.S.C." are abbreviations, not ends.
      if (protect.some((p) => reTest(p, left))) continue;

      const next = line.slice(match.index + match[0].length, match.index + match[0].length + 1);
      if (next && !SENTENCE_START.test(next)) continue;

      const stripped = left.trim();
      if (stripped) pieces.push(stripped);
      start = match.index + match[0].length;
    }

    const tail = line.slice(start).trim();
    if (tail) pieces.push(tail);
    return pieces;
  }

  /** Test without carrying lastIndex between calls. */
  function reTest(regex, text) {
    regex.lastIndex = 0;
    return regex.test(text);
  }

  // --- rule set (mirror of RuleSet) ---------------------------------------

  function compile(pattern, flags) {
    return new RegExp(pattern, typeof flags === "string" ? flags : "i");
  }

  function compileUnless(spec) {
    if (!spec.unless) return null;
    return compile(spec.unless, spec.unless_flags ?? "i");
  }

  function buildSignals(doc, key, labelsKey) {
    return (doc[key] ?? []).map((spec) => ({
      id: spec.id,
      pattern: compile(spec.pattern, spec.flags ?? doc.default_flags ?? "i"),
      labels: new Set(spec[labelsKey] ?? []),
      suppressesRules: new Set(spec.suppresses_rules ?? []),
      unless: compileUnless(spec),
    }));
  }

  function buildRuleSet(doc) {
    return {
      version: doc.version,
      severity: doc.severity,
      supersedes: doc.supersedes ?? {},
      protect: doc.sentence_splitter.protect_suffix_patterns.map((spec) =>
        compile(spec.pattern, spec.flags ?? "")
      ),
      exclusions: buildSignals(doc, "exclusions", "__none__"),
      negations: buildSignals(doc, "negations", "applies_to"),
      openSignals: buildSignals(doc, "open_signals", "conflicts_with"),
      rules: (doc.rules ?? []).map((spec) => ({
        id: spec.id,
        label: spec.label,
        pattern: compile(spec.pattern, spec.flags ?? doc.default_flags ?? "i"),
        unless: compileUnless(spec),
      })),
    };
  }

  function matches(item, sentence) {
    if (!reTest(item.pattern, sentence)) return false;
    return !(item.unless && reTest(item.unless, sentence));
  }

  function rank(ruleset, label) {
    return ruleset.severity[label] ?? 0;
  }

  // --- the classifier (mirror of RuleClassifier) --------------------------

  const CLASSIFIER_NAME = "rules";
  const RESTRICTIONS = new Set([
    "RESTRICTED_CITIZEN",
    "RESTRICTED_US_PERSON",
    "RESTRICTED_CLEARANCE",
  ]);

  function cleanEvidence(sentence) {
    if (sentence == null) return null;
    const cleaned = sentence.replace(/^- /, "").trim();
    return cleaned || null;
  }

  function decision(label, evidence, ruleIds) {
    const ids = Array.from(ruleIds);
    return {
      label,
      confidence: 1.0,
      evidence: cleanEvidence(evidence),
      matchedRule: ids.length ? ids[0] : null,
      matchedRules: ids,
      classifierName: CLASSIFIER_NAME,
    };
  }

  function classifySentence(ruleset, sentence, excluded) {
    const negated = new Set();
    const cancelled = new Set();
    for (const signal of ruleset.negations) {
      if (matches(signal, sentence)) {
        for (const label of signal.labels) negated.add(label);
        for (const id of signal.suppressesRules) cancelled.add(id);
      }
    }

    const hits = ruleset.rules.filter(
      (rule) =>
        !negated.has(rule.label) &&
        !cancelled.has(rule.id) &&
        matches(rule, sentence)
    );
    if (!hits.length) return null;

    let labels = new Set(hits.map((r) => r.label));
    for (const label of Array.from(labels)) {
      for (const beaten of ruleset.supersedes[label] ?? []) labels.delete(beaten);
    }

    if (excluded) {
      // EEO boilerplate that also carries a restriction: do not drop it, do
      // not trust it either.
      const restricting = Array.from(labels).some((l) => RESTRICTIONS.has(l));
      if (!restricting) return null;
      const ids = hits.filter((r) => labels.has(r.label)).map((r) => r.id);
      return { label: "UNCLEAR", sentence, ruleIds: ids, strength: ids.length };
    }

    let best = null;
    for (const label of labels) {
      if (best === null || rank(ruleset, label) > rank(ruleset, best)) best = label;
    }
    const ids = hits.filter((r) => r.label === best).map((r) => r.id);
    const others = hits.filter((r) => r.label !== best).map((r) => r.id);
    return {
      label: best,
      sentence,
      ruleIds: ids.concat(others),
      strength: ids.length,
    };
  }

  function classifyText(ruleset, text) {
    if (!text || !text.trim()) {
      // Never silently drop: an unreadable posting is a human's problem.
      return decision("UNCLEAR", null, []);
    }

    const candidates = [];
    const openHits = new Map(); // conflicted label -> open signal id

    for (const sentence of splitSentences(text, ruleset.protect)) {
      const excluded = ruleset.exclusions.some((e) => matches(e, sentence));

      if (!excluded) {
        for (const signal of ruleset.openSignals) {
          if (matches(signal, sentence)) {
            for (const label of signal.labels) {
              if (!openHits.has(label)) openHits.set(label, signal.id);
            }
          }
        }
      }

      const candidate = classifySentence(ruleset, sentence, excluded);
      if (candidate) candidates.push(candidate);
    }

    if (!candidates.length) return decision("OPEN", null, []);

    // Most restrictive, then best-evidenced, then longest, then earliest.
    let winner = candidates[0];
    let winnerIndex = 0;
    candidates.forEach((candidate, index) => {
      const a = [
        rank(ruleset, candidate.label),
        candidate.strength,
        candidate.sentence.length,
        -index,
      ];
      const b = [
        rank(ruleset, winner.label),
        winner.strength,
        winner.sentence.length,
        -winnerIndex,
      ];
      for (let i = 0; i < a.length; i++) {
        if (a[i] !== b[i]) {
          if (a[i] > b[i]) {
            winner = candidate;
            winnerIndex = index;
          }
          return;
        }
      }
    });

    const conflict = openHits.get(winner.label);
    if (conflict !== undefined) {
      // The posting contradicts itself. Say so rather than picking a side.
      return decision("UNCLEAR", winner.sentence, winner.ruleIds.concat([conflict]));
    }
    return decision(winner.label, winner.sentence, winner.ruleIds);
  }

  /** The public surface, matching the Python Classifier protocol. */
  function RuleClassifier(rulesDocument) {
    const ruleset = buildRuleSet(rulesDocument);
    return {
      name: CLASSIFIER_NAME,
      ruleset,
      classify: (text) => classifyText(ruleset, text),
    };
  }

  root.JobFilterRules = {
    RuleClassifier,
    buildRuleSet,
    splitSentences,
    LABELS: Object.keys({
      RESTRICTED_CITIZEN: 0,
      RESTRICTED_US_PERSON: 0,
      RESTRICTED_CLEARANCE: 0,
      INFO_NO_SPONSORSHIP: 0,
      UNCLEAR: 0,
      OPEN: 0,
    }),
    RESTRICTIONS,
  };

  if (typeof module !== "undefined" && module.exports) {
    module.exports = root.JobFilterRules;
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
