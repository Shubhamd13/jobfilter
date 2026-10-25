/*
 * Read a job posting out of a page: LinkedIn, Indeed, Workday, or any page on
 * request.
 *
 * Pure functions of (document, url). Nothing here touches chrome.* or the
 * network, so tests run it against saved HTML in headless Chrome.
 *
 * Every field has several selector candidates, newest DOM first. The result
 * records which selector produced each field (`extraction`), so when a site
 * changes its markup the fixture tests name the selector that died instead of
 * the extension silently going blank.
 */
(function (root) {
  "use strict";

  // --- HTML -> text (mirror of jobfilter/fetchers/textutil.py) ------------
  //
  // The classifier splits on line breaks, so block structure must survive the
  // same way it does on the Python side. test_extension.py diffs the two on
  // the real Greenhouse and Lever fixtures.

  const BLOCK_TAGS = new Set([
    "address", "article", "blockquote", "br", "div", "dd", "dl", "dt",
    "fieldset", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
    "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
    "table", "td", "th", "tr", "ul",
  ]);
  // The Python list (script, style, head, title) plus page chrome that never
  // appears in an API response but does in a live page: LinkedIn's "Show
  // more" buttons, icon SVGs, templates.
  const SKIP_TAGS = new Set([
    "script", "style", "head", "title",
    "noscript", "template", "svg", "button",
  ]);
  const VOID_TAGS = new Set(["br", "hr", "img", "input", "meta", "link", "wbr"]);

  function htmlToText(node) {
    if (!node) return "";
    const out = [];
    let pendingBullet = false;

    function start(tag) {
      if (tag === "li") {
        out.push("\n");
        // As in Python: write "- " only once real text arrives, so a
        // <li><p>text</p></li> does not strand the bullet on its own line.
        pendingBullet = true;
      } else if (BLOCK_TAGS.has(tag) && !pendingBullet) {
        out.push("\n");
      }
    }

    function end(tag) {
      if (tag === "li") pendingBullet = false;
      if (BLOCK_TAGS.has(tag)) out.push("\n");
    }

    function data(text) {
      if (pendingBullet && text.trim()) {
        out.push("- ");
        pendingBullet = false;
      }
      out.push(text);
    }

    function walk(current) {
      for (let child = current.firstChild; child; child = child.nextSibling) {
        if (child.nodeType === 3) {
          data(child.nodeValue);
        } else if (child.nodeType === 1) {
          const tag = child.localName;
          if (SKIP_TAGS.has(tag) || child.getAttribute("aria-hidden") === "true") {
            continue;
          }
          start(tag);
          if (!VOID_TAGS.has(tag)) {
            walk(child.localName === "template" ? child.content : child);
            end(tag);
          }
        }
      }
    }

    walk(node);
    return normalizeText(out.join(""));
  }

  function normalizeText(text) {
    if (!text) return "";
    return text
      .replace(/ /g, " ")
      .replace(/\r\n?/g, "\n")
      .replace(/[ \t]{2,}/g, " ")
      .replace(/[ \t]+$/gm, "")
      .replace(/^[ \t]+/gm, "")
      .replace(/\n{3,}/g, "\n\n")
      .trim();
  }

  const LOOKS_LIKE_MARKUP = /<\/?[a-z][a-z0-9]*[^<>]*>/i;

  /**
   * Text of an HTML string (JSON-LD descriptions are HTML).
   *
   * Some sites entity-escape it a second time (`&lt;p&gt;`), as Greenhouse's
   * API does. One parse then yields markup as text, so parse once more.
   */
  function markupToText(markup, doc) {
    if (!markup) return "";
    const parse = (source) => {
      const parsed = new DOMParser().parseFromString(String(source), "text/html");
      return parsed.body || parsed.documentElement || doc;
    };
    let body = parse(markup);
    if (LOOKS_LIKE_MARKUP.test(body.textContent)) body = parse(body.textContent);
    return htmlToText(body);
  }

  // --- selector helpers ---------------------------------------------------

  /**
   * First selector that matches an element with text. Reports which.
   *
   * A selector may contain `{jobId}`, filled in from the URL, so it only
   * matches the posting the user is looking at -- not a "similar jobs" card.
   * Job ids are digits (LinkedIn) or hex (Indeed), so they are selector-safe.
   */
  function pick(doc, selectors, minChars, jobId) {
    for (const selector of selectors) {
      if (selector.includes("{jobId}") && !jobId) continue;
      let element = null;
      try {
        element = doc.querySelector(selector.split("{jobId}").join(jobId));
      } catch (_) {
        continue; // a bad selector must not take the whole page down
      }
      if (!element) continue;
      const text = htmlToText(element);
      if (text.length >= (minChars || 1)) return { element, text, selector };
    }
    return null;
  }

  function oneLine(text) {
    const line = (text || "").replace(/\s+/g, " ").trim();
    return line || null;
  }

  function field(doc, selectors, extraction, name, jobId) {
    const hit = pick(doc, selectors, 1, jobId);
    extraction[name] = hit ? hit.selector : null;
    return hit ? oneLine(hit.text) : null;
  }

  // --- JSON-LD JobPosting: the structured fallback ------------------------
  //
  // Search engines index job pages through schema.org JobPosting, so many
  // career pages (and LinkedIn's logged-out view) carry it. It is the most
  // stable thing on a page: it does not change when the CSS does.

  function findJsonLdPosting(doc) {
    for (const script of doc.querySelectorAll('script[type="application/ld+json"]')) {
      let parsed;
      try {
        parsed = JSON.parse(script.textContent);
      } catch (_) {
        continue;
      }
      const found = findJobPosting(parsed);
      if (found) return found;
    }
    return null;
  }

  function findJobPosting(value) {
    if (!value || typeof value !== "object") return null;
    if (Array.isArray(value)) {
      for (const item of value) {
        const found = findJobPosting(item);
        if (found) return found;
      }
      return null;
    }
    const type = value["@type"];
    if (type === "JobPosting" || (Array.isArray(type) && type.includes("JobPosting"))) {
      return value;
    }
    return findJobPosting(value["@graph"]);
  }

  function jsonLdFields(posting, doc) {
    const org = posting.hiringOrganization;
    const places = [].concat(posting.jobLocation || []);
    const address = places.length ? places[0].address || {} : {};
    const location = [address.addressLocality, address.addressRegion, address.addressCountry]
      .map((part) => (part && typeof part === "object" ? part.name : part))
      .filter(Boolean)
      .join(", ");
    const identifier = posting.identifier;
    return {
      title: oneLine(posting.title),
      company: oneLine(typeof org === "string" ? org : org && org.name),
      location: location || (posting.jobLocationType === "TELECOMMUTE" ? "Remote" : null),
      text: markupToText(posting.description, doc),
      jobId: oneLine(
        identifier && typeof identifier === "object" ? identifier.value : identifier
      ),
    };
  }

  // --- sites ----------------------------------------------------------------

  /** The first element matching `selector` that comes after `anchor`, within `scope`. */
  function nextAfter(scope, anchor, selector) {
    for (const element of scope.querySelectorAll(selector)) {
      if (anchor.contains(element)) continue;
      if (anchor.compareDocumentPosition(element) & 4 /* FOLLOWING */) {
        if (oneLine(htmlToText(element))) return element;
      }
    }
    return null;
  }

  /** The last element matching `selector` that comes before `anchor`, within `scope`. */
  function previousBefore(scope, anchor, selector) {
    let found = null;
    for (const element of scope.querySelectorAll(selector)) {
      if (element.contains(anchor)) continue;
      if (!(anchor.compareDocumentPosition(element) & 2 /* PRECEDING */)) break;
      if (oneLine(htmlToText(element))) found = element;
    }
    return found;
  }

  /**
   * LinkedIn's 2026 top card has no stable classes, only order:
   * company, then a <p> with the title, then a <p> "City, ST · 1 day ago · …".
   *
   * Two layouts, both under [data-sdui-screen$="JobDetails"]:
   *   /jobs/view/{id}       company block has aria-label "Company, X." and the
   *                         job id in its componentkey; the title follows it.
   *   /jobs/search-results  company is a bare <p> beside the logo; the title
   *                         is a link to /jobs/view/{id}/.
   */
  function linkedInTopCard(doc, jobId) {
    const scope = doc.querySelector('[data-sdui-screen$="JobDetails"]') || doc;
    let title = null;
    let company = null;
    const labelled = scope.querySelector(`[aria-label^="Company, "][componentkey$="-${jobId}"]`);
    if (labelled) {
      const anchor = labelled.closest("a") || labelled;
      title = nextAfter(scope, anchor, "p, h1, h2");
      company = labelled.querySelector("p") || labelled;
    } else {
      const link = scope.querySelector(`p a[href*="/jobs/view/${jobId}/"]`);
      title = link && link.closest("p");
      company = title && previousBefore(scope, title, "p");
    }
    const meta = title && nextAfter(scope, title, "p");
    const metaText = meta ? oneLine(htmlToText(meta)) : null;
    return {
      title: title ? oneLine(htmlToText(title)) : null,
      company: company ? oneLine(htmlToText(company)) : null,
      location: metaText ? oneLine(metaText.split("·")[0]) : null,
      metaText,
    };
  }

  // --- listing signals: "2 weeks ago", "Over 100 applicants" ---------------
  //
  // Kept as the site's own words, not converted to dates: "2 weeks ago" is
  // only meaningful next to when it was read, which the server records.

  const POSTED_PART = /\b(?:ago|just now|today|yesterday|(?:re)?posted)\b/i;
  const APPLICANTS_PART = /\b(?:applicants?|clicked apply)\b/i;

  /** "Palo Alto, CA · Reposted 2 weeks ago · Over 100 applicants" -> its parts. */
  function parseListingMeta(text) {
    const parts = (text || "").split("·").map(oneLine).filter(Boolean);
    return {
      postedAgo: parts.find((part) => POSTED_PART.test(part)) || null,
      applicants: parts.find((part) => APPLICANTS_PART.test(part)) || null,
    };
  }

  /** The top card's "City · 2 weeks ago · N applicants" line, in any layout. */
  function linkedInListingMeta(doc, jobId) {
    const containers = pick(doc, [
      ".job-details-jobs-unified-top-card__primary-description-container",
      ".job-details-jobs-unified-top-card__tertiary-description-container",
      ".jobs-unified-top-card__primary-description",
    ]);
    if (containers) return { ...parseListingMeta(containers.text), selector: containers.selector };

    const card = linkedInTopCard(doc, jobId);
    if (card.metaText) return { ...parseListingMeta(card.metaText), selector: "top-card" };

    // Logged-out page: separate spans, no "·" line.
    const posted = pick(doc, [".posted-time-ago__text", ".topcard__flavor--metadata"]);
    const applicants = pick(doc, [".num-applicants__caption"]);
    if (!posted && !applicants) return { postedAgo: null, applicants: null, selector: null };
    return {
      postedAgo: posted ? oneLine(posted.text) : null,
      applicants: applicants ? oneLine(applicants.text) : null,
      selector: (posted || applicants).selector,
    };
  }

  const LINKEDIN = {
    id: "linkedin",
    host: /(^|\.)linkedin\.com$/i,
    jobId(url) {
      const fromPath = url.pathname.match(/^\/jobs\/view\/(?:[^/]*?-)?(\d{6,})\/?/);
      if (fromPath) return fromPath[1];
      const param = url.searchParams.get("currentJobId");
      return param && /^\d+$/.test(param) ? param : null;
    },
    canonicalUrl(url, jobId) {
      return `https://www.linkedin.com/jobs/view/${jobId}/`;
    },
    selectors: {
      // Logged-in job view and search right pane, then the logged-out page.
      description: [
        // 2026 server-driven UI: hashed class names, but a stable section id.
        // (Its "expandable-text-box" holds only the first paragraph.)
        "#JobDetails_AboutTheJob_{jobId}",
        "#job-details",
        ".jobs-description__content .jobs-box__html-content",
        ".jobs-description-content__text",
        ".jobs-description__content",
        ".show-more-less-html__markup",
        ".description__text",
      ],
      title: [
        ".job-details-jobs-unified-top-card__job-title h1",
        ".job-details-jobs-unified-top-card__job-title",
        ".jobs-unified-top-card__job-title",
        "h1.top-card-layout__title",
        "h1.topcard__title",
      ],
      company: [
        // 2026 UI: <div aria-label="Company, NVIDIA." componentkey="…-{jobId}">.
        '[aria-label^="Company, "][componentkey$="-{jobId}"] p',
        ".job-details-jobs-unified-top-card__company-name a",
        ".job-details-jobs-unified-top-card__company-name",
        ".jobs-unified-top-card__company-name a",
        ".jobs-unified-top-card__company-name",
        "a.topcard__org-name-link",
        ".topcard__flavor a",
      ],
      location: [
        ".job-details-jobs-unified-top-card__primary-description-container .tvm__text",
        ".job-details-jobs-unified-top-card__bullet",
        ".jobs-unified-top-card__bullet",
        ".topcard__flavor--bullet",
      ],
    },
    topCard: linkedInTopCard,
    listingMeta: linkedInListingMeta,
    // Last resort when every class name has changed: the visible heading.
    descriptionHeading: /^about the job$/i,
    // Job pages read "Title | Company | LinkedIn"; titles often contain " - ".
    tabTitle(title, url) {
      if (!/^\/jobs\/view\//.test(url.pathname)) return null;
      const parts = title.replace(/^\(\d+\+?\)\s*/, "").split(" | ");
      if (parts.length < 2 || !/^LinkedIn$/i.test(parts[parts.length - 1].trim())) return null;
      return { title: parts[0], company: parts.length > 2 ? parts[parts.length - 2] : null };
    },
  };

  const INDEED = {
    id: "indeed",
    host: /(^|\.)indeed\.com$/i,
    jobId(url) {
      const jk = url.searchParams.get("jk") || url.searchParams.get("vjk");
      return jk && /^[0-9a-f]{8,}$/i.test(jk) ? jk.toLowerCase() : null;
    },
    canonicalUrl(url, jobId) {
      return `${url.protocol}//${url.host}/viewjob?jk=${jobId}`;
    },
    selectors: {
      description: [
        "#jobDescriptionText",
        // 2026 React Native Web pane: only data-testids are stable.
        '[data-testid="vj-job-description-heading"] + div',
        '[data-testid="jobsearch-JobComponent-description"]',
        ".jobsearch-jobDescriptionText",
        ".jobsearch-JobComponent-description",
      ],
      title: [
        '[data-testid="jobsearch-JobInfoHeader-title"]',
        '[data-testid="vj-job-title"]',
        '[data-testid="vj-job-title-compact"]',
        "h1.jobsearch-JobInfoHeader-title",
        ".jobsearch-JobInfoHeader-title",
      ],
      company: [
        '[data-testid="inlineHeader-companyName"]',
        '[data-testid="company-info-metadata"] a[href*="/cmp/"]',
        '[data-company-name="true"]',
        ".jobsearch-CompanyInfoContainer a",
      ],
      location: [
        '[data-testid="inlineHeader-companyLocation"]',
        // Company link + rating come first; the location is the last block.
        '[data-testid="company-info-metadata"] > div > div:last-child',
        '[data-testid="job-location"]',
        '[data-testid="jobsearch-JobInfoHeader-companyLocation"]',
      ],
    },
    // Indeed appends a screen-reader suffix to the title.
    cleanTitle: (title) => title && title.replace(/\s*-\s*job post$/i, ""),
    descriptionHeading: /^(full )?job description$/i,
    // "Title - City, ST - Indeed.com" on /viewjob only: on a search page the
    // tab names the search ("Now Hiring: 700 … Jobs"), not the open posting.
    tabTitle(title, url) {
      if (!/^\/viewjob/.test(url.pathname)) return null;
      const stripped = title.replace(/\s*-\s*Indeed(\.com)?\s*$/i, "");
      if (stripped === title) return null;
      return { title: stripped.split(" - ")[0], company: null };
    },
  };

  // --- Workday: {tenant}.wd{N}.myworkdayjobs.com ---------------------------
  //
  // Two layouts, both read from live pages (2026-10):
  //   /{locale}/{site}/job/[{location}/]{Title}_{reqId}   the full job page
  //   /{locale}/{site}/details/{Title}_{reqId}            the pane beside the
  //                                                       search results
  // Classes are hashed; `data-automation-id` attributes are the stable hooks.
  // The results list reuses "locations" / "postedOn", so every detail
  // selector is scoped to the posting's own block.

  const WORKDAY_SCOPES = [
    '[data-automation-id="jobDetails"]', // search-results pane
    '[data-automation-id="job-posting-details"]', // full job page
  ];
  const WORKDAY_PATH = /^(.*?)\/(?:job|details)\/(?:[^/]+\/)*?([^/]*_([A-Za-z0-9][A-Za-z0-9-]*))(?:\/|$)/;

  function workdayDetail(doc, automationId) {
    for (const scope of WORKDAY_SCOPES) {
      const values = [...doc.querySelectorAll(`${scope} [data-automation-id="${automationId}"] dd`)]
        .map((dd) => oneLine(htmlToText(dd)))
        .filter(Boolean);
      if (values.length) return { text: values.join("; "), selector: scope };
    }
    return null;
  }

  /** The URL's id against the one the page shows; a repost may add "-1" to the URL's. */
  function sameJobId(urlId, pageId) {
    return !pageId || urlId === pageId || urlId.startsWith(`${pageId}-`);
  }

  const WORKDAY = {
    id: "workday",
    host: /(^|\.)myworkdayjobs\.com$/i,
    jobId(url) {
      const match = url.pathname.match(WORKDAY_PATH);
      return match ? match[3] : null;
    },
    // The full job page without the optional location segment: what the
    // pane's "Open in new window" link and the page's canonical tag use.
    canonicalUrl(url, jobId) {
      const [, prefix, segment] = url.pathname.match(WORKDAY_PATH);
      return `${url.origin}${prefix}/job/${segment}`;
    },
    // Workday's req ids are only unique within one tenant, and the page
    // never names the company (the JSON-LD has a legal entity, "2100 NVIDIA
    // USA"). The tenant does, and slugs the same as LinkedIn's "NVIDIA".
    companyFromUrl(url) {
      return url.hostname.split(".")[0].toLowerCase();
    },
    // The pane swaps a beat after the URL changes. Until then the page still
    // shows the previous job, which must not be filed under the new id.
    showsJob(doc, jobId) {
      const shown = workdayDetail(doc, "requisitionId");
      return sameJobId(jobId, shown && shown.text);
    },
    selectors: {
      description: [
        '[data-automation-id="jobDetails"] [data-automation-id="jobPostingDescription"]',
        '[data-automation-id="jobPostingPage"] [data-automation-id="jobPostingDescription"]',
        '[data-automation-id="jobPostingDescription"]',
      ],
      title: [
        '[data-automation-id="jobDetails"] [data-automation-id="jobPostingHeader"]',
        '[data-automation-id="jobPostingHeader"]',
      ],
      company: [],
      location: [],
    },
    // A posting can list several locations, one <dd> each.
    topCard(doc) {
      const locations = workdayDetail(doc, "locations");
      return { title: null, company: null, location: locations && locations.text };
    },
    listingMeta(doc) {
      const posted = workdayDetail(doc, "postedOn");
      return {
        postedAgo: posted ? posted.text : null,
        applicants: null,
        selector: posted ? posted.selector : null,
      };
    },
  };

  const SITES = [LINKEDIN, INDEED, WORKDAY];

  // Generic pages: where descriptions usually live, broadest last.
  const GENERIC_CANDIDATES = [
    '[class*="job-description" i]',
    '[id*="job-description" i]',
    '[class*="jobdescription" i]',
    '[class*="description" i]',
    '[id*="description" i]',
    '[class*="job-details" i]',
    '[class*="posting" i]',
    "article",
    "main",
    '[role="main"]',
  ];
  const MIN_DESCRIPTION_CHARS = 200;

  function siteFor(url) {
    return SITES.find((site) => site.host.test(url.hostname)) || null;
  }

  /** Find the element after a heading like "About the job". */
  function byHeading(doc, pattern) {
    for (const heading of doc.querySelectorAll("h1, h2, h3, h4, strong")) {
      if (!pattern.test(oneLine(heading.textContent) || "")) continue;
      // Climb until the container holds more than the heading itself.
      let container = heading.parentElement;
      while (container && htmlToText(container).length < MIN_DESCRIPTION_CHARS) {
        container = container.parentElement;
      }
      if (container) return htmlToText(container);
    }
    return null;
  }

  function extractKnown(site, doc, url) {
    const jobId = site.jobId(url);
    if (!jobId) return { ok: false, site: site.id, reason: "not-a-job-page" };
    if (site.showsJob && !site.showsJob(doc, jobId)) {
      return { ok: false, site: site.id, jobId, reason: "page-shows-another-job" };
    }

    const extraction = {};
    const description = pick(doc, site.selectors.description, 1, jobId);
    let text = description ? description.text : "";
    extraction.description = description ? description.selector : null;

    // A single-page app can keep the JSON-LD of the first job it loaded
    // after the user has clicked on to another one.
    const fallback = findJsonLdPosting(doc);
    let ld = fallback ? jsonLdFields(fallback, doc) : null;
    if (ld && ld.jobId && !sameJobId(jobId, ld.jobId)) ld = null;

    if (!text && ld && ld.text) {
      text = ld.text;
      extraction.description = "json-ld";
    }
    if (!text && site.descriptionHeading) {
      text = byHeading(doc, site.descriptionHeading) || "";
      if (text) extraction.description = "heading";
    }

    let title = field(doc, site.selectors.title, extraction, "title", jobId);
    if (site.cleanTitle) title = site.cleanTitle(title);
    let company = field(doc, site.selectors.company, extraction, "company", jobId);
    let location = field(doc, site.selectors.location, extraction, "location", jobId);
    if (!company && site.companyFromUrl) {
      [company, extraction.company] = [site.companyFromUrl(url), "url"];
    }
    if (site.topCard && (!title || !company || !location)) {
      const card = site.topCard(doc, jobId);
      if (!title && card.title) [title, extraction.title] = [card.title, "top-card"];
      if (!company && card.company) [company, extraction.company] = [card.company, "top-card"];
      if (!location && card.location) [location, extraction.location] = [card.location, "top-card"];
    }
    if (ld) {
      if (!title && ld.title) [title, extraction.title] = [ld.title, "json-ld"];
      if (!company && ld.company) [company, extraction.company] = [ld.company, "json-ld"];
      if (!location && ld.location) [location, extraction.location] = [ld.location, "json-ld"];
    }
    const tab = (!title || !company) && site.tabTitle ? site.tabTitle(doc.title || "", url) : null;
    if (tab) {
      if (!title && oneLine(tab.title)) [title, extraction.title] = [oneLine(tab.title), "document.title"];
      if (!company && oneLine(tab.company)) [company, extraction.company] = [oneLine(tab.company), "document.title"];
    }

    let postedAgo = null;
    let applicants = null;
    if (site.listingMeta) {
      const listing = site.listingMeta(doc, jobId);
      [postedAgo, applicants, extraction.listing] = [listing.postedAgo, listing.applicants, listing.selector];
    }

    const base = {
      site: site.id,
      jobId,
      url: site.canonicalUrl(url, jobId),
      title,
      company,
      location,
      postedAgo,
      applicants,
      extraction,
    };
    if (!text) {
      // A job page whose description we could not read. Say so: a blank
      // badge would look exactly like "no restriction".
      return { ok: false, reason: "description-not-found", ...base };
    }
    return { ok: true, text, ...base };
  }

  function extractGeneric(doc, url) {
    const extraction = {};
    const ldPosting = findJsonLdPosting(doc);
    const ld = ldPosting ? jsonLdFields(ldPosting, doc) : null;

    let text = ld && ld.text;
    if (text) {
      extraction.description = "json-ld";
    } else {
      let best = null;
      for (const selector of GENERIC_CANDIDATES) {
        let elements;
        try {
          elements = doc.querySelectorAll(selector);
        } catch (_) {
          continue;
        }
        for (const element of elements) {
          const candidate = htmlToText(element);
          if (candidate.length >= MIN_DESCRIPTION_CHARS && (!best || candidate.length > best.text.length)) {
            best = { text: candidate, selector };
          }
        }
        // The first candidate family that yields real text wins: a
        // "job-description" div beats the whole <main> around it.
        if (best) break;
      }
      if (best) {
        text = best.text;
        extraction.description = best.selector;
      } else {
        text = htmlToText(doc.body);
        extraction.description = text ? "body" : null;
      }
    }

    const meta = (name) => {
      const tag = doc.querySelector(`meta[property="${name}"], meta[name="${name}"]`);
      return tag ? oneLine(tag.getAttribute("content")) : null;
    };
    const h1 = pick(doc, ["h1"]);
    const title = (ld && ld.title) || (h1 && oneLine(h1.text)) || meta("og:title") || oneLine(doc.title);
    extraction.title = ld && ld.title ? "json-ld" : h1 ? "h1" : "meta/title";

    const base = {
      site: "web",
      jobId: (ld && ld.jobId) || null,
      url: `${url.origin}${url.pathname}${url.search}`,
      title: title || null,
      company: (ld && ld.company) || meta("og:site_name") || null,
      location: (ld && ld.location) || null,
      extraction,
    };
    if (!text) return { ok: false, reason: "description-not-found", ...base };
    return { ok: true, text, ...base };
  }

  /**
   * The one entry point.
   *
   * Known sites are read automatically. Any other page only when `generic`
   * is set -- that is the user pressing "Scan this page" in the popup.
   */
  function extractPosting(doc, href, options) {
    let url;
    try {
      url = new URL(href);
    } catch (_) {
      return { ok: false, site: null, reason: "bad-url" };
    }
    const site = siteFor(url);
    if (site) return extractKnown(site, doc, url);
    if (options && options.generic) return extractGeneric(doc, url);
    return { ok: false, site: null, reason: "unsupported-site" };
  }

  root.JobFilterExtract = {
    extractPosting,
    htmlToText,
    normalizeText,
    parseListingMeta,
    findJsonLdPosting,
    SITES,
    GENERIC_CANDIDATES,
  };
})(typeof globalThis !== "undefined" ? globalThis : this);
