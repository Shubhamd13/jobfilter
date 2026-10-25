"""Web API and server. Offline: the checker is faked, the store is :memory:."""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from typing import Sequence

import pytest

from jobfilter.classifiers import RuleClassifier
from jobfilter.fetchers import HttpClient
from jobfilter.models import Change, CompanyRefresh, Label, Origin, Platform, Posting, Result, Status
from jobfilter.storage import Store
from jobfilter.web import Api, build_server, make_refresher, parse_url_blob
from jobfilter.web.api import DEFAULT_PAGE_SIZE, MAX_PAGE_SIZE, MAX_URLS_PER_REQUEST

from .conftest import LEVER_SLUG, load_fixture
from .test_refresh import LEVER_BOARD_URL, Boards, seed_board
from .test_storage import make_posting, make_result


@pytest.fixture
def store() -> Store:
    with Store(":memory:") as store:
        yield store


@pytest.fixture
def checked() -> list[Sequence[str]]:
    """Records what the API asked the checker to fetch."""
    return []


@pytest.fixture
def api(store: Store, checked: list) -> Api:
    def fake_checker(urls: Sequence[str], expand: bool = False) -> list[Result]:
        checked.append((list(urls), expand))
        results = [
            make_result(
                make_posting(slug="acme", job_id=f"j{i}", title=f"Job {i}"),
                label=Label.RESTRICTED_CITIZEN if i == 0 else Label.OPEN,
                evidence="Must be a U.S. citizen." if i == 0 else None,
                matched_rule="must-be-us-citizen" if i == 0 else None,
            )
            for i, _ in enumerate(urls)
        ]
        if expand:
            results += [
                make_result(
                    make_posting(slug="acme", job_id="found1", title="Found Job"),
                    label=Label.OPEN,
                    origin=Origin.DISCOVERED,
                )
            ]
        return results

    return Api(store, fake_checker)


# --------------------------------------------------------------------------
# parse_url_blob
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param("a\nb", ["a", "b"], id="newlines"),
        pytest.param("a b c", ["a", "b", "c"], id="spaces"),
        pytest.param("  a  \n\n  b  ", ["a", "b"], id="blank-lines-and-padding"),
        pytest.param("# note\na", ["a"], id="comment-line"),
        pytest.param("a  # not a comment mid-line", ["a", "#", "not", "a", "comment", "mid-line"], id="hash-only-starts-a-line"),
        pytest.param("", [], id="empty"),
        pytest.param("   \n  ", [], id="whitespace-only"),
    ],
)
def test_parse_url_blob(text: str, expected: list[str]) -> None:
    assert parse_url_blob(text) == expected


# --------------------------------------------------------------------------
# Routing
# --------------------------------------------------------------------------

@pytest.mark.parametrize(
    "method,path",
    [
        pytest.param("GET", "/api/nope", id="unknown-get"),
        pytest.param("POST", "/api/stats", id="wrong-method"),
        pytest.param("DELETE", "/api/postings", id="unsupported-method"),
        pytest.param("GET", "/api/postings/abc", id="non-numeric-id"),
    ],
)
def test_unknown_routes_are_404(api: Api, method: str, path: str) -> None:
    response = api.handle(method, path)
    assert response.status == 404
    assert "error" in response.payload


# --------------------------------------------------------------------------
# GET endpoints
# --------------------------------------------------------------------------

def test_stats_on_an_empty_database(api: Api) -> None:
    response = api.handle("GET", "/api/stats")
    assert response.status == 200
    assert response.payload["stats"]["postings"] == 0
    assert response.payload["companies"] == []
    assert response.payload["labels"] == [str(label) for label in Label]


def test_postings_is_empty_before_anything_is_checked(api: Api) -> None:
    assert api.handle("GET", "/api/postings").payload["postings"] == []


def test_posting_detail_404s_for_an_unknown_id(api: Api) -> None:
    assert api.handle("GET", "/api/postings/4242").status == 404


# --------------------------------------------------------------------------
# POST /api/check
# --------------------------------------------------------------------------

