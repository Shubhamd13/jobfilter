"""HTML -> plain text. Pure functions, no network."""

from __future__ import annotations

import pytest

from jobfilter.fetchers.textutil import html_to_text, join_sections, normalize_text


@pytest.mark.parametrize(
    "markup,expected",
    [
        pytest.param("<p>Hello</p>", "Hello", id="paragraph"),
        pytest.param("<p>One</p><p>Two</p>", "One\n\nTwo", id="two-paragraphs"),
        pytest.param("a<br>b", "a\nb", id="br"),
        pytest.param("<div><span>in</span>line</div>", "inline", id="inline-not-split"),
        pytest.param("<h3>Head</h3><p>Body</p>", "Head\n\nBody", id="heading"),
        pytest.param("", "", id="empty"),
        pytest.param("no markup at all", "no markup at all", id="plain-passthrough"),
    ],
)
def test_block_structure_becomes_line_breaks(markup: str, expected: str) -> None:
    assert html_to_text(markup) == expected


@pytest.mark.parametrize(
    "markup,expected",
    [
        pytest.param(
            "<ul><li>One</li><li>Two</li></ul>", "- One\n\n- Two", id="plain-items"
        ),
        pytest.param(
            "<ul><li><p>One</p></li><li><p>Two</p></li></ul>",
            "- One\n\n- Two",
            id="lever-style-wrapped-items",
        ),
        pytest.param("<ul><li></li><li>Real</li></ul>", "- Real", id="empty-item"),
    ],
)
def test_bullets_stay_attached_to_their_text(markup: str, expected: str) -> None:
    """A bullet stranded on its own line would read as a sentence break."""
    assert html_to_text(markup) == expected


def test_entities_are_decoded() -> None:
    assert html_to_text("<p>R&amp;D &lt;b&gt; caf&eacute;&nbsp;x</p>") == "R&D <b> café x"


def test_script_and_style_content_is_dropped() -> None:
    markup = "<p>Keep</p><script>var citizen = 1;</script><style>.a{color:red}</style>"
    assert html_to_text(markup) == "Keep"


def test_double_escaped_html_needs_unescape_first() -> None:
    """Greenhouse ships `&lt;p&gt;Text&lt;/p&gt;` rather than `<p>Text</p>`."""
    escaped = "&lt;p&gt;U.S. citizens only&lt;/p&gt;"
    assert html_to_text(escaped) == "<p>U.S. citizens only</p>"
    assert html_to_text(escaped, unescape_first=True) == "U.S. citizens only"


def test_a_stray_angle_bracket_in_prose_survives() -> None:
    assert "5 years" in html_to_text("<p>&gt; 5 years experience</p>")


@pytest.mark.parametrize(
    "text,expected",
    [
        pytest.param("a   b", "a b", id="collapse-spaces"),
        pytest.param("  a  \n  b  ", "a\nb", id="trim-each-line"),
        pytest.param("a\n\n\n\n\nb", "a\n\nb", id="cap-blank-lines"),
        pytest.param("a\xa0b", "a b", id="nbsp"),
        pytest.param("a\r\nb", "a\nb", id="crlf"),
        pytest.param("\n\n  padded  \n\n", "padded", id="strip-ends"),
    ],
)
def test_normalize_text(text: str, expected: str) -> None:
    assert normalize_text(text) == expected


def test_join_sections_skips_empties() -> None:
    assert join_sections("A", "", None, "  ", "B") == "A\n\nB"
    assert join_sections() == ""
