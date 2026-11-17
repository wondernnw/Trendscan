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
python -m trendscan scan    # the same scan from the terminal
python -m trendscan history # last scans and their results
python -m trendscan digest --preview   # what the next email digest would contain
python -m pytest -v         # offline tests (fake feeds + fake Gemini)
```

## Scheduled scans

```bash
python -m trendscan schedule install --time 07:00             # every day at 07:00 local time
python -m trendscan schedule install --time 07:00 --weekdays  # Monday to Friday only
python -m trendscan schedule status
python -m trendscan schedule remove
```

Run these with the project's virtual environment active: the schedule remembers that Python.
On macOS, `install` registers a launchd job (`~/Library/LaunchAgents/local.trendscan.scan.plist`)
that runs `python -m trendscan scan --scheduled`. If the Mac was asleep at that time, the scan runs
when it wakes up; if it was switched off, that day's scan is skipped. The schedule file contains
only paths; the API key is read from `.env` when the scan starts.

- **One scan at a time.** A lock file next to the database stops a second scan (app button or
  scheduler) while one is running. The OS releases it if a scan crashes.
- **History.** Every scan is recorded in the `scan_runs` table. The app shows the last scan,
  including scheduled ones, and a scan history; crashed or stopped scans are marked as such.
- **Longer PDF limits.** Nobody waits for a scheduled scan, so it allows 300 s per PDF and 1200 s
  in total (manual scans: 120 s / 300 s). Gemini caps are the same for both.
- **Log.** `data/logs/scan.log` (rotated at 1 MB, git-ignored). The API key is replaced by `***`
  should a library ever include it in a message.

Windows/Linux: automatic installation is not implemented. Schedule the same command with Task
Scheduler or cron, e.g. `0 7 * * * cd /path/to/trendscan && .venv/bin/python -m trendscan scan --scheduled`.
If the project lives in `~/Documents` or `~/Desktop`, macOS may block background access to it;
`~/workspace` is not affected.

## How it works

```
config/sources.yaml ─┐
                     ▼
   sources.py  fetch RSS / arXiv API → normalized Item (URL, title, authors, date, stable id)
                     ▼
   runs.py     lock (one scan at a time) + record in `scan_runs`  ← app button, CLI, scheduler
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
   editions.py   new edition of a recurring report (title without year/issue matches, same
                 publisher) → linked to the previous edition → Gemini compares the opening
                 pages of both → quotes checked against each edition
                     ▼
   app.py      Streamlit evidence inbox
