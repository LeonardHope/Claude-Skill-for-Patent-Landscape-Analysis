"""Applicant name normalization.

Patent applicants show up under many name variants — different legal-entity
suffixes (Inc., Ltd., Co., GmbH, KK, 株式会社, 주식회사), different
punctuation, cross-language filings, parent/subsidiary splits. If the report
shows "Samsung Electronics Co., Ltd." and "Samsung Electronics Corp" ranked
separately, a non-patent reader will silently lose trust.

This module normalizes names in two passes:

1. Rule-based cleanup. Strip a fixed list of legal suffixes and standardize
   whitespace/case. Conservative — it never strips descriptive words like
   "Electronics" or "Display" because those can distinguish real sibling
   entities.

2. Alias lookup. A hand-curated reference file maps known name variants
   (after pass 1) to a canonical display name. Grows over time.

The full audit trail — which raw names mapped to which canonical — is
surfaced in the report's Methodology section so the attorney can defend
any merge that a client questions.
"""

from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path


_REFERENCES_DIR = Path(__file__).resolve().parent.parent / "references"


# ---------------------------------------------------------------------------
# Legal suffix stripping
# ---------------------------------------------------------------------------

# Ordered longest-first so "Co., Ltd." is stripped as a unit rather than
# leaving "Co." behind after stripping "Ltd.". Applied iteratively so
# trailing compound suffixes collapse fully.
_LEGAL_SUFFIXES = [
    # Compound English
    "CO., LTD.", "CO. LTD.", "CO LTD", "CO., LTD", "COMPANY LIMITED",
    "& CO. KG", "& CO KG", "& CO., KG",
    "CO., INC.", "CO. INC.", "CO INC", "CO., INC",
    "CORP. LTD.", "CORPORATION LIMITED",
    "HOLDINGS LIMITED", "HOLDINGS LTD.", "HOLDINGS LTD", "HOLDINGS, INC.",
    "GROUP LIMITED", "GROUP LTD.", "GROUP LTD", "GROUP, INC.",
    # Simple English
    "INCORPORATED", "INC.", "INC",
    "CORPORATION", "CORP.", "CORP",
    "COMPANY", "COMP.", "COMP", "CO.", "CO",
    "LIMITED", "LTD.", "LTD",
    "L.L.C.", "LLC", "L L C",
    "PLC", "P.L.C.",
    "PTY. LTD.", "PTY LTD", "PTY.", "PTY",
    "LP", "L.P.", "LLP", "L.L.P.",
    "HOLDINGS", "HOLDING", "GROUP", "GRP",
    # German
    "GMBH & CO. KG", "GMBH & CO KG", "GMBH",
    "MBH", "AG & CO. KG", "AG & CO KG", "AG",
    "KG", "KGAA",
    # French / Italian / Spanish
    "S.A.S.", "SAS", "S.A.R.L.", "SARL", "S.A.", "SA",
    "S.R.L.", "SRL", "S.P.A.", "SPA",
    # Dutch / Belgian
    "N.V.", "NV", "B.V.", "BV",
    # Nordic
    "OYJ", "OY", "AKTIEBOLAG", "AB", "A/S", "APS", "AS",
    # Japanese
    "KABUSHIKI KAISHA", "KABUSHIKIKAISHA", "K.K.", "KK", "YK",
    "株式会社", "有限会社", "合同会社",
    # Korean
    "주식회사", "(주)",
    # Chinese (simplified + traditional)
    "有限公司", "股份有限公司", "集团公司", "有限責任公司",
    # Russian / Cyrillic
    "ООО", "ОАО", "ЗАО", "ПАО",
]

# Prefixed Korean/Chinese company type markers (occur at start of string)
_LEGAL_PREFIXES = [
    "주식회사 ",
    "(주) ",
    "(株) ",
]

# Precompile stripping regexes. Legal suffixes must end the string (possibly
# with trailing punctuation / commas).
_SUFFIX_PATTERN = re.compile(
    r"\s*[,.]*\s*(?:" + "|".join(re.escape(s) for s in _LEGAL_SUFFIXES) + r")\s*[,.]*\s*$",
    re.IGNORECASE,
)
_PREFIX_PATTERN = re.compile(
    r"^(?:" + "|".join(re.escape(p) for p in _LEGAL_PREFIXES) + r")\s*",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_legal(name: str) -> str:
    """Strip all legal suffixes (iteratively) and known prefixes."""
    prev = None
    current = name
    # Iterate until stripping reaches a fixpoint — handles "Co., Ltd., Inc."
    while prev != current:
        prev = current
        current = _SUFFIX_PATTERN.sub("", current).strip()
    current = _PREFIX_PATTERN.sub("", current).strip()
    return current


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def _load_aliases() -> dict[str, str]:
    """Load alias table. Returns a flat dict mapping variant -> canonical."""
    path = _REFERENCES_DIR / "applicant_aliases.json"
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except json.JSONDecodeError:
        return {}
    flat: dict[str, str] = {}
    for entry in data.get("aliases", []):
        canonical = entry.get("canonical", "").strip().upper()
        if not canonical:
            continue
        flat[canonical] = canonical  # canonical maps to itself
        for variant in entry.get("variants", []):
            v = variant.strip().upper()
            if v:
                flat[v] = canonical
    return flat


def normalize_applicant(raw_name: str) -> str:
    """Normalize an applicant name for grouping.

    Steps:
        1. Trim, uppercase, collapse whitespace.
        2. Strip legal suffixes (iteratively) and prefixes.
        3. Look up in alias table; if found, return the canonical.
        4. Otherwise, return the rule-cleaned name as its own canonical.

    Passing an empty or None-like name returns "".
    """
    if not raw_name:
        return ""
    s = str(raw_name).strip()
    if not s:
        return ""
    # Uppercase first so suffix matching is case-insensitive-friendly
    s = s.upper()
    s = _WHITESPACE_RE.sub(" ", s)
    s = _strip_legal(s)
    s = _WHITESPACE_RE.sub(" ", s).strip(",. ")
    if not s:
        # Everything got stripped (rare edge case). Fall back to the
        # original uppercased form.
        return _WHITESPACE_RE.sub(" ", str(raw_name).strip().upper())
    aliases = _load_aliases()
    return aliases.get(s, s)


def normalize_many(raw_names: list[str]) -> list[str]:
    """Normalize a list of names, preserving order and deduplicating within the list."""
    seen: set[str] = set()
    out: list[str] = []
    for name in raw_names:
        n = normalize_applicant(name)
        if n and n not in seen:
            seen.add(n)
            out.append(n)
    return out


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------


def build_merge_audit(raw_to_normalized: list[tuple[str, str]]) -> dict[str, list[str]]:
    """Group raw names by the canonical they resolved to.

    Args:
        raw_to_normalized: pairs of (original_name, canonical_name) from every
            record in the landscape.

    Returns:
        dict mapping canonical_name -> sorted list of unique original variants
        that collapsed into it. Entries with only one variant are included
        too — the report filters them out for the merge audit display so only
        multi-variant merges are surfaced.
    """
    groups: dict[str, set[str]] = {}
    for raw, canonical in raw_to_normalized:
        if not canonical:
            continue
        groups.setdefault(canonical, set()).add(raw)
    return {k: sorted(v) for k, v in groups.items()}


def merges_only(audit: dict[str, list[str]]) -> dict[str, list[str]]:
    """Filter audit to entries where 2+ raw names collapsed into one canonical."""
    return {k: v for k, v in audit.items() if len(v) > 1}
