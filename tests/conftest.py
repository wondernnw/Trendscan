"""Shared test setup: tests never touch the network."""

import pytest

from trendscan import scan as scan_module


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """By default no source reports a revision. Tests that need one replace these again."""
    monkeypatch.setattr(scan_module, "fetch_arxiv_latest", lambda ids, source: [])
    monkeypatch.setattr(scan_module, "pdf_signature", lambda url: None)
