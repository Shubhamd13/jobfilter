# Extension page fixtures

Hand-built from the published DOM shape of each site (no logged-in pages were
scraped), each carrying the clutter a real page has: buttons, icons, the job
list beside the open posting, EEO boilerplate.

The `*_sdui_*` / `*_rnw_*` files follow the 2026 LinkedIn (job view and
search-results pane) and Indeed markup,
modelled on real downloads (hashed classes; ids, `componentkey` and
`data-testid` hooks). Downloads now drop the page's scripts and start with
`<!-- jobfilter snapshot of URL -->`, so the case URL is in the file.

When a site changes its markup and a test names a dead selector, replace the
matching file with a real page: open the posting, extension popup → Developer →
Download page HTML, scrub your name / profile / messages from it, and drop it
here under the same name. tests/test_extension.py lists what each file must
yield.

`workday_*.html` are real NVIDIA pages (2026-10): the full job page as saved from the
browser, and the search-results pane captured live from headless Chrome. Scripts,
styles, SVG icons and stylesheet links are stripped; JSON-LD is kept.
