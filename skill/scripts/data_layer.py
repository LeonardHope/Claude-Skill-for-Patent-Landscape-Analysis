"""Canonical patent record and family-aware helpers.

Both data sources (CSV input and Google Patents BigQuery) normalize their
output into PatentRecord instances before the analytics layer touches them.
This is the one canonical schema the rest of the skill speaks.

Design notes:
    - No deduplication at the data layer. Different analytics functions count
      differently (jurisdictions per family, filings per applicant, etc.), so
      the data layer preserves all records and exposes family-aware helpers
      for the analytics module to use.
    - Year fields are pre-computed at ingestion so every downstream function
      can filter by year without re-parsing dates.
    - Raw and normalized applicant lists are kept separately. Raw preserves
      the original strings for the audit trail; normalized is what analytics
      groups by.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


@dataclass
class PatentRecord:
    """Canonical patent record.

    Every field has a safe default so fetchers can populate what's available
    and leave the rest empty. The analytics layer handles missing data
    gracefully (metrics with empty inputs just aren't emitted).
    """

    # Identifiers
    publication_number: str = ""           # primary display key (e.g. "US 11000000 B2")
    lens_id: str = ""                      # Lens.org ID if available
    source_id: str = ""                    # id in the source system (e.g. BigQuery publication_number)

    # Dates as ISO strings + pre-computed years
    publication_date: str = ""
    publication_year: int = 0
    application_date: str = ""
    application_year: int = 0
    priority_date: str = ""                # earliest priority
    priority_year: int = 0
    grant_date: str = ""
    grant_year: int = 0

    # Where and what kind of document
    jurisdiction: str = ""                 # ISO-style code: US, EP, CN, WO, JP, KR, ...
    kind_code: str = ""                    # A1, B2, etc.
    document_type: str = ""                # "Patent Application" | "Granted Patent" | "" (unknown)

    # Content
    title: str = ""
    abstract: str = ""

    # Parties
    applicants_raw: list[str] = field(default_factory=list)         # as in source
    applicants_normalized: list[str] = field(default_factory=list)  # after normalizer
    inventors: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)

    # Family
    family_id: str = ""                    # stable family identifier
    family_size: int = 1                   # number of family members
    family_member_jurisdictions: list[str] = field(default_factory=list)

    # Classifications
    cpc_classes_full: list[str] = field(default_factory=list)       # full codes like "A61K 6/16"
    cpc_classes_short: list[str] = field(default_factory=list)      # 4-char like "A61K"

    # Optional extras
    legal_status: str = ""                 # ACTIVE / PENDING / ... (USPTO-only typically)
    cited_by_count: int = 0                # forward citations
    cites_count: int = 0                   # backward citations

    # Links
    url: str = ""                          # canonical URL for the reader to click through


# ---------------------------------------------------------------------------
# Family-aware helpers
# ---------------------------------------------------------------------------


def get_family_id(record: PatentRecord) -> str:
    """Return a stable family identifier for a record.

    Uses the source's family_id if present, otherwise falls back to the
    publication number so each orphan record is its own singleton family.
    """
    return record.family_id or record.publication_number or record.source_id


def group_by_family(records: Iterable[PatentRecord]) -> dict[str, list[PatentRecord]]:
    """Group records by family id."""
    groups: dict[str, list[PatentRecord]] = {}
    for r in records:
        groups.setdefault(get_family_id(r), []).append(r)
    return groups


def count_unique_families(records: Iterable[PatentRecord]) -> int:
    """Count distinct patent families across the given records."""
    return len({get_family_id(r) for r in records})


def select_family_representatives(records: Iterable[PatentRecord]) -> list[PatentRecord]:
    """Pick one representative record per family (earliest application date).

    Use this for analytics functions that should count one invention once
    (e.g. filing trends, applicant counts). For analytics that specifically
    want one row per jurisdiction (e.g. jurisdiction distribution), use the
    raw record list instead.
    """
    representatives: dict[str, PatentRecord] = {}
    for r in records:
        fid = get_family_id(r)
        current = representatives.get(fid)
        if current is None:
            representatives[fid] = r
            continue
        if _earlier(r, current):
            representatives[fid] = r
    return list(representatives.values())


def _earlier(a: PatentRecord, b: PatentRecord) -> bool:
    """Return True if a's filing date is earlier than b's."""
    a_year = a.application_year or a.priority_year or a.publication_year
    b_year = b.application_year or b.priority_year or b.publication_year
    if a_year != b_year:
        return a_year < b_year
    # Tie-break on the full date string (ISO dates sort correctly as strings)
    a_date = a.application_date or a.priority_date or a.publication_date
    b_date = b.application_date or b.priority_date or b.publication_date
    return a_date < b_date


# ---------------------------------------------------------------------------
# Filtering helpers
# ---------------------------------------------------------------------------


def filter_by_year_range(
    records: Iterable[PatentRecord],
    start_year: int,
    end_year: int,
    year_field: str = "application_year",
) -> list[PatentRecord]:
    """Filter records to those with year_field within [start_year, end_year].

    Defaults to application_year. Analytics functions that want a different
    time axis (priority_year, publication_year) pass year_field explicitly.
    Records with year_field == 0 are excluded (unknown dates).
    """
    out: list[PatentRecord] = []
    for r in records:
        y = getattr(r, year_field, 0)
        if y and start_year <= y <= end_year:
            out.append(r)
    return out


def filing_year(record: PatentRecord) -> int:
    """Canonical filing year: application year, then priority, then publication.

    This is the time axis every chart in the report uses. Publication year is
    only a last-resort fallback because it lags actual filing by ~18 months.
    """
    return record.application_year or record.priority_year or record.publication_year


def year_range(records: Iterable[PatentRecord]) -> tuple[int, int]:
    """Return (min_year, max_year) across the records' filing years."""
    years = [filing_year(r) for r in records if filing_year(r) > 0]
    if not years:
        return (0, 0)
    return (min(years), max(years))
