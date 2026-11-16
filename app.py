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
    st.markdown(f"**Max analyses per scan:** {settings.max_analyses_per_scan}")
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
    c = st.columns(6)
    c[0].metric("Fetched", report.fetched)
    c[1].metric("New items", report.new)
    c[2].metric("Gemini calls", report.gemini_calls)
    c[3].metric("Reused (cache)", report.cache_hits)
    c[4].metric("Pending", report.pending)
    c[5].metric("Failed", report.failed)
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

shown = [
    r for r in rows
    if (not cat_filter or category(r) in cat_filter)
    and (not src_filter or r["source_name"] in src_filter)
    and (not query or query.lower() in (r["title"] + " " + (r["summary"] or "")).lower())
]
st.caption(f"Showing {len(shown)} of {len(rows)}")

BADGE = {"Matches a trend": "🟢", "Emerging signal": "🟣", "No match": "⚪", "Pending analysis": "⏳"}

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
        if r["pdf_url"]:
            links.link_button("PDF ↗", r["pdf_url"], use_container_width=True)

        # Evidence flags about the source metadata itself
        flags = []
        if not r["published"]:
            flags.append("publication date missing")
        if not r["authors"]:
            flags.append("authors not given by the feed")
        if not (r["summary"] or "").strip():
            flags.append("feed provided no text — analysis is based on the title only")

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

            with st.expander("Evidence & verification"):
                mark = "✅ found verbatim in the source text" if a["quote_verified"] else "⚠️ NOT found in the source text — verify manually"
                st.markdown(f"> {a['supporting_quote']}\n\n{mark}")
                st.markdown(f"**Analysed text:** {a['analysed_text']}")
                for g in a["evidence_gaps"]:
                    st.markdown(f"- Gap: {g}")
                st.caption(f"Model {r['analysis_model']} · analysed {r['analyzed_at']} · item id `{r['item_id']}`")
            if not a["quote_verified"]:
                flags.append("supporting quote could not be verified in the source")

        if flags:
            st.warning("Evidence check: " + "; ".join(flags))
