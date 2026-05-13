# Ollama integration plan

Living plan for adding local LLM augmentation to FinScrape. Edit the
"Decisions" section as you answer the questions and we'll execute
against this directly.

---

## Guiding principle

**Python stays in charge of every deterministic step.** Ollama only
runs where natural language is genuinely the right tool, and it never
touches arithmetic, file parsing, or the bulk categorization path.

| Job | Tool | Why |
|---|---|---|
| Parse PDF / CSV → canonical rows | Python (`parsers/`) | Deterministic, fast, no hallucinations |
| Categorize rows that match a rule | Python (`categorize.py`) | 99.87% coverage today at microsecond speed |
| Sum, group, top-N, month-over-month | Python (pandas) | Never let an LLM do arithmetic on money |
| **Categorize rows the rules missed** | **Ollama** | Open-ended language, narrow scope, cacheable |
| **Narrate computed numbers into prose** | **Ollama** | "Your dining spend is up 40%…" |
| **Translate free-text Q&A → pandas filter** | **Ollama** | "How much on coffee last month?" |
| **Explain detected anomalies / subscriptions** | **Ollama** | One-line human explanations |

---

## What we'll build (phased)

### Phase 1 — Long-tail categorizer (smallest, highest ROI)

Goal: push the 1 Uncategorized row to 0 without touching any working
Python path; lay the plumbing for Phases 2-3.

New files:
- `ollama_client.py` — single thin wrapper around `POST /api/chat`
  with timeout, JSON-mode output, and a clean retry-once policy.
- `ollama_categorizer.py` — `categorize_uncategorized(df) -> df` that
  iterates only over rows whose `category == "Uncategorized"`, sends
  each unique item string to Ollama with the canonical `CATEGORIES`
  list as constrained vocabulary, and writes results into the DataFrame.
- `.ollama_cache/categories.json` — keyed by normalized item substring
  → `{category, type, confidence, model, ts}`. Second call for the same
  merchant is instant. Cache survives across runs.

Integration:
- `server.py` gains one env-gated call after `apply_categorization`:
  ```python
  if settings.use_ollama:
      df = ollama_categorize_unknowns(df, model=settings.ollama_model)
  ```
  If Ollama is unreachable, log a warning and continue — never block the
  pipeline.

Optional follow-on:
- `suggest_rules.py` — periodically reads `.ollama_cache/categories.json`
  and proposes regex additions to `_RULES` for high-frequency cached
  merchants. Outputs a diff for you to review, never edits source
  automatically.

### Phase 2 — Report narration

Pre-compute report numbers in pandas (totals, breakdowns, deltas vs.
prior periods), then ask Ollama to write a 2-3 sentence summary from
the pre-computed dict. The LLM never sees raw rows, only aggregates.

New:
- `reports.py` — pandas-only: `monthly_summary(df, month) -> dict`,
  `category_breakdown(df, month) -> dict`, `topline(df, month) -> dict`.
- `ollama_narrator.py` — `narrate(summary_dict, style="coach") -> str`.
- `GET /api/reports/{period}/summary` — returns numbers + (optional)
  narration when `?narrate=1` is set.

UI: new "Reports" tab or a card on the existing page that shows the
numbers prominently and the narration underneath.

### Phase 3 — Free-text Q&A

Two-shot pattern: question → JSON filter spec → pandas → answer dict
→ narration.

New:
- `qa.py` — `parse_question(text) -> FilterSpec`,
  `answer(df, spec) -> dict`, then narrator from Phase 2.
- A small chat input on the Reports page.

### Phase 4 — Anomaly / subscription explanation (nice-to-have)

Detection is pandas (same amount + monthly cadence + merchant
similarity for subscriptions; z-score on category month-over-month for
anomalies). Ollama writes the one-line human caption.

---

## What we explicitly will not touch

- Anything in `parsers/`. PDF/CSV parsing stays Python-only.
- The Python regex-rules pass in `categorize.py`. Ollama runs *after*
  it, not instead.
- The robot UI's flow during `/api/parse`. Phase 1 either fits in the
  existing latency budget (cache hit ≈ free, cache miss ≈ 200-2000 ms
  for ≤10 rows worst case) or we move it to a background step that
  fires after `Done!`. See Decision D7 below.

---

## Decisions I need from you

Mark your answer next to each. "Default" is what I'll assume if you
don't have a preference.

### D1. Is Ollama already installed and running on this machine?
- Default: yes, default host/port `http://localhost:11434`.
- If different host/port, write it here:

   **Your answer:**

### D2. Which model should we default to?
Reasonable choices for this workload (short classification, JSON
output, narration of small dicts):
- `llama3.2:3b` — fastest, ~1-2 GB RAM, fine for this task
- `llama3.1:8b` — better narration, ~6 GB RAM
- `qwen2.5:7b` — strong at structured JSON, ~5 GB RAM
- `mistral:7b` — middle-ground
- Other:

   **Your answer:**

