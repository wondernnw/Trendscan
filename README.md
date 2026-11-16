# TrendScan

Prototype for technology trend monitoring and scouting. It scans public sources, uses the
Gemini API (free tier) to summarize items, match them to **fictional** trend profiles and flag
possible emerging signals, and shows everything in a local evidence inbox with links back to
the original sources.

> **Data rule for the prototype:** only public source material and fictional trend profiles are
> sent to Gemini. On the free tier Google may use submitted content to improve its products.

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # then put your key after GEMINI_API_KEY=
```

Get a free key in Google AI Studio → *Get API key*. `.env` is git-ignored; never commit it.

## Run

```bash
streamlit run app.py        # opens http://localhost:8501 → click "Run scan"
python -m pytest -v         # offline tests (fake feeds + fake Gemini)
```

## How it works

```
config/sources.yaml ─┐
                     ▼
   sources.py  fetch RSS / arXiv API → normalized Item (URL, title, authors, date, stable id)
                     ▼
   storage.py  SQLite `items` (PRIMARY KEY item_id → no duplicates)
                     ▼
   scan.py     cache_key = content hash + profiles version + model + prompt version
               matches stored analysis?  → reuse (no API call)
               otherwise                 → analysis.py → Gemini (JSON schema) → our checks
               429 / auth / network      → stop calling Gemini, leave items "pending"
                     ▼
   app.py      Streamlit evidence inbox
```

| File | Responsibility |
|---|---|
| `trendscan/config.py` | Settings from env/.env; the only place the API key is read |
| `trendscan/sources.py` | Fetch + normalize feeds; stable ids; content fingerprint |
| `trendscan/storage.py` | SQLite tables `items` and `analyses` |
| `trendscan/analysis.py` | Prompt, output schema, Gemini call, error classification, quote verification |
| `trendscan/scan.py` | Orchestration, caching decision, throttling, per-scan cap |
| `app.py` | UI only |

## Configuration

| Setting | Where | Default |
|---|---|---|
| Sources | `config/sources.yaml` | arXiv cs.AI, MIT Technology Review AI, BIS publications |
| Trend profiles | `config/trend_profiles.yaml` (bump `version` after edits) | 5 fictional profiles |
| Model | `TRENDSCAN_GEMINI_MODEL` | `gemini-3.5-flash-lite` |
| Max Gemini calls per scan | `TRENDSCAN_MAX_ANALYSES_PER_SCAN` | 30 |
| Pause between calls (s) | `TRENDSCAN_SECONDS_BETWEEN_CALLS` | 12 (≈ 5 requests/min) |
| Database | `TRENDSCAN_DB_PATH` | `data/trendscan.db` |

**Free-tier limits:** limits apply per Google Cloud project (not per key); exceeding requests/min,
tokens/min or requests/day returns HTTP 429; daily quota resets at midnight Pacific time. Check
your project's current per-model limits in AI Studio. Each item costs one request.

## Known limitations (milestone 1)

- Gemini sees only the feed's abstract/teaser, not the full document, so page/section/figure
  references are not yet available. The UI states this on every analysis.
- Scans are manual and run in the browser session.

## Roadmap

1. Full-text retrieval for papers/reports (PDF) with page/section/figure-level evidence
2. Report and paper version tracking (old vs. new edition, summary of substantive changes)
3. Team review workflow (status, comments, assignment)
4. Scheduled scans and more sources (international AI news, industry reports, trend radars)