def test_check_runs_the_pipeline_and_records_it(
    api: Api, store: Store, checked: list
) -> None:
    response = api.handle("POST", "/api/check", body={"urls": "u1\nu2"})

    assert response.status == 200
    assert checked == [(["u1", "u2"], False)]
    assert response.payload["checked"] == 2
    assert response.payload["run_id"] == 1
    assert store.stats()["postings"] == 2
    assert store.stats()["runs"] == 1


def test_check_results_are_serializable(api: Api) -> None:
    payload = api.handle("POST", "/api/check", body={"urls": "u1"}).payload
    json.dumps(payload)  # would raise if a dataclass leaked through
    assert payload["results"][0]["label"] == "RESTRICTED_CITIZEN"


@pytest.mark.parametrize(
    "body", [{}, {"urls": ""}, {"urls": "   \n  "}, {"urls": "# only a comment"}]
)
def test_check_with_no_urls_is_a_400(api: Api, body: dict) -> None:
    response = api.handle("POST", "/api/check", body=body)
    assert response.status == 400
    assert "no URLs" in response.payload["error"]


def test_check_refuses_an_oversized_batch(api: Api, checked: list) -> None:
    body = {"urls": "\n".join(f"u{i}" for i in range(MAX_URLS_PER_REQUEST + 1))}
    response = api.handle("POST", "/api/check", body=body)
    assert response.status == 400
    assert str(MAX_URLS_PER_REQUEST) in response.payload["error"]
    assert checked == [], "nothing should have been fetched"


def test_check_passes_the_expand_flag_through(api: Api, checked: list) -> None:
    api.handle("POST", "/api/check", body={"urls": "u1", "expand": True})
    assert checked == [(["u1"], True)]


def test_expanded_check_reports_what_it_discovered(api: Api, store: Store) -> None:
    payload = api.handle(
        "POST", "/api/check", body={"urls": "u1", "expand": True}
    ).payload
    assert payload["requested"] == 1
    assert payload["checked"] == 2
    assert payload["discovered"] == 1
    assert store.stats()["postings"] == 2


def test_an_unexpanded_check_discovers_nothing(api: Api) -> None:
    payload = api.handle("POST", "/api/check", body={"urls": "u1"}).payload
    assert payload["discovered"] == 0


def test_origin_is_recorded_and_exposed(api: Api) -> None:
    api.handle("POST", "/api/check", body={"urls": "u1", "expand": True})
    rows = api.handle("GET", "/api/postings").payload["postings"]
    origins = {r["title"]: r["origin"] for r in rows}
    assert origins == {"Job 0": "requested", "Found Job": "discovered"}


def test_rechecking_updates_rather_than_duplicates(api: Api, store: Store) -> None:
    api.handle("POST", "/api/check", body={"urls": "u1"})
    api.handle("POST", "/api/check", body={"urls": "u1"})
    assert store.stats()["postings"] == 1
    assert store.stats()["runs"] == 2


# --------------------------------------------------------------------------
# Filters reach the store
# --------------------------------------------------------------------------

@pytest.fixture
def populated_api(api: Api) -> Api:
    api.handle("POST", "/api/check", body={"urls": "u1\nu2\nu3"})
    return api


@pytest.mark.parametrize(
    "query,expected",
    [
        pytest.param({}, 3, id="no-filter"),
        pytest.param({"label": ["RESTRICTED_CITIZEN"]}, 1, id="label"),
        pytest.param({"label": ["OPEN"]}, 2, id="label-open"),
        pytest.param({"company": ["acme"]}, 3, id="company"),
        pytest.param({"company": ["nobody"]}, 0, id="unknown-company"),
        pytest.param({"q": ["Job 0"]}, 1, id="search"),
        pytest.param({"q": ["citizen"]}, 1, id="search-evidence"),
        pytest.param({"limit": ["2"]}, 2, id="limit"),
        pytest.param({"limit": ["2"], "offset": ["2"]}, 1, id="offset"),
        pytest.param({"label": [""]}, 3, id="blank-filter-ignored"),
        pytest.param({"limit": ["not-a-number"]}, 3, id="bad-limit-falls-back"),
        pytest.param({"limit": ["-5"]}, 0, id="negative-limit-clamped-to-zero"),
    ],
)
def test_posting_filters(populated_api: Api, query: dict, expected: int) -> None:
    response = populated_api.handle("GET", "/api/postings", query)
    assert len(response.payload["postings"]) == expected


