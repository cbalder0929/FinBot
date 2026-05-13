"""Free-text Q&A over the transaction data.

Two-shot pipeline:
  1. **parse_question** — Ollama converts a natural-language question into
     a strict JSON filter spec (categories, date window, aggregate). The
     LLM only extracts intent here; it never sees the rows.
  2. **execute** — pandas applies the spec to the DataFrame and computes
     a single aggregate (a scalar or a short list). All arithmetic is
     pandas.
  3. **narrate_answer** — Ollama writes a 1-2 sentence answer using
     only the numbers in the result dict.

If Ollama is unreachable, the answer dict carries ``error`` and an empty
narration. Callers should serve the structured result alongside the
narration so the UI can still show data even when prose generation fails.

Public API:
  - ``parse_question(text) -> spec``  (raises OllamaError)
  - ``execute(df, spec) -> result``    (pure pandas, never raises)
  - ``narrate_answer(question, spec, result) -> str | None``
  - ``answer(question, df) -> {question, spec, result, narration, error?}``
"""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Optional

import pandas as pd

from categorize import CATEGORIES
from ollama_client import DEFAULT_HOST, DEFAULT_MODEL, OllamaError, chat, is_reachable


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
AGGREGATES = (
    "sum_debits",      # total spending matching the filter
    "sum_credits",     # total income matching the filter
    "net",             # credits - debits matching the filter
    "count",           # number of matching transactions
    "avg_per_month",   # average per-month spending
    "top_merchants",   # top-N items by debit total
    "top_categories",  # top-N categories by debit total
    "list",            # the matching transactions themselves
)

_VALID_TYPES = {"expense", "income", "transfer"}


def _empty_spec() -> dict:
    return {
        "categories": [],
        "exclude_categories": [],
        "types": [],
        "item_contains": [],
        "from_date": None,
        "to_date": None,
        "aggregate": "sum_debits",
        "limit": 10,
    }


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Step 1 — parse the question into a filter spec
# ---------------------------------------------------------------------------
def _parse_system_prompt() -> str:
    today = _today()
    return (
        "You convert user questions about personal finances into a strict JSON filter spec.\n"
        f"Today's date is {today}.\n\n"
        "Output JSON only, no prose, with this exact shape (use empty lists / null when unset):\n"
        '{\n'
        '  "categories": [canonical categories to INCLUDE],\n'
        '  "exclude_categories": [canonical categories to EXCLUDE],\n'
        '  "types": [list of "expense" | "income" | "transfer"],\n'
        '  "item_contains": [substrings to match in transaction descriptions, case-insensitive],\n'
        '  "from_date": "YYYY-MM-DD" or null,\n'
        '  "to_date": "YYYY-MM-DD" or null,\n'
        f'  "aggregate": one of {list(AGGREGATES)},\n'
        '  "limit": integer (for top_* and list aggregates, default 10)\n'
        '}\n\n'
        f"Canonical categories (use exactly these strings, no others): {list(CATEGORIES)}\n\n"
        "Rules:\n"
        "  - Default `types` to [\"expense\"] for spending questions, [\"income\"] for income questions, leave empty for net/list questions.\n"
        "  - 'Last month' = the calendar month before the current one.\n"
        "  - 'This month' = the current calendar month.\n"
        "  - 'This year' = Jan 1 of the current year through today.\n"
        "  - For merchant-specific questions ('how much at Starbucks'), use `item_contains`, not `categories`.\n"
        "  - For category questions ('how much on dining'), use `categories`.\n"
        "  - Default aggregate is 'sum_debits' for spending questions.\n"
        "  - If the question can't be answered from transaction data, return aggregate 'list' with empty filters.\n\n"
        "Examples:\n"
        f'  Q: "How much did I spend on coffee last month?"\n'
        f'  A: {{"categories":[],"exclude_categories":[],"types":["expense"],"item_contains":["coffee","starbucks","dunkin"],"from_date":"2026-04-01","to_date":"2026-04-30","aggregate":"sum_debits","limit":10}}\n\n'
        '  Q: "Top 5 merchants this year"\n'
        f'  A: {{"categories":[],"exclude_categories":[],"types":["expense"],"item_contains":[],"from_date":"2026-01-01","to_date":"{today}","aggregate":"top_merchants","limit":5}}\n\n'
        '  Q: "What\'s my net income for 2025?"\n'
        '  A: {"categories":[],"exclude_categories":[],"types":[],"item_contains":[],"from_date":"2025-01-01","to_date":"2025-12-31","aggregate":"net","limit":10}\n'
    )


