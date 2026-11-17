"""TrendScan — Streamlit UI (evidence inbox). Run with:  streamlit run app.py

This file only renders. All logic lives in the `trendscan` package.
"""

from __future__ import annotations

import streamlit as st

from trendscan.config import load_profiles, load_settings, load_sources
from trendscan.scan import run_scan
from trendscan.storage import Store

st.set_page_config(page_title="TrendScan", page_icon="🔭", layout="wide")

settings = load_settings()
store = Store(settings.db_path)
profiles_version, profiles = load_profiles(settings.profiles_file)
sources = load_sources(settings.sources_file)

# ---------------- sidebar: configuration + scan button ----------------
with st.sidebar:
    st.title("🔭 TrendScan")
    st.caption("Prototype · public sources · fictional trend profiles")

    if st.button("Run scan", type="primary", use_container_width=True):
        with st.status("Scanning …", expanded=True) as status:
            report = run_scan(settings, store, progress=status.write)
            status.update(label="Scan finished", state="complete", expanded=False)
        st.session_state["last_report"] = report

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
report = st.session_state.get("last_report")
if report:
    st.subheader("Last scan")
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
    if report.docs_failed:
        st.warning(f"{report.docs_failed} PDFs could not be read in this scan. "
                   "The affected items are flagged in the inbox.")
    if report.gemini_stopped:
        st.warning(f"Gemini analysis paused: {report.gemini_stopped} Collected items are still shown; "
                   "pending items are analysed on the next scan.")
    for name, err in report.source_errors.items():
        st.error(f"Source unavailable: **{name}** — {err}")

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
only_revised = st.checkbox(f"Only items with a new version ({len(revised)})") if revised else False

shown = [
    r for r in rows
    if (not cat_filter or category(r) in cat_filter)
    and (not src_filter or r["source_name"] in src_filter)
    and (not query or query.lower() in (r["title"] + " " + (r["summary"] or "")).lower())
    and (not only_revised or r["item_id"] in revised)
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
        for ch in res["changes"]:
            lines = [f"**{ch['description']}** · {ch['kind']} · significance _{ch['significance']}_"]
            for side, version in (("old", old), ("new", new)):
                quote, status = ch[f"{side}_quote"].strip(), ch[f"{side}_status"]
                if not quote:
                    continue
                mark = {"verified": "✅", "other_page": "✅", "spans_pages": "✅", "near_match": "🟡 close match"}.get(
                    status, "⚠️ not found in this version")
                where = page_link(version["label"], version["pdf_url"], ch[f"{side}_page"]) if status not in (
                    "not_found", "too_short") else version["label"]
                lines.append(f"{where} {mark}\n\n> {quote}")
            st.markdown("\n\n".join(lines))


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
        links.link_button("Open original source ↗", r["url"], use_container_width=True)
        pdf_url = r["doc_pdf_url"] or r["pdf_url"]
        if pdf_url:
            links.link_button("PDF ↗", pdf_url, use_container_width=True)

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

        if flags:
            st.warning("Evidence check: " + "; ".join(flags))