### D3. Opt-in via env flag, or always-on when Ollama is reachable?
- Default: opt-in via `USE_OLLAMA=1` (so the pipeline keeps working
  identically if Ollama is down or you don't want to use it).

   **Your answer:**

### D4. Phase 1 scope — categorize-and-cache only, or also the
"suggest a regex rule" loop?
- Default: categorize-and-cache only. Add rule suggestions in a
  follow-on once we see what merchants show up.

   **Your answer:**

### D5. Cache location — `./.ollama_cache/` in the project root?
- Default: yes, gitignored.

   **Your answer:**

### D6. Privacy — confirm nothing should ever leave the machine?
- Default: local-only. Ollama URL must be loopback (`127.0.0.1` /
  `localhost`). I'll add a guard that refuses to talk to remote URLs.

   **Your answer:**

### D7. Latency — should the LLM call run inside the `/api/parse`
request (delaying "Done!"), or after the response is returned?
- Default: inline if the cache is warm or there are ≤5 unknowns.
  Otherwise async + a follow-up status the UI polls for.
- Simpler alternative: always inline, accept the latency hit (probably
  fine — unknowns are rare).

   **Your answer:**

### D8. Which phases should I build now, in order?
- Default: Phase 1 only. Validate it works on your statements, then
  decide on Phase 2.
- Alternative: Phase 1 + Phase 2 (reports + narration) — bigger
  surface area but you get user-visible value sooner.

   **Your answer:**

### D9. Report cadence / shape (only relevant if Phase 2 in scope):
- Monthly summary? Weekly? Per-category trends? Subscription audit?
- Default: monthly summary + category breakdown + top-merchants list.

   **Your answer:**

### D10. UI for narration / Q&A (only relevant if Phase 2/3 in scope):
- A new "Reports" tab in the existing FinScrape UI, or a separate page?
- Default: a Reports tab inside the same single-page app, reachable
  from the header.

   **Your answer:**

---

## Cost & latency budget (Phase 1)

Rough numbers on `llama3.2:3b` running locally:

| Scenario | GPU-accelerated | **This machine (CPU-only)** |
|---|---|---|
| Cache hit | <1 ms | <1 ms |
| Cache miss, 1 item | 150-400 ms | **~3 minutes** ❌ |
| Cold model load | +2-5 s | +30-90 s |

### Hardware finding (this machine)

`ollama ps` shows `100% CPU` — no GPU acceleration available. Token
generation runs at roughly 0.8 tok/s, which makes a single category
classification take ~180 s (prompt eval + JSON output). That's
unusable for live UI integration.

For your current data: 1 unknown item per 768-row batch would block
the "Done!" message for ~3 minutes. Not viable.

### Options

1. **Pull a much smaller model.** `qwen2.5:0.5b` (~350 MB) or
   `gemma3:270m` (~270 MB) should run ~6-10× faster on CPU, putting
   single-call latency in the 10-20 s range. Accuracy is lower but for
   "classify one merchant string against a fixed list of 30 labels"
   they're plenty.
2. **Move LLM categorization off the request path.** The `/api/parse`
   endpoint returns rules-only, the UI shows `Done!` immediately, and
   a separate worker enqueues Uncategorized rows for background
   classification. The user refreshes (or a websocket pushes) to see
   the LLM-promoted rows later.
3. **Skip Phase 1.** The rules already cover 99.87% on your data
   (1 Uncategorized in 768). Move directly to Phase 2 (reports +
   narration) where the user is on a Reports page, can wait a minute,
   and the LLM does much higher-value work than chasing the tail of
   one row.
4. **Enable GPU.** Install NVIDIA drivers + CUDA (if you have an
   NVIDIA card) and restart `ollama`. `ollama ps` should then show
   GPU percentage instead of `100% CPU`.

---

## Open implementation questions (I'll answer these myself once D1-D10 land)

- Single-row prompts vs. batched ("classify these 10 items at once")?
  Batched is faster but harder to recover from a malformed response.
  Default: single-row with retry-once.
- JSON-mode (`format: "json"`) vs. free-text parsing? Always JSON-mode
  — far more reliable.
- Temperature? `0` for categorization, `0.4-0.6` for narration.
- Validation: confirm the returned `category` is in our `CATEGORIES`
  tuple; if not, fall back to `Uncategorized` rather than trusting a
  hallucinated bucket.

---

## When this plan is approved

I'll:
1. Add the agreed-upon files for the chosen phase(s).
2. Wire them into `server.py` behind the env flag.
3. Run an end-to-end test on your `statment/` folder showing
   before-vs-after Uncategorized counts and (Phase 2) a sample
   narration.
4. Update `CLAUDE.MD` with a short "LLM augmentation" section pointing
   future contributors at this file.
