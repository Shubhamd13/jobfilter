"""Build jobfilter/us_cities.tsv from GeoNames' cities1000.txt.

    curl -LO https://download.geonames.org/export/dump/cities1000.zip
    curl -LO https://download.geonames.org/export/dump/countryInfo.txt
    curl -LO https://download.geonames.org/export/dump/admin1CodesASCII.txt
    unzip cities1000.zip
    python scripts/build_us_cities.py cities1000.txt countryInfo.txt admin1CodesASCII.txt

Every US city, town and county seat with 1,000+ people, keyed by name, with
the state of the most populous one when several share a name. A name is left
out when:

- a bigger place outside the US has it ("Cambridge", "Birmingham", "Paris"),
  so a bare "Cambridge" stays the UK one;
- it is also a US state's name ("Washington", "Delaware"), which the state
  rules already read, or a country's ("Lebanon", "Peru");
- it is a small town (under 50,000) named like a foreign state or province
  ("Bolivar", "Flanders"), so "Cartagena, Bolívar" stays Colombian;
- it is a word location strings use for other things ("Remote", "Central").

Neighbourhoods (GeoNames PPLX: "Jamaica", "Chinatown") are skipped.

GeoNames data is CC BY 4.0, https://www.geonames.org/.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobfilter.location import US_STATES, city_key  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "jobfilter" / "us_cities.tsv"

# Populated places proper: plain, capitals and county / township seats.
FEATURE_CODES = {"PPL", "PPLA", "PPLA2", "PPLA3", "PPLA4", "PPLC"}

# Real US places whose names mean something else in a job location.
NOT_CITIES = {
    "remote", "hybrid", "home", "central", "field", "global", "union", "office",
    "metro", "downtown", "midtown", "uptown", "north", "south", "east", "west",
    "industry", "commerce", "enterprise", "normal", "mobile", "flexible",
    "liberty", "independence", "mission", "anywhere", "virtual", "travel",
    "colonial", "republic", "university", "city", "us", "usa", "america",
}

# A US place this big keeps its name even when a foreign province shares it.
PROVINCE_NAME_MIN_POPULATION = 50_000

# cities1000.txt columns (tab-separated, no header).
NAME, ASCII_NAME, FEATURE, COUNTRY, ADMIN1, POPULATION = 1, 2, 7, 8, 10, 14


def country_keys(country_info: Path) -> set[tuple[str, ...]]:
    """Every country's name, from GeoNames' countryInfo.txt."""
    with country_info.open(encoding="utf-8") as handle:
        return {
            city_key(line.split("\t")[4])
            for line in handle
            if line.strip() and not line.startswith("#")
        }


def province_keys(admin1: Path) -> set[tuple[str, ...]]:
    """Every non-US state / province name, from GeoNames' admin1CodesASCII.txt."""
    keys: set[tuple[str, ...]] = set()
    with admin1.open(encoding="utf-8") as handle:
        for line in handle:
            code, name, ascii_name, _ = line.rstrip("\n").split("\t")
            if not code.startswith("US."):
                keys.update((city_key(name), city_key(ascii_name)))
    return keys


def build(
    source: Path, countries: set[tuple[str, ...]], provinces: set[tuple[str, ...]]
) -> dict[tuple[str, ...], tuple[str, str, int]]:
    """Name key -> (display name, state code, population) of the biggest US place."""
    foreign: dict[tuple[str, ...], int] = {}
    us: dict[tuple[str, ...], tuple[str, str, int]] = {}
    taken = countries | {city_key(name) for name in US_STATES}
    codes = set(US_STATES.values())

    with source.open(encoding="utf-8", newline="") as handle:
        for row in csv.reader(handle, delimiter="\t", quoting=csv.QUOTE_NONE):
            population = int(row[POPULATION] or 0)
            keys = {city_key(row[NAME]), city_key(row[ASCII_NAME])} - {()}
            if row[COUNTRY] != "US":
                for key in keys:
                    foreign[key] = max(foreign.get(key, 0), population)
                continue
            if row[FEATURE] not in FEATURE_CODES or row[ADMIN1] not in codes:
                continue
            for key in keys:
                if key not in us or population > us[key][2]:
                    us[key] = (row[NAME], row[ADMIN1], population)

    return {
        key: entry
        for key, entry in us.items()
        if entry[2] > foreign.get(key, 0)
        and key not in taken
        and (key not in provinces or entry[2] >= PROVINCE_NAME_MIN_POPULATION)
        and " ".join(key) not in NOT_CITIES
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="GeoNames cities1000.txt")
    parser.add_argument("countries", type=Path, help="GeoNames countryInfo.txt")
    parser.add_argument("admin1", type=Path, help="GeoNames admin1CodesASCII.txt")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args(argv)

    cities = build(args.source, country_keys(args.countries), province_keys(args.admin1))
    rows = sorted({(name, state) for name, state, _ in cities.values()})
    with args.out.open("w", encoding="utf-8", newline="") as handle:
        handle.write("# US cities for jobfilter/location.py; built by scripts/build_us_cities.py\n")
        handle.write("# from GeoNames cities1000 (CC BY 4.0, https://www.geonames.org/).\n")
        for name, state in rows:
            handle.write(f"{name}\t{state}\n")
    print(f"{len(rows)} cities -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
