"""Statement parsers.

Public API:
    parse(path, source_hint=None) -> canonical DataFrame
    detect_source(path) -> source slug | None

Source slugs: ``discover``, ``amex``, ``boa_checking``, ``boa_credit``.

Each parser returns a DataFrame with the canonical schema defined in
``parsers.base.CANONICAL_COLUMNS`` so the rest of the pipeline
(categorization, reporting) doesn't need to know which bank a row came
from.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import amex, boa_checking, boa_credit, csv_passthrough, discover
from .base import empty_canonical

SourceSlug = str

# (primary tokens, secondary tokens, slug) — primary must match;
# secondary refines when one institution prints multiple product types.
_PDF_HEURISTICS: list[tuple[list[str], list[str], SourceSlug]] = [
    (["DISCOVER CARD", "DISCOVER.COM"], [], "discover"),
    (["AMERICAN EXPRESS", "AMERICANEXPRESS.COM", "MEMBERSHIP REWARDS"], [], "amex"),
    (["BANK OF AMERICA"], ["CHECKING", "SAVINGS"], "boa_checking"),
    (["BANK OF AMERICA"], ["CREDIT CARD", "CASH REWARDS", "VISA SIGNATURE"], "boa_credit"),
]

_CSV_HEURISTICS: list[tuple[list[str], SourceSlug]] = [
    (["Trans. Date", "Post Date", "Category"], "discover"),
    (["Extended Details", "Appears On Your Statement As"], "amex"),
    (["Running Bal."], "boa_checking"),
    (["Posted Date", "Reference Number", "Payee", "Address"], "boa_credit"),
]


def detect_source_pdf(text: str) -> SourceSlug | None:
    upper = text.upper()
    for primary_any, secondary_any, slug in _PDF_HEURISTICS:
        if any(r in upper for r in primary_any):
            if not secondary_any or any(s in upper for s in secondary_any):
                return slug
    return None


def detect_source_csv(columns: list[str]) -> SourceSlug | None:
    col_set = set(columns)
    for markers, slug in _CSV_HEURISTICS:
        if any(m in col_set for m in markers):
            return slug
    return None


def detect_source(path: Path) -> SourceSlug | None:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(str(path)) as pdf:
                text = ""
                for page in pdf.pages[:2]:
                    t = page.extract_text()
                    if t:
                        text += t
                return detect_source_pdf(text)
        except Exception:
            return None
    elif suffix == ".csv":
        try:
            df = pd.read_csv(path, nrows=0)
            return detect_source_csv(list(df.columns))
        except Exception:
            return None
    return None


def parse(path: Path, source_hint: SourceSlug | None = None) -> pd.DataFrame:
    path = Path(path)
    suffix = path.suffix.lower()
    source = source_hint or detect_source(path)

    if suffix == ".csv":
        if source:
            return csv_passthrough.parse(path, source)
        # Unknown CSV source — caller falls back to a raw read.
        return empty_canonical()

    # PDF dispatch
    if source == "discover":
        return discover.parse(path)
    if source == "amex":
        return amex.parse(path)
    if source == "boa_checking":
        return boa_checking.parse(path)
    if source == "boa_credit":
        return boa_credit.parse(path)

    # Unknown PDF — the BoA-checking parser is the most permissive
    # (any line starting with MM/DD/YY) so it's the safe fallback.
    return boa_checking.parse(path)
