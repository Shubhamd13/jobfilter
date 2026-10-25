"""The resume you sent with an application: stored files, and attaching one to a mark.

Storage, the API on top of it, and the bytes over a real socket. The page's
picker is exercised in test_web_ui_resumes.py.
"""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.request

import pytest

from jobfilter.storage import ResumeInUse, Store
from jobfilter.web import Api
from jobfilter.web.api import MAX_RESUME_BYTES

from .test_applications import seed
from .test_web import get, live  # noqa: F401 - live is a fixture

PDF = b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\n%%EOF\n"
OTHER_PDF = b"%PDF-1.7\n% the perception one\n%%EOF\n"
DOCX = b"PK\x03\x04" + b"\x00" * 40
DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


@pytest.fixture
def store() -> Store:
    with Store(":memory:") as opened:
        yield opened


@pytest.fixture
def api(store: Store) -> Api:
    return Api(store, lambda urls, expand=False: [])


def add(store: Store, data: bytes = PDF, filename: str = "sim.pdf") -> int:
    resume, _ = store.add_resume(filename.rsplit(".", 1)[0], filename, "application/pdf", data)
    return resume["id"]


def upload(api: Api, name: str, data: bytes | None):
    return api.handle("POST", "/api/resumes", query={"name": [name]}, raw=data)


# --------------------------------------------------------------------------
# Stored files
# --------------------------------------------------------------------------

def test_a_resume_is_kept_whole(store: Store) -> None:
    resume, reused = store.add_resume("sim", "sim.pdf", "application/pdf", PDF)
    assert reused is False
    assert resume["name"] == "sim" and resume["filename"] == "sim.pdf"
    assert resume["size"] == len(PDF)
    assert resume["sha256"] == hashlib.sha256(PDF).hexdigest()
    assert resume["used"] == 0
    assert "data" not in resume, "the list never carries the file"

    found = store.resume_file(resume["id"])
    assert found == {"filename": "sim.pdf", "content_type": "application/pdf", "data": PDF}


def test_the_same_file_twice_is_one_resume(store: Store) -> None:
    first, _ = store.add_resume("sim", "sim.pdf", "application/pdf", PDF)
    again, reused = store.add_resume("renamed copy", "copy of sim.pdf", "application/pdf", PDF)
    assert reused is True
    assert again["id"] == first["id"] and again["name"] == "sim", "the first name stays"
    assert len(store.resumes()) == 1


