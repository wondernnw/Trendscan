"""Milestone 2: full text of papers/reports and page-level evidence. Offline tests.

Run:  .venv/bin/python -m pytest -v
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from trendscan import scan as scan_module
from trendscan.analysis import GeminiAnalyzer
from trendscan.config import load_settings
from trendscan.documents import Document, extract_pages, locate_quote, locator_on_page, paged_text
from trendscan.sources import Item
from trendscan.storage import Store

PAGES = [
    "Abstract. We study how banks use AI agents for payment operations in twelve countries.",
    "Results. Table 2 shows that error rates fell by 31 percent after agents were intro-\nduced in the back office.",
    "Limitations. The sample covers only large banks and a single year of observations.",
]


def make_pdf(pages: list[str]) -> bytes:
    """Build a minimal valid PDF with one line of text per page (no parentheses in the text)."""
    n = len(pages)
    font_id = 3 + 2 * n
    objs = ["<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{' '.join(f'{3 + 2 * i} 0 R' for i in range(n))}] /Count {n} >>"]
    for i, text in enumerate(pages):
        content = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                    f"/Resources << /Font << /F1 {font_id} 0 R >> >> >>")
        objs.append(f"<< /Length {len(content)} >>\nstream\n{content}\nendstream")
    objs.append("<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    out, offsets = b"%PDF-1.4\n", []
    for i, obj in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{off:010d} 00000 n \n".encode() for off in offsets)
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


# ---------- documents.py ----------

def test_pdf_text_is_extracted_page_by_page():
    pages = extract_pages(make_pdf(["First page about tokenisation", "Second page about agents"]))
    assert pages == ["First page about tokenisation", "Second page about agents"]


def test_paged_text_keeps_whole_pages_within_the_limit():
    text, sent = paged_text(["a" * 100, "b" * 100, "c" * 100], max_chars=250)
    assert sent == 2 and "[[page 2]]" in text and "[[page 3]]" not in text


def test_locate_quote_statuses():
    assert locate_quote("error rates fell by 31 percent after agents", 2, PAGES) == ("verified", 2)
    # hyphenated line break in the PDF text ("intro-\nduced") must not break matching
    assert locate_quote("after agents were introduced in the back office", 2, PAGES) == ("verified", 2)
    assert locate_quote("The sample covers only large banks", 1, PAGES) == ("other_page", 3)
    assert locate_quote("Profits doubled in every country studied", 2, PAGES) == ("not_found", None)
    assert locate_quote("by 31", 2, PAGES) == ("too_short", None)
    # Gemini dropped a few words: close, but not word for word
    assert locate_quote("Table 2 shows that error rates fell by 31 percent in the back office", 2, PAGES) == ("near_match", 2)


def test_pdf_link_discovery_rules(monkeypatch):
    from trendscan import documents
    page = "https://example.org/pubs/paper-12-ready-to-run-tests"

    def discover(html):
        monkeypatch.setattr(documents, "_get", lambda url, max_bytes, timeout_s: html.encode())
        return documents.discover_pdf_url(page)

    assert discover('<a href="/pubs/paper-12-ready-to-run-tests.pdf">PDF</a> <a href="/other.pdf">x</a>') == page + ".pdf"
    # the only same-host PDF on the page is accepted even if its name differs slightly
    assert discover('<a href="/pubs/paper-12-ready-run-tests.pdf">PDF</a>') == "https://example.org/pubs/paper-12-ready-run-tests.pdf"
    # several candidates and no exact match: take none rather than guess
    assert discover('<a href="/a.pdf">a</a> <a href="/b.pdf">b</a>') is None


def test_figure_and_table_labels_are_checked_on_the_page():
    assert locator_on_page("table", "Table 2", PAGES[1]) is True
    assert locator_on_page("table", "Table 7", PAGES[1]) is False
    assert locator_on_page("section", "Section 4 Results", PAGES[1]) is None


# ---------- scan with full text ----------

FULLTEXT_JSON = {
    "what_happened": "A study of AI agents in bank payment operations.",
    "assessment": "Relevant, early evidence.",
    "trend_matches": [{"profile_id": "agentic-ai", "confidence": "high", "explanation": "AI agents."}],
    "emerging_signal": {"is_signal": False, "signal_name": "", "why_watch": "", "evidence_needed": ""},
    "central_bank_relevance": "Payment operations.",
    "next_step": "Read the paper.",
    "evidence_gaps": [],
    "evidence": [
        {"claim": "Errors fell.", "quote": "error rates fell by 31 percent after agents were introduced",
         "page": 2, "locator_type": "table", "locator": "Table 2"},
        {"claim": "Only large banks.", "quote": "The sample covers only large banks", "page": 2,
         "locator_type": "none", "locator": ""},
        {"claim": "Invented.", "quote": "Profits doubled in every country studied", "page": 3,
         "locator_type": "none", "locator": ""},
    ],
}
ABSTRACT_JSON = {k: v for k, v in FULLTEXT_JSON.items() if k != "evidence"} | {
    "supporting_quote": "Banks are piloting AI agents"}


class FakeInteractions:
    """Answers with full-text JSON when asked for the full-text schema, else abstract JSON."""
    def __init__(self):
        self.fulltext_calls = 0
        self.abstract_calls = 0

    def create(self, **kwargs):
        if "evidence" in kwargs["response_format"]["schema"]["properties"]:
            self.fulltext_calls += 1
            assert "[[page 2]]" in kwargs["input"]
            return SimpleNamespace(output_text=json.dumps(FULLTEXT_JSON))
        self.abstract_calls += 1
        return SimpleNamespace(output_text=json.dumps(ABSTRACT_JSON))


def paper(n: int) -> Item:
    return Item(item_id=f"paper:{n}", source_id="test", source_name="Test papers", source_kind="paper",
                url=f"https://example.org/p{n}", title=f"Paper {n}", publisher="Example",
                authors=["A. Author"], published=f"2026-11-1{n}T00:00:00+00:00",
                summary="Banks are piloting AI agents for payments.", pdf_url=f"https://example.org/p{n}.pdf")


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = replace(load_settings(), db_path=tmp_path / "t.db", seconds_between_calls=0)
    state = {"items": [paper(1)], "doc": Document("ok", "", "https://example.org/p1.pdf", PAGES), "downloads": 0}
    monkeypatch.setattr(scan_module, "load_sources",
                        lambda _: [{"id": "test", "name": "Test papers", "type": "rss", "url": "x"}])
    monkeypatch.setattr(scan_module, "fetch_source", lambda src: list(state["items"]))

    def fake_fetch_document(item, max_mb, timeout_s):
        state["downloads"] += 1
        return state["doc"]
    monkeypatch.setattr(scan_module, "fetch_document", fake_fetch_document)
    return settings, Store(settings.db_path), state


def run(settings, store, fake):
    analyzer = GeminiAnalyzer(None, settings.gemini_model, client=SimpleNamespace(interactions=fake))
    return scan_module.run_scan(settings, store, analyzer=analyzer, sleep=lambda s: None)


def test_paper_gets_page_level_evidence_and_citations_are_checked(env):
    settings, store, _ = env
    fake = FakeInteractions()
    r = run(settings, store, fake)
    assert (r.docs_fetched, r.fulltext_calls, fake.abstract_calls) == (1, 1, 0)

    result = store.get_analysis("paper:1")["result"]
    ev = result["evidence"]
    assert (ev[0]["status"], ev[0]["page"], ev[0]["locator_found"]) == ("verified", 2, True)
    assert (ev[1]["status"], ev[1]["cited_page"], ev[1]["page"]) == ("other_page", 2, 3)   # page corrected
    assert ev[2]["status"] == "not_found"                                                   # invented quote caught
    assert result["quote_verified"] is False
    assert result["coverage"] == {"mode": "full_text", "pages_analysed": 3, "pages_total": 3}


def test_rescan_downloads_no_pdf_again_and_makes_no_gemini_call(env):
    settings, store, state = env
    fake = FakeInteractions()
    run(settings, store, fake)
    r2 = run(settings, store, fake)
    assert state["downloads"] == 1 and fake.fulltext_calls == 1
    assert (r2.gemini_calls, r2.cache_hits, r2.docs_fetched) == (0, 1, 0)


def test_unreadable_pdf_falls_back_to_abstract_and_is_flagged(env):
    settings, store, state = env
    state["doc"] = Document("no_text", "PDF has no text layer (probably scanned)", "https://example.org/p1.pdf")
    fake = FakeInteractions()
    r = run(settings, store, fake)
    assert (r.docs_failed, fake.abstract_calls, fake.fulltext_calls) == (1, 1, 0)
    row = store.inbox()[0]
    assert row["doc_status"] == "no_text" and "supporting_quote" in row["analysis_result"]
    run(settings, store, fake)
    assert state["downloads"] == 1 and fake.abstract_calls == 1     # final answer: no retry, no new call


def test_inaccessible_pdf_is_retried_then_abstract_is_used(env):
    settings, store, state = env
    state["doc"] = Document("inaccessible", "PDF not reachable (download too slow)", "https://example.org/p1.pdf")
    fake = FakeInteractions()
    for _ in range(2):
        r = run(settings, store, fake)
        assert r.pending == 1 and r.gemini_calls == 0       # waiting: no quota spent on the abstract yet
    r3 = run(settings, store, fake)                         # third failed attempt is final
    assert state["downloads"] == 3 and fake.abstract_calls == 1 and r3.pending == 0
    run(settings, store, fake)
    assert state["downloads"] == 3                          # no fourth download


def test_full_text_cap_spreads_documents_over_scans(env):
    settings, store, state = env
    settings = replace(settings, max_fulltext_per_scan=2)
    state["items"] = [paper(1), paper(2), paper(3)]
    fake = FakeInteractions()
    r1 = run(settings, store, fake)
    assert (r1.docs_fetched, r1.fulltext_calls, r1.docs_waiting, r1.pending) == (2, 2, 1, 1)
    assert len(store.inbox()) == 3                          # the waiting paper is still visible
    r2 = run(settings, store, fake)
    assert (r2.docs_fetched, r2.fulltext_calls, r2.cache_hits, r2.pending) == (1, 1, 2, 0)


def test_untried_documents_are_downloaded_before_retries(env):
    settings, store, state = env
    settings = replace(settings, max_fulltext_per_scan=1)
    state["doc"] = Document("inaccessible", "PDF not reachable (Timeout)", None)
    run(settings, store, FakeInteractions())                 # paper 1 fails once
    order = []
    state["items"] = [paper(1), paper(2)]                    # paper 2 is newer, never tried
    scan_module.fetch_document = lambda item, mb, t: (order.append(item.item_id), state["doc"])[1]
    run(settings, store, FakeInteractions())
    state["items"] = [paper(1), paper(2), paper(3)]
    run(settings, store, FakeInteractions())
    assert order == ["paper:2", "paper:3"]                   # new documents first, the retry waits


def test_new_pdf_version_is_fetched_and_reanalysed(env):
    settings, store, state = env
    fake = FakeInteractions()
    run(settings, store, fake)
    v2 = paper(1); v2.pdf_url = "https://example.org/p1v2.pdf"; v2.version = "v2"
    state["items"] = [v2]
    state["doc"] = Document("ok", "", v2.pdf_url, PAGES + ["Appendix. New robustness checks were added in this version."])
    r = run(settings, store, fake)
    assert state["downloads"] == 2 and r.fulltext_calls == 1 and store.count_items() == 1