```

| File | Responsibility |
|---|---|
| `trendscan/config.py` | Settings from env/.env; the only place the API key is read |
| `trendscan/sources.py` | Fetch + normalize feeds; stable ids; content fingerprint |
| `trendscan/documents.py` | Find and download PDFs, extract text per page, check quotes and figure/table labels against a page |
| `trendscan/versions.py` | Compare two versions (ignoring moved and re-flowed text), prompt and checks for the change summary |
| `trendscan/editions.py` | Link new editions of recurring publications to the previous edition; comparison prompt |
| `trendscan/storage.py` | SQLite tables `items`, `analyses`, `documents`, `document_versions`, `version_changes`, `edition_links`, `scan_runs`, `digests` |
| `trendscan/analysis.py` | Prompts, output schemas, Gemini call, error classification, citation checks |
| `trendscan/scan.py` | Orchestration, caching decision, throttling, per-scan caps |
| `trendscan/runs.py` | One-scan-at-a-time lock, scan history, settings for scheduled scans |
| `trendscan/schedule.py` | Install/remove the macOS launchd schedule |
| `trendscan/__main__.py` | Command line (`scan`, `schedule`, `history`, `digest`), log file |
| `trendscan/digest.py` | Email digest: collect what is new, build the email, save (test mode) or send |
| `app.py` | UI only |

## Configuration

| Setting | Where | Default |
|---|---|---|
| Sources | `config/sources.yaml` | arXiv cs.AI, MIT Technology Review AI, BIS publications, BIS Annual Economic Report, FSB, ECB publications |
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
| PDF time limits, scheduled scans (s) | `TRENDSCAN_SCHEDULED_PDF_TIMEOUT_SECONDS`, `TRENDSCAN_SCHEDULED_DOC_SECONDS_PER_SCAN` | 300, 1200 |
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

## Email digest

After each scheduled scan TrendScan builds a digest of what no earlier digest reported:
emerging signals, trend matches, new versions with substantive changes, and new editions. Each
entry has the source link, what happened, the assessment, central-bank relevance, next step and
an evidence note (how many quotes were verified). Items without a trend match are only counted;
items not yet analysed wait for a later digest. Each section lists at most 15 entries, the rest
follow in the next digest. No new entries means no digest.

- **Test mode (default):** nothing is sent. The digest is saved to `data/digests/` as `.html`
  (browser) and `.eml` (opens in Apple Mail/Outlook), and shown in the app under "Last email digest".
- **Sending:** set `TRENDSCAN_EMAIL_MODE=smtp` and the `TRENDSCAN_SMTP_*` / `TRENDSCAN_EMAIL_TO`
  settings in `.env` (see `.env.example`). The connection is always encrypted (STARTTLS) and the
  password never appears in logs. Check with IT before sending from a script.
- `python -m trendscan digest` creates a digest now; `--preview` writes `data/digests/preview.html`
  without remembering anything as reported.

Feed and Gemini text is HTML-escaped in the email, and only http(s) links are kept.

## Editions of recurring publications

A new edition at a new address (BIS Annual Economic Report 2026 after 2025, Economic Bulletin
Issue 7 after Issue 6) is linked to the previous edition when:

- both are reports from the same publisher (set the same `publisher` in `sources.yaml` for
  feeds of one institution),
- both titles contain an edition marker (year, "May 2026", quarter, "Issue 7", "2nd edition"),
  the markers differ, and the titles are otherwise identical.

Wording is not compared: editions are rewritten, so shared sentences say little. Whether the
addresses follow the same pattern (`…/aer-2025`, `…/aer-2026`) is recorded and shown as
supporting evidence. Each link states why it was made.

When both PDFs are read, Gemini receives the opening pages of each edition (half the text budget
each, usually the overview) and reports the overall shift, continuing themes, up to six
differences with quotes from both editions, technology relevance and whether an earlier
assessment should be revisited. Quotes are checked against the edition they are said to come from.
"Removed" means not addressed in the compared pages of the new edition.

## Known limitations

- An edition is only linked to a previous edition that is in the inbox. FSB and ECB feeds list only
  recent items, so a yearly report's previous edition is usually not there (the BIS Annual Economic
  Report feed lists all editions, so it works from the first scan).
- Edition comparisons cover the opening pages only; in long reports the first pages include cover,
  copyright and contents.
- Many FSB and ECB items are web pages or press releases without a PDF; they are analysed from the
  feed summary and flagged "no PDF".
- An item is reported in a digest once. If a later full-text analysis changes its assessment, the
  digest does not report it again.

- A paper first seen at v2 or later is not compared with versions TrendScan never read.
- For heavily revised documents, each differing passage is shown to Gemini in shortened form.

- Only the first `TRENDSCAN_MAX_DOC_CHARS` characters of a document are analysed (whole pages).
  For dense papers that is roughly 12 pages. The inbox states which pages were not analysed.
- Some servers deliver PDFs slowly (BIS at roughly 20 KB/s when measured). Downloads that exceed
  the time limit are flagged, retried on up to three scans, and then analysed from the abstract.
- A verified quote proves the sentence is in the document, not that Gemini's interpretation is right.
- News items are analysed from the feed teaser only.
- Scheduled scans are installed automatically only on macOS, and only run while the Mac is on.

## Roadmap

1. ~~Scheduled scans~~ (done)
2. ~~Linking new editions published at a new address to the previous edition~~ (done)
3. Team review workflow (status, comments, assignment)
4. More sources (BIS AER, FSB, ECB added; next: international AI news, industry reports, trend radars)
