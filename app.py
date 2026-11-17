"""TrendScan — Streamlit UI (evidence inbox). Run with:  streamlit run app.py

This file only renders. All logic lives in the `trendscan` package.
"""

from __future__ import annotations

import streamlit as st

from datetime import datetime
from pathlib import Path

import streamlit.components.v1 as components

from trendscan import schedule
from trendscan.config import load_profiles, load_settings, load_sources
from trendscan.runs import ScanBusy, report_from_dict, run_and_record
from trendscan.storage import Store


def local_time(iso: str | None) -> str:
    """Stored timestamps are UTC; show them in the computer's local time."""
    return datetime.fromisoformat(iso).astimezone().strftime("%d %b %Y, %H:%M") if iso else "–"

st.set_page_config(page_title="TrendScan", page_icon="🔭", layout="wide")

settings = load_settings()
store = Store(settings.db_path)
profiles_version, profiles = load_profiles(settings.profiles_file)
sources = load_sources(settings.sources_file)

# ---------------- sidebar: configuration + scan button ----------------
with st.sidebar:
    st.title("🔭 TrendScan")
    st.caption("Prototype · public sources · fictional trend profiles")

    if st.button("Run scan", type="primary", width="stretch"):
        with st.status("Scanning …", expanded=True) as status:
            try:
                run_and_record(settings, store, "manual", progress=status.write)
                status.update(label="Scan finished", state="complete", expanded=False)
            except ScanBusy:
                status.update(label="Another scan is already running", state="error", expanded=False)
                st.warning("A scan is already running (possibly the scheduled one). "
                           "Its results appear here when it finishes; reload the page then.")

    sched = schedule.status()
    if sched:
        days = "Mon–Fri" if sched["weekdays_only"] else "daily"
        st.caption(f"⏰ Scheduled scan: {days} at {sched['time']}"
                   + ("" if sched["loaded"] else " · ⚠️ installed but not active"))
    else:
        st.caption("⏰ No scheduled scan · `python -m trendscan schedule install --time 07:00`")
    last_digest = next(iter(store.recent_digests(1)), None)
    mode = ("test mode, saved, not sent" if settings.email_mode == "test"
            else f"sent to {', '.join(settings.email_to) or '⚠️ no recipient set'}")
    st.caption(f"📧 Email digest ({mode}): " + (
        f"last {local_time(last_digest['created_at'])}, {last_digest['entries']} entries"
        if last_digest else "none yet · created after each scheduled scan"))

    st.divider()
    st.markdown(f"**Gemini model:** `{settings.gemini_model}`")
    st.markdown("**API key:** " + ("✅ set" if settings.gemini_configured else "❌ not set — analysis will stay pending"))
    st.markdown(f"**Max analyses per scan:** {settings.max_analyses_per_scan} "
                f"(full text: {settings.max_fulltext_per_scan})")
    with st.expander(f"Sources ({len(sources)})"):
        for s in sources:
            st.markdown(f"- [{s['name']}]({s['url']}) · {s['kind']}")
    with st.expander(f"Trend profiles (v{profiles_version}, fictional)"):
        for p in profiles:
            st.markdown(f"- **{p['name']}** · _{p.get('ring', '')}_  \n  {p['description'].strip()}")

# ---------------- last scan report ----------------
runs = store.recent_runs(10)
last = next((r for r in runs if r["status"] != "running"), None)
running = next((r for r in runs if r["status"] == "running"), None)
if running:
    st.info(f"A {running['trigger']} scan has been running since {local_time(running['started_at'])}. "
            "Reload the page later to see its results.")
report = report_from_dict(last["report"]) if last and last["report"] else None
if last and last["status"] != "ok":
    st.subheader("Last scan")
    st.error(f"The last {last['trigger']} scan ({local_time(last['started_at'])}) did not finish: "
             f"{last['status']}{' — ' + last['error'] if last['error'] else ''}. "
             "Items collected before that are shown; the next scan continues where it stopped.")
