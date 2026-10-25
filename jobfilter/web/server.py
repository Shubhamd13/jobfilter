"""Local web server. Standard library `http.server`, no framework.

Single user, localhost, a handful of endpoints -- a framework would be more
moving parts than the job needs. Routing and payloads live in api.py; this
module only moves bytes.
"""

from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import parse_qs, quote, urlparse

from .api import MAX_RESUME_BYTES, Api, ApiResponse

STATIC_DIR = Path(__file__).parent / "static"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
MAX_BODY_BYTES = 1_000_000
# An uploaded file arrives as the request body itself, under this type.
RAW_CONTENT_TYPE = "application/octet-stream"
MAX_RAW_BODY_BYTES = MAX_RESUME_BYTES
# A body over its limit is still read, and thrown away, up to this size: the
# sender then gets the refusal instead of a broken connection.
MAX_DISCARDED_BYTES = 32_000_000

CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
}


class Handler(BaseHTTPRequestHandler):
    """One request. `api` is attached to the server, not the handler class."""

    server_version = "jobfilter"
    protocol_version = "HTTP/1.1"

    @property
    def api(self) -> Api:
        return self.server.api  # type: ignore[attr-defined]

    def do_GET(self) -> None:  # noqa: N802 - name fixed by BaseHTTPRequestHandler
        parsed = urlparse(self.path)
        path = parsed.path

        if path.startswith("/api/"):
            self._send(self.api.handle("GET", path, parse_qs(parsed.query)))
            return

        self._send_static(path)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._send_json(404, {"error": "not found"})
            return

        body: dict[str, Any] | None = None
        raw: bytes | None = None
        content_type = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        try:
            if content_type == RAW_CONTENT_TYPE:
                raw = self._read_body(MAX_RAW_BODY_BYTES)
            else:
                body = self._read_json()
        except ValueError as exc:
            self._send_json(400, {"error": str(exc)})
            return

        self._send(self.api.handle("POST", parsed.path, parse_qs(parsed.query), body, raw))

    def log_message(self, fmt: str, *args: Any) -> None:
        """One tidy line per request instead of BaseHTTPRequestHandler's format."""
        if self.command == "GET" and self.path == "/api/refresh":
            return  # the page polls this every second during a refresh
        print(f"  {self.command} {self.path} -> {args[1] if len(args) > 1 else ''}")

    # -- plumbing ---------------------------------------------------------

    def _read_body(self, limit: int) -> bytes:
        length = int(self.headers.get("Content-Length") or 0)
        if length > limit:
            if length > MAX_DISCARDED_BYTES:
                # The body stays unread, so this connection cannot be used again.
                self.close_connection = True
            else:
                self._discard(length)
            raise ValueError("request body too large")
        return self.rfile.read(length) if length > 0 else b""

    def _discard(self, length: int) -> None:
        while length > 0:
            chunk = self.rfile.read(min(length, 65536))
            if not chunk:
                return
            length -= len(chunk)

    def _read_json(self) -> dict[str, Any]:
        raw = self._read_body(MAX_BODY_BYTES)
        if not raw:
            return {}
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON body: {exc}") from exc
        if not isinstance(parsed, dict):
            raise ValueError("request body must be a JSON object")
        return parsed

    def _send(self, response: ApiResponse) -> None:
        if response.body is None:
            self._send_json(response.status, response.payload)
        else:
            self._send_file(response)

    def _send_file(self, response: ApiResponse) -> None:
        """A stored file. A PDF opens in the browser; anything else downloads."""
        content_type = response.content_type or "application/octet-stream"
        name = response.filename or "file"
        plain = re.sub(r"[^A-Za-z0-9._ -]", "_", name)
        disposition = "inline" if content_type == "application/pdf" else "attachment"
        self._respond(
            response.status,
            content_type,
            response.body or b"",
            {
                "Content-Disposition": f"{disposition}; filename=\"{plain}\"; filename*=UTF-8''{quote(name)}",
                # The type above is the one to believe, whatever the bytes look like.
                "X-Content-Type-Options": "nosniff",
            },
        )

    def _send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._respond(status, "application/json; charset=utf-8", body)

    def _send_static(self, path: str) -> None:
        name = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / name).resolve()

        # Never serve outside the static directory, whatever the URL says.
        if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
            self._respond(404, "text/plain; charset=utf-8", b"not found")
            return

        content_type = CONTENT_TYPES.get(target.suffix, "application/octet-stream")
        self._respond(200, content_type, target.read_bytes())

    def _respond(
        self,
        status: int,
        content_type: str,
        body: bytes,
        headers: Mapping[str, str] | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


class Server(ThreadingHTTPServer):
    """ThreadingHTTPServer so a slow fetch does not block the whole page."""

    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], api: Api) -> None:
        super().__init__(address, Handler)
        self.api = api


def build_server(
    api: Api, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT
) -> Server:
    """Port 0 asks the OS for a free port -- what the tests use."""
    return Server((host, port), api)
