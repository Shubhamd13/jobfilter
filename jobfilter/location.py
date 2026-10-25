"""Where a posting's locations are: in the US, outside it, or unknown.

Job boards write locations every way there is -- "Costa Mesa, California,
United States", "San Francisco, CA", "Haslet, TX", "Los Angeles - HQ",
"Abu Dhabi", "Remote - MEX" -- and a posting may list several separated by
";", or as a comma list of bare cities ("Pittsburgh, San Mateo, Bengaluru").
Each part is judged on its own, so a posting can be in the US *and* outside
it.

A part is decided by the strongest signal it carries, in this order:

1. an explicit "United States" / "USA" / "US"           -> US
2. a non-US country ("Australia", "United Kingdom")     -> outside
3. a US state, by name or by ", TX" code                -> US
4. a well-known non-US city ("London", "Melbourne")     -> outside
5. a US city ("Sunnyvale", "Los Angeles", "Bozeman")    -> US

so "Melbourne, Florida" is US (step 3) while a bare "Melbourne" is
Australia (step 4). Anything else, including a bare "Remote", is unknown.

The same knowledge answers "does this location match what the user typed?"
(`place_matches`): "USA" matches every US location, "California" matches
"San Francisco, CA" and "San Francisco Bay Area", and anything else matches
as text. Pure functions, no I/O.
"""

from __future__ import annotations

import functools
import re
import unicodedata
from enum import StrEnum
from pathlib import Path


class Region(StrEnum):
    US = "us"
    OUTSIDE_US = "outside_us"
    # No part of the location could be placed: "Remote", blank, or unfamiliar.
    UNKNOWN = "unknown"


# The spelled-out name in any case; the abbreviations only in capitals, so a
# stray lowercase "us" is not the country.
_US_MARKER = re.compile(r"(?i:\bUnited States\b)|\bUSA\b|\bU\.S\.|\bUS\b")

US_STATES: dict[str, str] = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR",
    "California": "CA", "Colorado": "CO", "Connecticut": "CT", "Delaware": "DE",
    "Florida": "FL", "Georgia": "GA", "Hawaii": "HI", "Idaho": "ID",
    "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD",
    "Massachusetts": "MA", "Michigan": "MI", "Minnesota": "MN",
    "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE",
    "Nevada": "NV", "New Hampshire": "NH", "New Jersey": "NJ",
    "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR",
    "Pennsylvania": "PA", "Rhode Island": "RI", "South Carolina": "SC",
    "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX", "Utah": "UT",
    "Vermont": "VT", "Virginia": "VA", "Washington": "WA",
    "West Virginia": "WV", "Wisconsin": "WI", "Wyoming": "WY",
    "District of Columbia": "DC", "D.C.": "DC", "Puerto Rico": "PR",
}

US_STATE_CODES = frozenset(US_STATES.values())

# What a user may type to mean the whole country, compared without dots or
# case ("U.S.A." -> "usa").
US_ALIASES = frozenset(
    {"us", "usa", "united states", "united states of america", "america",
     "the us", "the usa", "the united states"}
)

# Country names, plus the codes boards write ("Remote - MEX", Workday's
# "VNM Ho Chi Minh") and the regions that stand in for a country ("Asia").
NON_US_COUNTRIES = (
    "Argentina", "Armenia", "Asia", "Australia", "Austria", "Belgium", "Brazil", "Bulgaria",
    "Canada", "Chile", "China", "Colombia", "Costa Rica", "Croatia", "Cyprus",
    "Czech Republic", "Czechia", "Denmark", "Egypt", "England", "Estonia",
    "Finland", "France", "Germany", "Greece", "Hong Kong", "Hungary",
    "Iceland", "India", "Indonesia", "Ireland", "Israel", "Italy", "Japan",
    "Kenya", "Korea", "Latvia", "Lithuania", "Luxembourg", "Malaysia",
    "Mexico", "México", "Morocco", "Netherlands", "New Zealand", "Nigeria",
    "Northern Ireland", "Norway", "Pakistan", "Palestine", "Peru", "Philippines", "Poland",
    "Portugal", "Qatar", "Romania", "Saudi Arabia", "Scotland", "Serbia",
    "Singapore", "Slovakia", "Slovenia", "South Africa", "South Korea",
    "Spain", "Sweden", "Switzerland", "Taiwan", "Thailand", "Turkey",
    "Türkiye", "UAE", "Ukraine", "United Arab Emirates", "United Kingdom",
    "UK", "Uruguay", "Vietnam", "Viet Nam", "Wales", "Europe", "EMEA", "APAC", "LATAM",
    "MEX", "CAN", "GBR", "IND", "VNM", "DEU", "FRA", "JPN", "KOR", "CHN", "TWN",
    "SGP", "AUS", "ISR", "POL", "ROU", "ESP", "ITA", "NLD", "SWE", "CHE", "IRL",
    "BRA", "ARM", "PHL", "MYS", "THA", "IDN", "TUR", "UKR", "BEL", "LUX", "AUT", "CZE",
    "DNK", "FIN", "NOR", "PRT", "GRC", "HUN", "ZAF", "NZL", "ARG", "CHL", "COL", "EGY", "SAU",
)