if report:
    st.subheader(f"Last scan · {last['trigger']} · {local_time(last['finished_at'])}")
    c = st.columns(7)
    c[0].metric("Fetched", report.fetched)
    c[1].metric("New items", report.new)
    c[2].metric("PDFs read", report.docs_fetched)
    c[3].metric("Gemini calls", report.gemini_calls)
    c[4].metric("Reused (cache)", report.cache_hits)
    c[5].metric("Pending", report.pending)
    c[6].metric("Failed", report.failed)
    if report.docs_waiting:
        st.info(f"{report.docs_waiting} papers/reports are waiting for their PDF or full-text analysis "
                "(per-scan limits). Run another scan to continue.")
    if report.revisions_checked:
        st.caption(f"Revision check: {report.revisions_checked} papers/reports checked, "
                   f"{report.revisions_found} changed at the source, {report.versions_added} new versions read, "
                   f"{report.change_summaries} comparisons summarized, {report.changes_pending} waiting.")
    if report.editions_linked:
        st.caption(f"Editions: {report.editions_linked} new editions linked to their previous edition, "
                   f"{report.edition_comparisons} compared in this scan, {report.editions_pending} waiting.")
    if report.docs_failed:
        st.warning(f"{report.docs_failed} PDFs could not be read in this scan. "
                   "The affected items are flagged in the inbox.")
    if report.gemini_stopped:
        st.warning(f"Gemini analysis paused: {report.gemini_stopped} Collected items are still shown; "
                   "pending items are analysed on the next scan.")
    for name, err in report.source_errors.items():
        st.error(f"Source unavailable: **{name}** — {err}")
if runs:
    with st.expander(f"Scan history (last {len(runs)})"):
        st.dataframe(
            [{"Started": local_time(r["started_at"]), "Trigger": r["trigger"], "Status": r["status"],
              "New": (r["report"] or {}).get("new"), "PDFs read": (r["report"] or {}).get("docs_fetched"),
              "Gemini calls": (r["report"] or {}).get("gemini_calls"),
              "Pending": (r["report"] or {}).get("pending"), "Error": r["error"] or ""}
             for r in runs],
            hide_index=True, width="stretch")

if last_digest and last_digest["mode"] == "test" and Path(last_digest["location"]).exists():
    with st.expander(f"Last email digest: {last_digest['subject']}"):
        st.caption(f"Test mode: saved as {last_digest['location']} (and .eml); not sent.")
        components.html(Path(last_digest["location"]).read_text(encoding="utf-8"), height=600, scrolling=True)

# ---------------- inbox ----------------
rows = store.inbox()
st.header(f"Evidence inbox ({len(rows)})")
if not rows:
    st.info("No items yet. Click **Run scan** in the sidebar.")
    st.stop()


def category(r: dict) -> str:
    a = r["analysis_result"]
    if r["analysis_status"] != "done" or not a:
        return "Pending analysis"
    if a["emerging_signal"]["is_signal"]:
        return "Emerging signal"
    if a["trend_matches"]:
        return "Matches a trend"
    return "No match"


f1, f2, f3 = st.columns([2, 2, 3])
cat_filter = f1.multiselect("Show", ["Matches a trend", "Emerging signal", "No match", "Pending analysis"])
src_filter = f2.multiselect("Source", sorted({r["source_name"] for r in rows}))
query = f3.text_input("Search title / summary")
revised = set(store.items_with_revisions())
editions = {l["new_item_id"]: l for l in store.get_edition_links()}
newer_edition = {l["old_item_id"]: l["new_item_id"] for l in editions.values()}
by_id = {r["item_id"]: r for r in rows}
c1, c2 = st.columns(2)
only_revised = c1.checkbox(f"Only items with a new version ({len(revised)})") if revised else False
only_editions = c2.checkbox(f"Only new editions of a series ({len(editions)})") if editions else False

