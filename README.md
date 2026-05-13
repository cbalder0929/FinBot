# FinBot

Create and activate a virtual environment first:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
```

Install dependencies:

```powershell
pip install -r requirements.txt
```

Optional Ollama setup for this machine:

```powershell
irm https://ollama.com/install.ps1 | iex
& "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" pull qwen2.5:0.5b
```

Enable Ollama for FinBot in the current PowerShell session:

```powershell
$env:USE_OLLAMA = "1"
$env:OLLAMA_MODEL = "qwen2.5:0.5b"
```

Run the app through FastAPI so the upload API is available:

```powershell
python -m uvicorn server:app --reload --host 127.0.0.1 --port 8000
```

Then open:

```text
http://127.0.0.1:8000
```

If you open `index.html` with a static server such as VS Code Live Server, keep the FastAPI command above running. The frontend will send API requests to `http://127.0.0.1:8000`.