@pytest.mark.parametrize(
    "query,expected",
    [
        pytest.param({"label": ["RESTRICTED_CITIZEN", "OPEN"]}, 3, id="repeated-label"),
        pytest.param({"exclude": ["acme"]}, 0, id="exclude"),
        pytest.param({"exclude": ["other"]}, 3, id="exclude-unknown"),
        pytest.param({"exclude": [""]}, 3, id="blank-exclude-ignored"),
    ],
)
def test_repeated_filters(populated_api: Api, query: dict, expected: int) -> None:
    payload = populated_api.handle("GET", "/api/postings", query).payload
    assert len(payload["postings"]) == expected
    assert payload["total"] == expected


def test_postings_report_the_total_beyond_the_page(populated_api: Api) -> None:
    payload = populated_api.handle(
        "GET", "/api/postings", {"limit": ["2"], "offset": ["2"]}
    ).payload
    assert len(payload["postings"]) == 1
    assert (payload["total"], payload["limit"], payload["offset"]) == (3, 2, 2)


def test_postings_default_and_maximum_page_size(populated_api: Api) -> None:
    assert populated_api.handle("GET", "/api/postings").payload["limit"] == DEFAULT_PAGE_SIZE
    payload = populated_api.handle(
        "GET", "/api/postings", {"limit": ["999999"]}
    ).payload
    assert payload["limit"] == MAX_PAGE_SIZE


@pytest.mark.parametrize(
    "query,expected",
    [
        pytest.param({"region": ["us"]}, 0, id="region"),
        pytest.param({"region": ["unknown"]}, 3, id="region-unknown"),
        pytest.param({"region": ["us", "outside_us", "unknown"]}, 3, id="every-region"),
        pytest.param({"place": ["remote"]}, 3, id="place"),
        pytest.param({"exclude_place": ["Remote"]}, 0, id="exclude-place"),
    ],
)
def test_location_filters_reach_the_store(
    populated_api: Api, query: dict, expected: int
) -> None:
    """make_posting's location is "Remote": unknown region, matched by place."""
    payload = populated_api.handle("GET", "/api/postings", query).payload
    assert payload["total"] == expected


def test_postings_carry_region_counts(populated_api: Api) -> None:
    payload = populated_api.handle("GET", "/api/postings", {"region": ["us"]}).payload
    assert payload["region_counts"] == {"us": 0, "outside_us": 0, "unknown": 3}


def test_place_endpoint_explains_and_counts(populated_api: Api) -> None:
    payload = populated_api.handle("GET", "/api/place", {"q": ["remote"]}).payload
    assert payload == {
        "place": "remote",
        "meaning": "locations containing this text",
        "total": 3,
    }
    usa = populated_api.handle("GET", "/api/place", {"q": ["USA"]}).payload
    assert (usa["meaning"], usa["total"]) == ("every US location", 0)


def test_place_endpoint_needs_q(api: Api) -> None:
    assert api.handle("GET", "/api/place", {"q": [" "]}).status == 400


def test_stats_lists_regions_and_places(populated_api: Api) -> None:
    payload = populated_api.handle("GET", "/api/stats").payload
    assert payload["regions"] == ["us", "outside_us", "unknown"]
    assert payload["places"] == [{"place": "Remote", "total": 3}]


def test_label_counts_follow_every_filter_but_the_label(populated_api: Api) -> None:
    payload = populated_api.handle(
        "GET", "/api/postings", {"label": ["OPEN"], "q": ["Job 0"]}
    ).payload
    assert payload["total"] == 0, "Job 0 is the restricted one"
    assert payload["label_counts"]["RESTRICTED_CITIZEN"] == 1
    assert payload["label_counts"]["OPEN"] == 0


def test_posting_detail_has_what_the_dialog_needs(populated_api: Api) -> None:
    listing = populated_api.handle("GET", "/api/postings").payload["postings"]
    posting_id = next(p["posting_id"] for p in listing if p["label"] != "OPEN")

    detail = populated_api.handle(
        "GET", f"/api/postings/{posting_id}"
    ).payload["posting"]

    assert detail["evidence"] == "Must be a U.S. citizen."
    assert detail["description_text"]
    assert json.loads(detail["matched_rules_json"]) == ["must-be-us-citizen"]
    assert len(detail["history"]) == 1