def parse_question(
    text: str,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
) -> dict:
    """LLM call #1 — turn natural language into a filter spec.

    Always returns a complete spec (defaults filled). Raises
    ``OllamaError`` only on connection/transport failure; malformed JSON
    from the model returns the empty default spec so the pipeline can
    continue and the user gets *something* back.
    """
    raw = chat(
        [
            {"role": "system", "content": _parse_system_prompt()},
            {"role": "user", "content": text},
        ],
        model=model,
        host=host,
        json_mode=True,
        temperature=0.0,
        timeout=600.0,
    )

    spec = _empty_spec()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return spec

    for key in spec.keys():
        if key in parsed:
            spec[key] = parsed[key]

    # ---- Defensive normalization. The LLM may invent shapes; only
    # accept values our executor can safely consume.
    if spec["aggregate"] not in AGGREGATES:
        spec["aggregate"] = "sum_debits"
    if not isinstance(spec["limit"], int):
        try:
            spec["limit"] = int(spec["limit"])
        except (TypeError, ValueError):
            spec["limit"] = 10
    spec["limit"] = max(1, min(spec["limit"], 100))

    spec["categories"] = [
        c for c in (spec["categories"] or []) if c in CATEGORIES
    ]
    spec["exclude_categories"] = [
        c for c in (spec["exclude_categories"] or []) if c in CATEGORIES
    ]
    spec["types"] = [
        t for t in (spec["types"] or []) if t in _VALID_TYPES
    ]
    spec["item_contains"] = [
        s for s in (spec["item_contains"] or []) if isinstance(s, str) and s.strip()
    ]
    for k in ("from_date", "to_date"):
        if spec[k] is not None and not isinstance(spec[k], str):
            spec[k] = None

    return spec


# ---------------------------------------------------------------------------
# Step 2 — apply the spec with pandas (this is the ONLY math step)
# ---------------------------------------------------------------------------
def execute(df: pd.DataFrame, spec: dict) -> dict:
    """Run the filter spec against ``df`` and return the aggregate result.

    Never raises — bad filters just produce zero rows.
    """
    if df.empty:
        return {"kind": "scalar", "value": 0, "count": 0, "filtered_count": 0, "currency": "USD"}

    m = df

    if spec["categories"]:
        m = m[m["category"].isin(spec["categories"])]
    if spec["exclude_categories"]:
        m = m[~m["category"].isin(spec["exclude_categories"])]
    if spec["types"]:
        m = m[m["type"].isin(spec["types"])]
    if spec["item_contains"]:
        pattern = "|".join(re.escape(s) for s in spec["item_contains"])
        m = m[m["item"].astype(str).str.contains(pattern, case=False, na=False, regex=True)]
    if spec["from_date"]:
        try:
            m = m[m["date"] >= pd.to_datetime(spec["from_date"])]
        except Exception:
            pass
    if spec["to_date"]:
        try:
            m = m[m["date"] <= pd.to_datetime(spec["to_date"])]
        except Exception:
            pass

    matched = int(len(m))
    agg = spec["aggregate"]
    limit = spec["limit"]

    if agg == "sum_debits":
        return {"kind": "scalar", "value": round(float(m["debits"].sum()), 2),
                "count": matched, "currency": "USD"}

    if agg == "sum_credits":
        return {"kind": "scalar", "value": round(float(m["credits"].sum()), 2),
                "count": matched, "currency": "USD"}

    if agg == "net":
        net = float(m["credits"].sum() - m["debits"].sum())
        return {"kind": "scalar", "value": round(net, 2),
                "count": matched, "currency": "USD"}

    if agg == "count":
        return {"kind": "scalar", "value": matched, "count": matched, "currency": None}

    if agg == "avg_per_month":
        cf = m.dropna(subset=["date"])
        if cf.empty:
            return {"kind": "scalar", "value": 0, "count": matched, "currency": "USD"}
        per_month = (
            cf.assign(month=cf["date"].dt.to_period("M").astype(str))
              .groupby("month")["debits"].sum()
        )
        return {"kind": "scalar", "value": round(float(per_month.mean()), 2),
                "count": matched, "months": int(per_month.shape[0]), "currency": "USD"}

    if agg == "top_merchants":
        expenses = m[m["debits"] > 0]
        if expenses.empty:
            return {"kind": "list", "items": [], "count": matched, "currency": "USD"}
        g = (expenses.groupby("item").agg(
                total=("debits", "sum"),
                count=("debits", "count"),
                category=("category", "first"),
             ).reset_index()
               .sort_values("total", ascending=False)
               .head(limit))
        return {"kind": "list",
                "items": [{"label": r["item"], "value": round(float(r["total"]), 2),
                           "count": int(r["count"]), "category": r["category"]}
                          for _, r in g.iterrows()],
                "count": matched, "currency": "USD"}

    if agg == "top_categories":
        expenses = m[m["debits"] > 0]
        if expenses.empty:
            return {"kind": "list", "items": [], "count": matched, "currency": "USD"}
        g = (expenses.groupby("category").agg(
                total=("debits", "sum"),
                count=("debits", "count"),
             ).reset_index()
               .sort_values("total", ascending=False)
               .head(limit))
        return {"kind": "list",
                "items": [{"label": r["category"], "value": round(float(r["total"]), 2),
                           "count": int(r["count"])} for _, r in g.iterrows()],
                "count": matched, "currency": "USD"}

    if agg == "list":
        head = m.head(limit)[["date", "debits", "credits", "category", "item"]]
        items = []
        for _, r in head.iterrows():
            d = r["date"]
            items.append({
                "date": str(pd.to_datetime(d).date()) if pd.notna(d) else None,
                "debits": round(float(r["debits"] or 0), 2),
                "credits": round(float(r["credits"] or 0), 2),
                "category": r["category"],
                "item": r["item"],
            })
        return {"kind": "list", "items": items, "count": matched, "currency": "USD"}

    return {"kind": "scalar", "value": 0, "count": matched, "currency": "USD"}