# Bare city names that, in job listings, nearly always mean these places.
# Cities with a well-known US namesake (Cambridge, Birmingham, Portland) are
# left out on purpose; Melbourne stays because a Florida posting names its state.
NON_US_CITIES = (
    "Abu Dhabi", "Adelaide", "Amsterdam", "Athens", "Auckland", "Bangalore",
    "Bangkok", "Barcelona", "Beijing", "Belfast", "Bengaluru", "Berlin",
    "Bogota", "Bogotá", "Brisbane", "Brussels", "Bucharest", "Budapest",
    "Buenos Aires", "Calgary", "Canberra", "Cape Town", "Chennai",
    "Ciudad de México", "Copenhagen", "Delhi", "Doha", "Dubai", "Dublin", "Edinburgh", "Frankfurt",
    "Geneva", "Glasgow", "Guadalajara", "Gurgaon", "Gurugram", "Haifa",
    "Hamburg", "Hanoi", "Helsinki", "Ho Chi Minh", "Hyderabad", "Istanbul", "Jakarta",
    "Johannesburg", "Krakow", "Kraków", "Kuala Lumpur", "Kyiv", "Kiev",
    "Lagos", "Lisbon", "London", "Lviv", "Madrid", "Manchester", "Manila",
    "Melbourne", "Mexico City", "Milan", "Montreal", "Montréal", "Mumbai", "Munich",
    "München", "Nairobi", "New Delhi", "Noida", "Osaka", "Oslo", "Ottawa",
    "Paris", "Perth", "Prague", "Pune", "Quebec", "Québec", "Riyadh", "Rome", "Santiago",
    "São Paulo", "Sao Paulo", "Seoul", "Shanghai", "Shenzhen", "Stockholm",
    "Sydney", "Taipei", "Tallinn", "Tel Aviv", "Tokyo", "Toronto",
    "Vancouver", "Vienna", "Warsaw", "Waterloo", "Wellington", "Yerevan", "Zurich",
    "Zürich",
)

# Canadian provinces, read alongside the cities above (below US states, so
# "Ontario, CA" is California) but never taken for a city in a comma list:
# "Woodbridge, Ontario" is one place.
NON_US_PROVINCES = ("Alberta", "British Columbia", "Ontario")

# Bare US city names, with their state so "California" finds them too. These
# hand-picked ones are found anywhere in a place ("Seattle HQ"); every other
# US city of 1,000+ people comes from us_cities.tsv and must be a whole
# comma-separated part, so "Largo" inside "Campo Largo" is not Florida.
US_CITIES: dict[str, str] = {
    "Alameda": "CA", "Atlanta": "GA", "Austin": "TX", "Baltimore": "MD", "Boston": "MA",
    "Chicago": "IL", "Dallas": "TX", "Denver": "CO", "Detroit": "MI",
    "Houston": "TX", "Huntsville": "AL", "Los Angeles": "CA",
    "Menlo Park": "CA", "Miami": "FL", "Minneapolis": "MN",
    "Mountain View": "CA", "New York": "NY", "NYC": "NY", "Palo Alto": "CA",
    "Philadelphia": "PA", "Phoenix": "AZ", "Pittsburgh": "PA",
    "Redwood City": "CA", "Salt Lake City": "UT", "San Antonio": "TX",
    "San Diego": "CA", "San Francisco": "CA", "San Jose": "CA",
    "San Mateo": "CA", "Santa Clara": "CA", "Savannah": "GA", "Seattle": "WA",
    "Silicon Valley": "CA", "Sunnyvale": "CA", "Waltham": "MA", "Wichita": "KS",
}


