"""Milestone 3: version tracking. Offline tests.

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
from trendscan.documents import Document
from trendscan.sources import Item
from trendscan.storage import Store
from trendscan.versions import diff_versions, sentences

V1 = [
    "Abstract. We study how banks use AI agents for payment operations in twelve countries.",
    "Results. Error rates fell by 31 percent after agents were introduced in the back office. "
    "Costs were unchanged in most banks.",
    "Limitations. The sample covers only large banks and a single year of observations.",
]
V2 = [
    "Abstract. We study how banks use AI agents for payment operations in twelve countries.",
    "Results. Error rates fell by 13 percent after agents were introduced in the back office. "
    "Costs were unchanged in most banks.",
    "Robustness. The effect disappears when the two largest banks are excluded from the sample.",
    "Limitations. The sample covers only large banks and a single year of observations.",
]


# ---------- the local comparison ----------

def test_sentences_keep_their_page_numbers():
    assert [page for page, _ in sentences(V1)] == [1, 1, 2, 2, 2, 3, 3]


def test_diff_finds_a_changed_number_and_an_added_passage():
    hunks, stats = diff_versions(V1, V2)
    assert [(h.kind, h.old_page, h.new_page) for h in hunks] == [("modified", 2, 2), ("added", 3, 3)]
    assert "31 percent" in hunks[0].old_text and "13 percent" in hunks[0].new_text
    assert "two largest banks" in hunks[1].new_text
    assert (stats["old_pages"], stats["new_pages"], stats["changes"]) == (3, 4, 2)


def test_only_the_changed_wording_is_listed():
    hunks, _ = diff_versions(V1, V2)
    assert (hunks[0].only_old, hunks[0].only_new) == (["31"], ["13"])
    assert hunks[1].only_old == [] and "two largest banks" in hunks[1].only_new[0]


def test_moved_passage_and_shifted_page_header_are_not_changes():
    body = ["Introduction. Central banks study agents closely and compare them with earlier automation tools.",
            "Figure 1: Error rates by bank size and by year of adoption across the whole sample.",
            "Methods. We collect transaction logs from twelve payment systems over three full years.",
            "Results. Larger banks adopt agents earlier than smaller banks in every country studied."]
    header = "7 Journal of Payments Research PREPRINT"
    old = [body[0] + " " + body[1], header + " " + body[2] + " " + body[3]]
    new = [body[0] + " " + body[2], header + " " + body[1] + " " + body[3]]     # the figure moved to page 2
    hunks, stats = diff_versions(old, new)
    assert hunks == [] and stats["moved_or_reflowed"] >= 1


def test_heavily_revised_document_still_fits_every_change_into_the_prompt():
    from trendscan.versions import Hunk, hunks_text
    hunks = [Hunk("modified", n, n, f"old passage {n} " * 400, f"new passage {n} " * 400, ["x" * 900], ["y" * 900])
             for n in range(1, 61)]
    text, shown = hunks_text(hunks, max_chars=60_000)
    assert shown == 60 and len(text) <= 60_000 + 60 and "CHANGE 60" in text


def test_reflowed_text_is_not_a_change():
    reflowed = [p.replace("payment operations", "pay-\nment   operations") for p in V1]
    assert diff_versions(V1, reflowed)[0] == []


# ---------- scan with a revised paper ----------

BASE = {
    "what_happened": "A study.", "assessment": "Relevant.", "trend_matches": [],
    "emerging_signal": {"is_signal": False, "signal_name": "", "why_watch": "", "evidence_needed": ""},
    "central_bank_relevance": "Payments.", "next_step": "Read.", "evidence_gaps": [],
}
CHANGES_JSON = {
    "summary": "The headline effect was revised down and a robustness section was added.",
    "substantive": True,
    "effect_on_assessment": "Yes, the main result is much weaker.",
    "changes": [
        {"description": "Error reduction revised from 31 to 13 percent.", "kind": "modified", "significance": "high",
         "old_page": 2, "old_quote": "Error rates fell by 31 percent after agents were introduced in the back office.",
         "new_page": 2, "new_quote": "Error rates fell by 13 percent after agents were introduced in the back office."},
        {"description": "New robustness section.", "kind": "added", "significance": "high",
         "old_page": 0, "old_quote": "",
         "new_page": 2, "new_quote": "The effect disappears when the two largest banks are excluded from the sample."},
        {"description": "Invented.", "kind": "added", "significance": "low", "old_page": 0, "old_quote": "",
         "new_page": 1, "new_quote": "Profits doubled in every single country that was studied."},
    ],
}


class Fake:
    """Fake Gemini: answers by the schema it is asked for, and counts the calls."""
    def __init__(self, fail_changes: Exception | None = None):
        self.calls = {"abstract": 0, "fulltext": 0, "changes": 0}
        self.fail_changes = fail_changes
        self.last_changes_prompt = ""

    def create(self, **kwargs):
        props = kwargs["response_format"]["schema"]["properties"]
        if "changes" in props:
            self.calls["changes"] += 1
            self.last_changes_prompt = kwargs["input"]
            if self.fail_changes:
                raise self.fail_changes
            return SimpleNamespace(output_text=json.dumps(CHANGES_JSON))
        if "evidence" in props:
            self.calls["fulltext"] += 1
            return SimpleNamespace(output_text=json.dumps(BASE | {"evidence": []}))
        self.calls["abstract"] += 1
        return SimpleNamespace(output_text=json.dumps(BASE | {"supporting_quote": "x"}))


class QuotaError(Exception):
    code = 429


def paper(version: str) -> Item:
    return Item(item_id="arxiv:2601.00001", source_id="test", source_name="Test papers", source_kind="paper",
                url="https://arxiv.org/abs/2601.00001", title="AI agents in payments", publisher="arXiv",
                authors=["A. Author"], published="2026-11-10T00:00:00+00:00",
                summary="Banks are piloting AI agents for payments.", version=version,
                pdf_url=f"https://arxiv.org/pdf/2601.00001{version}")


@pytest.fixture
def env(tmp_path, monkeypatch):
    settings = replace(load_settings(), db_path=tmp_path / "t.db", seconds_between_calls=0)
    state = {"feed": [paper("v1")], "latest": [], "pdfs": {"v1": V1, "v2": V2}, "downloads": []}
    monkeypatch.setattr(scan_module, "load_sources",
                        lambda _: [{"id": "test", "name": "Test papers", "type": "arxiv", "url": "x"}])
    monkeypatch.setattr(scan_module, "fetch_source", lambda src: list(state["feed"]))
    monkeypatch.setattr(scan_module, "fetch_arxiv_latest", lambda ids, source: list(state["latest"]))

    def fake_fetch_document(item, max_mb, timeout_s):
        state["downloads"].append(item.version)
        return Document("ok", "", item.pdf_url, state["pdfs"][item.version])
    monkeypatch.setattr(scan_module, "fetch_document", fake_fetch_document)
    return settings, Store(settings.db_path), state


def run(settings, store, fake):
    analyzer = GeminiAnalyzer(None, settings.gemini_model, client=SimpleNamespace(interactions=fake))
    return scan_module.run_scan(settings, store, analyzer=analyzer, sleep=lambda s: None)


def test_new_arxiv_version_is_detected_archived_and_summarized(env):
    settings, store, state = env
    fake = Fake()
    r1 = run(settings, store, fake)
    assert (r1.revisions_checked, r1.revisions_found, r1.versions_added) == (1, 0, 0)

    state["latest"] = [paper("v2")]          # the feed no longer lists the paper; the revision check finds v2
    state["feed"] = []
    r2 = run(settings, store, fake)
    assert (r2.revisions_found, r2.versions_added, r2.change_summaries) == (1, 1, 1)
    assert store.count_items() == 1          # still one inbox entry

    versions = store.get_versions("arxiv:2601.00001")
    assert [(v["label"], v["pdf_url"][-2:]) for v in versions] == [("v1", "v1"), ("v2", "v2")]   # both linkable
    assert store.get_version_pages("arxiv:2601.00001", 1) == V1                                # old text kept

    change = store.get_change("arxiv:2601.00001", 2)
    assert change["status"] == "done" and change["stats"]["changes"] == 2
    c = change["result"]["changes"]
    assert (c[0]["old_status"], c[0]["new_status"]) == ("verified", "verified")
    assert (c[1]["new_status"], c[1]["new_page"]) == ("other_page", 3)      # page corrected from 2 to 3
    assert c[2]["new_status"] == "not_found"                                # invented quote caught
    # Gemini saw only the differing passages, not the unchanged text
    assert "31 percent" in fake.last_changes_prompt and "twelve countries" not in fake.last_changes_prompt


def test_rescan_reuses_the_comparison(env):
    settings, store, state = env
    fake = Fake()
    run(settings, store, fake)
    state["latest"] = [paper("v2")]
    run(settings, store, fake)
    r3 = run(settings, store, fake)
    assert fake.calls == {"abstract": 0, "fulltext": 2, "changes": 1}
    assert (r3.gemini_calls, r3.revisions_found, r3.versions_added) == (0, 0, 0)
    assert state["downloads"] == ["v1", "v2"]


def test_comparison_stays_visible_when_gemini_is_unavailable(env):
    settings, store, state = env
    run(settings, store, Fake())
    state["latest"] = [paper("v2")]
    r = run(settings, store, Fake(fail_changes=QuotaError("quota")))
    change = store.get_change("arxiv:2601.00001", 2)
    assert change["status"] == "pending" and change["stats"]["changes"] == 2    # our own diff is there
    assert r.changes_pending == 1 and "429" in r.gemini_stopped
    fake = Fake()
    run(settings, store, fake)                                                  # Gemini is back
    assert fake.calls["changes"] == 1 and store.get_change("arxiv:2601.00001", 2)["status"] == "done"


def test_replaced_report_pdf_is_detected_by_its_change_stamp(env, monkeypatch):
    settings, store, state = env
    report = Item(item_id="rep:1", source_id="test", source_name="Test reports", source_kind="report",
                  url="https://example.org/report", title="Annual report", publisher="Example",
                  published="2026-11-10T00:00:00+00:00", summary="A report.")
    state["feed"] = [report]
    content = {"pages": V1, "stamp": "etag-1"}
    monkeypatch.setattr(scan_module, "fetch_document",
                        lambda item, mb, t: (state["downloads"].append("x"),
                                             Document("ok", "", "https://example.org/report.pdf", content["pages"]))[1])
    monkeypatch.setattr(scan_module, "pdf_signature", lambda url: content["stamp"])
    fake = Fake()
    run(settings, store, fake)               # reads the PDF
    run(settings, store, fake)               # remembers its stamp
    run(settings, store, fake)               # stamp unchanged: nothing happens
    assert len(state["downloads"]) == 1 and len(store.get_versions("rep:1")) == 1

    content.update(pages=V2, stamp="etag-2") # the publisher replaces the file
    r = run(settings, store, fake)
    assert (r.revisions_found, r.versions_added, r.change_summaries) == (1, 1, 1)
    old, new = store.get_versions("rep:1")
    assert old["pdf_url"] == new["pdf_url"] and store.get_version_pages("rep:1", 1) == V1


def test_same_text_under_a_new_stamp_is_not_a_new_version(env, monkeypatch):
    settings, store, state = env
    report = Item(item_id="rep:1", source_id="test", source_name="Test reports", source_kind="report",
                  url="https://example.org/report", title="Annual report", publisher="Example",
                  published="2026-11-10T00:00:00+00:00", summary="A report.")
    state["feed"] = [report]
    stamp = {"value": "etag-1"}
    monkeypatch.setattr(scan_module, "fetch_document",
                        lambda item, mb, t: Document("ok", "", "https://example.org/report.pdf", V1))
    monkeypatch.setattr(scan_module, "pdf_signature", lambda url: stamp["value"])
    fake = Fake()
    run(settings, store, fake); run(settings, store, fake)
    stamp["value"] = "etag-2"                # re-uploaded, same content
    r = run(settings, store, fake)
    assert r.revisions_found == 1 and r.versions_added == 0 and fake.calls["changes"] == 0
    assert len(store.get_versions("rep:1")) == 1
