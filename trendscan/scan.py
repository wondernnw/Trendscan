"""One scan: fetch sources -> store items -> fetch documents -> analyse -> compare versions and editions."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from .analysis import (EVIDENCE_CHECK_VERSION, AnalysisFailed, GeminiAnalyzer, GeminiUnavailable,
                       cache_key, check_evidence)
from .config import Settings, load_profiles, load_sources
from .documents import fetch_document, paged_text, pdf_signature
from .editions import EditionComparison, build_edition_prompt, edition_cache_key, find_edition_links
from .sources import fetch_arxiv_latest, fetch_source
from .storage import Store, text_hash
from .versions import (VersionChanges, build_changes_prompt, changes_cache_key, check_changes,
                       diff_versions, hunks_text)

log = logging.getLogger("trendscan")

FULLTEXT_KINDS = {"paper", "report"}   # source kinds for which we try to read the PDF
MAX_DOC_ATTEMPTS = 3                   # give up on an unreachable PDF after this many scans
SECONDS_BETWEEN_DOWNLOADS = 3          # be polite to the source servers


@dataclass
class ScanReport:
    fetched: int = 0
    new: int = 0
    unchanged: int = 0
    changed: int = 0
    source_errors: dict[str, str] = field(default_factory=dict)
    docs_fetched: int = 0          # PDFs downloaded and read in this scan
    docs_failed: int = 0           # PDF attempts that failed in this scan (flagged in the inbox)
    docs_waiting: int = 0          # papers/reports whose PDF or full-text analysis waits for a later scan
    gemini_calls: int = 0          # actual API requests made in this scan
    fulltext_calls: int = 0        # ... of which full-text analyses
    cache_hits: int = 0            # items whose stored analysis was reused
    analysed: int = 0              # successful new analyses
    failed: int = 0                # per-item failures (will be retried next scan)
    pending: int = 0               # items with no analysis yet after this scan
    gemini_stopped: str | None = None   # why Gemini calls stopped early, if they did
    revisions_checked: int = 0     # stored papers/reports asked about a newer version
    revisions_found: int = 0       # ... for which the source reports a new version or changed file
    versions_added: int = 0        # new versions whose text was read and archived in this scan
    change_summaries: int = 0      # version comparisons summarized by Gemini in this scan
    changes_pending: int = 0       # comparisons still waiting for their Gemini summary
    editions_linked: int = 0       # new editions linked to a previous edition in the inbox
    edition_comparisons: int = 0   # edition comparisons made by Gemini in this scan
    editions_pending: int = 0      # edition comparisons waiting for a PDF or for Gemini


def run_scan(settings: Settings, store: Store, analyzer: GeminiAnalyzer | None = None,
             progress: Callable[[str], None] = lambda msg: None,
             sleep: Callable[[float], None] = time.sleep) -> ScanReport:
    report = ScanReport()
    profiles_version, profiles = load_profiles(settings.profiles_file)
    if analyzer is None:
        analyzer = GeminiAnalyzer(settings.gemini_api_key, settings.gemini_model)

    # 1) Collect. A failing source is recorded and skipped; the others still run.
    for source in load_sources(settings.sources_file):
        progress(f"Fetching {source['name']} …")
        try:
            items = fetch_source(source)
        except Exception as exc:
            report.source_errors[source["name"]] = f"{type(exc).__name__}: {exc}"[:200]
            continue
        for item in items:
            report.fetched += 1
            outcome = store.upsert_item(item)   # "new" | "unchanged" | "changed"
            setattr(report, outcome, getattr(report, outcome) + 1)

    # 1b) Revisions: has a paper or report we already know been updated at its source?
    check_revisions(settings, store, report, progress)

    # 2) Documents: download and read PDFs of papers/reports we have no full text for yet.
    #    Bounded by a count and a time budget, so a slow server cannot stall the scan.
    started = time.monotonic()
    attempts_this_scan = 0
    wanted = []
    for row in store.inbox():
        if row["source_kind"] not in FULLTEXT_KINDS:
            continue
        item = store.get_item(row["item_id"])
        source_ref = item.pdf_url or item.url      # changes when e.g. arXiv publishes v2
        meta = store.get_document_meta(item.item_id)
        same_ref = bool(meta) and meta["source_ref"] == source_ref
        if same_ref and not meta["refetch"] and (
                meta["status"] != "inaccessible" or meta["attempts"] >= MAX_DOC_ATTEMPTS):
            continue                               # already have it, or a final answer why not
        wanted.append((item, source_ref, meta, same_ref))
    # Documents never tried before go first; retries of failed downloads come after them,
    # so one slow server cannot keep other documents waiting scan after scan.
    wanted.sort(key=lambda w: w[3])
    for item, source_ref, meta, same_ref in wanted:
        if (attempts_this_scan >= settings.max_fulltext_per_scan
                or time.monotonic() - started > settings.max_doc_seconds_per_scan):
            break                                  # the rest waits for the next scan
        if attempts_this_scan:
            sleep(SECONDS_BETWEEN_DOWNLOADS)
        progress(f"Reading PDF: {item.title[:60]} …")
        doc = fetch_document(item, settings.max_pdf_mb, settings.pdf_timeout_seconds)
        if doc.status != "no_pdf":     # "no PDF on this page" cost one page request, not a download
            attempts_this_scan += 1
        if same_ref and meta["refetch"] and meta["status"] == "ok" and doc.status != "ok":
            report.docs_failed += 1                # keep the readable copy we have; try again next scan
            continue
        label = item.version or f"retrieved {datetime.now(timezone.utc).date().isoformat()}"
        if store.save_document(item.item_id, source_ref, doc, (meta["attempts"] + 1) if same_ref else 1, label):
            report.versions_added += 1 if len(store.get_versions(item.item_id)) > 1 else 0
        if doc.status == "ok":
            report.docs_fetched += 1
        else:
            report.docs_failed += 1

    # 3) Decide what needs Gemini. We look at ALL stored items, so items left pending
    #    by an earlier scan (quota, outage) are picked up again automatically.
    to_analyse = []      # (item, cache key, full-text arguments or None)
    for row in store.inbox():
        item = store.get_item(row["item_id"])
        done_key = row["cache_key"] if row["analysis_status"] == "done" else None
        full_text = None
        if item.source_kind in FULLTEXT_KINDS:
            source_ref = item.pdf_url or item.url
            meta = store.get_document_meta(item.item_id)
            if not meta or meta["source_ref"] != source_ref or (
                    meta["status"] == "inaccessible" and meta["attempts"] < MAX_DOC_ATTEMPTS):
                # The PDF is still to be fetched (or retried). Don't spend a Gemini call on the
                # abstract now and another on the full text later: keep what we have and wait.
                report.docs_waiting += 1
                if done_key:
                    report.cache_hits += 1
                else:
                    report.pending += 1
                continue
            if meta["status"] == "ok":
                pages = store.get_document(item.item_id).pages
                doc_text, pages_sent = paged_text(pages, settings.max_doc_chars)
                full_text = {"pages": pages, "doc_text": doc_text, "pages_sent": pages_sent}
        key = cache_key(item, profiles_version, settings.gemini_model,
                        full_text["doc_text"] if full_text else None)
        if done_key == key:
            report.cache_hits += 1       # nothing changed -> reuse, no API call
            stored = row["analysis_result"]
            if full_text and stored.get("evidence_check_version") != EVIDENCE_CHECK_VERSION:
                # Our own citation-checking rules changed: re-check the stored result locally.
                store.save_analysis(item.item_id, key, row["analysis_model"],
                                    result=check_evidence(stored, full_text["pages"], full_text["pages_sent"]))
        else:
            to_analyse.append((item, key, full_text, bool(done_key)))

    # 4) Analyse, newest first, within the per-scan caps, with throttling.
    attempts = 0
    for item, key, full_text, has_older_result in to_analyse:
        over_cap = attempts >= settings.max_analyses_per_scan or (
            full_text and report.fulltext_calls >= settings.max_fulltext_per_scan)
        if report.gemini_stopped or over_cap:
            # Not analysed in this scan. An older (abstract) result stays visible meanwhile.
            report.docs_waiting += 1 if full_text else 0
            report.pending += 0 if has_older_result else 1
            continue
        if attempts:
            sleep(settings.seconds_between_calls)
        attempts += 1
        progress(f"Analysing ({'full text' if full_text else 'abstract'}): {item.title[:60]} …")
        try:
            if analyzer.client is not None:
                report.gemini_calls += 1
                report.fulltext_calls += 1 if full_text else 0
            result = analyzer.analyze(item, profiles, **(full_text or {}))
            store.save_analysis(item.item_id, key, settings.gemini_model, result=result)
            report.analysed += 1
        except GeminiUnavailable as exc:
            report.gemini_stopped = str(exc)
            report.docs_waiting += 1 if full_text else 0
            report.pending += 0 if has_older_result else 1
            log.warning("Gemini unavailable: %s", exc)
        except AnalysisFailed as exc:
            if not has_older_result:     # keep a good older result rather than overwrite it with an error
                store.save_analysis(item.item_id, key, settings.gemini_model, error=str(exc))
            report.failed += 1

    # 5) Versions: compare the two newest versions of every revised document.
    for item_id in store.items_with_revisions():
        old, new = store.get_versions(item_id)[-2:]
        key = changes_cache_key(old["text_hash"], new["text_hash"], settings.gemini_model)
        stored = store.get_change(item_id, new["version_no"])
        if stored and stored["cache_key"] == key and stored["status"] in ("done", "identical"):
            continue                                         # already compared: reuse
        old_pages = store.get_version_pages(item_id, old["version_no"])
        new_pages = store.get_version_pages(item_id, new["version_no"])
        hunks, stats = diff_versions(old_pages, new_pages)   # our own comparison, no Gemini

        def save(status, **extra):
            store.save_change(item_id, old["version_no"], new["version_no"], key, status, stats,
                              settings.gemini_model, **extra)

        if not hunks:
            save("identical")                                # only layout changed: nothing to summarize
            continue
        if report.gemini_stopped or attempts >= settings.max_analyses_per_scan:
            save("pending")
            report.changes_pending += 1
            continue
        if attempts:
            sleep(settings.seconds_between_calls)
        attempts += 1
        item = store.get_item(item_id)
        progress(f"Comparing versions: {item.title[:60]} …")
        changes_text, shown = hunks_text(hunks, settings.max_doc_chars)
        stats["changes_shown"] = shown
        prompt = build_changes_prompt(item, old["label"], new["label"], changes_text, shown, len(hunks), stats)
        try:
            if analyzer.client is not None:
                report.gemini_calls += 1
            result = check_changes(analyzer.generate(prompt, VersionChanges).model_dump(), old_pages, new_pages)
            save("done", result=result)
            report.change_summaries += 1
        except GeminiUnavailable as exc:
            report.gemini_stopped = str(exc)
            save("pending")
            report.changes_pending += 1
        except AnalysisFailed as exc:
            save("failed", error=str(exc))
            report.failed += 1

    # 6) Editions: link a new edition of a recurring report to the previous one and compare them.
    links = find_edition_links(store.inbox())                 # local rule, no network, no Gemini
    stored_links = {l["new_item_id"]: l for l in store.get_edition_links()}
    for gone in set(stored_links) - {l["new_item_id"] for l in links}:
        store.delete_edition_link(gone)                       # no longer matches the rule
    report.editions_linked = len(links)
    for link in links:
        new_id, old_id = link["new_item_id"], link["old_item_id"]
        stored = stored_links.get(new_id)

        def save_link(status, **extra):
            store.save_edition_link(new_id, old_id, link["series_key"], link["reason"], status, **extra)

        metas = {"previous": store.get_document_meta(old_id), "new": store.get_document_meta(new_id)}
        waiting = [side for side, m in metas.items()
                   if not m or (m["status"] == "inaccessible" and m["attempts"] < MAX_DOC_ATTEMPTS)]
        if waiting:
            save_link("waiting", error=f"The PDF of the {' and '.join(waiting)} edition has not been read yet.")
            report.editions_pending += 1
            continue
        unreadable = [f"{side} edition: {m['status']} ({m['detail'] or 'no detail'})"
                      for side, m in metas.items() if m["status"] != "ok"]
        if unreadable:
            save_link("unavailable", error="Cannot compare, a PDF could not be read. " + "; ".join(unreadable))
            continue
        old_pages = store.get_document(old_id).pages
        new_pages = store.get_document(new_id).pages
        key = edition_cache_key(text_hash(old_pages), text_hash(new_pages), settings.gemini_model,
                                settings.max_doc_chars)
        if stored and stored["old_item_id"] == old_id and stored["cache_key"] == key and stored["status"] == "done":
            continue                                          # already compared: reuse
        old_text, old_sent = paged_text(old_pages, settings.max_doc_chars // 2)   # half the budget each
        new_text, new_sent = paged_text(new_pages, settings.max_doc_chars // 2)
        stats = {"old_pages_sent": old_sent, "old_pages": len(old_pages),
                 "new_pages_sent": new_sent, "new_pages": len(new_pages)}
        if report.gemini_stopped or attempts >= settings.max_analyses_per_scan:
            save_link("pending", cache_key=key, stats=stats, error="waiting for Gemini")
            report.editions_pending += 1
            continue
        if attempts:
            sleep(settings.seconds_between_calls)
        attempts += 1
        old_item, new_item = store.get_item(old_id), store.get_item(new_id)
        progress(f"Comparing editions: {new_item.title[:60]} …")
        prompt = build_edition_prompt(old_item, new_item, old_text, old_sent, len(old_pages),
                                      new_text, new_sent, len(new_pages))
        try:
            if analyzer.client is not None:
                report.gemini_calls += 1
            result = check_changes(analyzer.generate(prompt, EditionComparison).model_dump(),
                                   old_pages, new_pages)
            save_link("done", cache_key=key, stats=stats, model=settings.gemini_model, result=result)
            report.edition_comparisons += 1
        except GeminiUnavailable as exc:
            report.gemini_stopped = str(exc)
            save_link("pending", cache_key=key, stats=stats, error=str(exc))
            report.editions_pending += 1
        except AnalysisFailed as exc:
            save_link("failed", cache_key=key, stats=stats, error=str(exc))
            report.failed += 1

    return report


def check_revisions(settings: Settings, store: Store, report: ScanReport,
                    progress: Callable[[str], None]) -> None:
    """Ask the sources whether known papers/reports have changed. Cheap: one arXiv request per
    50 papers and one HEAD request per report PDF. Nothing is downloaded here. A change is only
    recorded, and the documents phase then fetches the new file."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=settings.revision_check_days)).isoformat()
    recent = [r for r in store.inbox() if r["first_seen"] >= cutoff and r["source_kind"] in FULLTEXT_KINDS]

    # arXiv: the API tells us the current version number.
    arxiv = [r for r in recent if r["item_id"].startswith("arxiv:")]
    if arxiv:
        progress(f"Checking {len(arxiv)} arXiv papers for new versions …")
    for start in range(0, len(arxiv), 50):
        chunk = arxiv[start:start + 50]
        source = {"id": chunk[0]["source_id"], "name": chunk[0]["source_name"], "kind": chunk[0]["source_kind"]}
        try:
            latest = fetch_arxiv_latest([r["item_id"].split(":", 1)[1] for r in chunk], source)
        except Exception as exc:
            report.source_errors["arXiv revision check"] = f"{type(exc).__name__}: {exc}"[:200]
            break
        known = {r["item_id"]: r for r in chunk}
        report.revisions_checked += len(chunk)
        for item in latest:
            row = known.get(item.item_id)
            if row and item.version and row["version"] and item.version != row["version"]:
                item.source_id, item.source_name, item.source_kind = row["source_id"], row["source_name"], row["source_kind"]
                store.upsert_item(item)            # new version number and PDF link -> re-fetched in phase 2
                report.revisions_found += 1

    # Other papers/reports: has the PDF at the same address been replaced?
    others = [r for r in recent if not r["item_id"].startswith("arxiv:") and r["doc_status"] == "ok" and r["doc_pdf_url"]]
    if others:
        progress(f"Checking {len(others)} report PDFs for changes …")
    for row in others[:100]:
        signature = pdf_signature(row["doc_pdf_url"])
        if signature is None:
            continue
        report.revisions_checked += 1
        meta = store.get_document_meta(row["item_id"])
        if not meta["signature"]:
            store.set_signature(row["item_id"], signature, refetch=False)      # first look: remember the stamp
        elif signature != meta["signature"]:
            store.set_signature(row["item_id"], signature, refetch=True)       # changed: download again
            report.revisions_found += 1