def _words(names: tuple[str, ...]) -> re.Pattern[str]:
    """One case-insensitive alternation, matched on word edges."""
    alternation = "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))
    return re.compile(rf"(?<!\w)(?:{alternation})(?!\w)", re.IGNORECASE)


# Codes are matched case-sensitively: "UK" and "CAN" are codes, "can" is not.
_COUNTRY_CODES = tuple(n for n in NON_US_COUNTRIES if n.isupper())
_NON_US_COUNTRY = _words(tuple(n for n in NON_US_COUNTRIES if not n.isupper()))
_NON_US_CODE = re.compile(rf"\b(?:{'|'.join(_COUNTRY_CODES)})\b")
_US_STATE = _words(tuple(US_STATES))
# "San Francisco, CA", "Birmingham, AL 35211", "Billerica (MA)".
_US_STATE_CODE = re.compile(r"(?:,\s*|\()([A-Z]{2})\b")
# Workday leads with a two-letter country: "MX Silao, Guanajuato". Only codes
# that are not also a US state's are safe to read this way.
_NON_US_PREFIX = re.compile(r"^MX\s")
_NON_US_CITY = _words(NON_US_CITIES)
_NON_US_CITY_OR_PROVINCE = _words(NON_US_CITIES + NON_US_PROVINCES)
# A US state name buried inside a foreign one must not count.
_NOT_A_STATE = re.compile(r"New South Wales|Western Australia", re.IGNORECASE)
# ...and likewise a country inside a state name.
_NOT_A_COUNTRY = re.compile(r"New Mexico", re.IGNORECASE)
# The city of Washington is DC, not Washington state.
_WASHINGTON_DC = re.compile(
    r"Washington,?\s*(?:D\.?\s?C\.?|District of Columbia)", re.IGNORECASE
)

_STATE_BY_NAME = {name.lower(): code for name, code in US_STATES.items()}
# The first name listed per code is the one to show ("District of Columbia").
_STATE_NAME: dict[str, str] = {}
for _name, _code in US_STATES.items():
    _STATE_NAME.setdefault(_code, _name)

# Abbreviations spelled out, so "St. Louis" and "Saint Louis" are one name.
_SPELLED_OUT = {"st": "saint", "ste": "sainte", "ft": "fort", "mt": "mount"}


def city_key(text: str) -> tuple[str, ...]:
    """A name as words to look up: no case, accents, dots or dashes.

    "Winston-Salem" -> ("winston", "salem"); "St. Louis" -> ("saint", "louis").
    """
    plain = unicodedata.normalize("NFKD", text.casefold())
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    return tuple(_SPELLED_OUT.get(word, word) for word in re.findall(r"[^\W_]+", plain))


def _load_cities() -> dict[tuple[str, ...], str]:
    """Name key -> state code: us_cities.tsv, then the hand-picked US_CITIES."""
    cities: dict[tuple[str, ...], str] = {}
    table = Path(__file__).with_name("us_cities.tsv")
    for line in table.read_text(encoding="utf-8").splitlines():
        if line and not line.startswith("#"):
            name, state = line.split("\t")
            cities[city_key(name)] = state
    cities.update(_KNOWN_CITY_STATE)
    return cities


_KNOWN_CITY_STATE = {city_key(name): state for name, state in US_CITIES.items()}
_LONGEST_KNOWN_CITY = max(map(len, _KNOWN_CITY_STATE))
_CITY_STATE = _load_cities()


def _known_cities_in(place: str) -> list[tuple[str, ...]]:
    """The hand-picked US city names a place mentions, longest first at each word."""
    words = city_key(place)
    found: list[tuple[str, ...]] = []
    start = 0
    while start < len(words):
        for size in range(min(_LONGEST_KNOWN_CITY, len(words) - start), 0, -1):
            key = words[start : start + size]
            if key in _KNOWN_CITY_STATE:
                found.append(key)
                start += size
                break
        else:
            start += 1
    return found


def _us_cities_in(place: str) -> list[tuple[str, ...]]:
    """US city names in a place: hand-picked ones anywhere, the rest as whole parts."""
    found = _known_cities_in(place)
    found += [key for part in place.split(",") if (key := city_key(part)) in _CITY_STATE]
    return found


_PROVINCE = _words(NON_US_PROVINCES)


def _is_city(text: str) -> bool:
    """Is this text exactly one city name, US or not? A province is not."""
    if _PROVINCE.fullmatch(text):
        return False
    return city_key(text) in _CITY_STATE or bool(_NON_US_CITY.fullmatch(text))


