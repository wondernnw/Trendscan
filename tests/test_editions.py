"""Editions: linking a new edition of a recurring publication to the previous one. Offline tests.

Run:  .venv/bin/python -m pytest tests/test_editions.py -v
"""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from trendscan import documents as documents_module
from trendscan import scan as scan_module
from trendscan.analysis import GeminiAnalyzer
from trendscan.config import load_settings
from trendscan.documents import Document, discover_pdf_url
from trendscan.editions import edition_parts, find_edition_links
from trendscan.sources import Item, canonical_url
from trendscan.storage import Store

# ---------- the linking rule ----------

@pytest.mark.parametrize("title, key, marker", [
    ("BIS Annual Economic Report 2026", "bis annual economic report", "2026"),
    ("Economic Bulletin Issue 7, 2026", "economic bulletin", "issue 7 2026"),
    ("Financial Stability Review, November 2026", "financial stability review", "november 2026"),
    ("Global Monitoring Report on NBFI 2025/26", "global monitoring report on nbfi", "2025/26"),
    ("Methodology document – 2025 update", "methodology document", "2025"),
    ("Survey on AI in banking, 2nd edition", "survey on ai in banking", "2nd edition"),
    ("What AI may mean for banks", "what ai may mean for banks", ""),        # "may" is not a date
    ("Cross-border payments: a G20 view", "cross border payments a g20 view", ""),   # G20 is not an edition
])
def test_title_is_split_into_series_key_and_edition_marker(title, key, marker):
    assert edition_parts(title) == (key, marker)


def row(item_id, title, published, url, publisher="BIS", kind="report"):
    return {"item_id": item_id, "title": title, "published": published, "first_seen": published,
            "url": url, "publisher": publisher, "source_kind": kind}


def test_editions_of_a_series_are_chained_oldest_to_newest():
    rows = [row("a26", "BIS Annual Economic Report 2026", "2026-06-28", "https://www.bis.org/publications/aer-2026"),
            row("a24", "BIS Annual Economic Report 2024", "2024-06-30", "https://www.bis.org/publications/aer-2024"),
            row("a25", "BIS Annual Economic Report 2025", "2025-06-29", "https://www.bis.org/publications/aer-2025")]
    links = find_edition_links(rows)
    assert {(l["new_item_id"], l["old_item_id"]) for l in links} == {("a25", "a24"), ("a26", "a25")}
    assert all("addresses follow the same pattern" in l["reason"] for l in links)
    assert "2025 → 2026" in next(l for l in links if l["new_item_id"] == "a26")["reason"]


@pytest.mark.parametrize("second", [
    row("x2", "BIS Annual Economic Report 2025", "2025-06-29", "https://x/2", publisher="ECB"),  # other publisher
    row("x2", "BIS Annual Economic Report 2026", "2026-07-01", "https://x/2"),                  # same edition twice
    row("x2", "BIS Annual Economic Report 2025", "2025-06-29", "https://x/2", kind="paper"),     # not a report
    row("x2", "BIS Quarterly Review 2025", "2025-06-29", "https://x/2"),                        # other series
])
def test_no_link_without_a_real_series_step(second):
    first = row("x1", "BIS Annual Economic Report 2026", "2026-06-28", "https://x/1")
    assert find_edition_links([first, second]) == []


def test_identical_titles_without_edition_marker_are_not_linked():
    rows = [row("p1", "Peer Review of Türkiye", "2025-01-01", "https://x/1"),
            row("p2", "Peer Review of Türkiye", "2026-01-01", "https://x/2")]
    assert find_edition_links(rows) == []


def test_link_without_matching_address_pattern_says_so():
    rows = [row("f25", "Consolidated progress report for 2025", "2025-10-09", "https://fsb.org/2025/10/report-a/"),
            row("f26", "Consolidated progress report for 2026", "2026-11-16", "https://fsb.org/2026/11/other-slug/")]
    [link] = find_edition_links(rows)
    assert "addresses follow" not in link["reason"]


# ---------- scan with two editions ----------

OLD_ED = ["Overview. Inflation has receded but remains above target in many economies.",
          "Chapter III. Tokenisation could improve cross-border payments if settlement uses central bank money."]
NEW_ED = ["Overview. Growth is resilient while public debt keeps rising in most economies.",
          "Chapter III. Artificial intelligence is changing how central banks analyse economic data."]

