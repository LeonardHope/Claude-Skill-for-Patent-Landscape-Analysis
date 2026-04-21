"""BigQuery data fetcher — pulls landscape records from Google Patents.

Builds on the `google-patent-search` skill. That skill already handles BigQuery
authentication, cost estimation, and the underlying publications table schema.
We import its `bigquery_client` module and run a landscape-specific query that
returns exactly the fields our PatentRecord needs.

Setup prerequisites (handled by get_started.py):
    - The google-patent-search skill must be installed at
      ~/.claude/skills/google-patent-search/
    - gcloud CLI authenticated via `gcloud auth application-default login`
    - A GCP project with BigQuery API enabled (free tier: 1 TB/month)

Cost expectations for typical landscape queries:
    - G06N worldwide, 12 months: ~3-8 GB scanned
    - G06N US only, 24 months: ~1-2 GB
    - Full-text claims search (not used here): 40-150 GB

The skill's default cost ceiling is 5 GB. Landscape queries occasionally exceed
this. We pass max_bytes_override=20_000_000_000 (20 GB) for landscape pulls.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from applicant_normalizer import normalize_many
from data_layer import PatentRecord
from plain_language import extract_cpc_subclass


# ---------------------------------------------------------------------------
# Locate and import the google-patent-search skill
# ---------------------------------------------------------------------------


def _import_bigquery_client():
    """Locate google-patent-search and return its bigquery module + ScalarQueryParameter.

    Raises a clear error if the skill isn't installed.
    """
    skill_root = Path(os.path.expanduser("~/.claude/skills/google-patent-search"))
    scripts_dir = skill_root / "scripts"
    if not scripts_dir.exists():
        raise RuntimeError(
            "google-patent-search skill not found at "
            f"{skill_root}. Install it or run get_started.py to check."
        )
    # Add to path once
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import bigquery_client  # type: ignore
    from google.cloud.bigquery import ScalarQueryParameter  # type: ignore
    return bigquery_client, ScalarQueryParameter


# ---------------------------------------------------------------------------
# Landscape query
# ---------------------------------------------------------------------------


_LANDSCAPE_QUERY = """
SELECT
  p.publication_number,
  p.country_code,
  p.kind_code,
  p.filing_date,
  p.priority_date,
  p.family_id,
  (SELECT t.text FROM UNNEST(p.title_localized) t WHERE t.language = 'en' LIMIT 1) AS title,
  ARRAY(SELECT DISTINCT a.name FROM UNNEST(p.assignee_harmonized) a WHERE a.name IS NOT NULL) AS assignees,
  ARRAY(SELECT DISTINCT c.code FROM UNNEST(p.cpc) c WHERE c.code IS NOT NULL) AS cpc_codes
FROM `patents-public-data.patents.publications` p
WHERE
  p.filing_date >= @date_from
  AND p.filing_date <= @date_to
  AND EXISTS (
    SELECT 1
    FROM UNNEST(p.cpc) c
    WHERE {cpc_where}
  )
  {country_clause}
