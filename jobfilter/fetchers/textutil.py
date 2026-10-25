"""HTML -> plain text, standard library only.

A real parser rather than a tag-stripping regex: job descriptions carry nested
markup, entities and the occasional stray `<` in prose, and Step 3 splits this
output into sentences, so block structure has to survive as line breaks.
"""

from __future__ import annotations

import html
import re
from html.parser import HTMLParser

# Tags whose boundaries are a line break in the text rendering.
_BLOCK_TAGS = frozenset(
    {
        "address", "article", "blockquote", "br", "div", "dd", "dl", "dt",
        "fieldset", "figure", "footer", "h1", "h2", "h3", "h4", "h5", "h6",
        "header", "hr", "li", "main", "nav", "ol", "p", "pre", "section",
        "table", "td", "th", "tr", "ul",
    }
)
_SKIP_TAGS = frozenset({"script", "style", "head", "title"})

_TRAILING_WS = re.compile(r"[ \t]+$", re.MULTILINE)
_LEADING_WS = re.compile(r"^[ \t]+", re.MULTILINE)
_RUNS_OF_SPACE = re.compile(r"[ \t]{2,}")
_BLANK_LINES = re.compile(r"\n{3,}")


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._out: list[str] = []
        self._skip_depth = 0
        self._pending_bullet = False

    def handle_starttag(self, tag: str, attrs: object) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag == "li":
            self._out.append("\n")
            # Emit the marker only once real text arrives. Lever wraps each item
            # as `<li><p>text</p></li>`, and writing "- " eagerly would strand
            # the bullet on a line of its own -- which would then look like a
            # sentence boundary to the Step 3 classifier.
            self._pending_bullet = True
        elif tag in _BLOCK_TAGS and not self._pending_bullet:
            self._out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIP_TAGS:
            self._skip_depth = max(0, self._skip_depth - 1)
            return
        if tag == "li":
            self._pending_bullet = False
        if tag in _BLOCK_TAGS:
            self._out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth:
            return
        if self._pending_bullet and data.strip():
            self._out.append("- ")
            self._pending_bullet = False
        self._out.append(data)

    def text(self) -> str:
        return "".join(self._out)


def html_to_text(markup: str, *, unescape_first: bool = False) -> str:
    """Render an HTML fragment as plain text.

    `unescape_first` handles Greenhouse, whose `content` is HTML that has been
    entity-escaped a second time (`&lt;p&gt;` rather than `<p>`). One unescape
    turns it back into markup; the parser handles the entities inside it.
    """
    if not markup:
        return ""
    if unescape_first:
        markup = html.unescape(markup)
    parser = _TextExtractor()
    parser.feed(markup)
    parser.close()
    return normalize_text(parser.text())


def normalize_text(text: str) -> str:
    """Collapse whitespace while keeping the line and paragraph breaks."""
    if not text:
        return ""
    text = text.replace("\xa0", " ").replace("\r\n", "\n").replace("\r", "\n")
    text = _RUNS_OF_SPACE.sub(" ", text)
    text = _TRAILING_WS.sub("", text)
    text = _LEADING_WS.sub("", text)
    text = _BLANK_LINES.sub("\n\n", text)
    return text.strip()


def join_sections(*parts: str | None) -> str:
    """Join non-empty description chunks into one blank-line separated block."""
    return "\n\n".join(p.strip() for p in parts if p and p.strip())
