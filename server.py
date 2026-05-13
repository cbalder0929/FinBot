"""FinScrape server.

Serves the static frontend (``index.html``, ``styles.css``, ``app.js``,
``robot.js``) and exposes ``POST /api/parse`` that runs each uploaded
PDF/CSV through the parser pipeline in ``parsers/`` and the categorizer
in ``categorize.py``.

Run with::

    uvicorn server:app --reload

The response shape mirrors the one the frontend expects::

    {
      "created": [{"name": ..., "rows": int, "columns": [...]}, ...],
      "errors":  [{"file": ..., "error": "..."}, ...],
      "total_transactions": int
    }
"""
from __future__ import annotations

import os
import secrets
import tempfile
from io import StringIO
from pathlib import Path
from typing import Optional

import pandas as pd
from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse

from categorize import apply_categorization
from parsers import detect_source, parse as parse_file

ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Persistence — each /api/parse writes a CSV to .outputs/ so the Reports
# tab has data to aggregate across requests. Matches the file-naming
# contract bank-parser-fastapi established (<token>__<name>.csv).
# Gitignored; safe to delete to reset.
# ---------------------------------------------------------------------------
OUTPUT_DIR = ROOT / ".outputs"
OUTPUT_DIR.mkdir(exist_ok=True)


def _safe_stem(name: str) -> str:
    stem = Path(name).stem.strip() or "statement"
    stem = "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in stem)
    return stem[:80] or "statement"

# ---------------------------------------------------------------------------
# Optional Ollama augmentation (Phase 1 of OLLAMA_PLAN.md).
#
# Opt-in via ``USE_OLLAMA=1`` so the pipeline keeps working identically
# when Ollama isn't installed/running. Import is lazy too — if the
# module can't import (e.g. Python version mismatch) we log and run
# rules-only.
# ---------------------------------------------------------------------------
USE_OLLAMA = os.getenv("USE_OLLAMA", "0") == "1"
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
_categorize_uncategorized = None

if USE_OLLAMA:
    try:
        from ollama_categorizer import categorize_uncategorized as _categorize_uncategorized
        print(f"[server] Ollama augmentation enabled (model={OLLAMA_MODEL})")
    except Exception as e:  # pragma: no cover - defensive only
        print(f"[server] USE_OLLAMA=1 but ollama_categorizer failed to import: {e}")
        _categorize_uncategorized = None


# ---------------------------------------------------------------------------
# Legacy schema migration — keeps older Date/Description/Amount CSVs flowing.
# ---------------------------------------------------------------------------
def migrate_legacy_df(df: pd.DataFrame) -> pd.DataFrame:
    if "date" in df.columns:
        return df
    new = pd.DataFrame()
    new["date"] = pd.to_datetime(
        df.get("Date", pd.Series(dtype=str)), errors="coerce"
    ).dt.strftime("%Y-%m-%d")
    new["item"] = df.get("Description", pd.Series(dtype=str))
    amounts = pd.to_numeric(
        df.get("Amount", pd.Series(dtype=float)), errors="coerce"
    ).fillna(0)
    new["debits"] = amounts.where(amounts < 0, 0).abs()
    new["credits"] = amounts.where(amounts >= 0, 0)
    new["category"] = None
    new["type"] = None
    new["source"] = "unknown"
    new["account"] = "Unknown"
    return new


# ---------------------------------------------------------------------------
# FastAPI app + static frontend routes
# ---------------------------------------------------------------------------
app = FastAPI(title="FinScrape")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5500",
        "http://localhost:5500",
        "http://127.0.0.1:5501",
        "http://localhost:5501",
        "http://127.0.0.1:3000",
        "http://localhost:3000",
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ],
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


# We serve the four asset files explicitly (rather than mounting a static
# dir) so the layout authored by the frontend — everything at repo root —
# is preserved without any rearrangement.
@app.get("/")
def index() -> FileResponse:
    return _static_file("index.html", "text/html")


@app.get("/styles.css")
def styles_css() -> FileResponse:
    return _static_file("styles.css", "text/css")


@app.get("/app.js")
def app_js() -> FileResponse:
    return _static_file("app.js", "application/javascript")