def test_resumes_are_listed_newest_first(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = iter(["2026-10-01T09:00:00+00:00", "2026-10-02T09:00:00+00:00"])
    monkeypatch.setattr("jobfilter.storage.store.utcnow", lambda: next(clock))
    add(store, PDF, "sim.pdf")
    add(store, OTHER_PDF, "perception.pdf")
    assert [r["name"] for r in store.resumes()] == ["perception", "sim"]


def test_an_unknown_resume_has_no_file(store: Store) -> None:
    assert store.resume_file(999) is None


def test_rename(store: Store) -> None:
    resume_id = add(store)
    assert store.rename_resume(resume_id, "  simulation, v2 ")
    assert store.resumes()[0]["name"] == "simulation, v2"
    assert store.resumes()[0]["filename"] == "sim.pdf", "the file keeps its own name"

    assert store.rename_resume(999, "x") is False
    with pytest.raises(ValueError):
        store.rename_resume(resume_id, "   ")


# --------------------------------------------------------------------------
# Attached to a mark
# --------------------------------------------------------------------------

def test_attach_keep_and_detach(store: Store) -> None:
    posting_id = seed(store)["swe"]
    resume_id = add(store)

    store.set_application(posting_id, "applied", resume_id=resume_id)
    detail = store.posting(posting_id)
    assert (detail["resume_id"], detail["resume_name"]) == (resume_id, "sim")
    assert store.resumes()[0]["used"] == 1

    # A re-mark, or a saved note, leaves the resume where it is.
    store.set_application(posting_id, "applied")
    store.set_application(posting_id, "applied", "sent through a referral")
    assert store.posting(posting_id)["resume_id"] == resume_id

    # ...and attaching one leaves the note.
    other = add(store, OTHER_PDF, "perception.pdf")
    store.set_application(posting_id, "applied", resume_id=other)
    detail = store.posting(posting_id)
    assert (detail["resume_name"], detail["app_note"]) == ("perception", "sent through a referral")

    store.set_application(posting_id, "applied", resume_id=None)
    assert store.posting(posting_id)["resume_id"] is None


def test_attaching_does_not_move_when_it_was_marked(store: Store, monkeypatch: pytest.MonkeyPatch) -> None:
    posting_id = seed(store)["swe"]
    resume_id = add(store)
    clock = iter(["2026-10-01T09:00:00+00:00", "2026-10-02T09:00:00+00:00"])
    monkeypatch.setattr("jobfilter.storage.store.utcnow", lambda: next(clock))

    store.set_application(posting_id, "applied")
    store.set_application(posting_id, "applied", resume_id=resume_id)
    assert store.posting(posting_id)["app_updated_at"] == "2026-10-01T09:00:00+00:00"


def test_an_unknown_resume_cannot_be_attached(store: Store) -> None:
    posting_id = seed(store)["swe"]
    with pytest.raises(ValueError, match="no resume 999"):
        store.set_application(posting_id, "applied", resume_id=999)
    assert store.posting(posting_id)["app_status"] is None


def test_one_resume_on_several_applications(store: Store) -> None:
    ids = seed(store)
    resume_id = add(store)
    for job in ("swe", "senior", "mech"):
        store.set_application(ids[job], "applied", resume_id=resume_id)
    assert store.resumes()[0]["used"] == 3


def test_a_resume_in_use_cannot_be_deleted(store: Store) -> None:
    posting_id = seed(store)["swe"]
    resume_id = add(store)
    store.set_application(posting_id, "applied", resume_id=resume_id)

    with pytest.raises(ResumeInUse):
        store.delete_resume(resume_id)
    assert store.resume_file(resume_id) is not None

    store.set_application(posting_id, "applied", resume_id=None)
    assert store.delete_resume(resume_id) is True
    assert store.resumes() == [] and store.delete_resume(resume_id) is False


def test_clearing_the_mark_lets_the_resume_go(store: Store) -> None:
    posting_id = seed(store)["swe"]
    resume_id = add(store)
    store.set_application(posting_id, "applied", resume_id=resume_id)

    store.set_application(posting_id, None)
    assert store.resumes()[0]["used"] == 0, "the file stays; nothing points at it"
    store.set_application(posting_id, "applied")
    assert store.posting(posting_id)["resume_id"] is None


def test_a_resume_survives_a_refetch(store: Store) -> None:
    ids = seed(store)
    resume_id = add(store)
    store.set_application(ids["swe"], "applied", resume_id=resume_id)
    assert seed(store)["swe"] == ids["swe"]
    assert store.posting(ids["swe"])["resume_id"] == resume_id


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

def test_api_uploads_lists_and_returns_a_resume(api: Api) -> None:
    response = upload(api, "Alex_Sim.pdf", PDF)
    assert response.status == 201 and response.payload["reused"] is False
    resume = response.payload["resume"]
    assert (resume["name"], resume["filename"], resume["content_type"]) == (
        "Alex_Sim", "Alex_Sim.pdf", "application/pdf"
    )

    assert api.handle("GET", "/api/resumes").payload["resumes"] == [resume]

    file = api.handle("GET", f"/api/resumes/{resume['id']}/file")
    assert (file.status, file.body, file.content_type, file.filename) == (
        200, PDF, "application/pdf", "Alex_Sim.pdf"
    )


def test_api_says_when_a_file_was_uploaded_before(api: Api) -> None:
    first = upload(api, "sim.pdf", PDF).payload["resume"]
    again = upload(api, "sim (1).pdf", PDF)
    assert again.status == 200 and again.payload["reused"] is True
    assert again.payload["resume"]["id"] == first["id"]


def test_api_takes_a_docx(api: Api) -> None:
    response = upload(api, "Resume.DOCX", DOCX)
    assert response.status == 201
    assert response.payload["resume"]["content_type"] == DOCX_TYPE


@pytest.mark.parametrize(
    ("name", "data", "why"),
    [
        ("notes.txt", b"hello", ".pdf or .docx"),
        ("page.html", b"<script>alert(1)</script>", ".pdf or .docx"),
        ("", PDF, ".pdf or .docx"),
        ("sim.pdf", b"", "is empty"),
        ("sim.pdf", b"<html>not a pdf</html>", "not a PDF file"),
        ("sim.docx", PDF, "not a DOCX file"),
        ("sim.pdf", b"%PDF-" + b"0" * MAX_RESUME_BYTES, "larger than 5 MB"),
        ("sim.pdf", None, "request body"),
    ],
    ids=["text", "html", "no-name", "empty", "pdf-in-name-only", "docx-in-name-only", "too-big", "sent-as-json"],
)
def test_api_refuses_what_is_not_a_resume(api: Api, name: str, data: bytes | None, why: str) -> None:
    response = upload(api, name, data)
    assert response.status == 400 and why in response.payload["error"]
    assert api.handle("GET", "/api/resumes").payload["resumes"] == []


@pytest.mark.parametrize(
    ("given", "kept"),
    [
        ("../../etc/sim.pdf", "sim.pdf"),
        ("C:\\Users\\me\\Resume\\sim.pdf", "sim.pdf"),
        ("si\r\nm.pdf", "sim.pdf"),
        ("x" * 400 + ".pdf", "x" * 196 + ".pdf"),
    ],
    ids=["unix-folders", "windows-folders", "line-breaks", "very-long"],
)
def test_api_keeps_only_the_file_name(api: Api, given: str, kept: str) -> None:
    assert upload(api, given, PDF).payload["resume"]["filename"] == kept


def test_api_attaches_keeps_and_detaches(api: Api, store: Store) -> None:
    posting_id = seed(store)["swe"]
    resume_id = upload(api, "sim.pdf", PDF).payload["resume"]["id"]
    path = f"/api/postings/{posting_id}/application"

    assert api.handle("POST", path, body={"status": "applied", "resume_id": resume_id}).status == 200
    api.handle("POST", path, body={"status": "applied", "note": "referral"})
    detail = api.handle("GET", f"/api/postings/{posting_id}").payload["posting"]
    assert (detail["resume_id"], detail["resume_name"], detail["app_note"]) == (resume_id, "sim", "referral")

    api.handle("POST", path, body={"status": "applied", "resume_id": None})
    assert store.posting(posting_id)["resume_id"] is None
    assert store.posting(posting_id)["app_note"] == "referral"


@pytest.mark.parametrize("resume_id", ["3", 1.5, True, 999], ids=["string", "float", "boolean", "unknown"])
def test_api_refuses_a_bad_resume_id(api: Api, store: Store, resume_id: object) -> None:
    posting_id = seed(store)["swe"]
    response = api.handle(
        "POST", f"/api/postings/{posting_id}/application",
        body={"status": "applied", "resume_id": resume_id},
    )
    assert response.status == 400
    assert store.posting(posting_id)["app_status"] is None


def test_api_renames_and_deletes(api: Api, store: Store) -> None:
    posting_id = seed(store)["swe"]
    resume_id = upload(api, "sim.pdf", PDF).payload["resume"]["id"]
    path = f"/api/resumes/{resume_id}"

    assert api.handle("POST", path, body={"name": " Simulation "}).payload["name"] == "Simulation"
    assert api.handle("POST", path, body={"name": "  "}).status == 400
    assert api.handle("POST", path, body={}).status == 400

    store.set_application(posting_id, "applied", resume_id=resume_id)
    refused = api.handle("POST", path, body={"delete": True})
    assert refused.status == 409 and "detach" in refused.payload["error"]

    store.set_application(posting_id, "applied", resume_id=None)
    assert api.handle("POST", path, body={"delete": True}).status == 200
    assert api.handle("GET", "/api/resumes").payload["resumes"] == []


def test_api_404s_for_an_unknown_resume(api: Api) -> None:
    assert api.handle("GET", "/api/resumes/999/file").status == 404
    assert api.handle("POST", "/api/resumes/999", body={"name": "x"}).status == 404
    assert api.handle("POST", "/api/resumes/999", body={"delete": True}).status == 404


# --------------------------------------------------------------------------
# Over a real socket
# --------------------------------------------------------------------------

def send(base: str, name: str, data: bytes, content_type: str = "application/octet-stream"):
    request = urllib.request.Request(
        f"{base}/api/resumes?name={urllib.request.quote(name)}",
        data=data,
        headers={"Content-Type": content_type},
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_a_file_goes_up_and_comes_back_byte_for_byte(live: str) -> None:  # noqa: F811
    data = b"%PDF-1.7\n" + bytes(range(256)) * 600  # every byte value, about 150 KB
    status, payload = send(live, "Alex Résumé (sim).pdf", data)
    assert status == 201
    resume_id = payload["resume"]["id"]

    with urllib.request.urlopen(f"{live}/api/resumes/{resume_id}/file", timeout=5) as response:
        assert response.read() == data
        assert response.headers["Content-Type"] == "application/pdf"
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        disposition = response.headers["Content-Disposition"]
    assert disposition.startswith('inline; filename="Alex R_sum_ _sim_.pdf"')
    assert "filename*=UTF-8''Alex%20R%C3%A9sum%C3%A9%20%28sim%29.pdf" in disposition


def test_a_docx_downloads_rather_than_opens(live: str) -> None:  # noqa: F811
    _, payload = send(live, "resume.docx", DOCX)
    with urllib.request.urlopen(f"{live}/api/resumes/{payload['resume']['id']}/file", timeout=5) as response:
        assert response.headers["Content-Type"] == DOCX_TYPE
        assert response.headers["Content-Disposition"].startswith("attachment;")


def test_an_oversized_upload_is_refused(live: str) -> None:  # noqa: F811
    status, payload = send(live, "sim.pdf", b"%PDF-" + b"0" * MAX_RESUME_BYTES)
    assert status == 400 and "too large" in payload["error"]
    assert json.loads(get(live, "/api/resumes")[1])["resumes"] == []


def test_a_file_posted_as_json_is_not_taken(live: str) -> None:  # noqa: F811
    status, payload = send(live, "sim.pdf", b"{}", content_type="application/json")
    assert status == 400 and "request body" in payload["error"]


def test_json_endpoints_still_read_json(live: str) -> None:  # noqa: F811
    """A missing resume is a JSON 404, not a file."""
    status, body, content_type = get(live, "/api/resumes/1/file")
    assert status == 404 and json.loads(body)["error"] == "no resume 1"
    assert content_type.startswith("application/json")
