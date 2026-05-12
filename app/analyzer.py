from __future__ import annotations

import csv
import io
import json
import re
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
import zlib
from pathlib import Path
from typing import Any, Callable


HEADER_ALIASES = {
    "date": {"date", "posted date", "transaction date"},
    "description": {"description", "details", "memo", "transaction", "merchant", "payee"},
    "amount": {"amount", "transaction amount"},
    "debit": {"debit", "withdrawal", "money out"},
    "credit": {"credit", "deposit", "money in"},
}

CATEGORY_RULES = {
    "income": ("payroll", "salary", "deposit", "refund", "stripe", "direct dep"),
    "groceries": ("grocery", "market", "whole foods", "aldi", "kroger", "trader joe"),
    "dining": ("restaurant", "cafe", "coffee", "doordash", "uber eats", "grill"),
    "transport": ("uber", "lyft", "shell", "exxon", "chevron", "gas"),
    "utilities": ("utility", "electric", "water", "internet", "comcast", "verizon"),
    "housing": ("rent", "mortgage", "property"),
    "shopping": ("amazon", "target", "walmart", "store"),
}


def analyze_statement(
    filename: str,
    content: bytes,
    categorizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    ext = Path(filename or "").suffix.lower()
    transactions = parse_statement(content, ext)
    if not transactions:
        raise ValueError("No transactions were found in the uploaded statement.")

    categorize = categorizer or categorize_transaction
    enriched = [categorize(transaction) for transaction in transactions]
    total_in = round(sum(item["amount"] for item in enriched if item["amount"] > 0), 2)
    total_out = round(sum(abs(item["amount"]) for item in enriched if item["amount"] < 0), 2)

    return {
        "fileName": filename,
        "transactionCount": len(enriched),
        "totals": {
            "credits": total_in,
            "debits": total_out,
            "net": round(total_in - total_out, 2),
        },
        "transactions": enriched,
    }


def parse_statement(content: bytes, extension: str) -> list[dict[str, Any]]:
    if extension in {".csv", ".txt"}:
        return parse_csv_bytes(content)
    if extension in {".xlsx"}:
        return parse_xlsx_bytes(content)
    if extension == ".pdf":
        return parse_pdf_bytes(content)
    raise ValueError("Unsupported file type. Upload a PDF, CSV, or XLSX spreadsheet.")


def parse_csv_bytes(content: bytes) -> list[dict[str, Any]]:
    text = _decode_text(content)
    lines = [line for line in text.splitlines() if line.strip()]
    if not lines:
        return []

    reader = csv.DictReader(io.StringIO("\n".join(lines)))
    if not reader.fieldnames:
        return []
    return _rows_to_transactions(reader)


def parse_xlsx_bytes(content: bytes) -> list[dict[str, Any]]:
    with zipfile.ZipFile(io.BytesIO(content)) as workbook:
        shared_strings = _load_shared_strings(workbook)
        sheet_name = next(
            (name for name in sorted(workbook.namelist()) if name.startswith("xl/worksheets/sheet")),
            None,
        )
        if not sheet_name:
            return []

        namespace = {"ss": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
        root = ET.fromstring(workbook.read(sheet_name))
        rows: list[list[str]] = []
        for row in root.findall(".//ss:sheetData/ss:row", namespace):
            values = []
            for cell in row.findall("ss:c", namespace):
                values.append(_extract_cell_value(cell, shared_strings, namespace))
            rows.append(values)

    if len(rows) < 2:
        return []

    headers = [value.strip() for value in rows[0]]
    records = [dict(zip(headers, row)) for row in rows[1:] if any(value.strip() for value in row)]
    return _rows_to_transactions(records)


def parse_pdf_bytes(content: bytes) -> list[dict[str, Any]]:
    text = extract_pdf_text(content)
    transactions: list[dict[str, Any]] = []
    pattern = re.compile(
        r"^(?P<date>\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?)?\s*(?P<description>.+?)\s+(?P<amount>\(?-?\$?[\d,]+\.\d{2}\)?)$"
    )
    for row_number, line in enumerate((part.strip() for part in text.splitlines()), start=1):
        match = pattern.match(line)
        if not match:
            continue
        description = match.group("description").strip()
        amount = _to_amount(match.group("amount"))
        if not description:
            continue
        transactions.append(
            {
                "date": (match.group("date") or "").strip(),
                "description": description,
                "amount": amount,
                "direction": "credit" if amount >= 0 else "debit",
                "sourceRow": row_number,
            }
        )
    return transactions


def categorize_transaction(transaction: dict[str, Any]) -> dict[str, Any]:
    ollama_category = _categorize_with_ollama(transaction)
    merged = {**transaction, **ollama_category} if ollama_category else transaction.copy()
    if "category" not in merged:
        merged["category"] = _guess_category(transaction["description"], transaction["amount"])
    merged["direction"] = "credit" if merged["amount"] >= 0 else "debit"
    merged.setdefault("confidence", 0.55 if not ollama_category else 0.9)
    return merged


def extract_pdf_text(content: bytes) -> str:
    text_fragments: list[str] = []
    for raw_stream in re.findall(rb"stream\r?\n(.*?)\r?\nendstream", content, re.DOTALL):
        decoded = _maybe_inflate_stream(raw_stream)
        text_fragments.extend(_extract_parenthesized_text(decoded))
    return "\n".join(fragment for fragment in text_fragments if fragment.strip())


def _decode_text(content: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "latin-1"):
        try:
            return content.decode(encoding)
        except UnicodeDecodeError:
            continue
    return content.decode("utf-8", errors="ignore")


def _rows_to_transactions(rows: Any) -> list[dict[str, Any]]:
    transactions: list[dict[str, Any]] = []
    for row_number, raw_row in enumerate(rows, start=2):
        normalized = {str(key).strip().lower(): str(value).strip() for key, value in raw_row.items() if key}
        date = _find_value(normalized, "date")
        description = _find_value(normalized, "description")
        amount_text = _find_value(normalized, "amount")
        if amount_text:
            amount = _to_amount(amount_text)
        else:
            credit = _find_value(normalized, "credit")
            debit = _find_value(normalized, "debit")
            amount = _to_amount(credit) if credit else -abs(_to_amount(debit))
        if not description:
            continue
        transactions.append(
            {
                "date": date,
                "description": description,
                "amount": amount,
                "direction": "credit" if amount >= 0 else "debit",
                "sourceRow": row_number,
            }
        )
    return transactions


def _find_value(row: dict[str, str], target: str) -> str:
    for key, value in row.items():
        if key in HEADER_ALIASES[target]:
            return value
    return ""


def _to_amount(value: str) -> float:
    cleaned = value.strip().replace("$", "").replace(",", "")
    if not cleaned:
        return 0.0
    negative = cleaned.startswith("(") and cleaned.endswith(")")
    cleaned = cleaned.strip("()")
    amount = float(cleaned)
    return -abs(amount) if negative else amount


def _guess_category(description: str, amount: float) -> str:
    lowered = description.lower()
    for category, keywords in CATEGORY_RULES.items():
        if any(keyword in lowered for keyword in keywords):
            return category
    return "income" if amount > 0 else "general"


def _categorize_with_ollama(transaction: dict[str, Any]) -> dict[str, Any] | None:
    payload = {
        "model": "llama3.2",
        "stream": False,
        "prompt": (
            "Categorize this financial transaction and respond with compact JSON only "
            "using keys category and confidence. Transaction: "
            + json.dumps(
                {
                    "date": transaction.get("date", ""),
                    "description": transaction["description"],
                    "amount": transaction["amount"],
                }
            )
        ),
    }
    request = urllib.request.Request(
        "http://127.0.0.1:11434/api/generate",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=1.2) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        return None

    response_text = body.get("response", "").strip()
    if not response_text:
        return None
    try:
        parsed = json.loads(response_text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", response_text, re.DOTALL)
        if not match:
            return None
        parsed = json.loads(match.group(0))
    category = str(parsed.get("category", "")).strip().lower()
    confidence = float(parsed.get("confidence", 0.9))
    return {"category": category or None, "confidence": max(0.0, min(confidence, 1.0))}


def _maybe_inflate_stream(raw_stream: bytes) -> str:
    for candidate in (raw_stream, raw_stream.strip(b"\r\n")):
        try:
            return zlib.decompress(candidate).decode("latin-1", errors="ignore")
        except zlib.error:
            continue
    return raw_stream.decode("latin-1", errors="ignore")


def _extract_parenthesized_text(stream_text: str) -> list[str]:
    matches: list[str] = []
    for fragment in re.findall(r"\((.*?)(?<!\\)\)", stream_text):
        cleaned = (
            fragment.replace(r"\(", "(")
            .replace(r"\)", ")")
            .replace(r"\n", " ")
            .replace(r"\r", " ")
            .replace(r"\\", "\\")
        ).strip()
        if cleaned:
            matches.append(cleaned)
    return matches


def _load_shared_strings(workbook: zipfile.ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in workbook.namelist():
        return []
    namespace = {"ss": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    root = ET.fromstring(workbook.read("xl/sharedStrings.xml"))
    return ["".join(node.itertext()) for node in root.findall(".//ss:si", namespace)]


def _extract_cell_value(cell: ET.Element, shared_strings: list[str], namespace: dict[str, str]) -> str:
    cell_type = cell.attrib.get("t")
    if cell_type == "inlineStr":
        value = cell.findtext("ss:is/ss:t", default="", namespaces=namespace)
        return value
    value = cell.findtext("ss:v", default="", namespaces=namespace)
    if cell_type == "s" and value:
        return shared_strings[int(value)]
    return value