shown = [
    r for r in rows
    if (not cat_filter or category(r) in cat_filter)
    and (not src_filter or r["source_name"] in src_filter)
    and (not query or query.lower() in (r["title"] + " " + (r["summary"] or "")).lower())
    and (not only_revised or r["item_id"] in revised)
    and (not only_editions or r["item_id"] in editions)
]
st.caption(f"Showing {len(shown)} of {len(rows)}")

QUOTE_STATUS = {
    "verified": "✅ quote found on this page",
    "other_page": "✅ quote found — Gemini cited p. {cited}, it is on p. {page}",
    "spans_pages": "✅ quote found, continues onto the next page",
    "near_match": "🟡 close match on this page — wording differs slightly (e.g. footnote marks, omitted "
                  "words, a rebuilt table row); compare with the page",
    "not_found": "⚠️ quote NOT found in the PDF text — verify manually",
    "too_short": "⚠️ quote too short to verify",
}


def evidence_markdown(ev: dict, pdf_url: str | None) -> str:
    """One evidence entry: claim, where to find it, the quote, and our check of it."""
    page = f"PDF p. {ev['page']}"
    where = f"[{page} ↗]({pdf_url}#page={ev['page']})" if pdf_url else page
    if ev["locator"]:
        check = {True: " ✅ on this page", False: " ⚠️ label not found on this page",
                 None: " (as reported by Gemini)"}[ev["locator_found"]]
        where += f" · {ev['locator']}{check}"
    status = QUOTE_STATUS[ev["status"]].format(cited=ev["cited_page"], page=ev["page"])
    return f"**{ev['claim']}**  \n{where}\n\n> {ev['quote']}\n\n{status}"


BADGE = {"Matches a trend": "🟢", "Emerging signal": "🟣", "No match": "⚪", "Pending analysis": "⏳"}

def page_link(label: str, pdf_url: str | None, page: int) -> str:
    return f"[{label} p. {page} ↗]({pdf_url}#page={page})" if pdf_url else f"{label} p. {page}"


def render_versions(item_id: str) -> None:
    """Version history of one item: links to both versions, our comparison, Gemini's summary."""
    versions = store.get_versions(item_id)
    old, new = versions[-2:]
    change = store.get_change(item_id, new["version_no"])
    st.markdown(f"**🔁 New version: {old['label']} → {new['label']}**")
    if old["pdf_url"] and old["pdf_url"] != new["pdf_url"]:
        st.markdown(f"[Old version ({old['label']}) ↗]({old['pdf_url']}) · [New version ({new['label']}) ↗]({new['pdf_url']})")
    else:
        st.markdown(f"[Current version ↗]({new['pdf_url']}) · The file was replaced at the same address, so the old "
                    f"version is no longer online. TrendScan compares against the text it read on {old['fetched_at'][:10]}.")
    if len(versions) > 2:
        st.caption("Earlier versions on record: " + ", ".join(v["label"] for v in versions[:-2]))
    if not change:
        st.info("Comparison follows on the next scan.")
        return
    s = change["stats"]
    share = "less than 1%" if s["changed_share"] < 0.01 else f"about {s['changed_share']:.0%}"
    st.caption(f"Text comparison by TrendScan: {s['changes']} passages differ ({share} of the text), "
               f"{s.get('moved_or_reflowed', 0)} only moved or re-flowed · {s['old_pages']} → {s['new_pages']} pages")
    if change["status"] == "identical":
        st.markdown("No changes in the text. Only the layout or file differs.")
        return
    if change["status"] != "done":
        st.info(f"⏳ Summary of the changes pending ({change['error'] or 'waiting for Gemini'}). Retried on the next scan.")
        return
    res = change["result"]
    st.markdown(("**Substantive changes.** " if res["substantive"] else "**No substantive changes.** ") + res["summary"])
    st.markdown(f"**Should the earlier assessment be revisited?** {res['effect_on_assessment']}")
    with st.expander(f"Changes in detail ({len(res['changes'])})", expanded=res["substantive"]):
        if s.get("changes_shown", s["changes"]) < s["changes"]:
            st.caption(f"Gemini saw the first {s['changes_shown']} of {s['changes']} differing passages.")
        render_change_list(res["changes"], {"old": (old["label"], old["pdf_url"]),
                                            "new": (new["label"], new["pdf_url"])})


