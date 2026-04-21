"""Provenance model.

Every computed value in the landscape report is wrapped in a Metric so the
reader can click a "Why?" button and see the exact records, formula, caveats,
and sensitivity notes behind the number. This module defines the data classes
and a couple of convenience constructors.

Design principle: metrics without receipts are not allowed anywhere in the
report. If a value is going to appear in the HTML, it must be instantiated
through Metric() and carry its provenance with it. This is the trust unlock
and the whole reason the skill exists in its current form.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Metric:
    """A computed value with full provenance attached.

    Attributes:
        id: Stable identifier, unique within a report (e.g. "headline_total_families").
        label: Human-readable label for this metric ("Total patent families").
        value: The display value. Can be a string, number, list, dict — whatever the
            renderer expects for this particular slot.
        quantification: Structured numeric breakdown supporting the value
            (e.g. {"count": 247, "share_pct": 13.8, "period": "2018-2025"}).
        formula_plain: Plain-English description of how the value was computed.
            This is what the reader sees in the receipts panel.
        formula_technical: Technical form of the formula (SQL-ish or Python-ish).
            Shown as a secondary detail for attorneys defending the number.
        source_record_ids: Publication numbers of the specific patents that
            contributed to this metric. Powers drill-downs.
        caveats: Short text notes the reader should know about
            (e.g. "4 applicant name variants were merged into this entity").
        sensitivity_notes: "What would change this number" observations
            (e.g. "Using priority date instead of application date shifts the
            peak year from 2023 to 2021").
        computed_at: ISO-8601 UTC timestamp, set automatically on construction.
    """

    id: str
    label: str
    value: Any
    quantification: dict = field(default_factory=dict)
    formula_plain: str = ""
    formula_technical: str = ""
    source_record_ids: list[str] = field(default_factory=list)
    caveats: list[str] = field(default_factory=list)
    sensitivity_notes: list[str] = field(default_factory=list)
    computed_at: str = ""

    def __post_init__(self) -> None:
        if not self.computed_at:
            self.computed_at = _utc_iso()

    def to_dict(self) -> dict:
        """Serialize for embedding as JSON in the HTML report."""
        return asdict(self)


@dataclass
class ReportBundle:
    """Everything the renderer needs to produce a report.

    The bundle is the single handoff between the analytics layer and the
    HTML renderer. It carries:
        - query spec and run metadata (for the methodology section)
        - all metrics, keyed by id so the template can look them up
        - the full set of patent records (for drill-downs and deep links)
        - applicant normalization audit trail (for transparency)
    """

    query_spec: dict
    data_source: str  # "bigquery" | "csv" | "bigquery+uspto" etc.
    generated_at: str
    record_count: int
    family_count: int
    date_range: tuple[int, int]

    metrics: dict[str, Metric] = field(default_factory=dict)
    # records are PatentRecord instances; kept generic here to avoid a circular
    # import with data_layer.
    records: list = field(default_factory=list)
    applicant_merges: dict[str, list[str]] = field(default_factory=dict)

    def add_metric(self, metric: Metric) -> None:
        if metric.id in self.metrics:
            raise ValueError(f"Duplicate metric id: {metric.id}")
        self.metrics[metric.id] = metric

    def to_dict(self) -> dict:
        """Serialize the whole bundle for embedding as JSON in the HTML."""
        return {
            "query_spec": self.query_spec,
            "data_source": self.data_source,
            "generated_at": self.generated_at,
            "record_count": self.record_count,
            "family_count": self.family_count,
            "date_range": list(self.date_range),
            "metrics": {mid: m.to_dict() for mid, m in self.metrics.items()},
            "records": [_serialize_record(r) for r in self.records],
            "applicant_merges": self.applicant_merges,
        }


def _serialize_record(record) -> dict:
    """Convert a PatentRecord (or any dataclass with asdict support) to JSON-safe dict."""
    if hasattr(record, "__dataclass_fields__"):
        return asdict(record)
    if isinstance(record, dict):
        return record
    raise TypeError(f"Cannot serialize record of type {type(record).__name__}")
