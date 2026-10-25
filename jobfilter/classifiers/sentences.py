"""Split a description into sentences for evidence extraction.

The hard constraint: `U.S. citizen` must never be split into `U.S.` + `citizen`,
because that is the exact wording the classifier exists to find. Under-splitting
only makes an evidence quote longer; over-splitting destroys the match. So the
splitter is deliberately conservative.

Rules live in rules.json (`sentence_splitter.protect_suffix_patterns`) so a
future JS extension splits text identically.
"""

from __future__ import annotations

import re

# A boundary is terminal punctuation, any closing quotes/brackets, then space.
_BOUNDARY = re.compile(r"([.!?]+)([\"'\)\]]*)(\s+)")

# What a new sentence may start with: a capital, a digit, a quote, a bullet.
_SENTENCE_START = re.compile(r"[A-Z0-9\"'(\[‘“•\-]")


def split_sentences(text: str, protect: list[re.Pattern[str]]) -> list[str]:
    """Split `text` into sentences, honouring the protected abbreviations.

    Newlines are always boundaries: bullets and headings are their own units,
    which keeps a matched bullet from dragging the whole list in as evidence.
    """
    if not text:
        return []

    sentences: list[str] = []
    for line in text.split("\n"):
        line = line.strip()
        if line:
            sentences.extend(_split_line(line, protect))
    return sentences


def _split_line(line: str, protect: list[re.Pattern[str]]) -> list[str]:
    pieces: list[str] = []
    start = 0

    for match in _BOUNDARY.finditer(line):
        cut = match.end(2)
        left = line[start:cut]

        # "...a U.S." / "...(i.e." / "...8 U.S.C." are abbreviations, not ends.
        if any(p.search(left) for p in protect):
            continue

        # A lowercase word after the period means we misread the punctuation.
        nxt = line[match.end() : match.end() + 1]
        if nxt and not _SENTENCE_START.match(nxt):
            continue

        stripped = left.strip()
        if stripped:
            pieces.append(stripped)
        start = match.end()

    tail = line[start:].strip()
    if tail:
        pieces.append(tail)
    return pieces
