"""SQLite storage: the evidence inbox and the Gemini result cache.

Tables:
  items             — one row per source item. PRIMARY KEY item_id makes duplicates impossible.
  analyses          — one Gemini result per item, tagged with the cache_key it was produced for.
  documents         — current full text (per page) of a paper/report PDF, or why it is unavailable.
  document_versions — archive: the page texts of every version of a document we have read.
  version_changes   — comparison of two consecutive versions (local diff + Gemini summary).
  scan_runs         — history of scans (manual or scheduled) with their report.
  digests           — email digests saved (test mode) or sent, and which entries each reported.
  edition_links     — a new edition of a recurring publication, linked to the previous edition,
                      with the comparison of the two.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

from .documents import Document
from .sources import Item

SCHEMA = """
CREATE TABLE IF NOT EXISTS items (
    item_id      TEXT PRIMARY KEY,
    source_id    TEXT NOT NULL,
    source_name  TEXT NOT NULL,
    source_kind  TEXT NOT NULL,
    url          TEXT NOT NULL,
    title        TEXT NOT NULL,
    publisher    TEXT,
    authors      TEXT,            -- JSON list
    published    TEXT,            -- ISO 8601 or NULL
    summary      TEXT,
    version      TEXT,
    pdf_url      TEXT,
    content_hash TEXT NOT NULL,
    first_seen   TEXT NOT NULL,
    last_seen    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS analyses (
    item_id     TEXT PRIMARY KEY REFERENCES items(item_id),
    cache_key   TEXT NOT NULL,     -- content hash + profiles version + model + prompt version
    status      TEXT NOT NULL,     -- done | failed
    result      TEXT,              -- JSON from Gemini (when done)
    error       TEXT,              -- short error message (when failed)
    model       TEXT,
    analyzed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    item_id     TEXT PRIMARY KEY REFERENCES items(item_id),
    source_ref  TEXT NOT NULL,     -- what we fetched for (PDF link or page URL); a change means re-fetch
    pdf_url     TEXT,
    status      TEXT NOT NULL,     -- ok | inaccessible | no_pdf | too_large | no_text | unreadable
    detail      TEXT,
    page_count  INTEGER NOT NULL,
    pages       TEXT,              -- JSON list of page texts (index 0 = PDF page 1)
    attempts    INTEGER NOT NULL,
    fetched_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS document_versions (
    item_id     TEXT NOT NULL REFERENCES items(item_id),
    version_no  INTEGER NOT NULL,  -- 1, 2, 3 … in the order TrendScan read them
    label       TEXT NOT NULL,     -- e.g. "v2" (arXiv) or "retrieved 2026-11-16"
    pdf_url     TEXT,
    page_count  INTEGER NOT NULL,
    pages       TEXT NOT NULL,     -- JSON list of page texts
    text_hash   TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (item_id, version_no)
);

CREATE TABLE IF NOT EXISTS version_changes (
    item_id     TEXT NOT NULL REFERENCES items(item_id),
    old_no      INTEGER NOT NULL,
    new_no      INTEGER NOT NULL,
    cache_key   TEXT NOT NULL,     -- old text hash + new text hash + model + prompt version
    status      TEXT NOT NULL,     -- done | identical | pending | failed
    stats       TEXT,              -- JSON from our own comparison (always present)
    result      TEXT,              -- JSON from Gemini (when done)
    error       TEXT,
    model       TEXT,
    compared_at TEXT NOT NULL,
    PRIMARY KEY (item_id, new_no)
);

CREATE TABLE IF NOT EXISTS edition_links (
    new_item_id TEXT PRIMARY KEY REFERENCES items(item_id),
    old_item_id TEXT NOT NULL REFERENCES items(item_id),
    series_key  TEXT NOT NULL,     -- title without edition marker, e.g. "bis annual economic report"
    reason      TEXT NOT NULL,     -- why the two were linked (shown to the user)
    status      TEXT NOT NULL,     -- waiting | unavailable | pending | done | failed
    cache_key   TEXT,              -- both text hashes + model + prompt version
    stats       TEXT,              -- JSON: pages compared per edition
    result      TEXT,              -- JSON from Gemini (when done)
    error       TEXT,              -- why not compared (unavailable/failed/pending)
    model       TEXT,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS digests (
    digest_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT NOT NULL,
    mode        TEXT NOT NULL,     -- test (saved to a file) | smtp (sent)
    subject     TEXT NOT NULL,
    entries     INTEGER NOT NULL,
    location    TEXT NOT NULL      -- file path (test) or recipients (smtp)
);

CREATE TABLE IF NOT EXISTS digest_entries (
    entry_key   TEXT PRIMARY KEY,  -- e.g. "item:<id>", "version:<id>:<n>", "edition:<new>:<old>"
    digest_id   INTEGER NOT NULL REFERENCES digests(digest_id)
);

CREATE TABLE IF NOT EXISTS scan_runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger     TEXT NOT NULL,     -- manual | scheduled
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,     -- running | ok | error | interrupted
    report      TEXT,              -- JSON of the ScanReport (when finished)
    error       TEXT
);
"""


def text_hash(pages: list[str]) -> str:
    return hashlib.sha256("\f".join(pages).encode("utf-8")).hexdigest()[:16]



def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)
            self._migrate(c)

    @staticmethod
    def _migrate(c: sqlite3.Connection) -> None:
        """Bring a database created by an earlier milestone up to date."""
        columns = {row["name"] for row in c.execute("PRAGMA table_info(documents)")}
        if "signature" not in columns:    # change stamp of the PDF on the server (etag etc.)
            c.execute("ALTER TABLE documents ADD COLUMN signature TEXT")
        if "refetch" not in columns:      # 1 = the server copy changed, download it again
            c.execute("ALTER TABLE documents ADD COLUMN refetch INTEGER NOT NULL DEFAULT 0")
        # Documents read before version tracking existed become version 1.
        rows = c.execute(
            """SELECT d.item_id, d.pdf_url, d.page_count, d.pages, d.fetched_at, i.version
               FROM documents d JOIN items i ON i.item_id = d.item_id
               WHERE d.status = 'ok'
                 AND NOT EXISTS (SELECT 1 FROM document_versions v WHERE v.item_id = d.item_id)"""
        ).fetchall()
        for r in rows:
            c.execute("INSERT INTO document_versions VALUES (?,?,?,?,?,?,?,?)",
                      (r["item_id"], 1, r["version"] or f"retrieved {r['fetched_at'][:10]}", r["pdf_url"],
                       r["page_count"], r["pages"], text_hash(json.loads(r["pages"])), r["fetched_at"]))

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # ----- items -----

    def upsert_item(self, item: Item) -> str:
        """Insert a new item or refresh an existing one.

        Returns "new", "unchanged", or "changed" (same item_id, different content).
        """
        ts = now_iso()
        with self._conn() as c:
            row = c.execute("SELECT content_hash, version FROM items WHERE item_id = ?", (item.item_id,)).fetchone()
            if row is None:
                c.execute(
                    """INSERT INTO items (item_id, source_id, source_name, source_kind, url, title,
                           publisher, authors, published, summary, version, pdf_url,
                           content_hash, first_seen, last_seen)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(item_id) DO NOTHING""",
                    (item.item_id, item.source_id, item.source_name, item.source_kind, item.url,
                     item.title, item.publisher, json.dumps(item.authors), item.published,
                     item.summary, item.version, item.pdf_url, item.content_hash, ts, ts),
                )
                return "new"
            if row["content_hash"] == item.content_hash or _is_older(item.version, row["version"]):
                # Same content, or a feed that still lists an older version than the one we
                # already have (never go back from v2 to v1).
                c.execute("UPDATE items SET last_seen = ? WHERE item_id = ?", (ts, item.item_id))
                return "unchanged"
            # Content changed (e.g. corrected title, new arXiv version): keep the row, update text.
            c.execute(
                """UPDATE items SET title=?, summary=?, version=?, pdf_url=?, url=?,
                       content_hash=?, last_seen=? WHERE item_id=?""",
                (item.title, item.summary, item.version, item.pdf_url, item.url,
                 item.content_hash, ts, item.item_id),
            )
            return "changed"

    def get_item(self, item_id: str) -> Item | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM items WHERE item_id = ?", (item_id,)).fetchone()
        return _row_to_item(row) if row else None

    def count_items(self) -> int:
        with self._conn() as c:
            return c.execute("SELECT COUNT(*) FROM items").fetchone()[0]

    # ----- analyses (cache) -----

    def get_analysis(self, item_id: str) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM analyses WHERE item_id = ?", (item_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    def save_analysis(self, item_id: str, cache_key: str, model: str,
                      result: dict | None = None, error: str | None = None) -> None:
        status = "done" if result is not None else "failed"
        with self._conn() as c:
            c.execute(
                """INSERT INTO analyses (item_id, cache_key, status, result, error, model, analyzed_at)
                   VALUES (?,?,?,?,?,?,?)
                   ON CONFLICT(item_id) DO UPDATE SET cache_key=excluded.cache_key,
                       status=excluded.status, result=excluded.result, error=excluded.error,
                       model=excluded.model, analyzed_at=excluded.analyzed_at""",
                (item_id, cache_key, status, json.dumps(result) if result is not None else None,
                 error, model, now_iso()),
            )

    # ----- documents (full text) -----

    def get_document_meta(self, item_id: str) -> dict | None:
        """Status of the stored document without loading its page texts."""
        with self._conn() as c:
            row = c.execute(
                "SELECT source_ref, pdf_url, status, detail, page_count, attempts, signature, refetch "
                "FROM documents WHERE item_id = ?",
                (item_id,)).fetchone()
        return dict(row) if row else None

    def get_document(self, item_id: str) -> Document | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM documents WHERE item_id = ?", (item_id,)).fetchone()
        if not row:
            return None
        return Document(row["status"], row["detail"] or "", row["pdf_url"], json.loads(row["pages"] or "[]"))

    def save_document(self, item_id: str, source_ref: str, doc: Document, attempts: int,
                      label: str | None = None) -> bool:
        """Store the current document. A readable document whose text differs from the last
        archived version is also archived as a new version. Returns True if a version was added."""
        added = False
        with self._conn() as c:
            if doc.status == "ok":
                last = c.execute("SELECT version_no, text_hash FROM document_versions WHERE item_id = ? "
                                 "ORDER BY version_no DESC LIMIT 1", (item_id,)).fetchone()
                new_hash = text_hash(doc.pages)
                if last is None or last["text_hash"] != new_hash:
                    c.execute("INSERT INTO document_versions VALUES (?,?,?,?,?,?,?,?)",
                              (item_id, (last["version_no"] + 1) if last else 1,
                               label or f"retrieved {now_iso()[:10]}", doc.pdf_url, len(doc.pages),
                               json.dumps(doc.pages), new_hash, now_iso()))
                    added = True
            c.execute(
                """INSERT INTO documents (item_id, source_ref, pdf_url, status, detail, page_count, pages, attempts, fetched_at)
                   VALUES (?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(item_id) DO UPDATE SET source_ref=excluded.source_ref, pdf_url=excluded.pdf_url,
                       status=excluded.status, detail=excluded.detail, page_count=excluded.page_count,
                       pages=excluded.pages, attempts=excluded.attempts, fetched_at=excluded.fetched_at,
                       refetch=0""",
                (item_id, source_ref, doc.pdf_url, doc.status, doc.detail, len(doc.pages),
                 json.dumps(doc.pages), attempts, now_iso()),
            )
        return added

    def set_signature(self, item_id: str, signature: str, refetch: bool) -> None:
        with self._conn() as c:
            c.execute("UPDATE documents SET signature = ?, refetch = ? WHERE item_id = ?",
                      (signature, int(refetch), item_id))

    # ----- versions -----

    def get_versions(self, item_id: str) -> list[dict]:
        """All archived versions of a document, oldest first (without the page texts)."""
        with self._conn() as c:
            rows = c.execute("SELECT version_no, label, pdf_url, page_count, text_hash, fetched_at "
                             "FROM document_versions WHERE item_id = ? ORDER BY version_no", (item_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_version_pages(self, item_id: str, version_no: int) -> list[str]:
        with self._conn() as c:
            row = c.execute("SELECT pages FROM document_versions WHERE item_id = ? AND version_no = ?",
                            (item_id, version_no)).fetchone()
        return json.loads(row["pages"]) if row else []

    def items_with_revisions(self) -> list[str]:
        with self._conn() as c:
            rows = c.execute("SELECT item_id FROM document_versions GROUP BY item_id HAVING COUNT(*) > 1").fetchall()
        return [r["item_id"] for r in rows]

    def get_change(self, item_id: str, new_no: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM version_changes WHERE item_id = ? AND new_no = ?",
                            (item_id, new_no)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["stats"] = json.loads(d["stats"]) if d["stats"] else None
        d["result"] = json.loads(d["result"]) if d["result"] else None
        return d

    def save_change(self, item_id: str, old_no: int, new_no: int, cache_key: str, status: str,
                    stats: dict, model: str, result: dict | None = None, error: str | None = None) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO version_changes (item_id, old_no, new_no, cache_key, status, stats, result, error, model, compared_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(item_id, new_no) DO UPDATE SET old_no=excluded.old_no, cache_key=excluded.cache_key,
                       status=excluded.status, stats=excluded.stats, result=excluded.result, error=excluded.error,
                       model=excluded.model, compared_at=excluded.compared_at""",
                (item_id, old_no, new_no, cache_key, status, json.dumps(stats),
                 json.dumps(result) if result is not None else None, error, model, now_iso()),
            )

    # ----- editions -----

    def get_edition_links(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM edition_links").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["stats"] = json.loads(d["stats"]) if d["stats"] else None
            d["result"] = json.loads(d["result"]) if d["result"] else None
            out.append(d)
        return out

    def save_edition_link(self, new_item_id: str, old_item_id: str, series_key: str, reason: str,
                          status: str, cache_key: str | None = None, stats: dict | None = None,
                          model: str | None = None, result: dict | None = None,
                          error: str | None = None) -> None:
        with self._conn() as c:
            c.execute(
                """INSERT INTO edition_links (new_item_id, old_item_id, series_key, reason, status,
                       cache_key, stats, model, result, error, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(new_item_id) DO UPDATE SET old_item_id=excluded.old_item_id,
                       series_key=excluded.series_key, reason=excluded.reason, status=excluded.status,
                       cache_key=excluded.cache_key, stats=excluded.stats, result=excluded.result,
                       error=excluded.error, model=excluded.model, updated_at=excluded.updated_at""",
                (new_item_id, old_item_id, series_key, reason, status, cache_key,
                 json.dumps(stats) if stats is not None else None, model,
                 json.dumps(result) if result is not None else None, error, now_iso()),
            )

    def delete_edition_link(self, new_item_id: str) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM edition_links WHERE new_item_id = ?", (new_item_id,))

    # ----- email digests -----

    def reported_keys(self) -> set[str]:
        with self._conn() as c:
            return {r["entry_key"] for r in c.execute("SELECT entry_key FROM digest_entries")}

    def record_digest(self, mode: str, subject: str, location: str, keys: list[str]) -> int:
        """Remember a saved/sent digest and the entries it reported (so they are not repeated)."""
        with self._conn() as c:
            cur = c.execute("INSERT INTO digests (created_at, mode, subject, entries, location) VALUES (?,?,?,?,?)",
                            (now_iso(), mode, subject, len(keys), location))
            c.executemany("INSERT OR IGNORE INTO digest_entries VALUES (?,?)",
                          [(k, cur.lastrowid) for k in keys])
            return cur.lastrowid

    def recent_digests(self, limit: int = 5) -> list[dict]:
        with self._conn() as c:
            return [dict(r) for r in c.execute("SELECT * FROM digests ORDER BY digest_id DESC LIMIT ?", (limit,))]

    def done_changes(self) -> list[dict]:
        """Version comparisons summarized by Gemini (newest version per document)."""
        with self._conn() as c:
            rows = c.execute("SELECT * FROM version_changes WHERE status = 'done'").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["result"] = json.loads(d["result"]) if d["result"] else None
            out.append(d)
        return out

    # ----- scan history -----

    def start_run(self, trigger: str) -> int:
        with self._conn() as c:
            # Only one scan can run at a time (see runs.scan_lock). A row still marked
            # "running" therefore belongs to a scan whose process ended without finishing.
            c.execute("UPDATE scan_runs SET status='interrupted', finished_at=? WHERE status='running'",
                      (now_iso(),))
            cur = c.execute("INSERT INTO scan_runs (trigger, started_at, status) VALUES (?,?,'running')",
                            (trigger, now_iso()))
            return cur.lastrowid

    def finish_run(self, run_id: int, status: str, report: dict | None, error: str | None = None) -> None:
        with self._conn() as c:
            c.execute("UPDATE scan_runs SET finished_at=?, status=?, report=?, error=? WHERE run_id=?",
                      (now_iso(), status, json.dumps(report) if report is not None else None, error, run_id))

    def recent_runs(self, limit: int = 10) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM scan_runs ORDER BY run_id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["report"] = json.loads(d["report"]) if d["report"] else None
            out.append(d)
        return out

    # ----- inbox -----

    def inbox(self) -> list[dict]:
        """All items with their analysis (if any), newest publication first."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT i.*, a.cache_key, a.status AS analysis_status, a.result AS analysis_result,
                          a.error AS analysis_error, a.model AS analysis_model, a.analyzed_at,
                          d.status AS doc_status, d.detail AS doc_detail, d.pdf_url AS doc_pdf_url,
                          d.page_count AS doc_page_count
                   FROM items i LEFT JOIN analyses a ON a.item_id = i.item_id
                   LEFT JOIN documents d ON d.item_id = i.item_id
                   ORDER BY COALESCE(i.published, i.first_seen) DESC"""
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["authors"] = json.loads(d["authors"] or "[]")
            d["analysis_result"] = json.loads(d["analysis_result"]) if d["analysis_result"] else None
            out.append(d)
        return out


def _is_older(incoming: str | None, stored: str | None) -> bool:
    """True if both are version labels like "v2" and the incoming one is lower."""
    a, b = re.fullmatch(r"v(\d+)", incoming or ""), re.fullmatch(r"v(\d+)", stored or "")
    return bool(a and b) and int(a.group(1)) < int(b.group(1))


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        item_id=row["item_id"], source_id=row["source_id"], source_name=row["source_name"],
        source_kind=row["source_kind"], url=row["url"], title=row["title"],
        publisher=row["publisher"] or "", authors=json.loads(row["authors"] or "[]"),
        published=row["published"], summary=row["summary"] or "", version=row["version"],
        pdf_url=row["pdf_url"],
    )
