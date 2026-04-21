"""Analytics layer — computes every metric in the report with provenance.

Every function here returns one or more Metric objects. A Metric is the atomic
unit the HTML renderer can place in the template. The source_record_ids and
formula fields on each Metric power the "Why?" receipts panel in the report.

Record-list sizing: drill-down panels display at most ~200 records, so per-row
record lists are capped to keep the embedded JSON payload small. Aggregate
metrics (total families, total applicants) do not carry record lists at all —
a scrolling list of 16,000 entries is not useful to the reader and bloats the
HTML by ~8 MB.

Counting methodology (important and opinionated):

- Headline totals and applicant/technology charts count UNIQUE PATENT FAMILIES,
  not individual publications. An invention filed in US + EP + CN is one
  invention, not three. We use select_family_representatives() to pick one
  record per family (the earliest filing).

- Jurisdiction charts count INDIVIDUAL FILINGS. Each jurisdiction a patent is
  filed in is a separate data point, because that's what the reader wants to
  know about: "where are these being filed."

- Filing-trends charts count UNIQUE FAMILIES keyed by the family's earliest
  filing year. This is the standard patent-landscape time axis.

Claims are only factual, never editorial. We compute things like "filings grew
45%" because that's a deterministic arithmetic operation on the data. We never
say "this space is becoming competitive" because that's an editorial judgment.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from typing import Iterable

from data_layer import (
    PatentRecord,
    count_unique_families,
    filing_year,
    filter_by_year_range,
    get_family_id,
    select_family_representatives,
)
from plain_language import (
    cpc_plain_english,
    describe_trend,
    extract_cpc_subclass,
    format_share,
    is_regional_office,
    jurisdiction_name,
)
from provenance import Metric


# Maximum records to embed per drill-down row. Drill-down panels display
# at most 200 entries anyway, so capping here keeps the HTML manageable on
# landscapes that return 10k+ records.
MAX_DRILLDOWN_RECORDS_PER_ROW = 200


# ---------------------------------------------------------------------------
# Headline
# ---------------------------------------------------------------------------


def headline_metrics(
    records: list[PatentRecord],
    date_range: tuple[int, int],
) -> dict[str, Metric]:
    """Compute the six headline metrics shown at the top of the report.

    Returns a dict of Metric objects keyed by id:
        - headline_summary: one-sentence narrative + bullet stats
        - headline_total_families: total unique families
        - headline_total_applicants: distinct normalized applicants
        - headline_total_jurisdictions: distinct jurisdictions
        - headline_peak_year: year with the most filings
        - headline_trend: factual growth description
    """
    filtered = filter_by_year_range(records, *date_range)
    reps = select_family_representatives(filtered)
    total_families = len(reps)

    # Unique applicants (normalized)
    all_applicants: set[str] = set()
    for r in reps:
        for a in r.applicants_normalized:
            if a:
                all_applicants.add(a)

    # Unique jurisdictions: take all filings (not reps) since we want to
    # know the geographic footprint of the landscape, not just where
    # representative filings happen to land.
    all_jurisdictions: set[str] = {
        r.jurisdiction for r in filtered if r.jurisdiction
    }

    # Filings per year for trend
    year_counts_map: dict[int, int] = defaultdict(int)
    year_to_ids: dict[int, list[str]] = defaultdict(list)
    for r in reps:
        y = filing_year(r)
        if y:
            year_counts_map[y] += 1
            year_to_ids[y].append(r.publication_number)
    year_counts = sorted(year_counts_map.items())

    trend = describe_trend(year_counts)

    peak_year = trend["peak_year"]
    peak_count = trend.get("peak_count", 0)

    rep_ids = [r.publication_number for r in reps]

    start_year, end_year = date_range

    metrics: dict[str, Metric] = {}

    metrics["headline_total_families"] = Metric(
        id="headline_total_families",
        label="Total patent families",
        value=total_families,
        quantification={"count": total_families},
        formula_plain=(
            "Count of distinct patent families in the landscape. "
            "A family is a single invention filed in one or more jurisdictions, "
            "counted once regardless of how many filings resulted from it."
        ),
        formula_technical="len(select_family_representatives(records))",
        source_record_ids=[],  # aggregate metric — see individual charts for drill-downs
        caveats=[
            "Families where multiple filings share a simple-family identifier are merged into a single entry.",
            "Click the \"Leaders\" or \"Geography\" sections below to drill into specific patents.",
        ],
    )

    metrics["headline_total_applicants"] = Metric(
        id="headline_total_applicants",
        label="Distinct applicants",
        value=len(all_applicants),
        quantification={"count": len(all_applicants)},
        formula_plain=(
            "Count of distinct organizations or individuals filing patents in this landscape, "
            "after merging name variants that refer to the same entity."
        ),
        formula_technical="len({normalize(a) for r in families for a in r.applicants})",
        source_record_ids=[],  # aggregate — use Leaders chart for per-applicant drill-downs
        caveats=[
            "Applicant names were normalized (legal-suffix stripping, alias-table lookup) before counting.",
        ],
    )

    metrics["headline_total_jurisdictions"] = Metric(
        id="headline_total_jurisdictions",
        label="Jurisdictions with filings",
        value=len(all_jurisdictions),
        quantification={"count": len(all_jurisdictions)},
        formula_plain=(
            "Count of distinct jurisdictions (countries and regional offices like EPO and WIPO) "
            "in which patents from this landscape were filed."
        ),
        formula_technical="len({r.jurisdiction for r in records})",
        source_record_ids=[],  # aggregate — use Geography chart for per-jurisdiction drill-downs
        caveats=[
            "Regional offices (EPO, WIPO/PCT) are counted as separate jurisdictions alongside individual countries.",
        ],
    )

    metrics["headline_peak_year"] = Metric(
        id="headline_peak_year",
        label="Peak filing year",
        value=peak_year if peak_year else None,
        quantification={"year": peak_year, "count": peak_count},
        formula_plain=(
            f"Year with the largest number of unique-family filings. Based on earliest filing date "
            f"(application date if available, otherwise priority date)."
        ),
        formula_technical="max(year_counts, key=lambda y: y.count)",
        source_record_ids=year_to_ids.get(peak_year, [])[:MAX_DRILLDOWN_RECORDS_PER_ROW],
    )

    metrics["headline_trend"] = Metric(
        id="headline_trend",
        label="Filing trend",
        value=trend["text"],
        quantification={
            "direction": trend["direction"],
            "growth_pct": trend["growth_pct"],
            "start_year": trend["start_year"],
            "end_year": trend["end_year"],
            "start_count": trend["start_count"],
            "end_count": trend["end_count"],
            "peak_year": trend["peak_year"],
            "excluded_partial_years": trend.get("excluded_partial_years", []),
        },
        formula_plain=(
            "Growth percentage comparing the first year in the effective window to the last. "
            "Partial trailing years (e.g. half-year snapshot + publication lag) are excluded "
            "so a broken final year doesn't fake a trend reversal. "
            "Directional classification: growing (+10% or more), declining (-10% or less), stable (in between)."
        ),
        formula_technical=trend["formula"],
        source_record_ids=[],  # aggregate — see Filing Trends chart for per-year drill-downs
        sensitivity_notes=[
            "Using priority date instead of application date can shift year-to-year counts earlier by 1\u20132 years.",
            "Trailing years excluded: " + (", ".join(str(y) for y in trend.get("excluded_partial_years", [])) or "none"),
        ],
    )

    # Compose the headline summary as terse bullets. Each bullet is one
    # data-backed fact; no long trend sentences. The trend line is split from
    # its exclusion note so no single bullet runs more than one line.
    bullets: list[str] = []
    bullets.append(
        f"{total_families:,} patent families from {len(all_applicants):,} distinct applicants"
    )
    bullets.append(
        f"{len(all_jurisdictions):,} jurisdictions covered, {start_year}\u2013{end_year}"
    )
    if peak_year:
        bullets.append(f"Peak filing year: {peak_year} ({peak_count:,} filings)")
    if trend.get("direction") == "growing":
        bullets.append(
            f"Growing: +{abs(trend['growth_pct']):.0f}% from {trend['start_count']:,} in {trend['start_year']} "
            f"to {trend['end_count']:,} in {trend['end_year']}"
        )
    elif trend.get("direction") == "declining":
        bullets.append(
            f"Declining: {trend['growth_pct']:+.0f}% from {trend['start_count']:,} in {trend['start_year']} "
            f"to {trend['end_count']:,} in {trend['end_year']}"
        )
    elif trend.get("direction") == "stable":
        bullets.append(
            f"Stable around {trend['end_count']:,} filings/year ({trend['start_year']}\u2013{trend['end_year']})"
        )
    if trend.get("excluded_partial_years"):
        years_text = ", ".join(str(y) for y in trend["excluded_partial_years"])
        bullets.append(
            f"{years_text} excluded as incomplete (snapshot or publication lag)"
        )
    summary_text = "\n".join(f"\u2022 {b}" for b in bullets)

    metrics["headline_summary"] = Metric(
        id="headline_summary",
        label="Summary",
        value=summary_text,
        quantification={
            "total_families": total_families,
            "total_applicants": len(all_applicants),
            "total_jurisdictions": len(all_jurisdictions),
            "peak_year": peak_year,
            "peak_count": peak_count,
            "date_range": [start_year, end_year],
        },
        formula_plain=(
            "Narrative summary built from the other headline metrics. "
            "Every number appearing here is derived directly from the counts in the other headline tiles."
        ),
        source_record_ids=[],  # aggregate — individual receipts available on the underlying metric tiles
    )

    return metrics


# ---------------------------------------------------------------------------
# Leaders (top applicants)
# ---------------------------------------------------------------------------


def top_applicants_metric(
    records: list[PatentRecord],
    date_range: tuple[int, int],
    top_n: int = 15,
) -> Metric:
    """Rank applicants by number of unique patent families filed."""
    filtered = filter_by_year_range(records, *date_range)
    reps = select_family_representatives(filtered)
    total_families = len(reps)

    applicant_families: dict[str, list[str]] = defaultdict(list)
    for r in reps:
        for a in r.applicants_normalized:
            if a:
                applicant_families[a].append(r.publication_number)

    ranked = sorted(
        applicant_families.items(),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )[:top_n]

    rows = [
        {
            "rank": i + 1,
            "name": name,
            "family_count": len(record_ids),
            "share_pct": round(len(record_ids) / total_families * 100, 1) if total_families else 0.0,
            "share_label": format_share(len(record_ids), total_families),
            "record_ids": record_ids[:MAX_DRILLDOWN_RECORDS_PER_ROW],
            "record_ids_truncated": len(record_ids) > MAX_DRILLDOWN_RECORDS_PER_ROW,
        }
        for i, (name, record_ids) in enumerate(ranked)
    ]

    all_ids = [rid for r in rows for rid in r["record_ids"]]

    return Metric(
        id="top_applicants",
        label=f"Top {len(rows)} applicants by filings",
        value=rows,
        quantification={
            "total_families": total_families,
            "displayed_count": len(rows),
            "top_rank_count": rows[0]["family_count"] if rows else 0,
        },
        formula_plain=(
            "For each normalized applicant name, count the distinct patent families they appear on. "
            "Rank descending; show the top N. Share percentages are computed relative to the total "
            "family count in the landscape."
        ),
        formula_technical=(
            "Counter([normalize(a) for r in family_reps for a in r.applicants]).most_common(top_n)"
        ),
        source_record_ids=sorted(set(all_ids)),
        caveats=[
            "Applicant names normalized (legal suffixes stripped, alias table applied). "
            "See the Methodology section for the full merge audit.",
            "A family with multiple applicants credits each applicant, so shares can sum to more than 100%.",
        ],
        sensitivity_notes=[
            "Applicant assignment data may omit transfers after filing — a patent later sold to another company "
            "may still be credited to the original filer.",
        ],
    )


# ---------------------------------------------------------------------------
# Geography
# ---------------------------------------------------------------------------


def jurisdiction_distribution_metric(
    records: list[PatentRecord],
    date_range: tuple[int, int],
) -> Metric:
    """Count filings per jurisdiction.

    Uses all filings (not family reps) because each jurisdiction filing is
    a distinct data point for geographic coverage purposes.
    """
    filtered = filter_by_year_range(records, *date_range)

    juris_records: dict[str, list[str]] = defaultdict(list)
    for r in filtered:
        if r.jurisdiction:
            juris_records[r.jurisdiction].append(r.publication_number)

    total_filings = sum(len(ids) for ids in juris_records.values())

    ranked = sorted(
        juris_records.items(),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )

    rows = [
        {
            "code": code,
            "name": jurisdiction_name(code),
            "is_regional": is_regional_office(code),
            "count": len(record_ids),
            "share_pct": round(len(record_ids) / total_filings * 100, 1) if total_filings else 0.0,
            "share_label": format_share(len(record_ids), total_filings),
            "record_ids": record_ids[:MAX_DRILLDOWN_RECORDS_PER_ROW],
            "record_ids_truncated": len(record_ids) > MAX_DRILLDOWN_RECORDS_PER_ROW,
        }
        for code, record_ids in ranked
    ]

    all_ids = [rid for row in rows for rid in row["record_ids"]]

    return Metric(
        id="jurisdiction_distribution",
        label="Filings by jurisdiction",
        value=rows,
        quantification={
            "total_filings": total_filings,
            "unique_jurisdictions": len(rows),
        },
        formula_plain=(
            "Count of patent filings in each jurisdiction. Unlike the applicant ranking, this counts "
            "every filing separately — an invention filed in five jurisdictions contributes five data points."
        ),
        formula_technical="Counter(r.jurisdiction for r in records)",
        source_record_ids=sorted(set(all_ids)),
        caveats=[
            "Regional offices (EPO, WIPO/PCT) are listed alongside countries. They are not individual countries.",
        ],
    )


def filings_by_jurisdiction_year_metric(
    records: list[PatentRecord],
    date_range: tuple[int, int],
    top_n: int = 6,
) -> Metric:
    """Stacked time series: filings per top-N jurisdiction per year."""
    start_year, end_year = date_range
    filtered = filter_by_year_range(records, *date_range)

    # Pick the top N jurisdictions by total count in this window
    j_counts: Counter[str] = Counter(r.jurisdiction for r in filtered if r.jurisdiction)
    top_jurisdictions = [code for code, _ in j_counts.most_common(top_n)]

    # Build year -> jurisdiction -> count
    matrix: dict[int, dict[str, int]] = {}
    for y in range(start_year, end_year + 1):
        matrix[y] = {code: 0 for code in top_jurisdictions}
        matrix[y]["Other"] = 0

    for r in filtered:
        y = filing_year(r)
        if not y or y not in matrix:
            continue
        if r.jurisdiction in top_jurisdictions:
            matrix[y][r.jurisdiction] += 1
        else:
            matrix[y]["Other"] += 1

    # Pack into series
    years = sorted(matrix.keys())
    series = [
        {
            "code": code,
            "name": jurisdiction_name(code),
            "is_regional": is_regional_office(code),
            "values": [matrix[y][code] for y in years],
        }
        for code in top_jurisdictions
    ]
    if any(matrix[y]["Other"] for y in years):
        series.append({
            "code": "OTHER",
            "name": "Other",
            "is_regional": False,
            "values": [matrix[y]["Other"] for y in years],
        })

    return Metric(
        id="filings_by_jurisdiction_year",
        label="Filings by jurisdiction over time",
        value={
            "years": years,
            "series": series,
        },
        quantification={
            "top_n": top_n,
            "period": f"{start_year}-{end_year}",
        },
        formula_plain=(
            f"Filings per year, broken down by the top {top_n} jurisdictions. "
            "All remaining jurisdictions collapsed into 'Other'."
        ),
        formula_technical="pivot(records, index='year', columns='jurisdiction', aggfunc='count')",
        source_record_ids=[],  # aggregate over entire landscape
        caveats=[
            "Most recent year may appear low because applications are published ~18 months after filing.",
        ],
    )


# ---------------------------------------------------------------------------
# Trends
# ---------------------------------------------------------------------------


def filing_trends_metric(
    records: list[PatentRecord],
    date_range: tuple[int, int],
) -> Metric:
    """Total filings per year (family-deduplicated)."""
    start_year, end_year = date_range
    filtered = filter_by_year_range(records, *date_range)
    reps = select_family_representatives(filtered)

    year_counts: dict[int, int] = {y: 0 for y in range(start_year, end_year + 1)}
    year_ids: dict[int, list[str]] = {y: [] for y in range(start_year, end_year + 1)}
    for r in reps:
        y = filing_year(r)
        if y in year_counts:
            year_counts[y] += 1
            year_ids[y].append(r.publication_number)

    years = sorted(year_counts.keys())
    values = [year_counts[y] for y in years]

    return Metric(
        id="filing_trends_total",
        label="Filings per year (all jurisdictions)",
        value={
            "years": years,
            "counts": values,
        },
        quantification={
            "total_filings": sum(values),
            "period": f"{start_year}-{end_year}",
        },
        formula_plain=(
            "Count of unique patent families filed in each year, using the earliest filing date available. "
            "Family deduplication means a single invention filed in multiple jurisdictions is counted once."
        ),
        formula_technical="Counter(filing_year(r) for r in family_reps)",
        source_record_ids=[],  # aggregate over entire landscape
        caveats=[
            "Date axis uses application date (or earliest priority date if application date is missing), "
            "not publication date.",
        ],
        sensitivity_notes=[
            "The final year in the window is often undercounted because applications are published "
            "~18 months after filing, so some filings may not yet be in the data.",
        ],
    )


# ---------------------------------------------------------------------------
# Technology areas
# ---------------------------------------------------------------------------


def technology_areas_metric(
    records: list[PatentRecord],
    date_range: tuple[int, int],
    top_n: int = 15,
) -> Metric:
    """Rank CPC subclasses by the number of patent families they appear on."""
    filtered = filter_by_year_range(records, *date_range)
    reps = select_family_representatives(filtered)
    total_families = len(reps)

    # Count distinct families per CPC subclass
    class_families: dict[str, set[str]] = defaultdict(set)
    family_record_map: dict[str, str] = {}  # family_id -> a representative pub_number
    for r in reps:
        fid = get_family_id(r)
        family_record_map[fid] = r.publication_number
        # Use the pre-computed short classes if present, else compute
        shorts = r.cpc_classes_short or [extract_cpc_subclass(c) for c in r.cpc_classes_full]
        for short in set(shorts):
            if short:
                class_families[short].add(fid)

    ranked = sorted(
        class_families.items(),
        key=lambda kv: (-len(kv[1]), kv[0]),
    )[:top_n]

    rows = []
    for code, family_ids in ranked:
        all_record_ids = [family_record_map[fid] for fid in sorted(family_ids) if fid in family_record_map]
        rows.append({
            "code": code,
            "name": cpc_plain_english(code),
            "family_count": len(family_ids),
            "share_pct": round(len(family_ids) / total_families * 100, 1) if total_families else 0.0,
            "share_label": format_share(len(family_ids), total_families),
            "record_ids": all_record_ids[:MAX_DRILLDOWN_RECORDS_PER_ROW],
            "record_ids_truncated": len(all_record_ids) > MAX_DRILLDOWN_RECORDS_PER_ROW,
        })

    all_ids = [rid for row in rows for rid in row["record_ids"]]

    return Metric(
        id="technology_areas",
        label=f"Top {len(rows)} technology areas",
        value=rows,
        quantification={
            "total_families": total_families,
            "displayed_count": len(rows),
        },
        formula_plain=(
            "For each 4-character CPC technology class, count the distinct patent families that are "
            "classified under that class. Share percentages are relative to the total family count."
        ),
        formula_technical=(
            "class_families = {cpc_subclass(c): {family_id for r} for r in family_reps for c in r.cpc_codes}"
        ),
        source_record_ids=sorted(set(all_ids)),
        caveats=[
            "A patent family usually spans multiple CPC classes. A single family can contribute to several "
            "technology-area rows, so shares can exceed 100%.",
            "CPC codes are translated from their technical notation into plain English. The original codes "
            "remain available on hover.",
        ],
    )


# ---------------------------------------------------------------------------
# Notable patents
# ---------------------------------------------------------------------------


def notable_patents_metric(
    records: list[PatentRecord],
    date_range: tuple[int, int],
    top_n: int = 10,
) -> Metric:
    """Top N most-cited patents (by forward citation count).

    Falls back to most-recent-by-top-applicant listing when the data source
    doesn't provide citation counts.
    """
    filtered = filter_by_year_range(records, *date_range)

    has_citations = any(r.cited_by_count > 0 for r in filtered)

    if has_citations:
        ranked = sorted(filtered, key=lambda r: (-r.cited_by_count, r.publication_number))[:top_n]
        rows = [
            {
                "rank": i + 1,
                "publication_number": r.publication_number,
                "title": r.title or r.publication_number,
                "applicant": (r.applicants_normalized[0] if r.applicants_normalized else ""),
                "jurisdiction": jurisdiction_name(r.jurisdiction),
                "jurisdiction_code": r.jurisdiction,
                "year": filing_year(r),
                "cited_by_count": r.cited_by_count,
                "url": r.url,
            }
            for i, r in enumerate(ranked)
            if r.cited_by_count > 0
        ]
        criterion = "Most cited by other patents"
        formula = "Sort all records in the landscape by forward-citation count, take top N."
    else:
        # Fallback: most recent filings from top applicants
        reps = select_family_representatives(filtered)
        applicant_counts = Counter(
            a for r in reps for a in r.applicants_normalized if a
        )
        top_apps = {name for name, _ in applicant_counts.most_common(5)}
        candidates = [
            r for r in reps
            if any(a in top_apps for a in r.applicants_normalized)
        ]
        candidates.sort(key=lambda r: (-(filing_year(r) or 0), r.publication_number))
        rows = [
            {
                "rank": i + 1,
                "publication_number": r.publication_number,
                "title": r.title or r.publication_number,
                "applicant": (r.applicants_normalized[0] if r.applicants_normalized else ""),
                "jurisdiction": jurisdiction_name(r.jurisdiction),
                "jurisdiction_code": r.jurisdiction,
                "year": filing_year(r),
                "cited_by_count": 0,
                "url": r.url,
            }
            for i, r in enumerate(candidates[:top_n])
        ]
        criterion = "Most recent filings from the top 5 applicants"
        formula = (
            "Citation data not available from this data source. Fallback: list the most recent "
            "filings from the top 5 applicants by family count."
        )

    return Metric(
        id="notable_patents",
        label="Notable patents",
        value={
            "criterion": criterion,
            "rows": rows,
            "has_citations": has_citations,
        },
        quantification={"displayed_count": len(rows)},
        formula_plain=formula,
        source_record_ids=[row["publication_number"] for row in rows],
        caveats=[
            "Citation counts, when shown, reflect forward citations at the time the data was pulled.",
        ] if has_citations else [
            "Citation data was not available from the data source; most recent filings shown instead.",
        ],
    )


# ---------------------------------------------------------------------------
# Caveats
# ---------------------------------------------------------------------------


def document_type_breakdown_metric(
    records: list[PatentRecord],
    date_range: tuple[int, int],
) -> Metric:
    """Break down the landscape by document type (granted/application/utility model).

    Kind-code conventions vary by office. Common mappings:
      - A, A1, A2, A3, A9: published application (not yet granted)
      - B, B1, B2: granted patent (invention)
      - U, Y, U1, Y1: utility model (granted in CN/JP/KR, weaker than invention)
      - S, D: design patent (rare in landscape reports)
      - P: plant patent
    """
    filtered = filter_by_year_range(records, *date_range)
    counts: dict[str, int] = {}
    kind_to_label: dict[str, str] = {}
    for r in filtered:
        kc = (r.kind_code or "").strip().upper()
        if not kc:
            label = "Unknown"
        elif kc.startswith("U") or kc.startswith("Y"):
            label = "Utility model"
        elif kc.startswith("B"):
            label = "Granted patent"
        elif kc.startswith("A"):
            label = "Patent application"
        elif kc.startswith("S") or kc.startswith("D"):
            label = "Design patent"
        elif kc.startswith("P"):
            label = "Plant patent"
        else:
            label = kc
        counts[label] = counts.get(label, 0) + 1
        kind_to_label[kc] = label

    total = sum(counts.values())
    # Sort by count descending, with a stable order for readability
    order = ["Granted patent", "Patent application", "Utility model", "Design patent", "Plant patent", "Unknown"]
    rows = []
    for label in order:
        if label in counts:
            rows.append({
                "label": label,
                "count": counts[label],
                "share_pct": round(counts[label] / total * 100, 1) if total else 0.0,
            })
    # Any unrecognized kind codes, sorted by count
    remaining = sorted(
        ((l, c) for l, c in counts.items() if l not in order),
        key=lambda x: -x[1],
    )
    for label, count in remaining:
        rows.append({
            "label": label,
            "count": count,
            "share_pct": round(count / total * 100, 1) if total else 0.0,
        })

    return Metric(
        id="document_type_breakdown",
        label="Document types in this landscape",
        value=rows,
        quantification={"total": total, "type_count": len(rows)},
        formula_plain=(
            "Count of records by their document kind code (published application vs granted patent vs "
            "utility model vs other). Utility models are legally granted patents but have a lower inventive "
            "step bar, shorter term, and are common in CN/JP/KR; they should not be conflated with invention patents."
        ),
        formula_technical="Counter(classify_kind_code(r.kind_code) for r in records)",
        source_record_ids=[],
        caveats=[
            "Kind-code conventions vary by patent office; classification here is heuristic.",
            "Utility models (common in CN) count toward 'filings' but are weaker rights than invention patents.",
        ],
    )


def compute_caveats(
    records: list[PatentRecord],
    data_source: str,
    applicant_merges: dict[str, list[str]],
    source_metadata: dict | None = None,
) -> list[str]:
    """Return a list of data-caveat strings for the Methodology section."""
    caveats: list[str] = []
    source_metadata = source_metadata or {}

    # Truncation warning — surfaces when a BigQuery query hit the row limit.
    # This is a trust-critical caveat: if the reader doesn't know the data
    # was truncated, they may draw conclusions from a partial sample.
    if source_metadata.get("truncated"):
        row_limit = source_metadata.get("row_limit", 0)
        caveats.append(
            f"Query returned the row limit ({row_limit:,} rows). There are "
            f"additional records beyond this cap that are not reflected in the "
            f"charts. Re-run with a higher --limit to include them, or narrow "
            f"the query (smaller date window, specific countries) to fit under the cap."
        )

    # Snapshot freshness — the requested date window may not match what was returned
    requested_from = source_metadata.get("date_from")
    requested_to = source_metadata.get("date_to")
    actual_to = source_metadata.get("actual_to")
    if requested_to and actual_to and actual_to < requested_to:
        caveats.append(
            f"The BigQuery patents dataset is a periodic snapshot and currently "
            f"contains filings up to {actual_to}, not the requested window end of "
            f"{requested_to}. Applications filed after {actual_to} are not yet in the data."
        )

    # Data source notes
    if data_source.startswith("bigquery"):
        caveats.append(
            "Data source: Google Patents Public Datasets on BigQuery. "
            "Applicant fields reflect the original filing only and may not include post-filing assignments."
        )
    elif data_source.startswith("csv"):
        caveats.append(
            "Data source: user-supplied CSV export. Coverage and normalization depend on the upstream tool "
            "(typically Lens.org)."
        )
    if "uspto" in data_source:
        caveats.append(
            "Legal-status data supplemented from USPTO Open Data Portal for US filings only."
        )

    # Merge audit
    merge_count = sum(1 for v in applicant_merges.values() if len(v) > 1)
    if merge_count:
        caveats.append(
            f"{merge_count} applicant{'s' if merge_count != 1 else ''} had multiple name variants merged. "
            f"Full merge list available below."
        )

    # Missing fields
    records_without_priority = sum(1 for r in records if not r.priority_date and not r.application_date)
    if records_without_priority:
        caveats.append(
            f"{records_without_priority:,} record(s) had no application or priority date and were excluded from time-based charts."
        )

    caveats.append(
        "Filings count unique patent families in time-based and applicant charts, and individual filings in jurisdiction charts."
    )
    caveats.append(
        "Most recent year may undercount because applications are typically published ~18 months after filing."
    )

    return caveats


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


def compute_all_metrics(
    records: list[PatentRecord],
    date_range: tuple[int, int],
    top_applicants_n: int = 15,
    top_jurisdictions_n: int = 6,
    top_technology_n: int = 15,
    notable_n: int = 10,
) -> dict[str, Metric]:
    """Compute every metric the report template expects.

    Returns a flat dict of metric-id -> Metric. The template references metrics
    by id, so this is the final handoff before rendering.
    """
    metrics: dict[str, Metric] = {}
    metrics.update(headline_metrics(records, date_range))
    metrics["top_applicants"] = top_applicants_metric(records, date_range, top_applicants_n)
    metrics["jurisdiction_distribution"] = jurisdiction_distribution_metric(records, date_range)
    metrics["filings_by_jurisdiction_year"] = filings_by_jurisdiction_year_metric(
        records, date_range, top_jurisdictions_n
    )
    metrics["filing_trends_total"] = filing_trends_metric(records, date_range)
    metrics["technology_areas"] = technology_areas_metric(records, date_range, top_technology_n)
    metrics["notable_patents"] = notable_patents_metric(records, date_range, notable_n)
    metrics["document_type_breakdown"] = document_type_breakdown_metric(records, date_range)
    return metrics