def test_failures_endpoint(api: Api, store: Store) -> None:
    store.record_run(
        "web",
        [
            make_result(
                None,
                status=Status.UNSUPPORTED,
                label=Label.UNCLEAR,
                url="https://linkedin.com/x",
                error="unsupported",
            )
        ],
    )
    failures = api.handle("GET", "/api/failures").payload["failures"]
    assert len(failures) == 1
    assert failures[0]["url_as_given"] == "https://linkedin.com/x"


# --------------------------------------------------------------------------
# Board refresh: POST starts a background job, GET reports it
# --------------------------------------------------------------------------

class GatedRefresher:
    """A fake refresh that reports its first board, then waits to be released."""

    def __init__(self, fail: bool = False) -> None:
        self.calls: list[tuple[list[str], bool]] = []
        self.release = threading.Event()
        self._fail = fail

    def __call__(self, boards, deep, progress) -> list[CompanyRefresh]:
        self.calls.append(([board.slug for board in boards], deep))
        progress([], len(boards), boards[0])
        assert self.release.wait(5), "the test never released the refresh"
        if self._fail:
            raise RuntimeError("boom")
        outcomes = [
            CompanyRefresh(
                board.platform, board.slug, board.name, "ok", listed=3,
                changes={Change.NEW: 1} if index == 0 else {},
            )
            for index, board in enumerate(boards)
        ]
        progress(outcomes, len(boards), None)
        return outcomes


def refresh_api(store: Store, refresher) -> Api:
    """acme and zeta on Lever, plus a LinkedIn company that has no board."""
    store.record_run("t", [
        make_result(make_posting(slug="acme", job_id="a1")),
        make_result(make_posting(slug="zeta", job_id="z1")),
        make_result(make_posting(slug="seen", job_id="l1", platform=Platform.LINKEDIN)),
    ])
    return Api(store, lambda urls, expand=False: [], refresher=refresher)


def test_refresh_state_before_any_refresh(api: Api) -> None:
    state = api.handle("GET", "/api/refresh").payload["refresh"]
    assert state["running"] is False and state["results"] == []
    assert state["started_at"] is None


def test_refresh_without_a_refresher_is_a_503(api: Api) -> None:
    assert api.handle("POST", "/api/refresh", body={}).status == 503


def test_refresh_runs_in_the_background_and_reports_progress(store: Store) -> None:
    refresher = GatedRefresher()
    api = refresh_api(store, refresher)

    started = api.handle("POST", "/api/refresh", body={})
    assert started.status == 202
    assert started.payload["refresh"]["running"] is True
    assert started.payload["refresh"]["total"] == 2  # the LinkedIn company is not asked

    # While it runs: progress on GET, and a second start is refused.
    for _ in range(500):
        state = api.handle("GET", "/api/refresh").payload["refresh"]
        if state["current"]:
            break
        threading.Event().wait(0.01)
    assert state["running"] and state["done"] == 0
    assert state["current"] == {"platform": "lever", "slug": "acme", "name": None}
    again = api.handle("POST", "/api/refresh", body={})
    assert again.status == 409 and again.payload["refresh"]["running"] is True
    assert len(refresher.calls) == 1

    refresher.release.set()
    api.refresh_job.join(5)
    state = api.handle("GET", "/api/refresh").payload["refresh"]
    assert state["running"] is False and state["error"] is None
    assert state["done"] == state["total"] == 2 and state["current"] is None
    assert state["started_at"] and state["finished_at"]
    assert [(r["slug"], r["changes"], r["changed"]) for r in state["results"]] == [
        ("acme", {"new": 1}, 1), ("zeta", {}, 0),
    ]
    json.dumps(state)  # what the server will send

    # Finished: the next one may start.
    assert api.handle("POST", "/api/refresh", body={}).status == 202
    api.refresh_job.join(5)


def test_refresh_can_be_narrowed_to_some_companies(store: Store) -> None:
    refresher = GatedRefresher()
    refresher.release.set()
    api = refresh_api(store, refresher)

    body = {"companies": [{"platform": "lever", "slug": "zeta"}], "deep": True}
    assert api.handle("POST", "/api/refresh", body=body).status == 202
    api.refresh_job.join(5)
    assert refresher.calls == [(["zeta"], True)]
    assert api.handle("GET", "/api/refresh").payload["refresh"]["deep"] is True


