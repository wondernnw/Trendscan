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
   scan.py     revision check: arXiv API (current version of known papers), HEAD request
               (change stamp of known report PDFs) → changed documents are fetched again
                     ▼
   documents.py  papers/reports only: find PDF → download → text per page → `documents` table,
                 every version archived in `document_versions`
                 (unreachable, too slow, too large, scanned → flagged, abstract is used instead)
                     ▼
   scan.py     cache_key = content hash + profiles version + model + prompt version
                           (+ hash of the document text sent, for full-text analyses)
               matches stored analysis?  → reuse (no API call)
               otherwise                 → analysis.py → Gemini (JSON schema) → our checks
               429 / auth / network      → stop calling Gemini, leave items "pending"
                     ▼
   versions.py   two versions of a document → own text comparison → Gemini summarizes only
                 the passages that differ → quotes checked against both versions
                     ▼
   app.py      Streamlit evidence inbox
```

| File | Responsibility |
|---|---|
| `trendscan/config.py` | Settings from env/.env; the only place the API key is read |
| `trendscan/sources.py` | Fetch + normalize feeds; stable ids; content fingerprint |
| `trendscan/documents.py` | Find and download PDFs, extract text per page, check quotes and figure/table labels against a page |
| `trendscan/versions.py` | Compare two versions (ignoring moved and re-flowed text), prompt and checks for the change summary |
| `trendscan/storage.py` | SQLite tables `items`, `analyses`, `documents`, `document_versions`, `version_changes` |
| `trendscan/analysis.py` | Prompts, output schemas, Gemini call, error classification, citation checks |
| `trendscan/scan.py` | Orchestration, caching decision, throttling, per-scan caps |
| `app.py` | UI only |

## Configuration

| Setting | Where | Default |
|---|---|---|
| Sources | `config/sources.yaml` | arXiv cs.AI, MIT Technology Review AI, BIS publications |
| Trend profiles | `config/trend_profiles.yaml` (bump `version` after edits) | 5 fictional profiles |
| Model | `TRENDSCAN_GEMINI_MODEL` | `gemini-3.5-flash-lite` |
| Max Gemini calls per scan | `TRENDSCAN_MAX_ANALYSES_PER_SCAN` | 30 |
| Pause between calls (s) | `TRENDSCAN_SECONDS_BETWEEN_CALLS` | 12 (≈ 5 requests/min) |
| Text sent per document | `TRENDSCAN_MAX_DOC_CHARS` | 60000 characters (whole pages from page 1) |
| Full-text analyses per scan | `TRENDSCAN_MAX_FULLTEXT_PER_SCAN` | 10 |
| PDF size limit | `TRENDSCAN_MAX_PDF_MB` | 25 |
| Time limit per PDF download (s) | `TRENDSCAN_PDF_TIMEOUT_SECONDS` | 120 |
| Time budget for PDF downloads per scan (s) | `TRENDSCAN_MAX_DOC_SECONDS_PER_SCAN` | 300 |
| Revision check window | `TRENDSCAN_REVISION_CHECK_DAYS` | 90 days since an item was first seen |
| Database | `TRENDSCAN_DB_PATH` | `data/trendscan.db` |

**Free-tier limits:** limits apply per Google Cloud project (not per key); exceeding requests/min,
tokens/min or requests/day returns HTTP 429; daily quota resets at midnight Pacific time. Check
your project's current per-model limits in AI Studio. Each item costs one request.

## Page-level evidence (papers and reports)

For items of kind `paper` or `report`, TrendScan reads the PDF and asks Gemini for evidence
entries: claim, verbatim quote, PDF page, and figure/table/section where relevant. TrendScan then
checks each entry itself against the stored page text:

- **Quote**: found on the cited page, found on another page (the link is corrected), or not found (flagged).
- **Figure / table label**: checked to appear on that page.
- **Section names**: cannot be checked automatically and are shown "as reported by Gemini".

Page numbers are PDF page numbers (what a viewer opens with `#page=N`), which can differ from
the numbers printed on the pages.

## Version tracking

Every scan checks whether papers and reports already in the inbox have changed at the source:

- **arXiv**: one API request returns the current version number of all stored papers.
- **Other PDFs**: a HEAD request reads the file's change stamp (etag, last-modified, size).

A changed document is downloaded again and archived next to the old one. TrendScan then compares
the two texts itself. A passage counts as changed only if some of its wording appears nowhere in
the other version, so figures that moved, shifted page breaks and re-flowed lines are ignored.
Gemini sees only the differing passages and returns a summary, whether the change is substantive,
whether an earlier assessment should be revisited, and up to six changes with quotes. Each quote
is checked against the page text of the version it is said to come from.

The inbox links both versions when both are online (arXiv keeps every version). If a publisher
replaced the file at the same address, the old version is no longer online; TrendScan says so and
compares against the text it stored.

## Known limitations

- Version tracking covers a revised file at the same address. A new edition published at a new
  address (next year's annual report) is not yet linked to the previous one.
- A paper first seen at v2 or later is not compared with versions TrendScan never read.
- For heavily revised documents, each differing passage is shown to Gemini in shortened form.

- Only the first `TRENDSCAN_MAX_DOC_CHARS` characters of a document are analysed (whole pages).
  For dense papers that is roughly 12 pages. The inbox states which pages were not analysed.
- Some servers deliver PDFs slowly (BIS at roughly 20 KB/s when measured). Downloads that exceed
  the time limit are flagged, retried on up to three scans, and then analysed from the abstract.
- A verified quote proves the sentence is in the document, not that Gemini's interpretation is right.
- News items are analysed from the feed teaser only.
- Scans are manual and run in the browser session.

## Roadmap

1. Scheduled scans (would also allow longer time limits for slow PDF downloads)
2. Linking new editions published at a new address to the previous edition
3. Team review workflow (status, comments, assignment)
4. More sources (international AI news, industry reports, trend radars)
