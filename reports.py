"""Report aggregations.

Pure pandas — no LLM involvement here. All math, all deterministic.
``ollama_narrator.py`` consumes the dicts produced here and only writes
prose; it never sees raw transaction rows and never does arithmetic.

Conventions:
  - Transfers (``type == 'transfer'``) are excluded from cashflow math.
    Card payments and Zelle moves would otherwise double-count.
  - Money values are rounded to 2 decimals at the JSON boundary.
  - Dates are coerced to pandas Timestamps once; downstream functions
    assume ``df['date']`` is already datetime-like.

Public functions all take an optional ``from_date``/``to_date`` window
(ISO strings) and return JSON-ready dicts.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import pandas as pd


# ---------------------------------------------------------------------------
# Load + prepare
# ---------------------------------------------------------------------------
def load_outputs(output_dir: Path) -> pd.DataFrame:
    """Read every canonical-schema CSV in ``output_dir`` into one frame.

    Files that fail to parse are skipped silently — one bad CSV must
    not break the whole report.
    """
    frames: list[pd.DataFrame] = []
    for p in sorted(output_dir.glob("*.csv")):
        try:
            frames.append(pd.read_csv(p))
        except Exception:
            continue
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, ignore_index=True, sort=False)
    return _prepare(combined)


def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    """Normalize column types, dedupe, drop transfers from cashflow.

    ``df`` is mutated only via .copy() — callers may keep the original.
    """
    if df.empty:
        return df

    out = df.copy()
    out["date"] = pd.to_datetime(out.get("date"), errors="coerce")
    out["debits"] = pd.to_numeric(out.get("debits", 0), errors="coerce").fillna(0)
    out["credits"] = pd.to_numeric(out.get("credits", 0), errors="coerce").fillna(0)
    for col in ("item", "category", "type", "source", "account"):
        if col not in out.columns:
            out[col] = ""
        out[col] = out[col].fillna("").astype(str)

    # Same dedupe key the legacy app used — same date/source/item/amounts
    # is almost always the same transaction parsed twice.
    out = out.assign(
        debits=out["debits"].round(2),
        credits=out["credits"].round(2),
    ).drop_duplicates(
        subset=["date", "source", "item", "debits", "credits"], keep="first"
    )
    return out


def _cashflow(df: pd.DataFrame) -> pd.DataFrame:
    """Slice to rows that count toward income/spending (drop transfers)."""
    if df.empty or "type" not in df.columns:
        return df
    return df[df["type"] != "transfer"].copy()


def _window(
    df: pd.DataFrame,
    from_date: Optional[str],
    to_date: Optional[str],
) -> pd.DataFrame:
    """Apply optional ``from_date``/``to_date`` window."""
    if df.empty:
        return df
    if from_date:
        try:
            df = df[df["date"] >= pd.to_datetime(from_date)]
        except Exception:
            pass
    if to_date:
        try:
            df = df[df["date"] <= pd.to_datetime(to_date)]
        except Exception:
            pass
    return df


# ---------------------------------------------------------------------------
# Topline summary
# ---------------------------------------------------------------------------
def topline(
    df: pd.DataFrame,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> dict:
    """Headline numbers: total income, spending, net, top spending category,
    transaction count, period covered, and per-source breakdown.
    """
    df = _window(df, from_date, to_date)
    cf = _cashflow(df)

    if cf.empty:
        return {
            "total_income": 0.0,
            "total_spending": 0.0,
            "net": 0.0,
            "top_category": None,
            "transaction_count": 0,
            "period": None,
            "by_source": [],
        }

    income = float(cf["credits"].sum())
    spending = float(cf["debits"].sum())

    top_cat = None
    expenses = cf[cf["debits"] > 0]
    if not expenses.empty:
        top_cat = expenses.groupby("category")["debits"].sum().idxmax()

    period = None
    dates = df["date"].dropna()
    if not dates.empty:
        period = {"from": str(dates.min().date()), "to": str(dates.max().date())}

    by_source = [
        {
            "source": src,
            "income": round(float(g["credits"].sum()), 2),
            "spending": round(float(g["debits"].sum()), 2),
        }
        for src, g in cf.groupby("source")
    ]

    return {
        "total_income": round(income, 2),
        "total_spending": round(spending, 2),
        "net": round(income - spending, 2),
        "top_category": top_cat,
        "transaction_count": int(len(cf)),
        "period": period,
        "by_source": by_source,
    }


# ---------------------------------------------------------------------------
# Category breakdown
# ---------------------------------------------------------------------------
def by_category(
    df: pd.DataFrame,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> dict:
    """Spending grouped by category, descending by spend."""
    df = _window(df, from_date, to_date)
    cf = _cashflow(df)
    if cf.empty:
        return {"categories": []}

    g = cf.groupby("category").agg(
        spending=("debits", "sum"),
        income=("credits", "sum"),
        count=("item", "count"),
    ).reset_index().sort_values("spending", ascending=False)

    return {
        "categories": [
            {
                "category": r["category"],
                "spending": round(float(r["spending"]), 2),
                "income": round(float(r["income"]), 2),
                "count": int(r["count"]),
            }
            for _, r in g.iterrows()
        ]
    }


# ---------------------------------------------------------------------------
# Top merchants
# ---------------------------------------------------------------------------
def top_merchants(
    df: pd.DataFrame,
    limit: int = 10,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> dict:
    """Top ``limit`` merchants by total spend."""
    df = _window(df, from_date, to_date)
    cf = _cashflow(df)
    if cf.empty:
        return {"merchants": []}

    expenses = cf[cf["debits"] > 0]
    if expenses.empty:
        return {"merchants": []}

    g = expenses.groupby("item").agg(
        total=("debits", "sum"),
        count=("debits", "count"),
        category=("category", "first"),
    ).reset_index().sort_values("total", ascending=False).head(max(1, limit))

    return {
        "merchants": [
            {
                "item": r["item"],
                "total": round(float(r["total"]), 2),
                "count": int(r["count"]),
                "category": r["category"],
            }
            for _, r in g.iterrows()
        ]
    }


# ---------------------------------------------------------------------------
# Monthly trend
# ---------------------------------------------------------------------------
def monthly_trend(
    df: pd.DataFrame,
    from_date: Optional[str] = None,
    to_date: Optional[str] = None,
) -> dict:
    """Per-month income / spending / net, sorted oldest first."""
    df = _window(df, from_date, to_date)
    cf = _cashflow(df).dropna(subset=["date"])
    if cf.empty:
        return {"months": []}

    cf = cf.assign(month=cf["date"].dt.to_period("M").astype(str))
    g = cf.groupby("month").agg(
        income=("credits", "sum"),
        spending=("debits", "sum"),
    ).reset_index().sort_values("month")
    g["net"] = g["income"] - g["spending"]

    return {
        "months": [
            {
                "month": r["month"],
                "income": round(float(r["income"]), 2),
                "spending": round(float(r["spending"]), 2),
                "net": round(float(r["net"]), 2),
            }
            for _, r in g.iterrows()
        ]
    }


# ---------------------------------------------------------------------------
# Monthly drilldown — one specific month
# ---------------------------------------------------------------------------
def monthly_summary(df: pd.DataFrame, month: Optional[str] = None) -> dict:
    """Detailed numbers for a single month.

    ``month`` is ``YYYY-MM``. If omitted, picks the most recent month
    with any cashflow activity.
    """
    cf_all = _cashflow(df).dropna(subset=["date"])
    if cf_all.empty:
        return {"month": None, "topline": topline(df), "by_category": [], "top_merchants": []}

    cf_all = cf_all.assign(month=cf_all["date"].dt.to_period("M").astype(str))
    if month is None:
        month = cf_all["month"].max()

    mdf = cf_all[cf_all["month"] == month]
    if mdf.empty:
        return {"month": month, "topline": topline(df.iloc[0:0]), "by_category": [], "top_merchants": []}

    # Reuse the windowed helpers by setting from/to to the month bounds.
    start = pd.Period(month).start_time.strftime("%Y-%m-%d")
    end = pd.Period(month).end_time.strftime("%Y-%m-%d")

    # Compare to 6-month trailing average (excluding the month itself).
    last_6 = cf_all[
        (cf_all["month"] < month)
        & (cf_all["date"] >= pd.Period(month).start_time - pd.DateOffset(months=6))
    ]
    avg_spend_6mo = 0.0
    if not last_6.empty:
        per_month = last_6.groupby("month")["debits"].sum()
        avg_spend_6mo = float(per_month.mean()) if not per_month.empty else 0.0

    this_spend = float(mdf["debits"].sum())
    delta_vs_avg = this_spend - avg_spend_6mo
    delta_pct = (delta_vs_avg / avg_spend_6mo * 100.0) if avg_spend_6mo else None

    return {
        "month": month,
        "topline": topline(df, from_date=start, to_date=end),
        "by_category": by_category(df, from_date=start, to_date=end)["categories"],
        "top_merchants": top_merchants(df, limit=10, from_date=start, to_date=end)["merchants"],
        "spending_vs_6mo_avg": {
            "this_month": round(this_spend, 2),
            "average_6mo": round(avg_spend_6mo, 2),
            "delta": round(delta_vs_avg, 2),
            "delta_pct": round(delta_pct, 1) if delta_pct is not None else None,
        },
    }