@pytest.mark.parametrize(
    "companies, message",
    [
        ([{"platform": "lever", "slug": "nope"}], "no stored company with a board: lever:nope"),
        # Stored, but LinkedIn has no board to re-read.
        ([{"platform": "linkedin", "slug": "seen"}], "linkedin:seen"),
        ([], "non-empty list"),
        ("acme", "non-empty list"),
        ([{"slug": "acme"}], "platform and a slug"),
        (["acme"], "platform and a slug"),
    ],
)
def test_refresh_refuses_companies_it_cannot_refresh(
    store: Store, companies: object, message: str
) -> None:
    refresher = GatedRefresher()
    api = refresh_api(store, refresher)
    response = api.handle("POST", "/api/refresh", body={"companies": companies})
    assert response.status == 400 and message in response.payload["error"]
    assert refresher.calls == []


def test_refresh_with_nothing_stored_is_a_400(store: Store) -> None:
    api = Api(store, lambda urls, expand=False: [], refresher=GatedRefresher())
    response = api.handle("POST", "/api/refresh", body={})
    assert response.status == 400 and "no stored companies" in response.payload["error"]


def test_a_refresh_that_dies_says_so_and_frees_the_job(store: Store) -> None:
    refresher = GatedRefresher(fail=True)
    refresher.release.set()
    api = refresh_api(store, refresher)

    api.handle("POST", "/api/refresh", body={})
    api.refresh_job.join(5)
    state = api.handle("GET", "/api/refresh").payload["refresh"]
    assert state["running"] is False and state["error"] == "RuntimeError: boom"
    assert api.handle("POST", "/api/refresh", body={}).status == 202
    api.refresh_job.join(5)


def test_stats_say_which_companies_can_be_refreshed(store: Store) -> None:
    api = refresh_api(store, GatedRefresher())
    rows = {r["company_slug"]: r for r in api.handle("GET", "/api/stats").payload["companies"]}
    assert rows["acme"]["refreshable"] is True and rows["zeta"]["refreshable"] is True
    assert rows["seen"]["refreshable"] is False
    assert rows["acme"]["refresh"] is None  # never refreshed yet


def test_the_real_refresher_end_to_end(store: Store, monkeypatch) -> None:
    """POST /api/refresh with the production refresher, over a fake Lever."""
    clf = RuleClassifier()
    board = load_fixture("lever_board_rai.json")
    lever = Boards({LEVER_BOARD_URL: board[1:]})
    seed_board(store, clf, lever)
    lever.routes[LEVER_BOARD_URL] = board
    monkeypatch.setattr(
        "jobfilter.web.api.HttpClient",
        lambda timeout: HttpClient(transport=lever, backoff=0.0, sleep=lambda _: None),
    )
    api = Api(store, lambda urls, expand=False: [], clf, make_refresher(store, clf))

    assert api.handle("POST", "/api/refresh", body={}).status == 202
    api.refresh_job.join(5)
    state = api.handle("GET", "/api/refresh").payload["refresh"]
    assert state["error"] is None
    assert [(r["slug"], r["changes"]) for r in state["results"]] == [(LEVER_SLUG, {"new": 1})]

    [row] = api.handle("GET", "/api/stats").payload["companies"]
    assert row["refresh"]["status"] == "ok" and row["refresh"]["changes"] == {"new": 1}
    assert row["refresh"]["listed"] == len(board)

    changed = api.handle("GET", "/api/postings", {"changed": ["1"]}).payload
    assert changed["total"] == 1
    assert changed["postings"][0]["job_id"] == board[0]["id"]
    assert changed["postings"][0]["changes"] == "new"
    assert api.handle("GET", "/api/postings", {"changed": ["0"]}).payload["total"] == len(board)


# --------------------------------------------------------------------------
# The server itself: a real socket on a real port
# --------------------------------------------------------------------------

