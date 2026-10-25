"""Drive a real Chrome with the extension installed, over the DevTools pipe.

Branded Chrome ignores `--load-extension` (since 137), but the DevTools
protocol's `Extensions.loadUnpacked` still works when Chrome is started with
`--remote-debugging-pipe --enable-unsafe-extension-debugging`. Requests for
LinkedIn / Indeed are intercepted and answered with saved fixture pages, so
the content script runs on the real URL with no network at all.

Standard library only: the pipe protocol is NUL-terminated JSON on fds 3/4.
"""

from __future__ import annotations

import base64
import json
import os
import select
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

from .jsrunner import find_chrome


class CdpError(RuntimeError):
    pass


class Chrome:
    """One headless Chrome, one loaded extension, many tabs."""

    def __init__(self, extension_dir: Path, intercept: list[str]) -> None:
        chrome = find_chrome()
        if chrome is None:
            raise CdpError("no Chrome/Chromium on PATH")

        self._tmp = tempfile.TemporaryDirectory()
        self._buf = b""
        self._next_id = 0
        self.pages: dict[str, bytes] = {}

        ours_read, chrome_write = os.pipe()
        chrome_read, ours_write = os.pipe()

        def wire_pipes() -> None:  # runs in the child, before exec
            # Park both ends above 3/4 first so neither dup2 clobbers the other.
            parked = [os.dup(chrome_read) + 100, os.dup(chrome_write) + 100]
            os.dup2(chrome_read, parked[0])
            os.dup2(chrome_write, parked[1])
            os.dup2(parked[0], 3)
            os.dup2(parked[1], 4)

        self._proc = subprocess.Popen(
            [
                chrome,
                "--headless=new",
                "--no-sandbox",
                "--no-first-run",
                f"--user-data-dir={self._tmp.name}/profile",
                "--remote-debugging-pipe",
                "--enable-unsafe-extension-debugging",
                "about:blank",
            ],
            pass_fds=(3, 4),
            preexec_fn=wire_pipes,
            # Its own process group, so close() can stop the helpers too.
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(chrome_read)
        os.close(chrome_write)
        self._read_fd, self._write_fd = ours_read, ours_write
        self._intercept = intercept

        self.extension_id = self.send(
            "Extensions.loadUnpacked", {"path": str(extension_dir.resolve())}
        )["id"]

    # -- protocol ---------------------------------------------------------

    def _post(self, method: str, params: dict | None, session: str | None) -> int:
        self._next_id += 1
        message: dict[str, Any] = {"id": self._next_id, "method": method, "params": params or {}}
        if session:
            message["sessionId"] = session
        os.write(self._write_fd, json.dumps(message).encode() + b"\0")
        return self._next_id

    def _read(self, timeout: float) -> dict:
        deadline = time.monotonic() + timeout
        while b"\0" not in self._buf:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("no message from Chrome")
            ready, _, _ = select.select([self._read_fd], [], [], remaining)
            if ready:
                chunk = os.read(self._read_fd, 1 << 20)
                if not chunk:
                    raise CdpError("Chrome closed the pipe")
                self._buf += chunk
        raw, self._buf = self._buf.split(b"\0", 1)
        return json.loads(raw)

    def _on_event(self, message: dict) -> None:
        """Serve intercepted page loads from `self.pages`; 404 anything else."""
        if message.get("method") != "Fetch.requestPaused":
            return
        params = message["params"]
        url = params["request"]["url"].split("#")[0]
        body = self.pages.get(url)
        self._post(
            "Fetch.fulfillRequest",
            {
                "requestId": params["requestId"],
                "responseCode": 200 if body is not None else 404,
                "responseHeaders": [
                    {"name": "Content-Type", "value": "text/html; charset=utf-8"}
                ],
                "body": base64.b64encode(body or b"").decode(),
            },
            message.get("sessionId"),
        )

    def send(self, method: str, params: dict | None = None, session: str | None = None) -> dict:
        wanted = self._post(method, params, session)
        while True:
            message = self._read(15)
            if message.get("id") == wanted:
                if "error" in message:
                    raise CdpError(f"{method}: {message['error']}")
                return message["result"]
            self._on_event(message)

    def pump(self, seconds: float) -> None:
        """Process events (page loads) for a while without sending anything."""
        deadline = time.monotonic() + seconds
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                self._on_event(self._read(remaining))
            except TimeoutError:
                return

    # -- conveniences -----------------------------------------------------

    def open_tab(self) -> "Tab":
        target = self.send("Target.createTarget", {"url": "about:blank"})["targetId"]
        session = self.send("Target.attachToTarget", {"targetId": target, "flatten": True})[
            "sessionId"
        ]
        self.send(
            "Fetch.enable",
            {"patterns": [{"urlPattern": pattern} for pattern in self._intercept]},
            session,
        )
        return Tab(self, target, session)

    def service_worker(self) -> str:
        """Attach to the extension's service worker; returns its session."""
        prefix = f"chrome-extension://{self.extension_id}/"
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            for target in self.send("Target.getTargets")["targetInfos"]:
                if target["type"] == "service_worker" and target["url"].startswith(prefix):
                    return self.send(
                        "Target.attachToTarget",
                        {"targetId": target["targetId"], "flatten": True},
                    )["sessionId"]
            time.sleep(0.2)
        raise CdpError("extension service worker never started")

    def evaluate(self, session: str, expression: str) -> Any:
        result = self.send(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            session,
        )
        if "exceptionDetails" in result:
            raise CdpError(json.dumps(result["exceptionDetails"])[:1000])
        return result["result"].get("value")

    def close(self) -> None:
        # Chrome is a tree of processes. Killing only the browser leaves its
        # helpers (network service, renderers) alive for a few milliseconds,
        # still writing to the profile, and deleting the profile under them
        # fails now and then with "Directory not empty". So: the whole group.
        try:
            os.killpg(self._proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        self._proc.wait(timeout=10)
        os.close(self._read_fd)
        os.close(self._write_fd)
        self._remove_profile()

    def _remove_profile(self) -> None:
        """Delete the temp profile, allowing a moment for the helpers to die."""
        deadline = time.monotonic() + 5
        while True:
            try:
                self._tmp.cleanup()
                return
            except OSError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.05)


class Tab:
    def __init__(self, chrome: Chrome, target: str, session: str) -> None:
        self.chrome = chrome
        self.target = target
        self.session = session

    def goto(self, url: str) -> None:
        self.chrome.send("Page.navigate", {"url": url}, self.session)

    def evaluate(self, expression: str) -> Any:
        return self.chrome.evaluate(self.session, expression)

    def wait_for(self, expression: str, until, timeout: float = 15) -> Any:
        """Poll `expression` until `until(value)` holds; return the last value."""
        deadline = time.monotonic() + timeout
        value = None
        while time.monotonic() < deadline:
            value = self.evaluate(expression)
            if until(value):
                return value
            self.chrome.pump(0.2)
        return value

    def close(self) -> None:
        self.chrome.send("Target.closeTarget", {"targetId": self.target})
