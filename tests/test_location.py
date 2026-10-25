"""Location regions. Most cases are strings found in real postings."""

from __future__ import annotations

import pytest

from jobfilter.location import (
    Region,
    describe_place,
    in_region,
    place_matches,
    place_region,
    regions,
    split_places,
)

US, OUT, UNKNOWN = Region.US, Region.OUTSIDE_US, Region.UNKNOWN


@pytest.mark.parametrize(
    "place,expected",
    [
        # Explicit country.
        pytest.param("Costa Mesa, California, United States", US, id="greenhouse-full"),
        pytest.param("United States", US, id="country-only"),
        pytest.param("Remote, USA", US, id="usa"),
        pytest.param("Remote - US", US, id="us-code"),
        pytest.param("Dublin, Dublin, Ireland", OUT, id="ireland"),
        pytest.param("London, England, United Kingdom", OUT, id="uk"),
        pytest.param("Abu Dhabi, United Arab Emirates", OUT, id="uae"),
        pytest.param("Sydney, New South Wales, Australia", OUT, id="nsw-is-not-a-us-state"),
        pytest.param("Perth, Western Australia, Australia", OUT, id="western-australia"),
        pytest.param("Remote - MEX", OUT, id="country-code"),
        # State, by name or code.
        pytest.param("Seattle, Washington", US, id="state-name"),
        pytest.param("Washington, D.C.", US, id="dc"),
        pytest.param("Mountain View, California (HQ)", US, id="state-with-suffix"),
        pytest.param("Texas - Depot 2", US, id="state-first"),
        pytest.param("San Francisco, CA", US, id="state-code"),
        pytest.param("Birmingham, AL 35211", US, id="state-code-with-zip"),
        pytest.param("Albuquerque, New Mexico", US, id="new-mexico-is-not-mexico"),
        pytest.param("Melbourne, Florida", US, id="state-beats-foreign-city"),
        pytest.param("Indianapolis, Indiana", US, id="indiana-is-not-india"),
        # Bare cities.
        pytest.param("Abu Dhabi", OUT, id="bare-abu-dhabi"),
        pytest.param("London", OUT, id="bare-london"),
        pytest.param("Melbourne", OUT, id="bare-melbourne"),
        pytest.param("New Delhi", OUT, id="new-delhi"),
        pytest.param("Los Angeles - HQ", US, id="bare-us-city"),
        pytest.param("Sunnyvale", US, id="sunnyvale"),
        pytest.param("San Francisco Bay Area", US, id="bay-area"),
        pytest.param("Wichita Metro Area", US, id="metro-area"),
        # How Workday writes them: a country code first, or an office name.
        pytest.param("VNM Ho Chi Minh", OUT, id="iso3-code-first"),
        pytest.param("Hamme, BEL", OUT, id="iso3-code-last"),
        pytest.param("MX Silao, Guanajuato", OUT, id="two-letter-mexico-first"),
        pytest.param("Ciudad de México", OUT, id="mexico-city-in-spanish"),
        pytest.param("Palestine, Rawabi", OUT, id="palestine"),
        pytest.param("Armenia, Yerevan", OUT, id="armenia"),
        pytest.param("Asia", OUT, id="a-continent"),
        pytest.param("Ajax, Ontario", OUT, id="canadian-province"),
        pytest.param("Ontario, CA", US, id="ontario-california"),
        pytest.param("Waltham Office (POST)", US, id="office-named-for-its-city"),
        pytest.param("Billerica (MA)", US, id="state-code-in-brackets"),
        pytest.param("Alameda", US, id="alameda"),
        pytest.param("Savannah", US, id="savannah"),
        pytest.param("MA", UNKNOWN, id="a-bare-code-is-not-enough"),
        # Any US city of 1,000+ people (us_cities.tsv), as a whole part.
        pytest.param("Blacksburg, Va", US, id="table-city-with-lowercase-state"),
        pytest.param("Bozeman", US, id="table-city"),
        pytest.param("St. Louis", US, id="saint-abbreviated"),
        pytest.param("Winston Salem", US, id="dash-or-space"),
        pytest.param("Cañon City", US, id="accented"),
        pytest.param("Cambridge", UNKNOWN, id="bigger-uk-namesake-left-out"),
        pytest.param("Lebanon", UNKNOWN, id="country-name-is-not-a-us-town"),
        pytest.param("Cartagena de Indias, Bolívar", UNKNOWN, id="foreign-province-is-not-a-us-town"),
        pytest.param("Campo Largo, Paraná", UNKNOWN, id="table-city-only-as-a-whole-part"),
        pytest.param("Monterrey, Nuevo León", UNKNOWN, id="table-city-not-inside-a-name"),
        # Nothing to go on.
        pytest.param("Remote", UNKNOWN, id="remote"),
        pytest.param("Hybrid", UNKNOWN, id="hybrid"),
        pytest.param("Anywhere for us", UNKNOWN, id="lowercase-us-is-not-the-country"),
        pytest.param("Paris, ON", OUT, id="non-us-code-falls-through-to-city"),
    ],
)
def test_place_region(place: str, expected: Region) -> None:
    assert place_region(place) is expected


