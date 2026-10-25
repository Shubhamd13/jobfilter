"""Run JavaScript in headless Chrome and read the result back.

There is no node/deno/bun on this machine, but Chrome is here -- and V8 is the
engine the extension actually runs in, so this is the faithful target rather
than a stand-in. The harness is a self-contained HTML file (script and data
inlined, so `file://` CORS never comes into it) whose script writes JSON into
a <pre>, which `--dump-dom` hands back.
"""

from __future__ import annotations

import html
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

CHROME_CANDIDATES = (
    "google-chrome",
    "google-chrome-stable",
    "chromium",
    "chromium-browser",
)
RESULT_RE = re.compile(r'<pre id="jobfilter-result">(.*?)</pre>', re.DOTALL)
TIMEOUT_SECONDS = 60


def find_chrome() -> str | None:
    for name in CHROME_CANDIDATES:
        path = shutil.which(name)
        if path:
            return path
    return None


def build_page(scripts: list[Path], data: dict[str, Any], body: str) -> str:
    """A standalone page: inlined scripts, inlined data, one script to run.

    `body` is JavaScript that returns the value to hand back, or a promise of
    it; the value is JSON-serialized into the result element.
    """
    inlined = "\n".join(f"<script>\n{p.read_text()}\n</script>" for p in scripts)
    # "</" escaped as "<\/": data holding "</script>" (saved HTML pages do)
    # would otherwise end the inline script early. Same string to JavaScript.
    payload = "\n".join(
        f"const {name} = {_inline_json(value)};" for name, value in data.items()
    )
    return f"""<!doctype html>
<meta charset="utf-8">
<body><pre id="jobfilter-result"></pre>
{inlined}
<script>
{payload}
function __run() {{
{body}
}}
// `body` may return a promise (the extension's engine is async): await it
// before writing the result. --virtual-time-budget lets timers run first.
Promise.resolve()
  .then(__run)
  .then(
    (value) => ({{ ok: true, value }}),
    (error) => ({{ ok: false, error: String((error && error.stack) || error) }})
  )
  .then((out) => {{
    document.getElementById("jobfilter-result").textContent = JSON.stringify(out);
  }});
</script>
</body>"""


def _inline_json(value: Any) -> str:
    return json.dumps(value).replace("</", "<\\/")


def run_js(scripts: list[Path], data: dict[str, Any], body: str) -> Any:
    """Execute `body` in Chrome with `scripts` and `data` in scope."""
    chrome = find_chrome()
    if chrome is None:  # pragma: no cover - guarded by a skip in the tests
        raise RuntimeError("no Chrome/Chromium on PATH")

    with tempfile.TemporaryDirectory() as tmp:
        page = Path(tmp) / "harness.html"
        page.write_text(build_page(scripts, data, body))

        completed = subprocess.run(
            [
                chrome,
                "--headless=new",
                "--disable-gpu",
                "--no-sandbox",
                f"--user-data-dir={tmp}/profile",
                "--virtual-time-budget=20000",
                "--dump-dom",
                page.as_uri(),
            ],
            capture_output=True,
            text=True,
            timeout=TIMEOUT_SECONDS,
        )

    match = RESULT_RE.search(completed.stdout)
    if match is None:
        raise RuntimeError(
            "harness produced no result element.\n"
            f"stdout: {completed.stdout[:2000]}\nstderr: {completed.stderr[:2000]}"
        )

    result = json.loads(html.unescape(match.group(1)))
    if not result["ok"]:
        raise AssertionError(f"JavaScript threw:\n{result['error']}")
    return result["value"]
