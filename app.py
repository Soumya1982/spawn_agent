"""
app.py
------
Order Processing Report — Streamlit app.

This app shows the FINAL output after the two-phase pipeline has completed:
    1. python order_workflow.py --phase1   (validate + review pop-up)
    2. python order_workflow.py --phase2   (apply decisions + export + deploy)

Renders:
    Section 1 — Summary metrics  (source / processed / rejected counts)
    Section 2 — Table 1: Processing Summary  (colour-coded level badges)
    Section 3 — Table 2: Final Order Output  (approved records only)
    Section 4 — Rejected Orders info panel  (read-only, sourced from
                review_decisions.json — shows what was excluded and why)

Human review happens BEFORE this report in review_app.py.
This page is purely informational — no decision UI here.

Usage:
    streamlit run app.py
"""

from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

PROJECT_ROOT = Path(__file__).resolve().parent
LOG_DIR      = PROJECT_ROOT / "log_analysis"
OUTPUT_FILE  = PROJECT_ROOT / "generated_output" / "order_history_processed.json"
REVIEW_FILE  = PROJECT_ROOT / "generated_output" / "review_decisions.json"
STAGING_FILE = PROJECT_ROOT / "generated_output" / "order_history_staging.json"

# --------------------------------------------------------------------------- #
# Data loading helpers
# --------------------------------------------------------------------------- #

@st.cache_data(show_spinner=False)
def _load_json(path: Path) -> dict:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _latest_log() -> Path | None:
    logs = sorted(LOG_DIR.glob("workflow_log_*.json"))
    return logs[-1] if logs else None


def _load_decisions() -> dict[str, str]:
    if REVIEW_FILE.exists():
        try:
            return json.loads(REVIEW_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


# --------------------------------------------------------------------------- #
# Report-building helpers
# --------------------------------------------------------------------------- #

def _build_summary(log_data: dict, output_data: dict) -> dict:
    """Derive the top-level counts for the Summary section."""
    meta       = output_data.get("metadata", {})
    orders     = output_data.get("orders", [])
    val_report = output_data.get("validation_report", [])

    source_count = meta.get("row_count", len(orders))

    flagged_ids: set = {
        str(v.get("order_id"))
        for v in val_report
        if v.get("severity") == "error" and v.get("order_id") is not None
    }
    processed_ok  = sum(1 for o in orders if str(o.get("OrderID")) not in flagged_ids)
    flagged_count = len(flagged_ids)

    reason_counts = Counter(
        v.get("issue_type", "unknown")
        for v in val_report
        if v.get("severity") in ("error", "warning")
    )
    reason_str = ", ".join(
        f"{cnt} {itype.replace('_', ' ')}"
        for itype, cnt in reason_counts.most_common()
    ) or "none"

    # How many were rejected by the reviewer
    decisions    = _load_decisions()
    rejected_ids = {oid for oid, d in decisions.items() if d == "Rejected"}

    return {
        "source":       source_count,
        "processed_ok": processed_ok,
        "flagged":      flagged_count,
        "reason_str":   reason_str,
        "rejected":     len(rejected_ids),
    }


_SKIP_EVENTS = {"stage_start"}

def _build_table1(log_data: dict) -> pd.DataFrame:
    rows = []
    for e in log_data.get("events", []):
        level = e.get("level", "INFO")
        event = e.get("event", "")
        if event in _SKIP_EVENTS and level == "INFO":
            continue
        display_level = "SUCCESS" if level == "INFO" else level
        order_id      = e.get("details", {}).get("order_id", "") if e.get("details") else ""
        rows.append({
            "Stage":    e.get("stage", ""),
            "Level":    display_level,
            "Event":    event,
            "Message":  e.get("message", ""),
            "Order ID": order_id or "",
        })
    return pd.DataFrame(rows, columns=["Stage", "Level", "Event", "Message", "Order ID"])


_ORDER_COLS = [
    "OrderID", "CustomerID", "CustomerName", "ProductID", "ProductName",
    "Quantity", "UnitPrice", "TotalAmount", "OrderDate", "Status",
    "Region", "ExpectedTotalAmount",
]
_DISPLAY_COLS = [
    "OrderID", "Customer ID", "Customer Name", "ProductID", "Product Name",
    "Quantity", "Unit Price", "Total Amount", "Order Date", "Status",
    "Region", "Expected Total Amount",
]
_CURRENCY_COLS = {"Unit Price", "Total Amount", "Expected Total Amount"}

def _build_table2(output_data: dict) -> pd.DataFrame:
    orders = output_data.get("orders", [])
    df = pd.DataFrame(orders)
    present = [c for c in _ORDER_COLS if c in df.columns]
    df = df[present].copy()
    rename_map = dict(zip(_ORDER_COLS, _DISPLAY_COLS))
    df.rename(columns=rename_map, inplace=True)
    for col in _CURRENCY_COLS:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce").map(
                lambda x: f"{x:,.2f}" if pd.notna(x) else ""
            )
    return df