@pytest.mark.parametrize(
    "location,expected",
    [
        pytest.param(
            "Austin, Texas, United States; South San Francisco, California, United States",
            {US},
            id="two-us-places",
        ),
        pytest.param(
            "Abu Dhabi, United Arab Emirates; London, England, United Kingdom",
            {OUT},
            id="two-foreign-places",
        ),
        pytest.param("Austin, TX; London", {US, OUT}, id="both"),
        pytest.param("Pittsburgh, San Mateo, Bengaluru", {US, OUT}, id="comma-city-list"),
        pytest.param("Melbourne, Florida", {US}, id="city-and-state-not-a-list"),
        pytest.param("Toronto, Ontario, Canada", {OUT}, id="city-province-country"),
        pytest.param("Boulder, Bengaluru", {US, OUT}, id="comma-list-with-table-city"),
        pytest.param("Woodbridge, Ontario", {OUT}, id="province-is-not-a-list-item"),
        pytest.param("Costa Mesa, California, United States; Remote", {US}, id="remote-adds-nothing"),
        pytest.param("Remote", {UNKNOWN}, id="unknown"),
        pytest.param("", {UNKNOWN}, id="blank"),
        pytest.param(None, {UNKNOWN}, id="none"),
    ],
)
def test_regions(location: str | None, expected: set[Region]) -> None:
    assert regions(location) == expected


def test_in_region_takes_the_plain_string_sql_passes() -> None:
    assert in_region("Austin, TX; London", "us")
    assert in_region("Austin, TX; London", "outside_us")
    assert not in_region("Austin, TX", "outside_us")
    assert in_region(None, "unknown")


def test_split_places() -> None:
    assert split_places(" Austin, TX ;; London ;") == ["Austin, TX", "London"]
    assert split_places(None) == []
    assert split_places("Pittsburgh, San Mateo, Bengaluru") == ["Pittsburgh", "San Mateo", "Bengaluru"]
    assert split_places("San Francisco, CA; London") == ["San Francisco, CA", "London"]


BAY_AREA = "San Francisco Bay Area"
TWO_US = "Austin, Texas, United States; South San Francisco, California, United States"


@pytest.mark.parametrize(
    "location,wanted,expected",
    [
        # Every way of saying the country finds every US location.
        pytest.param(BAY_AREA, "USA", True, id="usa-bare-us-city"),
        pytest.param(BAY_AREA, "united states of america", True, id="usofa"),
        pytest.param(BAY_AREA, "U.S.A.", True, id="dotted"),
        pytest.param(BAY_AREA, "  United   States ", True, id="spacing"),
        pytest.param("Haslet, TX", "us", True, id="lowercase-us-typed"),
        pytest.param("Austin, TX; London", "USA", True, id="mixed-counts"),
        pytest.param("London", "USA", False, id="foreign"),
        pytest.param("Remote", "USA", False, id="unknown-is-not-us"),
        pytest.param(None, "USA", False, id="no-location"),
        # A state finds its own places however they are written.
        pytest.param(BAY_AREA, "California", True, id="state-via-city"),
        pytest.param("San Francisco, CA", "california", True, id="state-via-code"),
        pytest.param("California - LA", "CA", True, id="code-via-name"),
        pytest.param(TWO_US, "Texas", True, id="state-in-multi"),
        pytest.param("Seattle, Washington", "WA", True, id="wa"),
        pytest.param("Washington, D.C.", "WA", False, id="dc-is-not-wa"),
        pytest.param("Washington, D.C.", "Washington DC", True, id="dc"),
        pytest.param("Washington, District of Columbia, United States", "D.C.", True, id="dc-dots"),
        pytest.param("Perth, Western Australia, Australia", "WA", False, id="foreign-wa"),
        pytest.param("Jamaica Plain, Massachusetts", "CA", False, id="code-is-not-text"),
        pytest.param("Tbilisi, Georgia", "Georgia", True, id="state-name-still-matches-text"),
        # Everything else is text, in any case.
        pytest.param("Abu Dhabi", "abu dhabi", True, id="text"),
        pytest.param("Abu Dhabi, United Arab Emirates", "Abu Dhabi", True, id="text-contained"),
        pytest.param("Remote - MEX", "remote", True, id="remote-text"),
        pytest.param("London", "Paris", False, id="no-match"),
        pytest.param("London", "  ", False, id="blank-wanted"),
    ],
)
def test_place_matches(location: str | None, wanted: str, expected: bool) -> None:
    assert place_matches(location, wanted) is expected


@pytest.mark.parametrize(
    "wanted,expected",
    [
        ("USA", "every US location"),
        ("united states of america", "every US location"),
        ("ca", "anywhere in California"),
        ("washington dc", "anywhere in District of Columbia"),
        ("Abu Dhabi", "locations containing this text"),
    ],
)
def test_describe_place(wanted: str, expected: str) -> None:
    assert describe_place(wanted) == expected
