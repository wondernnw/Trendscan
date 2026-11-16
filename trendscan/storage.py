"""SQLite storage: the evidence inbox and the Gemini result cache.

Two tables:
  items     — one row per source item. PRIMARY KEY item_id makes duplicates impossible.
  analyses  — one Gemini result per item, tagged with the cache_key it was produced for.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

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
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Store:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.executescript(SCHEMA)

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
            row = c.execute("SELECT content_hash FROM items WHERE item_id = ?", (item.item_id,)).fetchone()
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
            if row["content_hash"] == item.content_hash:
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

    # ----- inbox -----

    def inbox(self) -> list[dict]:
        """All items with their analysis (if any), newest publication first."""
        with self._conn() as c:
            rows = c.execute(
                """SELECT i.*, a.cache_key, a.status AS analysis_status, a.result AS analysis_result,
                          a.error AS analysis_error, a.model AS analysis_model, a.analyzed_at
                   FROM items i LEFT JOIN analyses a ON a.item_id = i.item_id
                   ORDER BY COALESCE(i.published, i.first_seen) DESC"""
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["authors"] = json.loads(d["authors"] or "[]")
            d["analysis_result"] = json.loads(d["analysis_result"]) if d["analysis_result"] else None
            out.append(d)
        return out


def _row_to_item(row: sqlite3.Row) -> Item:
    return Item(
        item_id=row["item_id"], source_id=row["source_id"], source_name=row["source_name"],
        source_kind=row["source_kind"], url=row["url"], title=row["title"],
        publisher=row["publisher"] or "", authors=json.loads(row["authors"] or "[]"),
        published=row["published"], summary=row["summary"] or "", version=row["version"],
        pdf_url=row["pdf_url"],
    )
