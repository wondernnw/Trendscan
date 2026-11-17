"""Email digest: collect, build, save (test mode) or send (fake mail server). Offline tests.

Run:  .venv/bin/python -m pytest tests/test_digest.py -v
"""

from __future__ import annotations

import email
import logging
from dataclasses import replace
from datetime import datetime, timezone
from email import policy

import pytest

from trendscan import __main__ as cli
from trendscan import scan as scan_module
from trendscan import schedule
from trendscan.config import load_settings
from trendscan.digest import SECTION_LIMIT, DigestConfigError, collect_digest, run_digest
from trendscan.documents import Document
from trendscan.sources import Item
from trendscan.storage import Store

PROFILES = [{"id": "agentic-ai", "name": "Agentic AI"}, {"id": "tokenisation", "name": "Tokenisation"}]
WHEN = datetime(2026, 11, 17, 7, 0, tzinfo=timezone.utc)


def analysis(signal=False, matches=(), quote_ok=True, title_note=""):
    return {
        "what_happened": f"Something happened{title_note}.", "assessment": "Worth watching.",
        "trend_matches": [{"profile_id": p, "confidence": c, "explanation": "Fits."} for p, c in matches],
        "emerging_signal": {"is_signal": signal, "signal_name": "Agent payments" if signal else "",
                            "why_watch": "New pattern." if signal else "", "evidence_needed": "Pilots."},
        "central_bank_relevance": "Payments.", "next_step": "Monitor.", "evidence_gaps": [],
        "supporting_quote": "x", "quote_verified": quote_ok,
        "analysed_text": "feed abstract/teaser only (full text not retrieved)",
    }


def add(store, n, result=None, title=None, url=None, kind="news", published=None):
    item = Item(item_id=f"t:{n}", source_id="t", source_name="Test", source_kind=kind,
                url=url or f"https://example.org/{n}", title=title or f"Item {n}", publisher="Example",
                published=published or f"2026-11-{10 + n % 9:02d}T00:00:00+00:00", summary="s")
    store.upsert_item(item)
    if result is not None:
        store.save_analysis(item.item_id, "k", "m", result=result)
    return item


@pytest.fixture
def env(tmp_path):
    settings = replace(load_settings(), db_path=tmp_path / "t.db", digest_dir=tmp_path / "digests",
                       email_mode="test", email_to=())
    return settings, Store(settings.db_path)


def test_digest_lists_signals_and_matches_and_only_counts_the_rest(env):
    settings, store = env
    add(store, 1, analysis(signal=True))
    add(store, 2, analysis(matches=[("agentic-ai", "high")]))
    add(store, 3, analysis())                      # no match: counted only
    add(store, 4)                                  # not analysed yet: left for a later digest
    d = collect_digest(store, PROFILES)
    assert [(e.section, e.title) for e in d.entries] == [("signal", "Item 1"), ("match", "Item 2")]
    assert d.other_keys == ["item:t:3"]
    match = d.entries[1]
    assert ("Trends", "Agentic AI (high): Fits.") in match.lines
    assert ("Source", "https://example.org/2") in match.links


def test_test_mode_saves_files_sends_nothing_and_never_repeats(env):
    settings, store = env
    add(store, 1, analysis(signal=True))
    add(store, 2, analysis(matches=[("tokenisation", "low")]))
    digest, location = run_digest(settings, store, PROFILES, when=WHEN)
    assert location.endswith(".html")
    files = sorted(p.name for p in settings.digest_dir.iterdir())
    assert files == ["digest_2026-11-17_070000.eml", "digest_2026-11-17_070000.html"]

    msg = email.message_from_bytes((settings.digest_dir / files[0]).read_bytes(), policy=policy.default)
    assert msg["Subject"] == "TrendScan digest 17 Nov 2026: 1 emerging signal, 1 trend match"
    assert "Item 1" in msg.get_body(("plain",)).get_content()
    assert "AI-generated" in msg.get_body(("html",)).get_content()
    [record] = store.recent_digests()
    assert (record["mode"], record["entries"]) == ("test", 2)

    assert run_digest(settings, store, PROFILES, when=WHEN)[1] is None      # nothing new: no digest
    add(store, 5, analysis(signal=True))
    digest, _ = run_digest(settings, store, PROFILES, when=WHEN.replace(hour=8))
    assert [e.title for e in digest.entries] == ["Item 5"]                  # only the new one


def test_preview_remembers_nothing(env):
    settings, store = env
    add(store, 1, analysis(signal=True))
    _, path = run_digest(settings, store, PROFILES, preview=True)
    assert path.endswith("preview.html")
    assert store.recent_digests() == [] and store.reported_keys() == set()
    assert len(collect_digest(store, PROFILES).entries) == 1


def test_untrusted_feed_text_cannot_inject_markup_or_links(env):
    settings, store = env
    add(store, 1, analysis(signal=True, title_note=' <img src=x onerror="alert(1)">'),
        title='Report <script>alert("x")</script>', url="javascript:alert(1)")
    _, location = run_digest(settings, store, PROFILES, when=WHEN)
    page = open(location, encoding="utf-8").read()
    assert "<script>" not in page and "<img" not in page
    assert "&lt;script&gt;" in page
    assert "javascript:" not in page                     # unsafe link dropped, title shown as text


def test_long_backlog_is_split_over_several_digests(env):
    settings, store = env
    for n in range(SECTION_LIMIT + 4):
        add(store, n, analysis(matches=[("agentic-ai", "medium")]))
    d1, _ = run_digest(settings, store, PROFILES, when=WHEN)
    assert d1.count("match") == SECTION_LIMIT and d1.held_back == {"match": 4}
    d2, _ = run_digest(settings, store, PROFILES, when=WHEN.replace(hour=8))
    assert d2.count("match") == 4 and d2.held_back == {}