@app.get("/robot.js")
def robot_js() -> FileResponse:
    return _static_file("robot.js", "application/javascript")


@app.get("/reports.js")
def reports_js_static() -> FileResponse:
    return _static_file("reports.js", "application/javascript")


def _static_file(name: str, media_type: str) -> FileResponse:
    return FileResponse(
        ROOT / name,
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# /api/parse — parse + categorize uploads. One bad file does not break
# the batch; each failure is reported individually in ``errors``.
# ---------------------------------------------------------------------------
@app.post("/api/parse")
async def parse_endpoint(files: list[UploadFile] = File(...)) -> JSONResponse:
    created: list[dict] = []
    errors: list[dict] = []
    total_rows = 0

    for f in files:
        filename = f.filename or "statement"
        lower = filename.lower()
        is_pdf = lower.endswith(".pdf")
        is_csv = lower.endswith(".csv")

        if not is_pdf and not is_csv:
            errors.append({
                "file": filename,
                "error": "Only PDF and CSV files are supported.",
            })
            continue

        suffix = ".pdf" if is_pdf else ".csv"
        temp_path: Path | None = None
        try:
            content = await f.read()

            # pdfplumber + pandas both want a path on disk. NamedTemporaryFile
            # with delete=False is cross-platform when we unlink it ourselves.
            with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as tf:
                tf.write(content)
                temp_path = Path(tf.name)

            if is_pdf:
                df = parse_file(temp_path)
            else:
                source = detect_source(temp_path)
                df = parse_file(temp_path, source_hint=source)
                # Unknown CSV — fall back to a raw read so the user still
                # gets *something* in the response.
                if df.empty or "date" not in df.columns:
                    csv_text = content.decode("utf-8", errors="replace")
                    df = pd.read_csv(StringIO(csv_text))

            # Categorize whenever we recognize a schema.
            if not df.empty and ("date" in df.columns or "Date" in df.columns):
                if "date" not in df.columns:
                    df = migrate_legacy_df(df)
                df = apply_categorization(df)
                # Phase 1: optional LLM fallback for rows the rules
                # couldn't classify. Never raises — pipeline continues
                # on rules-only if Ollama is unreachable.
                if _categorize_uncategorized is not None:
                    try:
                        df = _categorize_uncategorized(df, model=OLLAMA_MODEL)
                    except Exception as e:
                        print(f"[server] ollama categorize_uncategorized raised: {e}")

            row_count = int(df.shape[0])
            total_rows += row_count

            # Persist canonical-schema CSV so the Reports tab can
            # aggregate across requests. Token + double-underscore +
            # safe stem matches the file-id contract.
            out_id = secrets.token_urlsafe(10)
            safe_name = _safe_stem(filename) + ".csv"
            out_path = OUTPUT_DIR / f"{out_id}__{safe_name}"
            out_path.write_text(df.to_csv(index=False), encoding="utf-8", newline="")

            created.append({
                "id": out_id,
                "name": filename,
                "rows": row_count,
                "columns": list(df.columns),
            })
        except Exception as e:
            # Per-file isolation: bad input becomes an entry in ``errors``,
            # never an HTTP 500.
            errors.append({"file": filename, "error": str(e)})
        finally:
            if temp_path is not None and temp_path.exists():
                try:
                    temp_path.unlink()
                except Exception:
                    pass
            try:
                await f.close()
            except Exception:
                pass

    return JSONResponse({
        "created": created,
        "errors": errors,
        "total_transactions": total_rows,
    })


# ---------------------------------------------------------------------------
# Reports — pandas does all the math (reports.py). Each endpoint loads
# every persisted CSV from .outputs/, applies optional from/to filters,
# and returns JSON. Narration is opt-in via ``?narrate=1`` and routes
# through ollama_narrator (slow path; honest about its latency budget).
# ---------------------------------------------------------------------------
import reports  # noqa: E402  (placed here to keep imports near use)


def _load_report_df() -> pd.DataFrame:
    return reports.load_outputs(OUTPUT_DIR)


def _maybe_narrate(section: str, payload: dict) -> tuple[Optional[str], Optional[str]]:
    """Return ``(narration, error_reason)`` for ``section`` from
    ollama_narrator. Either side may be ``None``. Never raises.

    Returning both lets the UI distinguish "Ollama is down" from
    "Ollama is slow and timed out" — those failure modes had been
    collapsed into one misleading "not reachable" message.
    """
    try:
        from ollama_narrator import narrate  # lazy import — Ollama optional
    except Exception as e:
        return None, f"narrator module failed to import: {e}"
    try:
        return narrate(section, payload)
    except Exception as e:
        print(f"[narrate] {section}: {e}")
        return None, f"narrator raised: {e}"


@app.get("/api/reports/topline")
def reports_topline(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    narrate: int = 0,
) -> JSONResponse:
    df = _load_report_df()
    payload = reports.topline(df, from_, to)
    body: dict = {"data": payload}
    if narrate:
        text, reason = _maybe_narrate("topline", payload)
        body["narration"] = text
        body["narration_error"] = reason
    return JSONResponse(body)


@app.get("/api/reports/by-category")
def reports_by_category(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    narrate: int = 0,
) -> JSONResponse:
    df = _load_report_df()
    payload = reports.by_category(df, from_, to)
    body: dict = {"data": payload}
    if narrate:
        text, reason = _maybe_narrate("by_category", payload)
        body["narration"] = text
        body["narration_error"] = reason
    return JSONResponse(body)


@app.get("/api/reports/top-merchants")
def reports_top_merchants(
    limit: int = Query(10),
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
) -> JSONResponse:
    df = _load_report_df()
    return JSONResponse({"data": reports.top_merchants(df, limit, from_, to)})


@app.get("/api/reports/trend")
def reports_trend(
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = Query(None),
    narrate: int = 0,
) -> JSONResponse:
    df = _load_report_df()
    payload = reports.monthly_trend(df, from_, to)
    body: dict = {"data": payload}
    if narrate:
        text, reason = _maybe_narrate("trend", payload)
        body["narration"] = text
        body["narration_error"] = reason
    return JSONResponse(body)


@app.get("/api/reports/monthly")
def reports_monthly(
    month: Optional[str] = Query(None),
    narrate: int = 0,
) -> JSONResponse:
    df = _load_report_df()
    payload = reports.monthly_summary(df, month)
    body: dict = {"data": payload}
    if narrate:
        text, reason = _maybe_narrate("monthly", payload)
        body["narration"] = text
        body["narration_error"] = reason
    return JSONResponse(body)


# ---------------------------------------------------------------------------
# Clear stored data — lets the user reset between sessions without
# poking around in .outputs/ by hand.
# ---------------------------------------------------------------------------
@app.delete("/api/files")
def clear_files() -> JSONResponse:
    deleted = 0
    for p in OUTPUT_DIR.glob("*.csv"):
        try:
            p.unlink()
            deleted += 1
        except OSError:
            continue
    return JSONResponse({"deleted": deleted})


# ---------------------------------------------------------------------------
# Q&A — Phase 3. Free-text question → LLM-parsed filter spec → pandas →
# LLM narration. Lazy import so the server still starts without Ollama.
# ---------------------------------------------------------------------------
from pydantic import BaseModel  # noqa: E402


class QARequest(BaseModel):
    question: str
    skip_narration: bool = False


@app.post("/api/qa")
def qa_endpoint(body: QARequest) -> JSONResponse:
    question = (body.question or "").strip()
    if not question:
        return JSONResponse({"error": "question is required"}, status_code=400)
    try:
        from qa import answer as qa_answer
    except Exception as e:
        return JSONResponse(
            {"error": f"Q&A module failed to import: {e}"}, status_code=500
        )
    df = _load_report_df()
    return JSONResponse(
        qa_answer(question, df, model=OLLAMA_MODEL, skip_narration=body.skip_narration)
    )


@app.get("/api/files")
def list_files() -> JSONResponse:
    items = []
    for p in sorted(OUTPUT_DIR.glob("*.csv"), key=lambda x: x.stat().st_mtime, reverse=True):
        if "__" in p.name:
            file_id, rest = p.name.split("__", 1)
        else:
            file_id, rest = p.stem, p.name
        items.append({"id": file_id, "filename": rest, "size": p.stat().st_size})
    return JSONResponse({"files": items})
