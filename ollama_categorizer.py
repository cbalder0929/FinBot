"""LLM fallback for the long-tail of unknown merchants.

Runs **only** on rows that the deterministic categorizer in
``categorize.py`` couldn't classify (``category == "Uncategorized"``).
The vast majority of transactions never see an LLM — typical batches
trigger zero or one call.

Pipeline per row:
  1. Normalize ``item`` to a cache key (strip Apple Pay suffixes,
     transaction IDs, store numbers).
  2. Look up the key in ``.ollama_cache/categories.json`` — if present,
     reuse and skip the network call.
  3. Otherwise send to Ollama with the canonical ``CATEGORIES`` tuple
     as constrained vocabulary and ``format: "json"``.
  4. Validate the response (category must be in ``CATEGORIES``, type
     must be ``expense | income | transfer``). Invalid → leave row as
     ``Uncategorized`` and move on.
  5. Cache the result so future batches don't pay the LLM cost.

If Ollama is unreachable, this function logs a warning and returns the
DataFrame unchanged. **It never raises.** The pipeline keeps working
with rules-only categorization when the LLM is unavailable.

Public API:
  - ``categorize_uncategorized(df, model=..., host=...) -> df``
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path

import pandas as pd

from categorize import CATEGORIES
from ollama_client import DEFAULT_HOST, DEFAULT_MODEL, OllamaError, chat, is_reachable


CACHE_DIR = Path(__file__).resolve().parent / ".ollama_cache"
CACHE_FILE = CACHE_DIR / "categories.json"

_VALID_TYPES = {"expense", "income", "transfer"}

# We omit "Uncategorized" from the prompt's allowed set so the model is
# pushed to actually pick a real category. If its choice is bad, we
# fall back to Uncategorized on our side.
_LLM_CATEGORIES = tuple(c for c in CATEGORIES if c != "Uncategorized")

_SYSTEM_PROMPT = (
    "You categorize personal banking transactions. Reply with strict JSON, no prose.\n"
    "\n"
    "Given a transaction description, return:\n"
    '  {"category": "<one of the canonical categories>",'
    ' "type": "<expense|income|transfer>",'
    ' "confidence": <number between 0.0 and 1.0>}\n'
    "\n"
    "Allowed categories (use exactly one of these strings):\n"
    + "\n".join(f"  - {c}" for c in _LLM_CATEGORIES) + "\n"
    "\n"
    "Rules:\n"
    "  - 'type' is 'transfer' for inter-account moves: Zelle, Venmo, Cash App,"
    " PayPal, wires, card payments, ACH transfers.\n"
    "  - 'type' is 'income' for inflows: payroll, refunds, interest, dividends.\n"
    "  - All other rows are 'expense'.\n"
    "  - If you genuinely cannot classify the merchant, set category to"
    " 'Uncategorized'.\n"
    "  - Output JSON only. Do not include explanations or markdown."
)


# ---------------------------------------------------------------------------
# Cache key normalization
# ---------------------------------------------------------------------------
def _normalize_for_cache(item: str) -> str:
    """Strip transaction-specific variation so different transactions at
    the same merchant share a cache entry.

    Removes (in order):
      - "APPLE PAY ENDING IN 5949" trailing suffix
      - Leading "AplPay " marker
      - Phone-number-shaped digit runs (NNN-NNN-NNNN, etc.)
      - "#12345" store/reference numbers (replaced with a space so the
        cleaned token doesn't smash into the next word — e.g.
        "BP#1636CHICAGO" → "BP CHICAGO", not "BPCHICAGO")
      - Any remaining standalone 3+ digit run
      - Whitespace runs collapsed to single spaces
    """
    s = (item or "").upper()
    s = re.sub(r"\s*APPLE\s*PAY\s*ENDING\s*IN\s*\d+", "", s)
    s = re.sub(r"^APLPAY\s+", "", s)
    # Phone-number-ish: 3+ digit runs joined by dashes/spaces.
    s = re.sub(r"\b\d{3,}[\s-]\d{3,}([\s-]\d{2,})?\b", " ", s)
    s = re.sub(r"#\s*\d+", " ", s)
    s = re.sub(r"\b\d{3,}\b", " ", s)
    s = re.sub(r"[\s-]+\s*$", "", s)             # trailing dashes/spaces
    s = re.sub(r"\s+", " ", s).strip()
    return s


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------
def _load_cache() -> dict[str, dict]:
    if not CACHE_FILE.exists():
        return {}
    try:
        return json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception:
        # Corrupted cache shouldn't kill the pipeline — start fresh.
        return {}


def _save_cache(cache: dict[str, dict]) -> None:
    CACHE_DIR.mkdir(exist_ok=True)
    CACHE_FILE.write_text(
        json.dumps(cache, indent=2, sort_keys=True),
        encoding="utf-8",
    )


# ---------------------------------------------------------------------------
# Single-row classification
# ---------------------------------------------------------------------------
def _classify_one(
    item: str,
    is_debit: bool,
    *,
    model: str,
    host: str,
) -> tuple[str, str, float] | None:
    """One LLM round-trip. Returns ``(category, type, confidence)`` or
    ``None`` if anything went wrong (we already logged it)."""
    user_msg = (
        f"Transaction description: {item}\n"
        f"Direction: {'debit (money out)' if is_debit else 'credit (money in)'}\n"
    )
    try:
        raw = chat(
            [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": user_msg},
            ],
            model=model,
            host=host,
            json_mode=True,
            temperature=0.0,
        )
    except OllamaError as e:
        print(f"[ollama] error classifying {item!r}: {e}")
        return None

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        print(f"[ollama] non-JSON response for {item!r}: {raw!r}")
        return None

    cat = str(parsed.get("category", "")).strip()
    typ = str(parsed.get("type", "")).strip().lower()
    raw_conf = parsed.get("confidence", 0.0)
    try:
        conf = float(raw_conf)
    except (TypeError, ValueError):
        conf = 0.0

    if cat not in CATEGORIES:
        print(f"[ollama] invalid category {cat!r} for {item!r}; treating as Uncategorized")
        return None
    if typ not in _VALID_TYPES:
        print(f"[ollama] invalid type {typ!r} for {item!r}; ignoring row")
        return None

    return (cat, typ, conf)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def categorize_uncategorized(
    df: pd.DataFrame,
    *,
    model: str = DEFAULT_MODEL,
    host: str = DEFAULT_HOST,
    max_calls: int = 50,
) -> pd.DataFrame:
    """Fill in ``Uncategorized`` rows by asking Ollama, with a disk cache.

    Returns the same DataFrame (mutated in-place for the affected rows).
    Never raises — if Ollama is down, you get the rules-only result back.

    ``max_calls`` is a circuit breaker so a runaway batch with thousands
    of unknown merchants doesn't pin the model for minutes. The remaining
    rows stay ``Uncategorized`` and can be retried on a later request
    (the cache survives across runs).
    """
    if df.empty or "category" not in df.columns:
        return df

    mask = df["category"].astype(str) == "Uncategorized"
    if not mask.any():
        return df

    if not is_reachable(host):
        print(f"[ollama] not reachable at {host}; skipping LLM categorization "
              f"({int(mask.sum())} rows stay Uncategorized)")
        return df

    cache = _load_cache()
    cache_hits = 0
    calls = 0
    promoted = 0

    for idx in df.index[mask]:
        if calls >= max_calls:
            print(f"[ollama] hit max_calls={max_calls}; stopping early")
            break

        item = str(df.at[idx, "item"] or "")
        debits = float(df.at[idx, "debits"] or 0)
        key = _normalize_for_cache(item)
        if not key:
            continue

        cached = cache.get(key)
        if cached is not None:
            cache_hits += 1
            if cached.get("category") in CATEGORIES and cached["category"] != "Uncategorized":
                df.at[idx, "category"] = cached["category"]
                df.at[idx, "type"] = cached["type"]
                promoted += 1
            continue

        result = _classify_one(item, debits > 0, model=model, host=host)
        calls += 1
        if result is None:
            continue
        cat, typ, conf = result

        cache[key] = {
            "category": cat,
            "type": typ,
            "confidence": conf,
            "model": model,
            "sample_item": item,
            "ts": int(time.time()),
        }
        if cat != "Uncategorized":
            df.at[idx, "category"] = cat
            df.at[idx, "type"] = typ
            promoted += 1

    if calls or cache_hits:
        _save_cache(cache)
        print(f"[ollama] {calls} LLM calls, {cache_hits} cache hits, "
              f"{promoted} rows promoted out of Uncategorized")

    return df