def render_change_list(changes: list[dict], sides: dict[str, tuple[str, str | None]]) -> None:
    """Changes between two versions or editions, each quote with TrendScan's check of it.
    `sides` maps "old"/"new" to (label, PDF link)."""
    for ch in changes:
        lines = [f"**{ch['description']}** · {ch['kind']} · significance _{ch['significance']}_"]
        for side, (label, pdf_url) in sides.items():
            quote, status = ch[f"{side}_quote"].strip(), ch[f"{side}_status"]
            if not quote:
                continue
            mark = {"verified": "✅", "other_page": "✅", "spans_pages": "✅", "near_match": "🟡 close match"}.get(
                status, f"⚠️ not found in the {label}")
            where = page_link(label, pdf_url, ch[f"{side}_page"]) if status not in (
                "not_found", "too_short") else label
            lines.append(f"{where} {mark}\n\n> {quote}")
        st.markdown("\n\n".join(lines))


def render_edition(link: dict, new_pdf: str | None) -> None:
    """New edition of a recurring publication: link to the previous edition and the comparison."""
    prev = by_id.get(link["old_item_id"])
    if not prev:
        return
    prev_pdf = prev["doc_pdf_url"] or prev["pdf_url"]
    st.markdown(f"**📚 New edition. Previous edition: [{prev['title']}]({prev['url']}) ↗** "
                f"({(prev['published'] or '')[:10] or 'date unknown'})")
    st.caption(f"Linked by TrendScan because: {link['reason']}.")
    if link["status"] in ("waiting", "unavailable", "pending", "failed"):
        icon = {"waiting": "⏳", "pending": "⏳", "unavailable": "⚠️", "failed": "⚠️"}[link["status"]]
        note = {"waiting": " Comparison follows once both PDFs are read.",
                "pending": " Retried on the next scan.", "failed": " Retried on the next scan.",
                "unavailable": ""}[link["status"]]
        st.info(f"{icon} Editions not compared yet: {link['error'] or link['status']}.{note}")
        return
    res, s = link["result"], link["stats"]
    st.markdown(res["summary"])
    if res["continuing_themes"]:
        st.markdown("**Continuing themes:** " + " · ".join(res["continuing_themes"]))
    st.markdown(f"**Technology relevance:** {res['technology_relevance']}")
    st.markdown(f"**Should the earlier assessment be revisited?** {res['effect_on_assessment']}")
    with st.expander(f"Differences in detail ({len(res['changes'])})"):
        st.caption(f"Compared: pages 1–{s['old_pages_sent']} of {s['old_pages']} (previous edition) and "
                   f"pages 1–{s['new_pages_sent']} of {s['new_pages']} (new edition). \"Removed\" means not "
                   "addressed in the compared pages of the new edition, not necessarily absent from it.")
        render_change_list(res["changes"], {"old": ("previous edition", prev_pdf),
                                            "new": ("new edition", new_pdf)})


