"""Landscape Builder — the skill's orchestrator.

Takes a query spec (either a BigQuery search or a CSV path), runs the full
pipeline (fetch -> normalize -> analyze -> render), and produces two outputs:

    1. A self-contained HTML report file on disk.
    2. A short Markdown headline that Claude prints in the chat.

This is the function that SKILL.md routes user requests to. It's also callable
from the command line for ad-hoc runs.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


# Ensure scripts/ is importable when this module is run directly
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from analytics import compute_all_metrics, compute_caveats
from applicant_normalizer import build_merge_audit, merges_only
from data_layer import PatentRecord, year_range
from html_renderer import render_report
from plain_language import cpc_plain_english, describe_trend
from provenance import ReportBundle


# ---------------------------------------------------------------------------
# Core entry point
# ---------------------------------------------------------------------------


def build_report(
    query_spec: dict,
    output_dir: str | Path | None = None,
    cost_ceiling_bytes: int = 20_000_000_000,
) -> dict:
    """Run the full pipeline and return a result dict.

    Args:
        query_spec: dict describing the search. Must include a 'mode' key:
            - mode: 'bigquery'
                cpc_prefixes: list[str]                 required
                date_from: 'YYYY-MM-DD'                 required
                date_to: 'YYYY-MM-DD'                   required
                countries: list[str]                    optional
                row_limit: int                          optional (default 5000)
                query_label: str                        optional (title of report)
            - mode: 'csv'
                csv_path: str                           required
                query_label: str                        optional
        output_dir: directory to write the HTML into. Defaults to ./reports/
            in the current working directory. A filename is auto-generated
            from the query label and timestamp.

    Returns:
        dict with:
            output_path: absolute path to the generated HTML
            headline_markdown: short Markdown block suitable for printing in chat
            stats: dict with record_count, family_count, date_range, etc.
    """
    mode = query_spec.get("mode", "").lower()
    query_label = query_spec.get("query_label") or _default_label(query_spec)

    if mode == "bigquery":
        records, source_meta = _fetch_bigquery(query_spec, cost_ceiling_bytes=cost_ceiling_bytes)
    elif mode == "csv":
        records, source_meta = _fetch_csv(query_spec)
    else:
        raise ValueError(
            f"Unknown query_spec mode: {mode!r}. Use 'bigquery' or 'csv'."
        )

    if not records:
        raise RuntimeError(
            "No patent records returned for this query. Check the filters and try again."
        )

    # Determine date range from the records themselves if not explicit
    min_year, max_year = year_range(records)
    if min_year == 0 or max_year == 0:
        raise RuntimeError("Records have no usable filing years; cannot compute metrics.")

    date_range = (min_year, max_year)

    # Compute all metrics
    metrics = compute_all_metrics(records, date_range)

    # Build applicant merge audit
    audit_pairs = []
    for r in records:
        for raw, norm in zip(r.applicants_raw, r.applicants_normalized):
            audit_pairs.append((raw, norm))
    audit = build_merge_audit(audit_pairs)

    # Compose the bundle
    source_label = _data_source_label(mode, source_meta)
    bundle = ReportBundle(
        query_spec=_public_query_spec(query_spec, source_meta, source_label),
        data_source=source_label,
        generated_at=datetime.now(timezone.utc).isoformat(),
        record_count=len(records),
        family_count=metrics["headline_total_families"].value,
        date_range=date_range,
        records=records,
        applicant_merges=audit,
    )
    bundle.query_spec["caveats"] = compute_caveats(records, source_label, audit, source_metadata=source_meta)
    for mid, m in metrics.items():
        bundle.add_metric(m)

    # Render
    if output_dir is None:
        output_dir = Path.cwd() / "reports"
    output_path = render_report(bundle, Path(output_dir), query_label=query_label)

    # Build the chat headline
    headline_markdown = _build_headline_markdown(
        metrics=metrics,
        query_label=query_label,
        date_range=date_range,
        source_meta=source_meta,
        output_path=output_path,
        merge_count=len(merges_only(audit)),
        caveats=bundle.query_spec["caveats"],
    )

    return {
        "output_path": str(output_path),
        "headline_markdown": headline_markdown,
        "stats": {
            "record_count": len(records),
            "family_count": metrics["headline_total_families"].value,
            "date_range": list(date_range),
            "data_source": source_label,
        },
    }


# ---------------------------------------------------------------------------
# Data source dispatch
# ---------------------------------------------------------------------------


def _fetch_bigquery(
    spec: dict,
    cost_ceiling_bytes: int = 20_000_000_000,
) -> tuple[list[PatentRecord], dict]:
    from data_fetcher_bigquery import fetch_landscape

    cpc_prefixes = spec.get("cpc_prefixes") or []
    date_from = spec.get("date_from")
    date_to = spec.get("date_to")
    countries = spec.get("countries") or None
    row_limit = int(spec.get("row_limit") or 20000)

    if not cpc_prefixes:
        raise ValueError("BigQuery mode requires 'cpc_prefixes' (list of CPC codes).")
    if not date_from or not date_to:
        raise ValueError("BigQuery mode requires 'date_from' and 'date_to' (YYYY-MM-DD).")

    return fetch_landscape(
        cpc_prefixes=cpc_prefixes,
        date_from=date_from,
        date_to=date_to,
        countries=countries,
        row_limit=row_limit,
        cost_ceiling_bytes=cost_ceiling_bytes,
    )


def _fetch_csv(spec: dict) -> tuple[list[PatentRecord], dict]:
    from data_fetcher_csv import fetch_from_csv

    csv_path = spec.get("csv_path")
    if not csv_path:
        raise ValueError("CSV mode requires 'csv_path'.")
    return fetch_from_csv(csv_path)


# ---------------------------------------------------------------------------
# Labels and metadata
# ---------------------------------------------------------------------------


def _default_label(spec: dict) -> str:
    """Generate a reasonable default query label from the spec."""
    mode = spec.get("mode", "")
    if mode == "bigquery":
        cpcs = spec.get("cpc_prefixes") or []
        parts = [cpc_plain_english(c) for c in cpcs]
        parts = [p for p in parts if p and p != "Unclassified technology"]
        date_range = ""
        if spec.get("date_from") and spec.get("date_to"):
            df = spec["date_from"][:4]
            dt = spec["date_to"][:4]
            date_range = f" \u2013 {df}\u2013{dt}" if df != dt else f" \u2013 {df}"
        label_core = parts[0] if parts else ", ".join(cpcs) if cpcs else "Patent Landscape"
        return f"{label_core}{date_range}"
    if mode == "csv":
        name = Path(spec.get("csv_path", "")).stem or "Patent Landscape"
        return name.replace("_", " ").replace("-", " ")
    return "Patent Landscape Report"


def _data_source_label(mode: str, meta: dict) -> str:
    if mode == "bigquery":
        return "Google BigQuery \u2014 Google Patents Public Datasets"
    if mode == "csv":
        name = meta.get("source_file_name", "")
        return f"CSV file ({name})" if name else "CSV file"
    return mode or "unknown"


def _public_query_spec(spec: dict, meta: dict, source_label: str) -> dict:
    """Trimmed version of the query spec safe to embed in the report template.

    Only includes user-facing fields; hides internal plumbing.
    """
    public = {
        "mode": spec.get("mode", ""),
        "source_label": source_label,
    }
    if spec.get("mode") == "bigquery":
        public["cpc_prefixes"] = spec.get("cpc_prefixes", [])
        public["date_from"] = spec.get("date_from", "")
        public["date_to"] = spec.get("date_to", "")
        public["countries"] = spec.get("countries") or []
    elif spec.get("mode") == "csv":
        public["source_file_name"] = meta.get("source_file_name", "")
    return public


# ---------------------------------------------------------------------------
# Chat headline
# ---------------------------------------------------------------------------


def _build_headline_markdown(
    metrics: dict,
    query_label: str,
    date_range: tuple[int, int],
    source_meta: dict,
    output_path: Path,
    merge_count: int,
    caveats: list[str] | None = None,
) -> str:
    """Build the short Markdown the skill prints in the chat.

    Pattern: narrative sentence + bullet stats + file pointer. Matches the
    in-report headline but compressed for the chat UX.
    """
    total_families = metrics["headline_total_families"].value
    total_applicants = metrics["headline_total_applicants"].value
    total_jurisdictions = metrics["headline_total_jurisdictions"].value
    peak_year_metric = metrics["headline_peak_year"]
    peak_year = peak_year_metric.value
    peak_count = peak_year_metric.quantification.get("count", 0)
    trend = metrics["headline_trend"].value

    # Format date range — don't repeat year if start == end
    if date_range[0] == date_range[1]:
        date_span = f"{date_range[0]}"
    else:
        date_span = f"{date_range[0]}\u2013{date_range[1]}"

    lines = [
        f"## {query_label}",
        "",
        f"**{total_families:,} patent families** filed by "
        f"**{total_applicants:,} applicants** across "
        f"**{total_jurisdictions:,} jurisdictions** in {date_span}.",
        "",
    ]

    # Surface critical caveats (truncation, snapshot freshness) prominently
    if source_meta.get("truncated") or (
        source_meta.get("actual_to") and source_meta.get("date_to") and
        source_meta["actual_to"] < source_meta["date_to"]
    ):
        lines.append("> **Heads up:**")
        if source_meta.get("truncated"):
            lines.append(
                f"> - The query hit the {source_meta.get('row_limit', 5000):,}-row cap. "
                f"There are more records beyond this — re-run with `--limit` raised or narrow the query."
            )
        if source_meta.get("actual_to") and source_meta.get("date_to") and source_meta["actual_to"] < source_meta["date_to"]:
            lines.append(
                f"> - BigQuery snapshot currently has data up to **{source_meta['actual_to']}**, "
                f"not the requested end of {source_meta['date_to']}. Effective window is shorter than requested."
            )
        lines.append("")

    lines.append(f"- Peak filing year: **{peak_year}** ({peak_count:,} filings)")
    if trend and not trend.startswith(str(peak_count)):
        lines.append(f"- Trend: {trend}")

    top = metrics.get("top_applicants")
    if top and top.value:
        top3 = top.value[:3]
        bullet = ", ".join(
            f"{row['name']} ({row['family_count']})" for row in top3
        )
        lines.append(f"- Top applicants: {bullet}")

    juris = metrics.get("jurisdiction_distribution")
    if juris and juris.value:
        top3 = juris.value[:3]
        bullet = ", ".join(
            f"{row['name']} ({row['count']})" for row in top3
        )
        lines.append(f"- Top jurisdictions: {bullet}")

    if merge_count:
        lines.append(f"- {merge_count} applicant name variants merged (see Methodology in the report)")

    lines.append("")
    lines.append(f"**Full interactive report:** `{output_path}`")
    lines.append("")
    lines.append("_Double-click the file to open it in your browser. It is self-contained, works offline, and is safe to email._")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _iso_n_months_ago(n: int) -> tuple[str, str]:
    """Return (from, to) ISO date strings for a trailing-N-months window."""
    today = datetime.now(timezone.utc).date()
    start = today - timedelta(days=int(n * 30.44))
    return start.isoformat(), today.isoformat()


# ---------------------------------------------------------------------------
# USPTO file history fallback
# ---------------------------------------------------------------------------


def _import_uspto_skill():
    """Locate the uspto-patent-search skill and add its scripts/ to sys.path.

    Returns the loaded modules we need. Raises RuntimeError if not installed.
    """
    skill_root = Path(os.path.expanduser("~/.claude/skills/uspto-patent-search"))
    scripts_dir = skill_root / "scripts"
    if not scripts_dir.exists():
        raise RuntimeError(
            "uspto-patent-search skill not found at "
            f"{skill_root}. Install it to use the history subcommand."
        )
    if str(scripts_dir) not in sys.path:
        sys.path.insert(0, str(scripts_dir))
    import file_wrapper  # type: ignore
    import download_documents  # type: ignore
    import patent_search  # type: ignore
    return file_wrapper, download_documents, patent_search


def download_file_history(
    patent_numbers: list[str],
    output_dir: str | Path | None = None,
    key_docs_only: bool = True,
) -> dict:
    """Download USPTO file wrapper PDFs for a list of US patent numbers.

    Resolves each patent number to an application number, then downloads the
    key prosecution documents (office actions, responses, notices, IDS forms)
    into ./reports/file-histories/{app_number}/.

    Args:
        patent_numbers: US patent numbers in any format (e.g. "11,000,000",
            "US11000000", "11000000").
        output_dir: Directory to save PDFs into. Each patent gets its own
            subdirectory. Defaults to ./reports/file-histories/.
        key_docs_only: If True, only fetch the standard set of key prosecution
            documents. If False, fetch everything in the file wrapper.

    Returns:
        dict with per-patent results:
            { "US-12345-B2": {"app_number": "16/...", "downloaded": 12, "dest": "..."}, ... }
    """
    file_wrapper, download_documents, patent_search = _import_uspto_skill()

    output_dir = Path(output_dir) if output_dir else Path.cwd() / "reports" / "file-histories"
    output_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, dict] = {}

    for patent_number in patent_numbers:
        try:
            # Step 1: Resolve patent number -> application number via file_wrapper
            meta = file_wrapper.get_application_by_patent_number(patent_number)
            app_number = None
            if isinstance(meta, dict):
                # The ODP response shape: {"patentFileWrapperDataBag": [{ "applicationNumberText": ... }]}
                bag = meta.get("patentFileWrapperDataBag") or meta.get("patentBag") or []
                if bag and isinstance(bag, list):
                    app_number = (
                        bag[0].get("applicationNumberText")
                        or bag[0].get("applicationNumber")
                    )
            if not app_number:
                results[patent_number] = {
                    "status": "failed",
                    "error": "Could not resolve patent number to an application number.",
                }
                continue

            dest = output_dir / app_number.replace("/", "_")
            dest.mkdir(parents=True, exist_ok=True)

            # Step 2: Download the documents
            summary = download_documents.download_documents(
                app_number=app_number,
                output_dir=str(dest),
                key_only=key_docs_only,
            )

            results[patent_number] = {
                "status": "ok",
                "app_number": app_number,
                "output_dir": str(dest),
                "summary": summary,
            }
        except Exception as e:
            results[patent_number] = {
                "status": "failed",
                "error": f"{type(e).__name__}: {e}",
            }

    return results


def _main() -> int:
    parser = argparse.ArgumentParser(
        prog="landscape_builder",
        description="Generate a patent landscape report as a self-contained HTML file.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # bigquery subcommand
    p_bq = sub.add_parser("search", help="Search Google Patents BigQuery by CPC + date range.")
    p_bq.add_argument("--cpc", action="append", required=True,
                      help="CPC prefix(es). Pass --cpc multiple times for multiple prefixes.")
    date_group = p_bq.add_mutually_exclusive_group(required=True)
    date_group.add_argument("--months", type=int, help="Last N months ending today.")
    date_group.add_argument("--years", type=int, help="Last N years ending today.")
    date_group.add_argument("--from-to", nargs=2, metavar=("FROM", "TO"),
                            help="Explicit date range: YYYY-MM-DD YYYY-MM-DD.")
    p_bq.add_argument("--country", action="append", default=None,
                      help="Restrict to country code(s). Pass multiple times.")
    p_bq.add_argument("--limit", type=int, default=20000, help="Max rows to fetch (default 20000).")
    p_bq.add_argument("--max-gb", type=float, default=20.0,
                      help="Max query scan size in GB (default 20). Raise for broad worldwide searches.")
    p_bq.add_argument("--label", default=None, help="Title of the report.")
    p_bq.add_argument("--output-dir", default=None, help="Output directory (default ./reports).")

    # csv subcommand
    p_csv = sub.add_parser("csv", help="Build a report from a Lens-format CSV file.")
    p_csv.add_argument("csv_path", help="Path to the CSV file.")
    p_csv.add_argument("--label", default=None, help="Title of the report.")
    p_csv.add_argument("--output-dir", default=None, help="Output directory (default ./reports).")

    # history subcommand (USPTO file wrapper download)
    p_hist = sub.add_parser(
        "history",
        help="Download USPTO file wrapper PDFs for US patents. Uses the uspto-patent-search skill.",
    )
    p_hist.add_argument(
        "patent_numbers",
        nargs="+",
        help="One or more US patent numbers (e.g. 11000000 or US-11000000-B2).",
    )
    p_hist.add_argument(
        "--output-dir",
        default=None,
        help="Directory to save PDFs into (default ./reports/file-histories/).",
    )
    p_hist.add_argument(
        "--all-docs",
        action="store_true",
        help="Download every document in the file wrapper (default: key prosecution docs only).",
    )

    args = parser.parse_args()

    if args.command == "search":
        if args.months:
            date_from, date_to = _iso_n_months_ago(args.months)
        elif args.years:
            date_from, date_to = _iso_n_months_ago(args.years * 12)
        else:
            date_from, date_to = args.from_to
        spec = {
            "mode": "bigquery",
            "cpc_prefixes": args.cpc,
            "date_from": date_from,
            "date_to": date_to,
            "countries": args.country,
            "row_limit": args.limit,
            "query_label": args.label,
        }
    elif args.command == "csv":
        spec = {
            "mode": "csv",
            "csv_path": args.csv_path,
            "query_label": args.label,
        }
    elif args.command == "history":
        # USPTO file history is a separate flow — not a report build.
        try:
            results = download_file_history(
                args.patent_numbers,
                output_dir=args.output_dir,
                key_docs_only=not args.all_docs,
            )
        except Exception as e:
            print(f"Error: {e}", file=sys.stderr)
            return 2
        print("## USPTO File History")
        print()
        for pn, r in results.items():
            if r.get("status") == "ok":
                summary = r.get("summary", {})
                downloaded = summary.get("downloaded", 0) if isinstance(summary, dict) else 0
                total = summary.get("total", 0) if isinstance(summary, dict) else 0
                print(f"- **{pn}** \u2192 application `{r['app_number']}`")
                print(f"  - {downloaded}/{total} document(s) downloaded")
                print(f"  - Saved to `{r['output_dir']}`")
            else:
                print(f"- **{pn}** \u2014 failed: {r.get('error', 'unknown error')}")
        return 0
    else:
        parser.print_help()
        return 1

    cost_ceiling = int(getattr(args, "max_gb", 20.0) * 1_000_000_000)
    try:
        result = build_report(spec, output_dir=args.output_dir, cost_ceiling_bytes=cost_ceiling)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    print(result["headline_markdown"])
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
