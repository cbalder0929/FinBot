# FinBot

Run the app through FastAPI so the upload API is available:

```powershell
python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000
```

Then open:

```text
http://127.0.0.1:8000
```

If you open `index.html` with a static server such as VS Code Live Server, keep the FastAPI command above running. The frontend will send API requests to `http://127.0.0.1:8000`.
