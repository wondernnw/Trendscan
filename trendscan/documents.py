"""Full-text retrieval for papers and reports: find the PDF, download it, extract text per page.

`fetch_document` never raises for an unreachable or unreadable PDF. It returns a Document
whose `status` says what happened, so the scan can flag the problem and carry on.
"""

from __future__ import annotations

import io
import re
import time
import unicodedata
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from urllib.parse import urljoin, urlsplit

import requests

from .sources import USER_AGENT, Item, canonical_url

MAX_PAGES = 300            # never extract more pages than this from one PDF
MIN_TEXT_CHARS = 500       # less text than this in the whole PDF -> treat as scanned / no text layer


class SlowDownload(requests.Timeout):
    """The server answered but delivered too slowly to finish within the time limit."""


@dataclass
class Document:
    status: str                       # ok | inaccessible | no_pdf | too_large | no_text | unreadable
    detail: str = ""                  # short human-readable reason when status != ok
    pdf_url: str | None = None
    pages: list[str] = field(default_factory=list)   # pages[0] is PDF page 1


class _TooLarge(Exception):
    pass


# ---------- finding the PDF ----------

def discover_pdf_url(page_url: str, timeout_s: float = 120) -> str | None:
    """Find the PDF that belongs to a publication page.

    Deliberately strict, because attaching the wrong document would produce confident
    but false page citations. Accepted, in this order:
      1. the page's own address + ".pdf"
      2. the page's `citation_pdf_url` meta tag
      3. the only PDF link on the page that is on the same host (BIS sometimes shortens
         the file name, e.g. "ready-run" for the page "ready-to-run")
    If the page links several PDFs and none matches 1 or 2, we take none.
    """
    if page_url.lower().endswith(".pdf"):
        return page_url
    html = _get(page_url, max_bytes=5_000_000, timeout_s=timeout_s).decode("utf-8", errors="replace")
    expected = canonical_url(page_url).rstrip("/") + ".pdf"
    links = {canonical_url(urljoin(page_url, href)) for href in re.findall(r'href="([^"]+\.pdf)"', html, flags=re.I)}
    if expected in links:
        return expected
    meta = re.search(r'<meta[^>]+name="citation_pdf_url"[^>]+content="([^"]+)"', html, flags=re.I)
    if meta:
        return urljoin(page_url, meta.group(1))
    same_host = [u for u in links if urlsplit(u).netloc == urlsplit(expected).netloc]
    return same_host[0] if len(same_host) == 1 else None


# ---------- downloading ----------

def _get(url: str, max_bytes: int, timeout_s: float) -> bytes:
    """GET with a size cap, a time limit, and one retry for connection problems / 5xx errors.

    A download that is merely too slow (SlowDownload) is not retried straight away:
    a second attempt would most likely be just as slow and double the waiting time.
    """
    last: Exception | None = None
    started = time.monotonic()
    for attempt in range(2):
        remaining = timeout_s - (time.monotonic() - started)   # the limit covers both attempts together
        if attempt:
            if remaining < 10:
                break
            time.sleep(3)
        try:
            return _get_once(url, max_bytes, remaining)
        except SlowDownload:
            raise
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = exc
        except requests.HTTPError as exc:
            if exc.response is not None and exc.response.status_code < 500:
                raise                      # 4xx will not get better by retrying
            last = exc
    raise last  # type: ignore[misc]


