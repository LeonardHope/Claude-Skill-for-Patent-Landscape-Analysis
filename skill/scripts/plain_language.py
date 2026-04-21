"""Plain-language translation for patent jargon.

This module exists because the target reader (e.g. a VC venture manager) does
not know what a CPC class is, what "priority date" means, or what EP and WO
represent. Every number or label that reaches the HTML is routed through here
for translation.

Loaded once at module import from skill/references/*.json, cached.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any


_REFERENCES_DIR = Path(__file__).resolve().parent.parent / "references"


# ---------------------------------------------------------------------------
# Reference loaders (cached)
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_cpc() -> dict[str, Any]:
    path = _REFERENCES_DIR / "cpc_plain_english.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


@lru_cache(maxsize=1)
def _load_jurisdictions() -> dict[str, Any]:
    path = _REFERENCES_DIR / "jurisdiction_names.json"
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# CPC translation
# ---------------------------------------------------------------------------


_CPC_CLASS_RE = re.compile(r"^([A-HY])(\d{2})([A-Z])")


def extract_cpc_subclass(full_code: str) -> str:
    """Extract the 4-character subclass from a full CPC code.

    Examples:
        "A61K 6/16" -> "A61K"
        "G06N 3/08" -> "G06N"
        "Y02E 10/50" -> "Y02E"

    Returns the first 4 chars if no structured match found.
    """
    if not full_code:
        return ""
    s = full_code.strip().replace(" ", "")
    m = _CPC_CLASS_RE.match(s)
    return m.group(0) if m else s[:4].upper()


def cpc_plain_english(code: str) -> str:
    """Translate a CPC code to a plain-language description.

    Accepts either a full code ("G06N 3/08") or a subclass ("G06N").
    Falls back to the section description, then "Unclassified technology".
    """
    if not code:
        return "Unclassified technology"
    data = _load_cpc()
    subclass = extract_cpc_subclass(code)
    classes = data.get("classes", {})
    if subclass in classes:
        return classes[subclass]
    # Fall back to section-level
    sections = data.get("sections", {})
    if subclass and subclass[0] in sections:
        return sections[subclass[0]]
    return "Unclassified technology"


def cpc_section_label(code: str) -> str:
    """Return just the section label for a code (for hierarchical grouping)."""
    if not code:
        return "Unclassified"
    section = code.strip().upper()[0]
    return _load_cpc().get("sections", {}).get(section, "Unclassified")


# ---------------------------------------------------------------------------
# Jurisdiction translation
# ---------------------------------------------------------------------------


def jurisdiction_name(code: str) -> str:
    """Return the plain-English name for a jurisdiction code.

    - Country codes ("US") -> "United States"
    - Regional offices ("EP", "WO", "EA") -> short name ("EPO", "WIPO / PCT", "EAPO")
    - Unknown -> the code itself so the reader can at least see something
    """
    if not code:
        return ""
    code = code.strip().upper()
    data = _load_jurisdictions()
    if code in data.get("countries", {}):
        return data["countries"][code]
    regional = data.get("regional_offices", {})
    if code in regional:
        return regional[code]["short"]
    return code


def jurisdiction_full_name(code: str) -> str:
    """Return the full / long name for a jurisdiction code."""
    code = (code or "").strip().upper()
    data = _load_jurisdictions()
    if code in data.get("countries", {}):
        return data["countries"][code]
    regional = data.get("regional_offices", {})
    if code in regional:
        return regional[code]["full"]
    return code


def jurisdiction_note(code: str) -> str:
    """Return an explanatory note for regional offices (EP, WO, etc.) or '' for countries."""
    code = (code or "").strip().upper()
    regional = _load_jurisdictions().get("regional_offices", {})
    if code in regional:
        return regional[code].get("note", "")
    return ""


def is_regional_office(code: str) -> bool:
    """True if the code is a regional office (not a single country)."""
    code = (code or "").strip().upper()
    return code in _load_jurisdictions().get("regional_offices", {})


def country_name_for_geojson(code: str) -> str | None:
    """Return the country name as it appears in the bundled world GeoJSON.

    Some codes map to names that differ between ISO-3166 and Natural Earth
    GeoJSON (e.g. US -> "United States of America"). Overrides live in
    jurisdiction_names.json under "geojson_name_overrides".

    Returns None for regional offices (they don't belong on a country map).
    """
    code = (code or "").strip().upper()
    data = _load_jurisdictions()
    if code in data.get("regional_offices", {}):
        return None
    overrides = data.get("geojson_name_overrides", {})
    if code in overrides:
        return overrides[code]
    return data.get("countries", {}).get(code)


# ---------------------------------------------------------------------------
# Terminology helpers
# ---------------------------------------------------------------------------


TERMINOLOGY: dict[str, str] = {
    "patent_family": "a single invention filed in multiple places, counted once",
    "priority_date": "when an invention was first filed anywhere in the world",
    "application_date": "when the patent application was filed in this specific jurisdiction",
    "publication_date": "when the patent was first made public (typically ~18 months after filing)",
    "applicant": "the organization or individual filing the patent",
    "assignee": "the current owner of the patent",
    "CPC_class": "a technology category used by patent offices to organize inventions",
    "kind_code": "a suffix indicating whether the document is an application or a granted patent",
    "granted_patent": "a patent that has been examined and approved",
    "patent_application": "a patent that has been filed but not yet granted",
}


def define(term: str) -> str:
    """Return a plain-language definition for a patent term. Empty if not found."""
    return TERMINOLOGY.get(term, "")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def format_share(count: int, total: int) -> str:
    """Format a share as 'XX.X%'. Returns '0.0%' for zero totals."""
    if not total:
        return "0.0%"
    return f"{(count / total * 100):.1f}%"


def format_count(n: int) -> str:
    """Format an integer with thousands separators."""
    return f"{n:,}"


def describe_trend(year_counts: list[tuple[int, int]]) -> dict:
    """Compute a plain-English factual trend description from per-year counts.

    Args:
        year_counts: list of (year, count) tuples, in any order.

    Returns:
        dict with:
          - text: one-sentence factual description suitable for a headline
          - direction: "growing" | "declining" | "stable" | "mixed"
          - start_year, end_year, start_count, end_count
          - growth_pct: (end - start) / start * 100, rounded to 1 decimal
          - peak_year: year with the highest count
          - formula: plain-English formula for the receipt

    The function never makes editorial claims. Only quantifiable comparisons.
    """
    if not year_counts:
        return {
            "text": "No filings in this date range.",
            "direction": "none",
            "start_year": 0,
            "end_year": 0,
            "start_count": 0,
            "end_count": 0,
            "growth_pct": 0.0,
            "peak_year": 0,
            "formula": "No data available.",
        }

    sorted_yc = sorted(year_counts)
    peak_year, peak_count = max(sorted_yc, key=lambda yc: yc[1])

    # Trailing-partial-year detection. Patent landscape reports are frequently
    # affected by two kinds of partial data at the tail of the time range:
    #   1. BigQuery snapshot freshness (e.g. the 2025 snapshot ends in June).
    #   2. Publication lag (applications are published ~18 months after filing).
    #
    # Without compensation, a fine growth trend gets reported as a dramatic
    # collapse ("filings fell -87% from 2016 to 2025") because the last year
    # in the window contains a small fraction of the true volume.
    #
    # Heuristic: starting from the end of the series, skip any year whose count
    # is less than 50% of the median count of the remaining years. Record the
    # skipped years so the receipts panel can note the exclusion honestly.
    effective = list(sorted_yc)
    excluded: list[tuple[int, int]] = []
    while len(effective) >= 3:
        others = [c for (_, c) in effective[:-1]]
        sorted_others = sorted(others)
        median = sorted_others[len(sorted_others) // 2]
        last_year, last_count = effective[-1]
        if last_count < median * 0.5:
            excluded.append(effective.pop())
            continue
        break

    eff_start_year, eff_start_count = effective[0]
    eff_end_year, eff_end_count = effective[-1]
    # Real endpoints (for display truth)
    start_year, start_count = sorted_yc[0]
    end_year, end_count = sorted_yc[-1]

    # Single-year special case: no trend to describe, just report the count
    if eff_start_year == eff_end_year:
        return {
            "text": f"{eff_start_count:,} filings in {eff_start_year}.",
            "direction": "single_year",
            "start_year": eff_start_year,
            "end_year": eff_end_year,
            "start_count": eff_start_count,
            "end_count": eff_end_count,
            "growth_pct": 0.0,
            "peak_year": peak_year,
            "peak_count": peak_count,
            "formula": f"Single year of data: count for {eff_start_year} = {eff_start_count}.",
            "excluded_partial_years": [y for (y, _) in excluded],
        }

    if eff_start_count > 0:
        growth_pct = round((eff_end_count - eff_start_count) / eff_start_count * 100, 1)
    else:
        growth_pct = 0.0 if eff_end_count == 0 else 100.0

    # Directional classification using ± 10% as "stable" tolerance
    if abs(growth_pct) < 10:
        direction = "stable"
        text = (
            f"Filings held roughly steady from {eff_start_count:,} in {eff_start_year} "
            f"to {eff_end_count:,} in {eff_end_year}."
        )
    elif growth_pct >= 10:
        direction = "growing"
        text = (
            f"Filings grew {growth_pct:+.0f}% from {eff_start_count:,} in {eff_start_year} "
            f"to {eff_end_count:,} in {eff_end_year}."
        )
    else:
        direction = "declining"
        text = (
            f"Filings fell {growth_pct:+.0f}% from {eff_start_count:,} in {eff_start_year} "
            f"to {eff_end_count:,} in {eff_end_year}."
        )

    # If peak is not at the endpoints, mention it
    if peak_year not in (eff_start_year, eff_end_year) and peak_count > max(eff_start_count, eff_end_count):
        text += f" Peak activity was in {peak_year} with {peak_count:,} filings."

    # Note excluded partial years so the reader understands why the endpoint
    # in the trend differs from the endpoint of the requested window
    if excluded:
        years_text = ", ".join(str(y) for (y, _) in sorted(excluded))
        text += (
            f" ({years_text} excluded from the trend because counts look "
            f"incomplete \u2014 likely snapshot or publication lag.)"
        )

    formula = (
        f"Growth = (effective_end - effective_start) / effective_start * 100 = "
        f"({eff_end_count} - {eff_start_count}) / {eff_start_count} * 100 = {growth_pct}%"
    )
    if excluded:
        formula += (
            f"\nTrailing years excluded as incomplete: "
            + ", ".join(f"{y} ({c} filings)" for (y, c) in sorted(excluded))
        )

    return {
        "text": text,
        "direction": direction,
        "start_year": eff_start_year,
        "end_year": eff_end_year,
        "start_count": eff_start_count,
        "end_count": eff_end_count,
        "growth_pct": growth_pct,
        "peak_year": peak_year,
        "peak_count": peak_count,
        "formula": formula,
        "excluded_partial_years": [y for (y, _) in excluded],
        "actual_start_year": start_year,
        "actual_end_year": end_year,
    }
