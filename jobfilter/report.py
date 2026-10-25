"""Rendering. Pure functions from results to strings, so the tables are testable."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Callable

from .models import Change, CompanyRefresh, Label, Origin, Result, Status

ELLIPSIS = "…"
GAP = "  "

# The evidence quote is the point of the table, so it is budgeted first and the
# title is squeezed to fit around it.
MIN_EVIDENCE_W = 30
MIN_TITLE_W = 12
MAX_TITLE_W = 34
MAX_COMPANY_W = 24
ABS_MIN_W = 3  # a column can be squeezed to this before the layout gives up


def truncate(text: str, width: int) -> str:
    """One line, at most `width` chars, ellipsis when cut."""
    flat = " ".join(text.split())
    if width <= 0:
        return ""
    if len(flat) <= width:
        return flat
    return flat[: width - 1] + ELLIPSIS


def render_table(
    headers: list[str],
    rows: list[list[str]],
    widths: list[int],
    align: str = "",
) -> str:
    """Fixed-width table. `align` is one char per column: 'l' (default) or 'r'."""
    align = align.ljust(len(headers), "l")

    def line(cells: list[str]) -> str:
        out = []
        for cell, width, how in zip(cells, widths, align):
            text = truncate(cell, width)
            out.append(text.rjust(width) if how == "r" else text.ljust(width))
        return GAP.join(out).rstrip()

    rule = GAP.join("-" * w for w in widths)
    return "\n".join([line(headers), rule, *(line(row) for row in rows)])


def render_results(results: list[Result], width: int = 100) -> str:
    """company | title | label | evidence, one row per result, input order.

    An ORIGIN column appears only when `--expand` actually found something, so
    the ordinary run keeps its full width for the evidence quote.
    """
    if not results:
        return "no URLs to check"

    expanded = any(r.origin is Origin.DISCOVERED for r in results)
    origin_w = max(len("ORIGIN"), len(str(Origin.DISCOVERED))) if expanded else 0

    headers = ["COMPANY", "TITLE", "LABEL", "EVIDENCE"]
    rows = [
        [r.company, r.title or "-", str(r.label), _evidence_cell(r)] for r in results
    ]
    widths = _columns(results, width - (origin_w + len(GAP) if expanded else 0))

    if expanded:
        headers.insert(3, "ORIGIN")
        for row, result in zip(rows, results):
            row.insert(3, str(result.origin))
        widths.insert(3, origin_w)

    return render_table(headers, rows, widths)


def _columns(results: list[Result], width: int) -> list[int]:
    """Budget the four columns, squeezing title then company when space is short.

    The label has to stay legible or the row says nothing, and the evidence is
    the reason to read the table at all, so those two give ground last.
    """
    label_w = max(len("LABEL"), max(len(str(r.label)) for r in results))
    space = width - label_w - 3 * len(GAP)  # three gaps between four columns

    company_w = _fit(results, lambda r: r.company, lo=7, hi=MAX_COMPANY_W)
    title_w = _fit(results, lambda r: r.title or "-", lo=MIN_TITLE_W, hi=MAX_TITLE_W)

    over = company_w + title_w + MIN_EVIDENCE_W - space
    if over > 0:
        shed = min(over, title_w - ABS_MIN_W)
        title_w -= shed
        over -= shed
    if over > 0:
        company_w = max(ABS_MIN_W, company_w - over)

    evidence_w = max(ABS_MIN_W, space - company_w - title_w)
    return [company_w, title_w, label_w, evidence_w]


def render_company_summary(results: list[Result], width: int = 100) -> str:
    """Per company: how many postings, and how many of them are restricted."""
    if not results:
        return ""

    headers = ["COMPANY", "TOTAL", "RESTRICT", "UNCLEAR", "INFO", "OPEN", "ERRORS"]
    tallies = _tally(results)
    rows = [_summary_row(t) for t in tallies]

    if len(tallies) > 1:
        rows.append(_summary_row(_Tally.combine(tallies)))

    company_w = max(len("COMPANY"), min(28, max(len(r[0]) for r in rows)))
    widths = [company_w] + [len(h) for h in headers[1:]]
    return render_table(headers, rows, widths, align="l" + "r" * 6)


def render_refresh(outcomes: list[CompanyRefresh], width: int = 100) -> str:
    """Per company: what its board lists now and what a refresh found changed.

    Changed companies first -- they are the reason to read the table -- then
    boards that failed, then the unchanged ones.
    """
    if not outcomes:
        return "no stored companies with a board to refresh"

    kinds = list(Change)
    headers = ["COMPANY", "PLATFORM", "LISTED", *(str(k).upper() for k in kinds), "FAILED", "NOTE"]
    ordered = sorted(outcomes, key=lambda o: (-o.changed, o.ok, o.slug.lower()))
    rows = [
        [
            o.name or o.slug,
            str(o.platform),
            str(o.listed) if o.ok else "-",
            *(str(o.changes.get(kind, 0) or "-") for kind in kinds),
            str(o.failed or "-"),
            o.error or "",
        ]
        for o in ordered
    ]
    company_w = max(len("COMPANY"), min(MAX_COMPANY_W, max(len(r[0]) for r in rows)))
    platform_w = max(len("PLATFORM"), max(len(r[1]) for r in rows))
    numeric = [max(len(h), 6) for h in headers[2:-1]]
    fixed = company_w + platform_w + sum(numeric) + len(GAP) * (len(headers) - 1)
    widths = [company_w, platform_w, *numeric, max(len("NOTE"), width - fixed)]
    table = render_table(headers, rows, widths, align="ll" + "r" * len(numeric) + "l")

    changed = sum(1 for o in outcomes if o.changed)
    broken = sum(1 for o in outcomes if not o.ok)
    summary = (
        f"{len(outcomes)} board(s) refreshed: {changed} changed"
        + (f", {broken} could not be read" if broken else "")
        + "."
    )
    return f"{table}\n\n{summary}"


@dataclass(frozen=True, slots=True)
class _Tally:
    company: str
    total: int
    restricted: int
    errors: int
    counts: Counter[Label]

    @staticmethod
    def combine(tallies: list[_Tally]) -> _Tally:
        counts: Counter[Label] = Counter()
        for tally in tallies:
            counts.update(tally.counts)
        return _Tally(
            company="ALL",
            total=sum(t.total for t in tallies),
            restricted=sum(t.restricted for t in tallies),
            errors=sum(t.errors for t in tallies),
            counts=counts,
        )


def _summary_row(tally: _Tally) -> list[str]:
    return [
        tally.company,
        str(tally.total),
        str(tally.restricted),
        str(tally.counts[Label.UNCLEAR]),
        str(tally.counts[Label.INFO_NO_SPONSORSHIP]),
        str(tally.counts[Label.OPEN]),
        str(tally.errors),
    ]


def _tally(results: list[Result]) -> list[_Tally]:
    by_company: dict[str, list[Result]] = {}
    for result in results:
        by_company.setdefault(result.company, []).append(result)

    tallies = [
        _Tally(
            company=company,
            total=len(rows),
            restricted=sum(1 for r in rows if _is_restricted(r)),
            errors=sum(1 for r in rows if r.status is not Status.OK),
            # A failure counts under ERRORS only, never under a label -- its
            # UNCLEAR is an artefact of the failure, not a classification.
            counts=Counter(r.label for r in rows if r.status is Status.OK),
        )
        for company, rows in by_company.items()
    ]
    # Worst first: that is what the reader is looking for.
    tallies.sort(key=lambda t: (-t.restricted, -t.errors, -t.total, t.company))
    return tallies


def _is_restricted(result: Result) -> bool:
    return result.status is Status.OK and result.label.is_restriction


def _evidence_cell(result: Result) -> str:
    if result.error:
        return f"[{result.error_type}] {result.error}"
    return result.evidence or "-"


def _fit(results: list[Result], get: Callable[[Result], str], *, lo: int, hi: int) -> int:
    return max(lo, min(hi, max(len(get(r)) for r in results)))
