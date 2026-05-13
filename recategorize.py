"""Re-categorize transactions in an existing CSV.

Useful when you've tweaked rules in ``categorize.py`` or edited
``category_overrides.json`` and want to apply them to a file you've
already parsed — without re-running the PDF parsers.

Usage::

    python recategorize.py INPUT.csv [OUTPUT.csv] [--summary]

If OUTPUT is omitted, writes ``<INPUT>.recategorized.csv`` next to the
input. With ``--summary``, prints a category tally plus a diff of how
many rows changed category.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pandas as pd

from categorize import apply_categorization


def _print_summary(before: pd.Series, after: pd.Series) -> None:
    print("category counts (after):")
    for cat, n in after.value_counts().items():
        print(f"  {n:>5}  {cat}")
    changed = (before.fillna("") != after.fillna("")).sum()
    print(f"\nrows whose category changed: {changed} / {len(after)}")
    if changed:
        moved = (
            pd.DataFrame({"from": before, "to": after})
            .loc[before.fillna("") != after.fillna("")]
            .groupby(["from", "to"]).size().reset_index(name="n")
            .sort_values("n", ascending=False).head(15)
        )
        print("\ntop category shifts (from -> to):")
        for _, r in moved.iterrows():
            src = r["from"] if str(r["from"]) and str(r["from"]).lower() != "nan" else "(none)"
            print(f"  {r['n']:>4}  {src!s:<40} -> {r['to']}")


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("input", type=Path, help="CSV to re-categorize")
    p.add_argument("output", nargs="?", type=Path, default=None,
                   help="output path (default: <input>.recategorized.csv)")
    p.add_argument("--summary", action="store_true",
                   help="print category counts and top shifts")
    p.add_argument("--in-place", action="store_true",
                   help="overwrite the input file (ignores OUTPUT)")
    args = p.parse_args(argv)

    if not args.input.exists():
        print(f"error: {args.input} not found", file=sys.stderr)
        return 2

    df = pd.read_csv(args.input)
    if "item" not in df.columns:
        print("error: input CSV has no 'item' column — is this a canonical-schema file?",
              file=sys.stderr)
        return 2

    before = df.get("category", pd.Series([""] * len(df))).astype(str)
    df = apply_categorization(df)
    after = df["category"].astype(str)

    out = args.input if args.in_place else (
        args.output or args.input.with_suffix(".recategorized.csv")
    )
    df.to_csv(out, index=False)
    print(f"wrote {out}  ({len(df)} rows)")

    if args.summary:
        print()
        _print_summary(before, after)

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