BASE = {
    "what_happened": "A report.", "assessment": "Relevant.", "trend_matches": [],
    "emerging_signal": {"is_signal": False, "signal_name": "", "why_watch": "", "evidence_needed": ""},
    "central_bank_relevance": "High.", "next_step": "Read.", "evidence_gaps": [],
}
EDITION_JSON = {
    "summary": "The focus moved from tokenisation to artificial intelligence.",
    "continuing_themes": ["macroeconomic outlook"],
    "changes": [
        {"description": "AI chapter replaces tokenisation chapter.", "kind": "modified", "significance": "high",
         "old_page": 2, "old_quote": "Tokenisation could improve cross-border payments if settlement uses central bank money.",
         "new_page": 1, "new_quote": "Artificial intelligence is changing how central banks analyse economic data."},
        {"description": "Invented.", "kind": "added", "significance": "low", "old_page": 0, "old_quote": "",
         "new_page": 1, "new_quote": "Quantum computers have already broken all payment encryption."},
    ],
    "technology_relevance": "AI moves into focus; tokenisation drops out of the shown pages.",
    "effect_on_assessment": "Yes, revisit the tokenisation assessment.",
}


class Fake:
    """Fake Gemini answering by schema; counts edition comparisons separately."""
    def __init__(self, fail_editions: Exception | None = None):
        self.calls = {"edition": 0, "other": 0}
        self.fail_editions = fail_editions
        self.last_prompt = ""

    def create(self, **kwargs):
        props = kwargs["response_format"]["schema"]["properties"]
        if "continuing_themes" in props:
            self.calls["edition"] += 1
            self.last_prompt = kwargs["input"]
            if self.fail_editions:
                raise self.fail_editions
            return SimpleNamespace(output_text=json.dumps(EDITION_JSON))
        self.calls["other"] += 1
        extra = {"evidence": []} if "evidence" in props else {"supporting_quote": "x"}
        return SimpleNamespace(output_text=json.dumps(BASE | extra))


class QuotaError(Exception):
    code = 429


def report_item(year: int) -> Item:
    return Item(item_id=f"bis-aer:{year}", source_id="bis-aer", source_name="BIS AER", source_kind="report",
                url=f"https://www.bis.org/publications/aer-{year}", title=f"BIS Annual Economic Report {year}",
                publisher="Bank for International Settlements (BIS)", published=f"{year}-06-28T00:00:00+00:00",
                summary=f"The BIS Annual Economic Report {year}.")


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = replace(load_settings(), db_path=tmp_path / "t.db", seconds_between_calls=0)
    state = {"feed": [report_item(2026), report_item(2025)],
             "docs": {2025: Document("ok", "", "https://www.bis.org/publications/aer-2025.pdf", OLD_ED),
                      2026: Document("ok", "", "https://www.bis.org/publications/aer-2026_0.pdf", NEW_ED)}}
    monkeypatch.setattr(scan_module, "load_sources",
                        lambda _: [{"id": "bis-aer", "name": "BIS AER", "type": "rss", "url": "x"}])
    monkeypatch.setattr(scan_module, "fetch_source", lambda src: list(state["feed"]))
    monkeypatch.setattr(scan_module, "fetch_document",
                        lambda item, max_mb, timeout_s: state["docs"][int(item.item_id[-4:])])
    return settings, Store(settings.db_path), state


def run(settings, store, fake, **overrides):
    analyzer = GeminiAnalyzer(None, settings.gemini_model, client=SimpleNamespace(interactions=fake))
    return scan_module.run_scan(replace(settings, **overrides), store, analyzer=analyzer, sleep=lambda s: None)


def test_new_edition_is_linked_compared_and_quotes_checked_in_each_edition(env):
    settings, store, _ = env
    fake = Fake()
    r = run(settings, store, fake)
    assert (r.editions_linked, r.edition_comparisons, r.editions_pending) == (1, 1, 0)
    assert fake.calls == {"edition": 1, "other": 2}       # 2 full-text analyses + 1 comparison

    [link] = store.get_edition_links()
    assert (link["new_item_id"], link["old_item_id"], link["status"]) == ("bis-aer:2026", "bis-aer:2025", "done")
    assert "addresses follow the same pattern" in link["reason"]
    real, invented = link["result"]["changes"]
    assert (real["old_status"], real["old_page"]) == ("verified", 2)       # found in the PREVIOUS edition
    assert (real["new_status"], real["new_page"]) == ("other_page", 2)     # Gemini said p. 1; corrected to p. 2
    assert invented["new_status"] == "not_found"
    assert link["stats"] == {"old_pages_sent": 2, "old_pages": 2, "new_pages_sent": 2, "new_pages": 2}
    # Both editions were sent, each labelled, as untrusted data.
    assert "PREVIOUS EDITION" in fake.last_prompt and "Tokenisation could improve" in fake.last_prompt
    assert "NEW EDITION" in fake.last_prompt and "Artificial intelligence is changing" in fake.last_prompt