def _split_city_list(place: str) -> list[str]:
    """A comma list of bare cities, split; any other place, whole.

    "Pittsburgh, San Mateo, Bengaluru" is three places, but "Melbourne,
    Florida", "San Francisco, CA" and "Toronto, Ontario, Canada" are one: the
    comma separates places only when every part is a city name on its own.
    """
    parts = [part.strip() for part in place.split(",") if part.strip()]
    if len(parts) >= 2 and all(_is_city(part) for part in parts):
        return parts
    return [place]


def split_places(location: str | None) -> list[str]:
    """The separate places a location string lists, in order."""
    return [
        piece
        for part in (location or "").split(";")
        if part.strip()
        for piece in _split_city_list(part.strip())
    ]


def named_states(place: str) -> set[str]:
    """US state codes a place names outright, by name or by ", TX" code."""
    place = _WASHINGTON_DC.sub("District of Columbia", _NOT_A_STATE.sub("", place))
    found = {_STATE_BY_NAME[m.group(0).lower()] for m in _US_STATE.finditer(place)}
    found.update(c for c in _US_STATE_CODE.findall(place) if c in US_STATE_CODES)
    return found


def city_states(place: str) -> set[str]:
    """US state codes implied by a bare well-known city ("Sunnyvale" -> CA)."""
    return {_CITY_STATE[key] for key in _us_cities_in(place)}


def place_region(place: str) -> Region:
    """Classify one place (no ';')."""
    if _US_MARKER.search(place):
        return Region.US
    if (
        _NON_US_COUNTRY.search(_NOT_A_COUNTRY.sub("", place))
        or _NON_US_CODE.search(place)
        or _NON_US_PREFIX.search(place)
    ):
        return Region.OUTSIDE_US
    if named_states(place):
        return Region.US
    if _NON_US_CITY_OR_PROVINCE.search(place):
        return Region.OUTSIDE_US
    if city_states(place):
        return Region.US
    return Region.UNKNOWN


@functools.lru_cache(maxsize=4096)
def regions(location: str | None) -> frozenset[Region]:
    """Every region a posting's location covers; {UNKNOWN} when none is known.

    Cached: a database has far fewer distinct location strings than postings,
    and SQL filtering calls this once per row.
    """
    found = {place_region(place) for place in split_places(location)}
    found.discard(Region.UNKNOWN)
    return frozenset(found) or frozenset({Region.UNKNOWN})


def in_region(location: str | None, region: str) -> bool:
    return region in regions(location)


def _key(text: str) -> str:
    """What the user typed, compared loosely: no case, dots or extra spaces."""
    return " ".join(text.replace(".", "").lower().split())


def _state_wanted(key: str) -> str | None:
    """The state code a typed key names: "california", "ca", "washington dc"."""
    if key.upper() in US_STATE_CODES:
        return key.upper()
    if key in ("washington dc", "dc"):
        return "DC"
    return _STATE_BY_NAME.get(key) or _STATE_BY_NAME.get(key.replace(" ", ""))


def _in_state(place: str, state: str) -> bool:
    """Is this place in that state? Only US places are; "Perth, WA" is not."""
    if place_region(place) is not Region.US:
        return False
    return state in named_states(place) or state in city_states(place)


@functools.lru_cache(maxsize=16384)
def place_matches(location: str | None, wanted: str) -> bool:
    """Does a posting's location match a place the user asked for?

    - "USA", "U.S.", "United States of America"... -> any part is in the US
    - a state name or code ("California", "CA")    -> any part is in that
      state, by name, code or well-known city
    - anything else                                -> case-insensitive text
      containment, so "abu dhabi" finds "Abu Dhabi, United Arab Emirates"

    A state *name* also matches as text, so "Georgia" still finds a location
    that spells it out; a bare two-letter code does not, since "ca" is inside
    half the words there are.
    """
    if not location:
        return False
    key = _key(wanted)
    if not key:
        return False
    if key in US_ALIASES:
        return Region.US in regions(location)

    state = _state_wanted(key)
    if state and any(_in_state(place, state) for place in split_places(location)):
        return True
    if state and len(key) == 2:
        return False
    return wanted.strip().lower() in location.lower()


def describe_place(wanted: str) -> str:
    """How `place_matches` will read what the user typed, in words for the UI."""
    key = _key(wanted)
    if key in US_ALIASES:
        return "every US location"
    state = _state_wanted(key)
    if state:
        return f"anywhere in {_STATE_NAME[state]}"
    return "locations containing this text"
