"""HTML renderer — turns a ReportBundle into a self-contained HTML file.

The output is the deliverable: a single .html file the user can email to a
client. Everything is inlined:
    - ECharts library (~1 MB)
    - World countries GeoJSON (~250 KB)
    - Report data as JSON
    - CSS and JS runtime

Opens in any browser, works offline forever, no network calls, no server.

We use Jinja2 for templating because loops and conditionals in the template
are clearer than string interpolation.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from provenance import ReportBundle


SKILL_ROOT = Path(__file__).resolve().parent.parent
TEMPLATE_DIR = SKILL_ROOT / "templates"
VENDOR_DIR = SKILL_ROOT / "vendor"


def _load_vendor(file_name: str) -> str:
    """Read a vendor file (echarts, GeoJSON) as text.

    Raises a clear error if it's missing.
    """
    path = VENDOR_DIR / file_name
    if not path.exists():
        raise FileNotFoundError(
            f"Vendor file not found: {path}. "
            f"Run skill/get_started.py to download required vendor assets."
        )
    return path.read_text(encoding="utf-8")


def _safe_embed_json(obj) -> str:
    """Serialize obj to JSON in a form safe for embedding in a <script> tag.

    Escapes '</' to '<\\/' so a stray '</script>' substring cannot break out
    of the script block. This is the standard trick for inlining JSON in HTML.
    """
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")


def _collect_referenced_record_ids(bundle: ReportBundle) -> set[str]:
    """Walk every metric and collect the publication numbers it references.

    We only need to embed records the HTML actually drills into. For a typical
    landscape, that's on the order of 1–3k unique IDs vs the full 20k raw
    records — a 10x reduction in the HTML payload.
    """
    ids: set[str] = set()
    for m in bundle.metrics.values():
        # Direct source ids for every metric
        ids.update(m.source_record_ids or [])
        # List-valued metrics (top_applicants, jurisdiction_distribution,
        # technology_areas) embed per-row record_ids
        value = m.value
        if isinstance(value, list):
            for row in value:
                if isinstance(row, dict):
                    ids.update(row.get("record_ids") or [])
        elif isinstance(value, dict):
            # notable_patents: rows list with publication_number
            rows = value.get("rows") or []
            if isinstance(rows, list):
                for row in rows:
                    if isinstance(row, dict) and row.get("publication_number"):
                        ids.add(row["publication_number"])
            # filings_by_jurisdiction_year has 'series' but no per-row IDs
    return {rid for rid in ids if rid}


def _google_patents_url(publication_number: str) -> str:
    """Build a Google Patents URL from a BigQuery publication number.

    BigQuery's patents.publications table stores publication numbers as
    ``CC-NNNN-KK`` (e.g. ``US-11000000-B2``) but Google Patents' URL format
    is ``CCNNNNKK/en`` — no hyphens, ``/en`` suffix.

    The tricky case: US published applications are stored as
    ``US-YYYYNNNNNN-A1`` with a 10-digit middle part (year + 6-digit sequence),
    while Google Patents canonical URLs use the 11-digit format
    ``US-YYYYNNNNNNN-A1`` (year + 7-digit sequence with a leading zero).
    For example:
        BQ:      US-2025229339-A1
        Canonical URL: https://patents.google.com/patent/US20250229339A1/en

    So we need to insert a ``0`` after the year for US application numbers
    stored in the compressed 10-digit form. All other formats (granted
    patents, non-US applications) can be concatenated as-is.

    Confirmed against real patents via curl:
        CN-222135971-U    -> CN222135971U     (200 OK)
        EP-4624188-A1     -> EP4624188A1      (200 OK)
        US-11000000-B2    -> US11000000B2     (200 OK — granted, no padding)
        US-2025229339-A1  -> US20250229339A1  (200 OK — zero-padded)
    """
    if not publication_number:
        return ""

    parts = publication_number.strip().split("-")
    if len(parts) == 3:
        country = parts[0].strip().upper()
        number = parts[1].strip()
        kind = parts[2].strip().upper()

        # US published-application fix: 10-digit number starting with a plausible
        # 4-digit year means it's YYYY + 6-digit sequence and needs a leading
        # zero inserted so the sequence becomes 7 digits.
        if (
            country == "US"
            and len(number) == 10
            and number[:4].isdigit()
            and 1900 <= int(number[:4]) <= 2100
        ):
            number = number[:4] + "0" + number[4:]

        clean = f"{country}{number}{kind}"
    else:
        # Unknown format — fall back to simple hyphen stripping
        clean = publication_number.replace(" ", "").replace("-", "")

    return f"https://patents.google.com/patent/{clean}/en"


def _full_record_dict(record) -> dict:
    """Return a record dict with all fields needed for client-side analytics.

    The interactive document-type filter recomputes every metric in JS when
    the user toggles a pill. That requires the embedded records to carry
    enough data for family deduplication, applicant ranking, jurisdiction
    counting, technology-area grouping, and notable-patent sorting.

    URL is always (re)computed at render time from the publication number
    so that old cached records pick up URL format fixes.
    """
    pub = getattr(record, "publication_number", "")
    return {
        "p": pub,                                                           # publication_number
        "t": (getattr(record, "title", "") or "")[:160],                   # title (truncated)
        "j": getattr(record, "jurisdiction", ""),                          # jurisdiction
        "y": getattr(record, "application_year", 0),                       # application_year
        "k": getattr(record, "kind_code", ""),                             # kind_code
        "f": getattr(record, "family_id", "") or pub,                      # family_id
        "a": list(getattr(record, "applicants_normalized", []) or []),     # applicants (all)
        "c": list(getattr(record, "cpc_classes_short", []) or []),         # cpc subclasses (all)
        "ci": getattr(record, "cited_by_count", 0),                        # cited_by_count
        "u": _google_patents_url(pub),                                     # url
    }


def _all_records_for_embed(bundle: ReportBundle) -> list[dict]:
    """Build the records array for embedding in the HTML.

    For the interactive document-type filter, ALL records must be embedded
    (not just referenced ones) because client-side JS recomputes every metric
    when the user toggles a filter pill. Field names are abbreviated to
    single-character keys to keep the JSON payload small — the JS runtime
    maps them back to readable names at load time.
    """
    return [_full_record_dict(r) for r in bundle.records]


def _patch_notable_patent_urls(bundle: ReportBundle, report_data: dict) -> None:
    """Recompute URLs on the notable_patents metric rows.

    Notable-patents rows are built in analytics.py from PatentRecord.url, which
    was frozen at BigQuery fetch time (hyphenated format, 404s). Rebuild them
    at render time so the table in the HTML always uses the canonical URL
    format regardless of when the underlying records were fetched.
    """
    metrics = report_data.get("metrics") or {}
    np_metric = metrics.get("notable_patents")
    if not np_metric:
        return
    value = np_metric.get("value") or {}
    rows = value.get("rows") or []
    for row in rows:
        pub = row.get("publication_number") or ""
        if pub:
            row["url"] = _google_patents_url(pub)


def _jurisdiction_map_data(bundle: ReportBundle) -> list[dict]:
    """Build the data array ECharts expects for the world map.

    Reads the jurisdiction_distribution metric, keeps only non-regional
    entries (countries), and maps each code to its GeoJSON name.
    Regional offices (EPO, WIPO) are filtered out — they get their own
    callout block next to the map.
    """
    from plain_language import country_name_for_geojson

    metric = bundle.metrics.get("jurisdiction_distribution")
    if not metric:
        return []
    rows = metric.value or []
    map_data = []
    for row in rows:
        if row.get("is_regional"):
            continue
        gj_name = country_name_for_geojson(row.get("code", ""))
        if not gj_name:
            continue
        map_data.append(
            {
                "name": gj_name,
                "code": row.get("code"),
                "displayName": row.get("name"),
                "value": row.get("count", 0),
                "share_label": row.get("share_label", ""),
                "record_ids": row.get("record_ids", []),
            }
        )
    return map_data


def _regional_callouts(bundle: ReportBundle) -> list[dict]:
    """Build the side callouts for regional offices (EPO, WIPO, EAPO, ...)."""
    from plain_language import jurisdiction_note

    metric = bundle.metrics.get("jurisdiction_distribution")
    if not metric:
        return []
    callouts = []
    for row in metric.value or []:
        if not row.get("is_regional"):
            continue
        callouts.append(
            {
                "code": row.get("code"),
                "name": row.get("name"),
                "count": row.get("count", 0),
                "share_label": row.get("share_label", ""),
                "note": jurisdiction_note(row.get("code", "")),
                "record_ids": row.get("record_ids", []),
            }
        )
    return callouts


def render_report(
    bundle: ReportBundle,
    output_path: Path | str,
    query_label: str = "Patent Landscape Report",
) -> Path:
    """Render a ReportBundle to a self-contained HTML file.

    Args:
        bundle: The report data bundle produced by the analytics layer.
        output_path: Where to write the HTML file. Parent directory is created
            if it doesn't exist. If output_path is a directory, a file name is
            auto-generated from the query label and timestamp.
        query_label: Short human-readable label for the report, shown in the
            header and used in the filename. E.g. "AI patent landscape \u2013 2024".

    Returns:
        The absolute path to the written file.
    """
    env = Environment(
        loader=FileSystemLoader(str(TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "jinja"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    template = env.get_template("report.html.jinja")

    echarts_js = _load_vendor("echarts.min.js")
    world_geojson_raw = _load_vendor("world.geo.json")
    # Parse-then-reserialize to guarantee valid JSON and to strip whitespace
    world_geojson = _safe_embed_json(json.loads(world_geojson_raw))

    report_data = bundle.to_dict()
    # Replace the full record list with an abbreviated-key projection.
    # ALL records are embedded (not filtered to referenced ones) because
    # the interactive document-type filter recomputes metrics client-side.
    report_data["records"] = _all_records_for_embed(bundle)
    report_data["map_data"] = _jurisdiction_map_data(bundle)
    report_data["regional_callouts"] = _regional_callouts(bundle)
    # Patch URLs on metric rows so they match the canonical format
    _patch_notable_patent_urls(bundle, report_data)
    data_json = _safe_embed_json(report_data)

    rendered_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    html = template.render(
        query_label=query_label,
        generated_at_display=rendered_at,
        echarts_js=echarts_js,
        world_geojson=world_geojson,
        data_json=data_json,
        bundle=report_data,
    )

    # Resolve output path
    output_path = Path(output_path)
    if output_path.is_dir():
        slug = _slugify(query_label)
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M")
        output_path = output_path / f"patent-landscape_{slug}_{timestamp}.html"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(html, encoding="utf-8")
    return output_path.resolve()


def _slugify(text: str) -> str:
    """Convert a label into a safe filename slug."""
    import re
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.strip().lower())
    return slug.strip("-")[:60] or "report"