for r in shown:
    a = r["analysis_result"]
    cat = category(r)
    with st.container(border=True):
        head, links = st.columns([5, 1])
        head.markdown(f"#### {BADGE[cat]} {r['title']}")
        authors = ", ".join(r["authors"][:4]) + (" et al." if len(r["authors"]) > 4 else "")
        meta = [r["publisher"] or r["source_name"], authors or None,
                (r["published"] or "")[:10] or None, r["source_kind"], r["version"]]
        head.caption(" · ".join(m for m in meta if m))
        links.link_button("Open original source ↗", r["url"], width="stretch")
        pdf_url = r["doc_pdf_url"] or r["pdf_url"]
        if pdf_url:
            links.link_button("PDF ↗", pdf_url, width="stretch")

        # Evidence flags about the source metadata itself
        flags = []
        if not r["published"]:
            flags.append("publication date missing")
        if not r["authors"]:
            flags.append("authors not given by the feed")
        if not (r["summary"] or "").strip():
            flags.append("feed provided no text — analysis is based on the title only")
        full_text_result = bool(a and a.get("evidence") is not None)
        if r["source_kind"] in ("paper", "report") and not full_text_result:
            if r["doc_status"] is None:
                flags.append("full text not retrieved yet — run another scan")
            elif r["doc_status"] == "ok":
                flags.append("full text retrieved; page-level analysis follows on the next scan")
            else:
                flags.append(f"full text unavailable ({r['doc_detail']}) — analysis is based on the abstract only")

        if cat == "Pending analysis":
            reason = r["analysis_error"] or "not analysed yet"
            st.info(f"⏳ Analysis pending ({reason}). The item stays in the inbox and is retried on the next scan.")
            with st.expander("Source text from feed"):
                st.write(r["summary"] or "_(none)_")
        else:
            left, right = st.columns(2)
            left.markdown("**What happened** (from the source)")
            left.write(a["what_happened"])
            right.markdown("**Assessment** (Gemini's interpretation)")
            right.write(a["assessment"])

            if a["trend_matches"]:
                st.markdown("**Matches existing trend profiles**")
                for m in a["trend_matches"]:
                    st.markdown(f"- **{m['profile_name']}** · confidence _{m['confidence']}_ — {m['explanation']}")
            sig = a["emerging_signal"]
            if sig["is_signal"]:
                st.markdown(f"**🟣 Possible emerging signal: {sig['signal_name']}**")
                st.markdown(f"- *Why watch:* {sig['why_watch']}\n- *Evidence needed for a deeper look:* {sig['evidence_needed']}")

            st.markdown(f"**Relevance to central banking / financial services:** {a['central_bank_relevance']}")
            st.markdown(f"**Suggested next step:** {a['next_step']}")

            with st.expander("Evidence & verification", expanded=full_text_result):
                if full_text_result:
                    for ev in a["evidence"]:
                        st.markdown(evidence_markdown(ev, pdf_url))
                    cov = a["coverage"]
                    if cov["pages_analysed"] < cov["pages_total"]:
                        st.markdown(f"**Not analysed:** PDF pages {cov['pages_analysed'] + 1}–{cov['pages_total']} "
                                    "(beyond the per-document text limit)")
                else:
                    mark = "✅ found verbatim in the source text" if a["quote_verified"] else "⚠️ NOT found in the source text — verify manually"
                    st.markdown(f"> {a['supporting_quote']}\n\n{mark}")
                st.markdown(f"**Analysed text:** {a['analysed_text']}")
                for g in a["evidence_gaps"]:
                    st.markdown(f"- Gap: {g}")
                st.caption(f"Model {r['analysis_model']} · analysed {r['analyzed_at']} · item id `{r['item_id']}`")
            if full_text_result:
                bad = sum(ev["status"] in ("not_found", "too_short") for ev in a["evidence"])
                near = sum(ev["status"] == "near_match" for ev in a["evidence"])
                if bad:
                    flags.append(f"{bad} of {len(a['evidence'])} quotes could not be verified in the PDF text")
                if near:
                    flags.append(f"{near} of {len(a['evidence'])} quotes are only a close match")
                if not a["evidence"]:
                    flags.append("Gemini gave no page-level evidence")
            elif not a["quote_verified"]:
                flags.append("supporting quote could not be verified in the source")

        if r["item_id"] in revised:
            st.divider()
            render_versions(r["item_id"])
        if r["item_id"] in editions:
            st.divider()
            render_edition(editions[r["item_id"]], r["doc_pdf_url"] or r["pdf_url"])
        if r["item_id"] in newer_edition and newer_edition[r["item_id"]] in by_id:
            newer = by_id[newer_edition[r["item_id"]]]
            st.caption(f"📚 A newer edition is in the inbox: **{newer['title']}**")

        if flags:
            st.warning("Evidence check: " + "; ".join(flags))