def test_matches_are_ordered_strongest_first_then_newest(env):
    settings, store = env
    add(store, 1, analysis(matches=[("agentic-ai", "low")]), published="2026-11-16T00:00:00+00:00")
    add(store, 2, analysis(matches=[("agentic-ai", "high")]), published="2026-11-01T00:00:00+00:00")
    add(store, 3, analysis(matches=[("agentic-ai", "high")]), published="2026-11-12T00:00:00+00:00")
    assert [e.title for e in collect_digest(store, PROFILES).entries] == ["Item 3", "Item 2", "Item 1"]


def test_substantive_new_version_and_new_edition_are_reported(env):
    settings, store = env
    add(store, 1, analysis(), kind="paper")
    for label, pages in (("v1", ["Old text."]), ("v2", ["New text."])):
        store.save_document("t:1", f"https://x/{label}", Document("ok", "", f"https://x/{label}.pdf", pages), 1, label)
    change = {"summary": "Headline result revised.", "substantive": True, "changes": [],
              "effect_on_assessment": "Yes."}
    store.save_change("t:1", 1, 2, "ck", "done", {}, "m", result=change)
    add(store, 2, analysis(), kind="paper")
    store.save_change("t:2", 1, 2, "ck", "done", {}, "m", result=change | {"substantive": False})

    add(store, 3, analysis(), title="Annual Report 2025", kind="report")
    add(store, 4, analysis(), title="Annual Report 2026", kind="report")
    store.save_edition_link("t:4", "t:3", "annual report", "same title apart from the edition", "done",
                            result={"summary": "Focus moved to AI.", "continuing_themes": [], "changes": [],
                                    "technology_relevance": "AI.", "effect_on_assessment": "Yes."})
    d = collect_digest(store, PROFILES)
    sections = {e.section: e for e in d.entries}
    assert sections["version"].title == "Item 1 (v1 → v2)"                  # t:2 was not substantive
    assert ("Previous edition", "Annual Report 2025") in sections["edition"].lines
    assert len([e for e in d.entries if e.section == "version"]) == 1


class FakeSMTP:
    sent = []

    def __init__(self, host, port, timeout):
        self.host, self.port, self.calls = host, port, []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self, context):
        self.calls.append("starttls")

    def login(self, user, password):
        self.calls.append(("login", user))

    def send_message(self, msg):
        self.calls.append("send")
        FakeSMTP.sent.append((self, msg))


def test_smtp_mode_sends_encrypted_and_records(env):
    settings, store = env
    settings = replace(settings, email_mode="smtp", smtp_host="mail.example.org", smtp_user="me",
                       smtp_password="pw", email_to=("radar@example.org",))
    add(store, 1, analysis(signal=True))
    FakeSMTP.sent = []
    _, location = run_digest(settings, store, PROFILES, when=WHEN, smtp_factory=FakeSMTP)
    [(server, msg)] = FakeSMTP.sent
    assert (server.host, server.port) == ("mail.example.org", 587)
    assert server.calls == ["starttls", ("login", "me"), "send"]           # encrypted before login
    assert msg["To"] == "radar@example.org" and location == "radar@example.org"
    assert store.recent_digests()[0]["mode"] == "smtp"
    assert not settings.digest_dir.exists()                                # nothing saved locally


def test_smtp_mode_without_settings_refuses_and_remembers_nothing(env):
    settings, store = env
    add(store, 1, analysis(signal=True))
    with pytest.raises(DigestConfigError):
        run_digest(replace(settings, email_mode="smtp"), store, PROFILES, smtp_factory=FakeSMTP)
    assert store.reported_keys() == set()                                  # will be in the next digest


def test_mail_password_is_hidden_from_settings_repr_and_logs(caplog):
    s = replace(load_settings(), smtp_password="hunter2-secret")
    assert "hunter2" not in repr(s)
    flt = cli.RedactSecret("AIza-key", "hunter2-secret")
    caplog.handler.addFilter(flt)
    try:
        logging.getLogger("trendscan.t").warning("login failed for pw=%s", "hunter2-secret")
    finally:
        caplog.handler.removeFilter(flt)
    assert "hunter2" not in caplog.text and "pw=***" in caplog.text


def test_scheduled_scan_saves_a_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("TRENDSCAN_DB_PATH", str(tmp_path / "cli.db"))
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("TRENDSCAN_EMAIL_MODE", "test")
    monkeypatch.setattr(schedule, "LOG_DIR", tmp_path / "logs")
    monkeypatch.setattr(scan_module, "load_sources", lambda _: [])
    import trendscan.config as config_module
    real = config_module.load_settings
    monkeypatch.setattr(cli, "load_settings", lambda: replace(real(), digest_dir=tmp_path / "digests"))
    store = Store(tmp_path / "cli.db")
    add(store, 1, analysis(signal=True))
    logger = logging.getLogger("trendscan")
    before = list(logger.handlers)
    try:
        assert cli.main(["scan", "--scheduled"]) == cli.EXIT_OK
    finally:
        for h in logger.handlers[len(before):]:
            logger.removeHandler(h)
            h.close()
    assert len(list((tmp_path / "digests").glob("digest_*.html"))) == 1
    assert "Digest saved (test mode, not sent; 1 entries)" in (tmp_path / "logs" / "scan.log").read_text()
