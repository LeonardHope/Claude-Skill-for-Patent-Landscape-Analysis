"""CSV data fetcher — reads Lens.org-format patent CSVs into PatentRecord[].

Lens.org exports use:
    - UTF-8 with BOM on the first header
    - ';;' as the multi-value delimiter inside a cell
    - ISO date strings (YYYY-MM-DD)
    - A '#' column for row index

This fetcher preserves backward compatibility with the old React app's CSV
workflow while emitting records in the new canonical schema.
"""

from __future__ import annotations

import csv
from pathlib import Path

from applicant_normalizer import normalize_applicant, normalize_many
from data_layer import PatentRecord
from plain_language import extract_cpc_subclass


_MULTI_DELIM = ";;"


def _split_multi(value: str) -> list[str]:
    if not value or not value.strip():
        return []
    return [v.strip() for v in value.split(_MULTI_DELIM) if v.strip()]


def _year_from(iso_date: str) -> int:
    if not iso_date or len(iso_date) < 4:
        return 0
    try:
        return int(iso_date[:4])
    except ValueError:
        return 0


def _safe_int(value: str) -> int:
    if not value:
        return 0
    try:
        return int(value)
    except ValueError:
        return 0


def _compute_family_id(simple_family_members: list[str], lens_id: str) -> str:
    """Use the smallest Lens ID in the simple family as a stable family key.

    If the record has no family members, fall back to its own Lens ID, making it
    a singleton family.
    """
    if simple_family_members:
        return sorted(simple_family_members)[0]
    return lens_id


def parse_csv(path: str | Path) -> list[PatentRecord]:
    """Parse a Lens.org-format CSV into PatentRecord instances.

    Args:
        path: path to the CSV file.

    Returns:
        List of PatentRecord instances with applicants normalized.
    """
    path = Path(path)
    records: list[PatentRecord] = []

    with path.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            lens_id = (row.get("Lens ID") or "").strip()
            display_key = (row.get("Display Key") or "").strip()
            simple_family_members = _split_multi(row.get("Simple Family Members") or "")
            family_id = _compute_family_id(simple_family_members, lens_id)

            pub_date = (row.get("Publication Date") or "").strip()
            app_date = (row.get("Application Date") or "").strip()
            pri_date = (row.get("Earliest Priority Date") or "").strip()

            cpc_full = _split_multi(row.get("CPC Classifications") or "")
            cpc_short = sorted({extract_cpc_subclass(c) for c in cpc_full if c})

            applicants_raw = _split_multi(row.get("Applicants") or "")

            record = PatentRecord(
                publication_number=display_key or lens_id,
                lens_id=lens_id,
                source_id=lens_id,
                publication_date=pub_date,
                publication_year=_year_from(pub_date),
                application_date=app_date,
                application_year=_year_from(app_date),
                priority_date=pri_date,
                priority_year=_year_from(pri_date),
                jurisdiction=(row.get("Jurisdiction") or "").strip().upper(),
                kind_code=(row.get("Kind") or "").strip(),
                document_type=(row.get("Document Type") or "").strip(),
                title=(row.get("Title") or "").strip(),
                abstract=(row.get("Abstract") or "").strip(),
                applicants_raw=applicants_raw,
                applicants_normalized=normalize_many(applicants_raw),
                inventors=_split_multi(row.get("Inventors") or ""),
                owners=_split_multi(row.get("Owners") or ""),
                family_id=family_id,
                family_size=_safe_int(row.get("Simple Family Size") or "1"),
                family_member_jurisdictions=_split_multi(
                    row.get("Simple Family Member Jurisdictions") or ""
                ),
                cpc_classes_full=cpc_full,
                cpc_classes_short=cpc_short,
                legal_status=(row.get("Legal Status") or "").strip(),
                cited_by_count=_safe_int(row.get("Cited by Patent Count") or "0"),
                cites_count=_safe_int(row.get("Cites Patent Count") or "0"),
                url=(row.get("URL") or "").strip(),
            )

            # Skip records with no usable filing year
            if (
                record.application_year == 0
                and record.priority_year == 0
                and record.publication_year == 0
            ):
                continue

            records.append(record)

    return records


def fetch_from_csv(path: str | Path) -> tuple[list[PatentRecord], dict]:
    """Public fetcher entry point.

    Returns:
        (records, source_metadata) tuple. source_metadata carries information
        about the CSV run (file name, row count, etc.) for inclusion in the
        report's Methodology section.
    """
    path = Path(path)
    records = parse_csv(path)
    return records, {
        "kind": "csv",
        "source_file": str(path.resolve()),
        "source_file_name": path.name,
        "raw_record_count": len(records),
    }
