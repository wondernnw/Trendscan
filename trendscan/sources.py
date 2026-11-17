"""Fetch public feeds and normalize every entry into one `Item` shape.

Each Item carries its own citation metadata (URL, title, publisher, authors,
publication date) so a finding can always be traced back to the original source.
"""

from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from time import struct_time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import feedparser
import requests

USER_AGENT = "TrendScan/0.1 (technology trend research prototype)"
TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|mc_cid$|mc_eid$)")
ARXIV_ID = re.compile(r"arxiv\.org/abs/([^v/]+(?:/\d+)?)(v(\d+))?$")


@dataclass
class Item:
    item_id: str              # stable dedup key, e.g. "arxiv:2611.18553" or "mit-tr-ai:<hash>"
    source_id: str
    source_name: str
    source_kind: str          # news | paper | report
    url: str                  # original source URL (canonicalized)
    title: str
    publisher: str
    authors: list[str] = field(default_factory=list)
    published: str | None = None   # ISO 8601 (UTC) or None if the feed gave no date
    summary: str = ""              # abstract / teaser text from the feed (plain text)
    version: str | None = None     # e.g. "v2" for arXiv — basis for later revision tracking
    pdf_url: str | None = None

    @property
    def content_hash(self) -> str:
        """Fingerprint of the content Gemini sees. Changes if the source text changes."""
        basis = "\n".join([self.title, self.summary, self.version or ""])
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


# ---------- helpers ----------

def canonical_url(url: str) -> str:
    """Normalize a URL so the same article always maps to the same string."""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query) if not TRACKING_PARAMS.match(k)]
    scheme = "https" if parts.scheme in ("http", "https") else parts.scheme
    return urlunsplit((scheme, parts.netloc.lower(), parts.path, urlencode(query), ""))


def strip_html(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def to_iso(parsed: struct_time | None) -> str | None:
    if not parsed:
        return None
    return datetime(*parsed[:6], tzinfo=timezone.utc).isoformat()


def short_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


# ---------- normalizers (one per source type) ----------

def normalize_arxiv(entry, source: dict) -> Item:
    abs_url = entry.get("link") or entry.get("id", "")
    m = ARXIV_ID.search(abs_url)
    arxiv_id = m.group(1) if m else short_hash(abs_url)
    version = f"v{m.group(3)}" if m and m.group(3) else None
    pdf = next((l.get("href") for l in entry.get("links", []) if l.get("type") == "application/pdf"), None)
    return Item(
        item_id=f"arxiv:{arxiv_id}",
        source_id=source["id"],
        source_name=source["name"],
        source_kind=source.get("kind", "paper"),
        url=f"https://arxiv.org/abs/{arxiv_id}",   # version-less URL always points to latest
        title=strip_html(entry.get("title", "")),
        publisher="arXiv",
        authors=[a.get("name", "") for a in entry.get("authors", []) if a.get("name")],
        published=to_iso(entry.get("published_parsed")),
        summary=strip_html(entry.get("summary", "")),
        version=version,
        pdf_url=pdf,
    )


def normalize_rss(entry, source: dict, feed_title: str) -> Item:
    url = canonical_url(entry.get("link", ""))
    guid = entry.get("id") or url
    authors = [a.get("name", "") for a in entry.get("authors", []) if a.get("name")]
    if not authors and entry.get("author"):
        authors = [entry["author"]]
    pdf = next((l.get("href") for l in entry.get("links", [])
                if (l.get("type") == "application/pdf") or l.get("href", "").endswith(".pdf")), None)
    return Item(
        item_id=f"{source['id']}:{short_hash(guid)}",
        source_id=source["id"],
        source_name=source["name"],
        source_kind=source.get("kind", "news"),
        url=url,
        title=strip_html(entry.get("title", "")),
        publisher=feed_title or source["name"],
        authors=authors,
        published=to_iso(entry.get("published_parsed") or entry.get("updated_parsed")),
        summary=strip_html(entry.get("summary", "")),
        pdf_url=pdf,
    )


# ---------- fetching ----------

def fetch_source(source: dict, timeout: int = 30) -> list[Item]:
    """Fetch one source and return normalized Items. Raises on network/HTTP errors."""
    resp = requests.get(source["url"], timeout=timeout, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    feed = feedparser.parse(resp.content)
    entries = feed.entries[: source.get("max_items", 10)]
    if source["type"] == "arxiv":
        items = [normalize_arxiv(e, source) for e in entries]
    else:
        publisher = source.get("publisher") or _publisher_from_feed(feed.feed.get("title", ""))
        items = [normalize_rss(e, source, publisher) for e in entries]
    return [i for i in items if i.url and i.title]


def fetch_arxiv_latest(arxiv_ids: list[str], source: dict, timeout: int = 30) -> list[Item]:
    """Ask the arXiv API for the current version of known papers (one request for up to 50 ids)."""
    resp = requests.get("https://export.arxiv.org/api/query",
                        params={"id_list": ",".join(arxiv_ids), "max_results": len(arxiv_ids)},
                        timeout=timeout, headers={"User-Agent": USER_AGENT})
    resp.raise_for_status()
    entries = feedparser.parse(resp.content).entries
    return [normalize_arxiv(e, source) for e in entries if e.get("title")]


def _publisher_from_feed(title: str) -> str:
    # "Artificial intelligence – MIT Technology Review" -> "MIT Technology Review"
    for sep in (" – ", " - ", " | "):
        if sep in title:
            return title.split(sep)[-1].strip()
    return title
