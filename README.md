# FinBot

FinBot is a lightweight web-based financial statement analyzer built with FastAPI and vanilla
JavaScript. Upload a PDF, CSV, or XLSX export and the app extracts transactions, asks Ollama for
categories when available, and falls back to built-in heuristics when Ollama is offline.

## Run locally

```bash
python -m pip install -r requirements.txt
uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000` and upload a statement file.

## Test

```bash
pytest
```
