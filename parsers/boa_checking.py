"""Bank of America checking/savings PDF parser.

Statement layout: section headers like ``Deposits and other additions`` /
``Withdrawals and other subtractions`` separate credits from debits; each
transaction line begins with ``MM/DD/YY``.
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

_DATE_RE = re.compile(r"^\d{2}/\d{2}/\d{2}\b")
_NOISE = ("TOTAL", "ACCOUNT SUMMARY", "PAGE", "BALANCE", "STATEMENT", "CONTINUED", "BEGINNING", "ENDING")

_CREDIT_HEADERS = (
    "DEPOSITS AND OTHER ADDITIONS",
    "OTHER CREDITS",
    "INTEREST PAID",
)
_DEBIT_HEADERS = (
    "WITHDRAWALS AND OTHER SUBTRACTIONS",
    "SERVICE FEES",
    "OTHER DEBITS",
    "CHECKS",
)


def parse(path: Path) -> pd.DataFrame:
    rows: list[dict] = []
    current_is_credit: bool | None = None
    statement_year: int | None = None

    with pdfplumber.open(str(path)) as pdf:
        first_text = pdf.pages[0].extract_text() or "" if pdf.pages else ""
        statement_year = extract_year(first_text)

        for page in pdf.pages:
            text = page.extract_text()
            if not text:
                continue
            for line in text.split("\n"):
                upper = line.upper().strip()

                # Track which section we're in so a bare positive amount can be
                # routed to debits vs credits even when the PDF text doesn't
                # carry a sign.
                for hdr in _CREDIT_HEADERS:
                    if hdr in upper:
                        current_is_credit = True
                        break
                else:
                    for hdr in _DEBIT_HEADERS:
                        if hdr in upper:
                            current_is_credit = False
                            break

                if not _DATE_RE.match(line):
                    continue
                if any(kw in line.upper() for kw in _NOISE):
                    continue

                parts = line.split()
                if len(parts) < 3:
                    continue

                try:
                    raw_amount = clean_amount(parts[-1])
                except ValueError:
                    continue

                amount = abs(raw_amount)
                date = normalize_date(parts[0], statement_year)
                item = clean_description(" ".join(parts[1:-1]))

                if current_is_credit is True:
                    debits, credits = 0.0, amount
                elif current_is_credit is False:
                    debits, credits = amount, 0.0
                else:
                    # No section context yet — fall back to amount sign.
                    if raw_amount >= 0:
                        debits, credits = 0.0, raw_amount
                    else:
                        debits, credits = abs(raw_amount), 0.0

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
    return to_canonical(df, "boa_checking", "BoA Checking")
