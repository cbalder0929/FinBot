"""American Express credit card PDF parser.

Similar shape to the BoA credit parser but with Amex-specific noise
filters (e.g. FX conversion sub-lines that also start with a date).
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd
import pdfplumber

from .base import (
    CANONICAL_COLUMNS,
    clean_amount,
    clean_description,
    empty_canonical,
    extract_year,
    normalize_date,
    to_canonical,
)

_DATE_RE = re.compile(r"^\d{2}/\d{2}(?:/\d{2})?\b")
_DATE_TOKEN_RE = re.compile(r"^\d{2}/\d{2}(?:/\d{2})?$")
_NOISE = ("TOTAL", "ACCOUNT SUMMARY", "PAGE", "BALANCE", "STATEMENT", "CONTINUED", "MINIMUM")
_PAYMENT_KEYWORDS = ("PAYMENT", "THANK YOU", "AUTOPAY", "ONLINE PAYMENT")
# Amex sometimes prints FX conversion lines that also start with a date.
_FX_KEYWORDS = ("EXCHANGE RATE", "FOREIGN TRANSACTION", "CONVERSION")


def _is_date_token(t: str) -> bool:
    return bool(_DATE_TOKEN_RE.match(t))


def parse(path: Path) -> pd.DataFrame:
    rows: list[dict] = []
    statement_year: int | None = None

    with pdfplumber.open(str(path)) as pdf:
        first_text = pdf.pages[0].extract_text() or "" if pdf.pages else ""
        statement_year = extract_year(first_text)

        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            for line in text.split("\n"):
                if not _DATE_RE.match(line):
                    continue
                line_upper = line.upper()
                if any(kw in line_upper for kw in _NOISE):
                    continue
                if any(kw in line_upper for kw in _FX_KEYWORDS):
                    continue

                parts = line.split()
                if len(parts) < 3:
                    continue

                date_raw = parts[0]
                desc_start = 2 if (len(parts) > 2 and _is_date_token(parts[1])) else 1

                if len(parts) < desc_start + 2:
                    continue

                try:
                    raw_amount = clean_amount(parts[-1])
                except ValueError:
                    continue

                date = normalize_date(date_raw, statement_year)
                item = clean_description(" ".join(parts[desc_start:-1]))
                item_upper = item.upper()

                is_payment = any(kw in item_upper for kw in _PAYMENT_KEYWORDS)
                if is_payment or raw_amount < 0:
                    debits, credits = 0.0, abs(raw_amount)
                else:
                    debits, credits = raw_amount, 0.0

                rows.append({
                    "date": date,
                    "debits": debits,
                    "credits": credits,
                    "category": None,
                    "item": item,
                    "type": None,
                    "source": None,
                    "account": None,
                })

    if not rows:
        return empty_canonical()
    df = pd.DataFrame(rows, columns=CANONICAL_COLUMNS)
    return to_canonical(df, "amex", "American Express")
