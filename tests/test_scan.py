"""Core guarantees of milestone 1, tested offline with fake feeds and a fake Gemini client.

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
from trendscan.sources import Item, canonical_url
from trendscan.storage import Store


def make_item(n: int, summary: str | None = None) -> Item:
    return Item(
        item_id=f"test:{n}", source_id="test", source_name="Test feed", source_kind="news",
        url=f"https://example.org/a{n}", title=f"Article {n} about AI agents",
        publisher="Example", authors=["A. Author"], published="2026-11-1%dT00:00:00+00:00" % n,
        summary=summary or f"Banks are piloting AI agents for task {n}. Results were mixed.",
    )


GOOD_JSON = {
    "what_happened": "Banks are piloting AI agents.",
    "assessment": "Early-stage but relevant.",
    "trend_matches": [
        {"profile_id": "agentic-ai", "confidence": "high", "explanation": "Mentions AI agents."},
        {"profile_id": "invented-profile", "confidence": "low", "explanation": "Hallucinated id."},
    ],
    "emerging_signal": {"is_signal": False, "signal_name": "", "why_watch": "", "evidence_needed": ""},
    "central_bank_relevance": "Operational efficiency in banking.",
    "next_step": "Monitor.",
    "supporting_quote": "Banks are piloting AI agents",
    "evidence_gaps": ["Only a teaser was available."],
}


class FakeInteractions:
    def __init__(self, fail_after: int | None = None, error=None):
        self.calls = 0
        self.fail_after = fail_after
        self.error = error

    def create(self, **kwargs):
        self.calls += 1
        if self.fail_after is not None and self.calls > self.fail_after:
            raise self.error
        return SimpleNamespace(output_text=json.dumps(GOOD_JSON))


class QuotaError(Exception):
    code = 429


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Settings pointing at a temp DB, no throttling, and a fake feed with 3 items."""
    settings = replace(load_settings(), db_path=tmp_path / "t.db", seconds_between_calls=0)
    feed = {"items": [make_item(1), make_item(2), make_item(3)]}
    monkeypatch.setattr(scan_module, "load_sources",
                        lambda _: [{"id": "test", "name": "Test feed", "type": "rss", "url": "x"}])
    monkeypatch.setattr(scan_module, "fetch_source", lambda src: list(feed["items"]))
    return settings, Store(settings.db_path), feed


def analyzer_with(fake: FakeInteractions, settings) -> GeminiAnalyzer:
    return GeminiAnalyzer(None, settings.gemini_model, client=SimpleNamespace(interactions=fake))


def test_rescan_creates_no_duplicates_and_no_repeat_gemini_calls(env):
    settings, store, _ = env
    fake = FakeInteractions()
    analyzer = analyzer_with(fake, settings)

    r1 = run(settings, store, analyzer)
    assert (r1.new, r1.gemini_calls, r1.analysed) == (3, 3, 3)

    r2 = run(settings, store, analyzer)
    assert r2.new == 0 and r2.unchanged == 3
    assert r2.gemini_calls == 0 and r2.cache_hits == 3
    assert store.count_items() == 3
    assert fake.calls == 3          # total API calls across both scans


def test_items_stay_visible_when_gemini_unavailable_and_are_retried(env):
    settings, store, _ = env
    fake = FakeInteractions(fail_after=1, error=QuotaError("quota"))   # 1 success, then 429
    r1 = run(settings, store, analyzer_with(fake, settings))
    assert r1.analysed == 1 and r1.pending == 2
    assert "429" in r1.gemini_stopped
    assert fake.calls == 2           # stopped right after the 429, didn't hammer the API

    inbox = store.inbox()
    assert len(inbox) == 3           # all collected items still visible
    assert sum(r["analysis_status"] is None for r in inbox) == 2   # shown as pending

    # Quota is back: only the 2 pending items are analysed, the done one is reused.
    fake2 = FakeInteractions()
    r2 = run(settings, store, analyzer_with(fake2, settings))
    assert (r2.gemini_calls, r2.cache_hits, r2.pending) == (2, 1, 0)


def test_no_api_key_collects_everything_as_pending(env):
    settings, store, _ = env
    r = run(settings, store, GeminiAnalyzer(None, settings.gemini_model))
    assert r.new == 3 and r.gemini_calls == 0 and r.pending == 3
    assert len(store.inbox()) == 3


def test_changed_content_is_reanalysed(env):
    settings, store, feed = env
    fake = FakeInteractions()
    run(settings, store, analyzer_with(fake, settings))
    feed["items"][0] = make_item(1, summary="Banks are piloting AI agents. Corrected figures added.")
    r = run(settings, store, analyzer_with(fake, settings))
    assert r.changed == 1 and r.gemini_calls == 1 and store.count_items() == 3


def test_profile_change_invalidates_cache(env, tmp_path):
    settings, store, _ = env
    fake = FakeInteractions()
    run(settings, store, analyzer_with(fake, settings))
    bumped = tmp_path / "profiles.yaml"
    bumped.write_text(settings.profiles_file.read_text().replace("version: 1", "version: 2"))
    r = run(replace(settings, profiles_file=bumped), store, analyzer_with(fake, settings))
    assert r.gemini_calls == 3


def test_failing_source_does_not_block_others(env, monkeypatch):
    settings, store, feed = env
    monkeypatch.setattr(scan_module, "load_sources", lambda _: [
        {"id": "down", "name": "Broken feed", "type": "rss", "url": "x"},
        {"id": "test", "name": "Test feed", "type": "rss", "url": "y"}])

    def fetch(src):
        if src["id"] == "down":
            raise ConnectionError("unreachable")
        return list(feed["items"])
    monkeypatch.setattr(scan_module, "fetch_source", fetch)
    r = run(settings, store, GeminiAnalyzer(None, settings.gemini_model))
    assert "Broken feed" in r.source_errors and r.new == 3


def test_postprocess_drops_unknown_profiles_and_verifies_quote(env):
    settings, store, _ = env
    run(settings, store, analyzer_with(FakeInteractions(), settings))
    result = store.get_analysis("test:1")["result"]
    assert [m["profile_id"] for m in result["trend_matches"]] == ["agentic-ai"]
    assert result["trend_matches"][0]["profile_name"] == "Agentic AI systems"
    assert result["quote_verified"] is True


def test_canonical_url_strips_tracking():
    assert canonical_url("http://Example.org/a?utm_source=x&id=5") == "https://example.org/a?id=5"


def run(settings, store, analyzer):
    return scan_module.run_scan(settings, store, analyzer=analyzer, sleep=lambda s: None)