def test_rescan_reuses_the_comparison(env):
    settings, store, _ = env
    fake = Fake()
    run(settings, store, fake)
    r2 = run(settings, store, fake)
    assert r2.edition_comparisons == 0 and r2.gemini_calls == 0
    assert fake.calls["edition"] == 1


def test_each_edition_gets_half_the_text_budget(env):
    settings, store, state = env
    long_page = "Lorem ipsum central banking text. " * 60          # ~2,000 characters per page
    state["docs"][2025] = Document("ok", "", "https://x/old.pdf", [long_page] * 10)
    state["docs"][2026] = Document("ok", "", "https://x/new.pdf", [long_page] * 10)
    run(settings, store, Fake(), max_doc_chars=8_000)
    stats = store.get_edition_links()[0]["stats"]
    assert stats["old_pages_sent"] == stats["new_pages_sent"] == 1 and stats["old_pages"] == 10


def test_comparison_waits_until_both_pdfs_are_read(env):
    settings, store, _ = env
    fake = Fake()
    r1 = run(settings, store, fake, max_fulltext_per_scan=1)    # only one PDF fits into this scan
    [link] = store.get_edition_links()
    assert link["status"] == "waiting" and "has not been read yet" in link["error"]
    assert r1.editions_pending == 1 and fake.calls["edition"] == 0

    r2 = run(settings, store, fake, max_fulltext_per_scan=1)    # second PDF arrives
    assert r2.edition_comparisons == 1
    assert store.get_edition_links()[0]["status"] == "done"


def test_unreadable_pdf_is_flagged_without_a_gemini_call(env):
    settings, store, state = env
    state["docs"][2025] = Document("too_large", "PDF is 31 MB (limit 25 MB)", "https://x/old.pdf", [])
    fake = Fake()
    run(settings, store, fake)
    [link] = store.get_edition_links()
    assert link["status"] == "unavailable"
    assert "previous edition: too_large (PDF is 31 MB" in link["error"]
    assert fake.calls["edition"] == 0


def test_comparison_is_pending_when_gemini_is_unavailable_and_items_stay_visible(env):
    settings, store, _ = env
    r1 = run(settings, store, Fake(fail_editions=QuotaError("quota")))
    assert r1.editions_pending == 1 and "429" in r1.gemini_stopped
    assert store.get_edition_links()[0]["status"] == "pending"
    assert len(store.inbox()) == 2

    fake = Fake()
    r2 = run(settings, store, fake)                            # quota is back
    assert r2.edition_comparisons == 1 and fake.calls["other"] == 0   # analyses were reused
    assert store.get_edition_links()[0]["status"] == "done"


# ---------- sources and PDF discovery ----------

def test_pdf_labelled_full_text_is_chosen_among_several(monkeypatch):
    html = ('<a href="https://www.fsb.org/uploads/P091025-2.pdf"><img alt=""></a> Methodology 2025 update '
            '<a href="https://www.fsb.org/uploads/P161126.pdf">PDF full text <span>(787 KB)</span></a>')
    monkeypatch.setattr(documents_module, "_get", lambda url, max_bytes, timeout_s: html.encode())
    assert discover_pdf_url("https://www.fsb.org/2026/11/progress-report/") == "https://www.fsb.org/uploads/P161126.pdf"


def test_several_unlabelled_pdfs_are_still_refused(monkeypatch):
    html = '<a href="https://x.org/a.pdf">Annex</a> <a href="https://x.org/b.pdf">Slides</a>'
    monkeypatch.setattr(documents_module, "_get", lambda url, max_bytes, timeout_s: html.encode())
    assert discover_pdf_url("https://x.org/report/") is None


def test_double_slash_in_address_is_collapsed():
    assert canonical_url("https://www.ecb.europa.eu//press/pub/x.html") == "https://www.ecb.europa.eu/press/pub/x.html"


def test_pages_without_pdf_do_not_use_up_the_download_limit(env):
    settings, store, state = env
    for n in range(3):                       # newer web articles: checked first, have no PDF
        state["feed"].append(replace(report_item(2026), item_id=f"ecb:{n}000", title=f"Web article {n}",
                                     url=f"https://ecb.example/{n}", published="2026-11-10T00:00:00+00:00"))
        state["docs"][int(f"{n}000")] = Document("no_pdf", "no PDF link found on the source page", None, [])
    r = run(settings, store, Fake(), max_fulltext_per_scan=2)
    assert r.docs_fetched == 2               # both report PDFs read despite 3 pages without PDF
    assert r.edition_comparisons == 1