LIMIT @row_limit
"""


def _build_cpc_where(num_prefixes: int) -> str:
    """Build the CPC prefix WHERE clause for N prefixes."""
    if num_prefixes == 0:
        return "TRUE"
    return " OR ".join(f"c.code LIKE CONCAT(@cpc_prefix_{i}, '%')" for i in range(num_prefixes))


def _yyyymmdd(iso_date: str) -> int:
    """Convert YYYY-MM-DD string to YYYYMMDD int (the BigQuery format)."""
    return int(iso_date.replace("-", ""))


def _int_date_to_iso(int_date) -> str:
    """Convert BQ INT64 YYYYMMDD to ISO YYYY-MM-DD string."""
    if int_date is None:
        return ""
    s = str(int(int_date))
    if len(s) != 8:
        return ""
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


# ---------------------------------------------------------------------------
# Public fetcher entry points
# ---------------------------------------------------------------------------


def fetch_landscape(
    cpc_prefixes: list[str] | None = None,
    date_from: str = "",
    date_to: str = "",
    countries: list[str] | None = None,
    row_limit: int = 20000,
    cost_ceiling_bytes: int = 20_000_000_000,
    force: bool = False,
) -> tuple[list[PatentRecord], dict]:
    """Run a landscape query against Google Patents BigQuery.

    Args:
        cpc_prefixes: List of CPC prefixes to match (OR). E.g. ["G06N"] or
            ["G06N", "G06V", "G10L 15"]. Required — skill is not designed for
            unbounded landscape pulls.
        date_from: ISO date string YYYY-MM-DD (inclusive, filing date).
        date_to: ISO date string YYYY-MM-DD (inclusive, filing date).
        countries: Optional list of country codes to restrict to (US, EP, CN, ...).
            None = all countries.
        row_limit: Max rows to return. 5000 is safe for most landscape analytics.
        cost_ceiling_bytes: Max scan size for this query. Default 20 GB.
        force: Bypass the BigQuery client's default 5 GB ceiling (we pass
            max_bytes_override instead, so force is usually not needed).

    Returns:
        (records, source_metadata) tuple. source_metadata includes the query
        spec, row counts, and data source description for the Methodology
        section.
    """
    if not cpc_prefixes:
        raise ValueError(
            "fetch_landscape requires at least one CPC prefix. "
            "Unbounded landscape pulls are not supported for cost reasons."
        )
    if not date_from or not date_to:
        raise ValueError("fetch_landscape requires date_from and date_to in YYYY-MM-DD format.")

    bigquery_client, ScalarQueryParameter = _import_bigquery_client()

    cpc_where = _build_cpc_where(len(cpc_prefixes))
    country_clause = ""
    params: list = []

    # CPC prefix params
    for i, prefix in enumerate(cpc_prefixes):
        params.append(ScalarQueryParameter(f"cpc_prefix_{i}", "STRING", prefix.strip().upper()))

    # Date range params
    params.append(ScalarQueryParameter("date_from", "INT64", _yyyymmdd(date_from)))
    params.append(ScalarQueryParameter("date_to", "INT64", _yyyymmdd(date_to)))

    # Country filter (optional)
    if countries:
        placeholders = ", ".join(f"@country_{i}" for i in range(len(countries)))
        country_clause = f"AND p.country_code IN ({placeholders})"
        for i, c in enumerate(countries):
            params.append(ScalarQueryParameter(f"country_{i}", "STRING", c.strip().upper()))

    # Row limit
    params.append(ScalarQueryParameter("row_limit", "INT64", int(row_limit)))

    sql = _LANDSCAPE_QUERY.format(
        cpc_where=cpc_where,
        country_clause=country_clause,
    )

    client = bigquery_client.get_client()
    rows = client.run_query(
        sql,
        query_params=params,
        force=force,
        max_bytes_override=cost_ceiling_bytes,
    )

    records = [_row_to_patent_record(row) for row in rows]
    # Filter out any records that came back with no usable year
    records = [
        r for r in records
        if r.application_year or r.priority_year or r.publication_year
    ]

    truncated = len(rows) >= row_limit

    # Find the actual filing-date span in the returned data. BigQuery snapshots
    # lag by months; this lets the report display the *effective* date range
    # rather than the user's requested window.
    actual_from = ""
    actual_to = ""
    for r in records:
        if r.application_date:
            if not actual_from or r.application_date < actual_from:
                actual_from = r.application_date
            if not actual_to or r.application_date > actual_to:
                actual_to = r.application_date

    metadata = {
        "kind": "bigquery",
        "source": "google_patents_bigquery",
        "table": "patents-public-data.patents.publications",
        "cpc_prefixes": cpc_prefixes,
        "date_from": date_from,
        "date_to": date_to,
        "actual_from": actual_from,
        "actual_to": actual_to,
        "countries": countries or [],
        "row_limit": row_limit,
        "raw_row_count": len(rows),
        "usable_record_count": len(records),
        "truncated": truncated,
    }
    return records, metadata


def _row_to_patent_record(row: dict) -> PatentRecord:
    """Map a BigQuery result row to a PatentRecord."""
    filing_iso = _int_date_to_iso(row.get("filing_date"))
    priority_iso = _int_date_to_iso(row.get("priority_date"))

    pub_number = row.get("publication_number") or ""
    country = (row.get("country_code") or "").upper()
    kind = row.get("kind_code") or ""

    raw_assignees: list[str] = [a for a in (row.get("assignees") or []) if a]
    cpc_full: list[str] = [c for c in (row.get("cpc_codes") or []) if c]
    cpc_short = sorted({extract_cpc_subclass(c) for c in cpc_full if c})

    document_type = _infer_document_type(kind)

    return PatentRecord(
        publication_number=pub_number,
        lens_id="",
        source_id=pub_number,
        publication_date="",  # not fetched (column dropped to reduce query cost)
        publication_year=0,
        application_date=filing_iso,
        application_year=int(filing_iso[:4]) if filing_iso else 0,
        priority_date=priority_iso,
        priority_year=int(priority_iso[:4]) if priority_iso else 0,
        grant_date="",
        grant_year=0,
        jurisdiction=country,
        kind_code=kind,
        document_type=document_type,
        title=row.get("title") or "",
        abstract="",
        applicants_raw=raw_assignees,
        applicants_normalized=normalize_many(raw_assignees),
        inventors=[],
        owners=[],
        family_id=str(row.get("family_id") or ""),
        family_size=1,
        family_member_jurisdictions=[],
        cpc_classes_full=cpc_full,
        cpc_classes_short=cpc_short,
        legal_status="",
        cited_by_count=0,  # not included in this query (too expensive)
        cites_count=0,
        url=_google_patents_url(pub_number),
    )


def _infer_document_type(kind_code: str) -> str:
    """Infer 'Patent Application' vs 'Granted Patent' from a kind code."""
    if not kind_code:
        return ""
    k = kind_code.upper()
    # A1, A2, A9 — publications of applications (unexamined)
    # B1, B2 — granted patents
    # C, S, Y, T — various specialty kinds
    if k.startswith("A"):
        return "Patent Application"
    if k.startswith("B"):
        return "Granted Patent"
    return ""


def _google_patents_url(publication_number: str) -> str:
    if not publication_number:
        return ""
    return f"https://patents.google.com/patent/{publication_number.replace(' ', '')}"