def _get_once(url: str, max_bytes: int, timeout_s: float) -> bytes:
    started = time.monotonic()
    with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=(10, 30), stream=True) as resp:
        resp.raise_for_status()
        chunks, size = [], 0
        for chunk in resp.iter_content(64 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise _TooLarge()
            if time.monotonic() - started > timeout_s:
                total = int(resp.headers.get("content-length") or 0)
                of = f" of {total / 1e6:.1f}" if total else ""
                raise SlowDownload(f"download too slow: {size / 1e6:.1f}{of} MB after {timeout_s:.0f} s")
            chunks.append(chunk)
        return b"".join(chunks)


def pdf_signature(pdf_url: str) -> str | None:
    """A cheap change stamp for a PDF on the server (no download): etag, last-modified, size.
    None if the server cannot be asked or gives none of these."""
    try:
        resp = requests.head(pdf_url, headers={"User-Agent": USER_AGENT}, timeout=(10, 15), allow_redirects=True)
        resp.raise_for_status()
    except requests.RequestException:
        return None
    parts = [resp.headers.get(h, "") for h in ("etag", "last-modified", "content-length")]
    return "|".join(parts) if any(parts) else None


# ---------- text extraction ----------

def extract_pages(pdf_bytes: bytes) -> list[str]:
    """Return the text of each page. Index 0 is PDF page 1 (what a viewer opens with #page=1)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    pages = []
    for page in reader.pages[:MAX_PAGES]:
        try:
            text = page.extract_text() or ""
        except Exception:
            text = ""                      # one broken page should not lose the whole document
        pages.append(text.replace("\x00", "").strip())
    return pages


def fetch_document(item: Item, max_mb: int = 25, timeout_s: float = 120) -> Document:
    """`timeout_s` is the time limit for each download (source page, then PDF)."""
    try:
        pdf_url = item.pdf_url or discover_pdf_url(item.url, timeout_s)
    except _TooLarge:
        return Document("inaccessible", "source page too large to read")
    except requests.RequestException as exc:
        return Document("inaccessible", f"source page not reachable ({_short(exc)})")
    if not pdf_url:
        return Document("no_pdf", "no PDF link found on the source page")

    try:
        data = _get(pdf_url, max_bytes=max_mb * 1_000_000, timeout_s=timeout_s)
    except _TooLarge:
        return Document("too_large", f"PDF is larger than {max_mb} MB", pdf_url)
    except requests.RequestException as exc:
        return Document("inaccessible", f"PDF not reachable ({_short(exc)})", pdf_url)

    if b"%PDF" not in data[:1024]:
        return Document("unreadable", "the link did not return a PDF file", pdf_url)
    try:
        pages = extract_pages(data)
    except Exception as exc:
        return Document("unreadable", f"PDF could not be parsed ({type(exc).__name__})", pdf_url)
    if sum(len(p) for p in pages) < MIN_TEXT_CHARS:
        return Document("no_text", "PDF has no text layer (probably scanned)", pdf_url)
    return Document("ok", "", pdf_url, pages)


def _short(exc: Exception) -> str:
    if isinstance(exc, SlowDownload):
        return str(exc)
    resp = getattr(exc, "response", None)
    if resp is not None:
        return f"HTTP {resp.status_code}"
    return type(exc).__name__


# ---------- text for the prompt ----------

def paged_text(pages: list[str], max_chars: int) -> tuple[str, int]:
    """Join whole pages with [[page N]] markers until `max_chars` is reached.

    Returns (text, number_of_pages_included). Pages are never cut in half, except
    when page 1 alone is longer than the limit.
    """
    parts, total = [], 0
    for n, page in enumerate(pages, start=1):
        chunk = f"[[page {n}]]\n{page}\n"
        if total + len(chunk) > max_chars:
            if n == 1:
                parts.append(chunk[:max_chars])
            break
        parts.append(chunk)
        total += len(chunk)
    return "\n".join(parts), len(parts)


# ---------- checking citations against the page text ----------

def alnum(text: str) -> str:
    """Letters and digits only, lower-cased. Makes quote matching robust against PDF
    line breaks, hyphenation, ligatures and typographic quotes."""
    text = unicodedata.normalize("NFKC", text).lower()
    return "".join(ch for ch in text if ch.isalnum())


def locate_quote(quote: str, cited_page: int, pages: list[str]) -> tuple[str, int | None]:
    """Check where a quote really appears.

    Returns (status, page):
      verified     — found on the cited page
      other_page   — found, but on a different page (returned)
      spans_pages  — found across the break between the returned page and the next
      near_match   — almost word for word on the returned page, with small differences
                     (footnote marks inside the sentence, omitted words, a rebuilt table row)
      not_found    — not in the analysed pages
      too_short    — fewer than 20 letters/digits: too little to verify
    """
    # A quote with "…" is checked as fragments that must all be on the same page.
    fragments = [f for f in (alnum(p) for p in re.split(r"\[?(?:\.\.\.|…)\]?", quote)) if f]
    if sum(len(f) for f in fragments) < 20:
        return "too_short", None
    norm = [alnum(p) for p in pages]

    def on(text: str) -> bool:
        return all(f in text for f in fragments)

    if 1 <= cited_page <= len(norm) and on(norm[cited_page - 1]):
        return "verified", cited_page
    for n, text in enumerate(norm, start=1):
        if on(text):
            return "other_page", n
    for n in range(1, len(norm)):
        if on(norm[n - 1] + norm[n]):
            return "spans_pages", n
    quote_text = "".join(fragments)
    order = ([cited_page] if 1 <= cited_page <= len(norm) else []) + [
        n for n in range(1, len(norm) + 1) if n != cited_page]
    for n in order:
        if _overlap(quote_text, norm[n - 1]) >= NEAR_MATCH:
            return "near_match", n
    return "not_found", None


NEAR_MATCH = 0.85      # share of the quote that must appear on the page, in order
MIN_BLOCK = 8          # ignore shared runs shorter than this many characters (chance matches)


def _overlap(quote: str, page: str) -> float:
    """Share of `quote` found on `page` as in-order runs of at least MIN_BLOCK characters."""
    blocks = SequenceMatcher(None, quote, page, autojunk=False).get_matching_blocks()
    return sum(b.size for b in blocks if b.size >= MIN_BLOCK) / len(quote)


def locator_on_page(locator_type: str, locator: str, page_text: str) -> bool | None:
    """For "Figure 3" / "Table 2": does that label appear on the page? None = not checkable."""
    number = re.search(r"\d+", locator or "")
    if locator_type not in ("figure", "table") or not number:
        return None
    prefixes = ("figure", "fig", "graph", "chart") if locator_type == "figure" else ("table", "tab")
    text = alnum(page_text)
    return any(p + number.group() in text for p in prefixes)