# --------------------------------------------------------------------------- #
# Level colour helpers
# --------------------------------------------------------------------------- #

_LEVEL_BG = {
    "SUCCESS": "#d4edda",
    "WARNING": "#fff3cd",
    "ERROR":   "#f8d7da",
    "INFO":    "#d4edda",
}
_LEVEL_FG = {
    "SUCCESS": "#155724",
    "WARNING": "#856404",
    "ERROR":   "#721c24",
    "INFO":    "#155724",
}

def _badge_html(level: str) -> str:
    bg = _LEVEL_BG.get(level, "#e2e3e5")
    fg = _LEVEL_FG.get(level, "#383d41")
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 10px;'
        f'border-radius:10px;font-weight:600;font-size:12px;">{level}</span>'
    )


def _render_table1_html(df: pd.DataFrame) -> str:
    th_style = (
        "style='background:#f7f8fa;color:#57606a;font-size:13px;"
        "padding:8px 12px;border:1px solid #e5e7eb;text-align:left;'"
    )
    td_style = "style='padding:8px 12px;border:1px solid #e5e7eb;font-size:13px;'"
    headers   = "".join(f"<th {th_style}>{col}</th>" for col in df.columns)
    rows_html = ""
    for _, row in df.iterrows():
        cells = ""
        for col in df.columns:
            val = row[col]
            if col == "Level":
                cells += f"<td {td_style}>{_badge_html(str(val))}</td>"
            else:
                cells += f"<td {td_style}>{val}</td>"
        rows_html += f"<tr>{cells}</tr>"
    return (
        "<div style='overflow-x:auto'>"
        f"<table style='border-collapse:collapse;width:100%'>"
        f"<thead><tr>{headers}</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table></div>"
    )


# --------------------------------------------------------------------------- #
# Streamlit app
# --------------------------------------------------------------------------- #

def main() -> None:
    st.set_page_config(
        page_title="Order Processing Report",
        page_icon="📦",
        layout="wide",
    )

    # ── Guard: review still in progress ──────────────────────────────────────
    if STAGING_FILE.exists() and not OUTPUT_FILE.exists():
        st.warning(
            "**Review in progress** — the staging file exists but the final "
            "output has not been generated yet. "
            "Complete your review in the **Order Review** pop-up "
            "(or run `python order_workflow.py --phase2` manually)."
        )
        st.stop()

    # ── Load data ─────────────────────────────────────────────────────────────
    log_path = _latest_log()
    if log_path is None:
        st.error("No workflow log found in log_analysis/. Run order_workflow.py first.")
        return
    if not OUTPUT_FILE.exists():
        st.error(
            "Output file not found. Run `python order_workflow.py --phase1` "
            "to start the pipeline."
        )
        return

    log_data    = _load_json(log_path)
    output_data = _load_json(OUTPUT_FILE)

    # ── Header ────────────────────────────────────────────────────────────────
    st.title("📦 Order Processing Report")
    st.caption(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
    st.divider()

    # ── Section 1: Summary metrics ────────────────────────────────────────────
    st.subheader("Summary")
    summary = _build_summary(log_data, output_data)

    col1, col2, col3, col4 = st.columns(4)
    col1.metric("Source Records",        summary["source"])
    col2.metric("Processed Successfully", summary["processed_ok"])
    col3.metric("Flagged Issues",        summary["flagged"])
    col4.metric("Rejected by Reviewer",  summary["rejected"])

    if summary["flagged"] > 0:
        st.warning(f"**Issues found:** {summary['reason_str']}")
    else:
        st.success("All records processed cleanly — no issues detected.")

    st.divider()

    # ── Section 2: Table 1 — Processing Summary ───────────────────────────────
    st.subheader("Table 1 — Processing Summary")
    table1_df = _build_table1(log_data)
    st.write(_render_table1_html(table1_df), unsafe_allow_html=True)

    st.divider()

    # ── Section 3: Table 2 — Final Order Output ───────────────────────────────
    st.subheader("Table 2 — Final Order Output")
    st.caption("Contains only orders that passed review (approved or not flagged).")
    table2_df = _build_table2(output_data)
    st.dataframe(table2_df, use_container_width=True, hide_index=True)

    # ── Section 4: Rejected Orders info panel (read-only) ────────────────────
    decisions = _load_decisions()
    rejected  = {oid: d for oid, d in decisions.items() if d == "Rejected"}
    if rejected:
        st.divider()
        st.subheader("Rejected Orders")
        st.caption(
            f"{len(rejected)} order(s) were rejected by the reviewer and excluded "
            "from the final output. They are stored in a temporary file that will "
            "be automatically deleted after 30 days."
        )
        rej_df = pd.DataFrame(
            [{"Order ID": oid, "Decision": dec} for oid, dec in decisions.items()
             if dec == "Rejected"],
            columns=["Order ID", "Decision"],
        )
        st.dataframe(rej_df, use_container_width=True, hide_index=True)


if __name__ == "__main__":
    main()