# ---------------------------------------------------------------------------
# Step 3 — narrate the result
# ---------------------------------------------------------------------------
_NARRATE_SYSTEM = (
    "You answer personal finance questions in plain English using only the "
    "numbers in the JSON payload you are given.\n\n"
    "Rules:\n"
    "  - Do not invent or compute numbers. Every figure in your answer must appear in the payload.\n"
    "  - 1-2 sentences. Concrete: include the dollar amount, the time period, "
    "and the merchant/category if relevant.\n"
    "  - Round dollar amounts to whole dollars in prose (e.g. '$312').\n"
    "  - If the result is zero rows, say so plainly — do not guess what the user meant.\n"
    "  - No moralizing, no financial advice — just answer the question.\n"
    "  - Output prose only, no JSON, no markdown.\n"
)


def narrate_answer(
    question: str,
    spec: dict,
    result: dict,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
) -> Optional[str]:
    """LLM call #2 — write the human-readable answer.

    Returns ``None`` on any failure; caller should fall back to showing
    the structured result without prose.
    """
    payload = {"question": question, "filter": spec, "result": result}
    try:
        text = chat(
            [
                {"role": "system", "content": _NARRATE_SYSTEM},
                {"role": "user", "content": json.dumps(payload, indent=2, default=str)},
            ],
            model=model,
            host=host,
            json_mode=False,
            temperature=0.4,
            timeout=600.0,
        )
    except OllamaError as e:
        print(f"[qa] narration failed: {e}")
        return None

    text = (text or "").strip()
    if not text or len(text) < 5:
        return None
    return text


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------
def answer(
    question: str,
    df: pd.DataFrame,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    skip_narration: bool = False,
) -> dict:
    """Full pipeline: parse → execute → narrate.

    Always returns a dict shaped like::
        {
          "question": str,
          "spec":     dict | None,
          "result":   dict | None,
          "narration": str | None,
          "error":    str | None,
        }

    Never raises. If the LLM is unreachable we still return ``spec=None``
    and ``error`` set so the caller can surface a clear message.
    """
    out = {"question": question, "spec": None, "result": None, "narration": None, "error": None}

    if df.empty:
        out["spec"] = _empty_spec()
        out["result"] = {"kind": "scalar", "value": 0, "count": 0, "currency": "USD"}
        out["narration"] = (
            "I don't have any data to answer that yet — process some "
            "statements on the Upload tab first."
        )
        return out

    if not is_reachable(host):
        out["error"] = "Ollama is not reachable. Make sure it's running on localhost:11434."
        return out

    try:
        spec = parse_question(question, model=model, host=host)
    except OllamaError as e:
        out["error"] = f"Couldn't parse the question: {e}"
        return out

    result = execute(df, spec)
    out["spec"] = spec
    out["result"] = result

    if skip_narration:
        return out

    out["narration"] = narrate_answer(question, spec, result, model=model, host=host)
    return out