@pytest.fixture
def live(api: Api):
    """A running server on an OS-assigned port."""
    server = build_server(api, host="127.0.0.1", port=0)
    # serve_forever polls at 0.5s by default, so shutdown() waits up to that
    # long -- once per test, which dominated the suite runtime.
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
    )
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield base
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def get(base: str, path: str) -> tuple[int, bytes, str]:
    try:
        with urllib.request.urlopen(base + path, timeout=5) as response:
            return response.status, response.read(), response.headers["Content-Type"]
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), exc.headers.get("Content-Type", "")


def post(base: str, path: str, payload: object) -> tuple[int, dict]:
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    request = urllib.request.Request(
        base + path, data=body, headers={"Content-Type": "application/json"}
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


@pytest.mark.parametrize(
    "path,needle,content_type",
    [
        pytest.param("/", b"<title>jobfilter</title>", "text/html", id="index"),
        pytest.param("/app.css", b"--critical", "text/css", id="css"),
        pytest.param("/app.js", b"openDetail", "text/javascript", id="js"),
    ],
)
def test_static_files_are_served(
    live: str, path: str, needle: bytes, content_type: str
) -> None:
    status, body, ctype = get(live, path)
    assert status == 200
    assert needle in body
    assert content_type in ctype


def test_the_page_references_the_assets_it_needs(live: str) -> None:
    _, body, _ = get(live, "/")
    assert b'href="/app.css"' in body
    assert b'src="/app.js"' in body
    assert b'src="/applications.js"' in body
    assert b'src="/pages.js"' in body


def test_the_page_has_the_refresh_controls(live: str) -> None:
    """Every id app.js looks up for the board refresh exists in the page."""
    _, body, _ = get(live, "/")
    for element_id in ("refresh-all", "refresh-deep", "refresh-status", "changed-only"):
        assert f'id="{element_id}"'.encode() in body, element_id
    assert b'data-key="changed"' in body and b'data-key="refreshed"' in body


def test_the_page_has_the_filter_pane_and_pagers(live: str) -> None:
    """Every id app.js looks up for the pane exists in the page."""
    _, body, _ = get(live, "/")
    for element_id in (
        "q", "label-list", "company-search", "company-suggestions",
        "only-chips", "hidden-chips", "pager-top", "pager-bottom", "reset",
        "browse-pane", "region-list", "place-search", "place-suggestions",
        "place-chips", "hidden-place-chips", "locations-clear", "companies-clear",
    ):
        assert f'id="{element_id}"'.encode() in body, element_id


@pytest.mark.parametrize(
    "path",
    [
        pytest.param("/../../../etc/passwd", id="dot-dot"),
        pytest.param("/%2e%2e/%2e%2e/etc/passwd", id="encoded-dot-dot"),
        pytest.param("/../store.py", id="escape-to-package"),
        pytest.param("/nope.html", id="missing-file"),
    ],
)
def test_static_serving_cannot_escape_the_static_directory(
    live: str, path: str
) -> None:
    status, _, _ = get(live, path)
    assert status == 404


def test_api_over_http(live: str) -> None:
    status, body, ctype = get(live, "/api/stats")
    assert status == 200
    assert "application/json" in ctype
    assert json.loads(body)["stats"]["postings"] == 0


def test_check_over_http(live: str, checked: list) -> None:
    status, payload = post(live, "/api/check", {"urls": "u1\nu2"})
    assert status == 200
    assert payload["checked"] == 2
    assert checked == [(["u1", "u2"], False)]

    status, body, _ = get(live, "/api/postings")
    assert len(json.loads(body)["postings"]) == 2


def test_concurrent_requests_do_not_trip_sqlite_thread_affinity(live: str) -> None:
    """ThreadingHTTPServer serves each request on its own thread."""
    post(live, "/api/check", {"urls": "u1\nu2\nu3"})
    codes: list[int] = []

    def hit() -> None:
        codes.append(get(live, "/api/stats")[0])
        codes.append(get(live, "/api/postings")[0])

    threads = [threading.Thread(target=hit) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert len(codes) == 16
    assert set(codes) == {200}


def test_refresh_over_http(store: Store) -> None:
    refresher = GatedRefresher()
    refresher.release.set()
    server = build_server(refresh_api(store, refresher), port=0)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, payload = post(base, "/api/refresh", {})
        assert status == 202 and payload["refresh"]["total"] == 2
        server.api.refresh_job.join(5)
        status, body, _ = get(base, "/api/refresh")
        assert status == 200 and json.loads(body)["refresh"]["done"] == 2
    finally:
        server.shutdown()
        server.server_close()


def test_malformed_json_body_is_a_400(live: str) -> None:
    status, payload = post(live, "/api/check", b"not json at all")
    assert status == 400
    assert "invalid JSON" in payload["error"]


def test_a_non_object_json_body_is_a_400(live: str) -> None:
    status, payload = post(live, "/api/check", b"[1, 2, 3]")
    assert status == 400
    assert "JSON object" in payload["error"]


def test_posting_to_a_non_api_path_is_a_404(live: str) -> None:
    status, _ = post(live, "/whatever", {})
    assert status == 404


def test_closing_soon_through_the_api(api: Api, store: Store) -> None:
    from datetime import date, timedelta

    soon = (date.today() + timedelta(days=2)).isoformat()
    store.upsert_posting(make_posting(job_id="soon", text=f"Deadline to apply: {soon}"))
    store.upsert_posting(make_posting(job_id="open", text="No date."))

    assert api.handle("GET", "/api/stats").payload["closing_soon_days"] == 7
    closing = api.handle("GET", "/api/postings", {"closing": ["1"]}).payload
    assert [p["job_id"] for p in closing["postings"]] == ["soon"]
    assert closing["postings"][0]["apply_by"] == soon
    ordered = api.handle("GET", "/api/postings", {"order": ["deadline"]}).payload
    assert [p["job_id"] for p in ordered["postings"]] == ["soon", "open"]


def test_posted_filter_and_order_through_the_api(api: Api, store: Store) -> None:
    from datetime import date, timedelta

    for job_id, days in (("old", 40), ("new", 2)):
        day = (date.today() - timedelta(days=days)).isoformat()
        store.upsert_posting(make_posting(job_id=job_id, raw={"createdAt": f"{day}T12:00:00"}))

    recent = api.handle("GET", "/api/postings", {"posted": ["7"]}).payload
    assert [p["job_id"] for p in recent["postings"]] == ["new"]
    ordered = api.handle("GET", "/api/postings", {"order": ["posted"]}).payload
    assert [p["job_id"] for p in ordered["postings"]] == ["new", "old"]
    oldest = api.handle("GET", "/api/postings", {"order": ["posted_asc"]}).payload
    assert [p["job_id"] for p in oldest["postings"]] == ["old", "new"]
    ignored = api.handle("GET", "/api/postings", {"posted": ["soon"], "order": ["bogus"]}).payload
    assert ignored["total"] == 2


@pytest.mark.parametrize(
    "query",
    [
        pytest.param({}, id="nothing"),
        pytest.param({"kind": ["posted"], "from": ["2026-10-01"], "to": ["2026-10-31"]}, id="unknown-kind"),
        pytest.param({"kind": ["added"], "from": ["2026-10-31"], "to": ["2026-10-01"]}, id="backwards"),
        pytest.param({"kind": ["added"], "from": ["Oct 1"], "to": ["2026-10-31"]}, id="not-a-day"),
    ],
)
def test_calendar_rejects_a_bad_request(api: Api, query: dict) -> None:
    assert api.handle("GET", "/api/calendar", query).status == 400


def test_calendar_counts_with_filters(api: Api, store: Store) -> None:
    from datetime import date

    today = date.today().isoformat()
    first = store.upsert_posting(make_posting(job_id="a"))
    store.upsert_posting(make_posting(job_id="b"))
    store.set_application(first, "applied")
    month = {"from": [today[:8] + "01"], "to": [today]}

    added = api.handle("GET", "/api/calendar", {"kind": ["added"], **month}).payload
    assert added["days"] == {today: 2}
    applied = api.handle("GET", "/api/calendar", {"kind": ["applied"], **month}).payload
    assert applied["days"] == {today: 1}
    narrowed = api.handle("GET", "/api/calendar", {"kind": ["added"], "app": ["none"], **month}).payload
    assert narrowed["days"] == {today: 1}
    day = api.handle("GET", "/api/postings", {"applied_on": [today]}).payload
    assert [p["job_id"] for p in day["postings"]] == ["a"]
