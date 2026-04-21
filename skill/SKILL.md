---
name: patent-landscape-report
description: >
  Generate a decision-grade patent landscape report as a self-contained
  interactive HTML file. The report contains a plain-language overview,
  top-applicant leaderboard, world map + country ranking, filing trends over
  time, technology-area breakdown (plain-English CPC translations), notable
  patents list, and a methodology & caveats section. Every number in the
  report carries provenance — the reader clicks any metric to see the exact
  patents that contributed to it.

  Use this skill when the user wants to:
  - Build a patent landscape report on a technology area (e.g. "last 12
    months of AI patents", "solid-state battery separators landscape",
    "who's filing in quantum computing")
  - Produce a deliverable they can email to a non-patent-fluent reader
    (VC, executive, client) who needs to answer invest/don't-invest
    questions
  - Turn a pre-collected Lens.org CSV export into the same standardized
    report format
  - Refresh a previous landscape snapshot against the latest Google Patents
    data

  Do NOT use this skill for:
  - Looking up a specific patent by number (use uspto-patent-search or
    google-patent-search directly)
  - Prosecution history or file wrapper documents (use uspto-patent-search)
  - Claims text search or litigation research (use google-patent-search
    directly — it's the lower layer)
  - Patent drafting, filing, or prosecution tasks

argument-hint: "[technology area or file path]"
---

# Patent Landscape Report Skill

## What this skill produces

Two outputs from the same underlying data layer:

1. **A Markdown headline** printed inline in the chat — one-sentence narrative
   summary, peak year, trend direction, top 3 applicants and jurisdictions,
   merge-audit count, and the file path.

2. **A self-contained interactive HTML file** written to `./reports/` in
   the current working directory. The file inlines everything it needs
   (ECharts ~1 MB, world GeoJSON ~250 KB, all data as JSON, CSS, JS). It
   opens in any browser, works offline forever, is safe to email.

The HTML has seven sections: Overview (narrative summary + four stat tiles),
Leaders (top applicants bar chart), Geography (world choropleth + regional
callouts for EPO/WIPO + country ranking bar chart), Trends (filings over time
total + stacked-by-jurisdiction), Technology (CPC in plain English), Notable
patents (top-cited table), and Methodology & caveats.

Every metric on the page has a "Why?" button. Clicking it opens a side panel
showing: plain-English formula, quantification breakdown, the list of specific
patents that contributed (each clickable to Google Patents), caveats, and
sensitivity notes ("what would change this number").

## Setup

Before the first run, verify the environment:

```bash
python3 ~/.claude/skills/patent-landscape-report/get_started.py
```

This checks:
- Python 3.11+ installed
- `jinja2` installed (run `pip install jinja2` if not)
- The `google-patent-search` skill is installed (for BigQuery mode)
- Vendor files (`echarts.min.js`, `world.geo.json`) are present in
  `skill/vendor/`; downloads them if not

For BigQuery mode, the user also needs `gcloud auth application-default
login` set up. The `google-patent-search` skill's `get_started.py` handles
that side — defer to it if setup is needed.

## How to Handle User Requests

### Step 1: Parse the request

Identify which mode the user wants:
- **BigQuery search mode**: They want you to pull patents from Google Patents
  BigQuery. Look for technology descriptions ("AI", "batteries", "CRISPR"),
  date hints ("last 12 months", "since 2020"), maybe jurisdiction hints
  ("US only", "worldwide").
- **CSV mode**: They provide a path to a Lens.org CSV they already exported.

### Step 2: Map technology descriptions to CPC prefixes

If the user asks for a landscape by topic, translate to CPC prefixes. Common
mappings:

| Topic | CPC prefix(es) |
|---|---|
| AI / machine learning / neural networks | `G06N` |
| Computer vision | `G06V` |
| Natural language processing | `G06F 40` |
| Speech recognition | `G10L 15` |
| Batteries / fuel cells | `H01M` |
| Solar / photovoltaic | `H02S`, `H01L 31` |
| Wind power | `F03D` |
| Quantum computing | `G06N 10` |
| Semiconductors | `H01L` |
| Wireless / 5G / cellular | `H04W` |
| Autonomous vehicles | `B60W 60` |
| CRISPR / gene editing | `C12N 15` |
| Antibodies / therapeutics | `A61K 39`, `C07K 16` |
| 3D printing / additive mfg | `B33Y` |
| Dental | `A61C` |
| Medical devices | `A61B` |
| Drug delivery | `A61M` |

If the topic is ambiguous or broad, ask the user to confirm the CPC prefix
before running (expensive searches shouldn't be guessed).

### Step 3: Run the builder

From Python:

```python
import os, sys
sys.path.insert(0, os.path.expanduser("~/.claude/skills/patent-landscape-report/scripts"))
from landscape_builder import build_report

result = build_report({
    "mode": "bigquery",
    "cpc_prefixes": ["G06N"],                     # required for BQ
    "date_from": "2025-04-12",                    # YYYY-MM-DD
    "date_to": "2026-04-12",                      # YYYY-MM-DD
    "countries": None,                            # None = worldwide
    "row_limit": 5000,                            # sane default
    "query_label": "AI patent landscape \u2014 last 12 months",
})
print(result["headline_markdown"])
```

For a CSV file:

```python
result = build_report({
    "mode": "csv",
    "csv_path": "/path/to/Lens Export.csv",
    "query_label": "Client X portfolio review",
})
```

Or from the command line (useful when the user wants to re-run something
quickly):

```bash
# BigQuery mode, trailing 12 months
python3 scripts/landscape_builder.py search --cpc G06N --months 12 --label "AI — last 12 months"

# BigQuery mode, explicit date range
python3 scripts/landscape_builder.py search --cpc H01M --from-to 2020-01-01 2025-12-31 --label "Battery landscape"

# CSV mode
python3 scripts/landscape_builder.py csv "~/Downloads/my_export.csv" --label "Competitor portfolio"
```

### Step 4: Present the result

The `build_report` call returns a dict with:
- `output_path`: absolute path to the HTML file
- `headline_markdown`: the Markdown block to print inline
- `stats`: dict with record_count, family_count, date_range, data_source

Print the headline verbatim in your chat response. Do **not** paraphrase or
summarize it further — it's already compressed, and paraphrasing risks
stripping the receipts. Point the user at the file path and let them know
they can double-click to open it.

### Step 5: If the user wants to tweak

Common follow-ups after the first report:
- "Narrow it to just the US" → add `countries=["US"]` and re-run
- "Include computer vision too" → add `G06V` to `cpc_prefixes`
- "Stretch the date range" → pass explicit `date_from`/`date_to`
- "Open the report" → `open {output_path}` on macOS, `xdg-open` on Linux

Never silently re-run an expensive BigQuery query with modified parameters.
Always show the new filter set and confirm before re-running if the cost
might exceed 5 GB (the skill's data_fetcher_bigquery raises its ceiling to
20 GB, so typical queries run without friction, but very broad CPC + multi-
year worldwide searches can exceed that).

## Cost awareness

BigQuery charges by bytes scanned. The `google-patent-search` skill handles
cost estimation via dry-runs before every query. Typical landscape query
costs:

- `G06N` worldwide, 12 months: ~3–8 GB scanned
- `G06N` US only, 24 months: ~1–2 GB
- Very broad searches (e.g. all `H04L` worldwide, 10 years): 20+ GB

If a query exceeds the cost ceiling, the skill raises a `BigQueryError`. Show
the error to the user, explain what would narrow it, and wait for them to
approve before passing `force=True`.

Free-tier allowance: 1 TB/month. A typical landscape report run is a rounding
error against this.

## Output location

Default: `./reports/` in the current working directory (whichever folder the
user is working in when they invoke the skill). Each report gets a timestamped
filename like `patent-landscape_ai-landscape_20260412-1034.html`.

Users can override with the `output_dir` argument in `build_report()` or
the `--output-dir` CLI flag.

## What the report does NOT include

Some fields from the Lens.org CSV workflow are not available in BigQuery mode
and are gracefully omitted from search-mode reports:

- **Legal status** (ACTIVE / PENDING / etc.) — only available from USPTO for
  US patents. Not in BigQuery. CSV-mode reports keep it.
- **Forward citation counts** — too expensive to query at scale. The Notable
  Patents section falls back to "most recent filings from top applicants"
  when citations are unavailable.
- **Detailed family member lists** — the skill uses BigQuery's `family_id`
  for dedup but doesn't fetch every family member's metadata.
- **File history (prosecution documents)** — not included in the landscape
  report itself. Use the `history` subcommand below to pull file wrappers for
  specific patents, or delegate to the `uspto-patent-search` skill.

These limitations are documented in the Methodology section of every report.

## USPTO escalation path

The landscape report is a snapshot built from BigQuery (or CSV) data. For
deeper work on specific US patents — file histories, prosecution rejections,
PTAB challenges, current legal status, assignment chain-of-title — the user
should use the `uspto-patent-search` skill. This skill exposes one convenience
wrapper for the most common follow-up: downloading file wrappers.

### Download file histories for specific US patents

```bash
python3 scripts/landscape_builder.py history US11000000 US11000001 \
  --output-dir reports/file-histories/
```

Or from Python:

```python
from landscape_builder import download_file_history
results = download_file_history(
    ["US-11000000-B2", "11000001"],
    output_dir="reports/file-histories/",
    key_docs_only=True,   # False = download everything in the wrapper
)
```

The result is one subdirectory per patent containing the key prosecution
documents as PDFs. Document types included by default: office actions,
amendments, IDS filings, notices of allowance, examiner interviews. Pass
`--all-docs` to get everything in the wrapper (can be hundreds of files).

This is the bridge between the landscape report (breadth — thousands of
patents, metadata only) and USPTO deep-dive (depth — one patent, full
prosecution history). When a user reading a landscape report says "I want
to understand patent US X," the history subcommand is the next step.

### Other USPTO-only follow-ups

For anything beyond file-history PDFs — prosecution analytics, PTAB
challenges, chain of title, examiner statistics — call the
`uspto-patent-search` skill directly. SKILL.md in that skill has the full
routing matrix. Common follow-ups:

| User wants | Use |
|---|---|
| Download file history PDFs | `landscape_builder history` (wraps uspto-patent-search) |
| Current legal status | `uspto-patent-search.patent_search.search_by_patent_number` |
| Prosecution rejections | `uspto-patent-search.office_actions_search` |
| PTAB challenges (IPR/PGR) | `uspto-patent-search.ptab_search.search_proceedings` |
| Current owner / assignment chain | `uspto-patent-search.assignment_search.get_assignment_chain` |
| Continuations / family tree | `uspto-patent-search.file_wrapper.get_continuity` |

All of these take a US patent number and work independently of the landscape
report. Run them after the landscape report reveals a patent worth deep-diving.

## Troubleshooting

- **"No patent records returned"**: Either the CPC prefix is wrong or the
  date range is too narrow. Double-check the CPC against the table above.
- **"BigQuery auth failed"**: Run `gcloud auth application-default login`
  and retry.
- **"Query would scan X GB"**: Narrow the filters (country, date range, more
  specific CPC) or ask the user for explicit approval to exceed the ceiling.
- **"Vendor file not found"**: Run `get_started.py` to download the vendor
  assets (echarts.min.js and world.geo.json).
- **"google-patent-search skill not found"**: Install it from its repo and
  re-run `get_started.py`.
