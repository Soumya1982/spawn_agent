"""
review_app.py
-------------
Human-in-the-loop review screen — shown BEFORE the final processed JSON
is generated.

This app reads the staging file written by order_workflow.py (phase 1) and
shows two sections:

    Section 1 — Processing Summary table
        A colour-coded table of every stage event (SUCCESS / WARNING / ERROR)
        so the reviewer can see what the pipeline found before deciding.

    Section 2 — Flagged Orders review
        Only the rows that have warnings or errors are shown here.
        Each card shows the order details, the specific issues, and an
        Approve / Reject radio (no default selection).

The reviewer approves or rejects each flagged order, then clicks Submit.
This writes review_decisions.json and signals order_workflow.py (phase 2).

Flow:
    order_workflow.py --phase1   (validate, transform, write staging JSON)
          │
          └──▶  streamlit run review_app.py   (this file — blocks until done)
                      │
                      └──▶  review_decisions.json written
                                  │
                                  └──▶  order_workflow.py --phase2
                                            (apply decisions, write final JSON, deploy)

UI rules:
    • Section 1 shows the full processing summary with colour-coded badges.
    • Section 2 shows only flagged rows — not clean ones.
    • Radio buttons: "Approve" | "Reject" — NO default selection.
    • "Submit Decisions" is disabled until every flagged row has a choice.
    • Approved rows → cleaned data flows into the final JSON.
    • Rejected rows → written to a separate temp file (rejected_orders_<run_id>.json)
      that is automatically deleted after 30 days.

Usage (called automatically by order_workflow.py, or manually):
    streamlit run review_app.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import streamlit as st

# --------------------------------------------------------------------------- #
# Paths
# --------------------------------------------------------------------------- #
PROJECT_ROOT  = Path(__file__).resolve().parent
STAGING_FILE  = PROJECT_ROOT / "generated_output" / "order_history_staging.json"
REVIEW_FILE   = PROJECT_ROOT / "generated_output" / "review_decisions.json"
REJECTED_DIR  = PROJECT_ROOT / "generated_output" / "rejected_temp"
SIGNAL_FILE   = PROJECT_ROOT / "generated_output" / ".review_complete"
LOG_DIR       = PROJECT_ROOT / "log_analysis"

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _load_staging() -> dict:
    with STAGING_FILE.open(encoding="utf-8") as f:
        return json.load(f)


def _load_latest_log_events() -> list[dict]:
    """
    Return the events list from the most recent workflow audit log.
    Falls back to an empty list if no log exists or parsing fails.
    """
    logs = sorted(LOG_DIR.glob("workflow_log_*.json"))
    if not logs:
        return []
    try:
        with logs[-1].open(encoding="utf-8") as f:
            return json.load(f).get("events", [])
    except (json.JSONDecodeError, OSError):
        return []


def _save_decisions(decisions: dict[str, str]) -> None:
    """Persist {order_id: 'Approved'|'Rejected'} to review_decisions.json."""
    REVIEW_FILE.parent.mkdir(parents=True, exist_ok=True)
    REVIEW_FILE.write_text(
        json.dumps(decisions, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def _save_rejected_temp(rejected_orders: list[dict], run_id: str) -> Path:
    """
    Write rejected order records to a dated temp file.
    The file name embeds the ISO date so the 30-day cleanup job can find it.
    """
    REJECTED_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = REJECTED_DIR / f"rejected_orders_{run_id}_{today}.json"
    out_path.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "rejected_at": datetime.now(timezone.utc).isoformat(),
                "expires_after_days": 30,
                "orders": rejected_orders,
            },
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return out_path


def _cleanup_old_rejected(retention_days: int = 30) -> int:
    """Delete rejected_temp files older than retention_days. Returns count deleted."""
    if not REJECTED_DIR.exists():
        return 0
    now = datetime.now(timezone.utc)
    deleted = 0
    for f in REJECTED_DIR.glob("rejected_orders_*.json"):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            rejected_at = datetime.fromisoformat(data.get("rejected_at", ""))
            age_days = (now - rejected_at).days
            if age_days >= retention_days:
                f.unlink()
                deleted += 1
        except (json.JSONDecodeError, ValueError, OSError):
            pass
    return deleted


def _flagged_items(staging: dict) -> list[dict]:
    """
    Return one entry per flagged OrderID containing the order record +
    its list of issues (severity error or warning).
    """
    val_report: list[dict] = staging.get("validation_report", [])
    orders: list[dict]     = staging.get("orders", [])
    order_map = {str(o.get("OrderID", "")): o for o in orders}

    issues_by_order: dict[str, list[dict]] = {}
    for v in val_report:
        if v.get("severity") not in ("error", "warning"):
            continue
        oid = str(v.get("order_id", ""))
        if not oid or oid in ("None", ""):
            continue
        issues_by_order.setdefault(oid, []).append(v)

    result = []
    for oid, issues in issues_by_order.items():
        result.append({
            "order_id": oid,
            "order":    order_map.get(oid, {}),
            "issues":   issues,
        })
    return result


_SEVERITY_COLOR = {"error": "#f8d7da", "warning": "#fff3cd"}
_SEVERITY_TEXT  = {"error": "#721c24", "warning": "#856404"}

def _severity_badge(severity: str) -> str:
    bg = _SEVERITY_COLOR.get(severity, "#e2e3e5")
    fg = _SEVERITY_TEXT.get(severity, "#383d41")
    label = severity.upper()
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 8px;'
        f'border-radius:8px;font-weight:600;font-size:11px;">{label}</span>'
    )


# --------------------------------------------------------------------------- #
# Processing Summary table (Section 1)
# --------------------------------------------------------------------------- #

_SKIP_EVENTS = {"stage_start"}

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

def _level_badge(level: str) -> str:
    bg = _LEVEL_BG.get(level, "#e2e3e5")
    fg = _LEVEL_FG.get(level, "#383d41")
    return (
        f'<span style="background:{bg};color:{fg};padding:2px 10px;'
        f'border-radius:10px;font-weight:600;font-size:12px;">{level}</span>'
    )


def _build_processing_summary_html(events: list[dict]) -> str:
    """
    Build a colour-coded HTML table of stage events.
    Mirrors the Table 1 logic in app.py.
    """

    th = (
        "style='background:#f7f8fa;color:#57606a;font-size:13px;"
        "padding:8px 12px;border:1px solid #e5e7eb;text-align:left;'"
    )
    td = "style='padding:8px 12px;border:1px solid #e5e7eb;font-size:13px;'"

    cols = ["Stage", "Level", "Event", "Message", "Order ID"]
    headers = "".join(f"<th {th}>{c}</th>" for c in cols)

    rows_html = ""
    for e in events:
        level = e.get("level", "INFO")
        event = e.get("event", "")
        if event in _SKIP_EVENTS and level == "INFO":
            continue
        display_level = "SUCCESS" if level == "INFO" else level
        order_id = (
            e.get("details", {}).get("order_id", "")
            if e.get("details") else ""
        ) or ""
        cells = (
            f"<td {td}>{e.get('stage', '')}</td>"
            f"<td {td}>{_level_badge(display_level)}</td>"
            f"<td {td}>{event}</td>"
            f"<td {td}>{e.get('message', '')}</td>"
            f"<td {td}>{order_id}</td>"
        )
        rows_html += f"<tr>{cells}</tr>"

    return (
        "<div style='overflow-x:auto'>"
        "<table style='border-collapse:collapse;width:100%'>"
        f"<thead><tr>{headers}</tr></thead>"
        f"<tbody>{rows_html}</tbody>"
        "</table></div>"
    )


# --------------------------------------------------------------------------- #
# Main app
# --------------------------------------------------------------------------- #

def main() -> None:
    st.set_page_config(
        page_title="Order Review",
        page_icon="🔍",
        layout="wide",
    )

    # ── Guard: staging file must exist ───────────────────────────────────────
    if not STAGING_FILE.exists():
        st.error(
            "No staging file found. "
            "Run `python order_workflow.py --phase1` first."
        )
        st.stop()

    # ── If already completed this session, show confirmation ─────────────────
    if st.session_state.get("review_submitted"):
        st.success("✅ Decisions submitted. order_workflow.py will now finalize the output.")
        st.info("You may close this window.")
        st.stop()

    staging  = _load_staging()
    run_id   = staging.get("metadata", {}).get("run_id", "unknown")
    flagged  = _flagged_items(staging)

    # ── Run 30-day cleanup silently on each load ──────────────────────────────
    _cleanup_old_rejected()

    # ── Header ───────────────────────────────────────────────────────────────
    st.title("🔍 Order Review — Action Required")
    st.markdown(
        f"**Run ID:** `{run_id}`  \n"
        f"**{len(flagged)} order(s)** were flagged during validation and require "
        "your decision before the final report is generated."
    )
    st.caption(
        "Review the processing summary below, then approve or reject each "
        "flagged order. **Approved** orders are included in the final output. "
        "**Rejected** orders are excluded and stored temporarily for 30 days."
    )
    st.divider()

    # ── Section 1: Processing Summary table ───────────────────────────────────
    with st.expander("📋 Processing Summary", expanded=True):
        events = _load_latest_log_events()
        if events:
            summary_html = _build_processing_summary_html(events)
            st.write(summary_html, unsafe_allow_html=True)
        else:
            st.info("No log events found — run order_workflow.py --phase1 first.")

    st.divider()

    # ── Section 2 header ─────────────────────────────────────────────────────
    st.subheader("Flagged Orders — Approve or Reject")

    # ── No flagged orders ─────────────────────────────────────────────────────
    if not flagged:
        st.success(
            "No warnings or errors were detected. "
            "All orders are clean — no review needed."
        )
        if st.button("Proceed to Final Report", type="primary"):
            _save_decisions({})
            SIGNAL_FILE.write_text("done", encoding="utf-8")
            st.session_state["review_submitted"] = True
            st.rerun()
        st.stop()

    # ── Per-order review cards ────────────────────────────────────────────────
    # Initialise decisions dict in session_state (no pre-selection)
    if "decisions" not in st.session_state:
        st.session_state["decisions"] = {}

    decisions: dict[str, str | None] = st.session_state["decisions"]

    for item in flagged:
        oid    = item["order_id"]
        order  = item["order"]
        issues = item["issues"]

        worst  = "error" if any(i.get("severity") == "error" for i in issues) else "warning"
        badge  = _severity_badge(worst)

        # Card container
        with st.container(border=True):
            col_title, col_badge = st.columns([6, 1])
            with col_title:
                st.markdown(
                    f"**Order {oid}** — {order.get('CustomerName', '—')} "
                    f"/ {order.get('ProductName', '—')}",
                )
            with col_badge:
                st.markdown(badge, unsafe_allow_html=True)

            # Order detail row
            d1, d2, d3, d4 = st.columns(4)
            d1.metric("Quantity",      order.get("Quantity",    "—"))
            d2.metric("Unit Price",    order.get("UnitPrice",   "—"))
            d3.metric("Total Amount",  order.get("TotalAmount", "—"))
            d4.metric("Order Date",    order.get("OrderDate",   "—"))

            # Issues list
            st.markdown("**Issues detected:**")
            for iss in issues:
                sev_html = _severity_badge(iss.get("severity", "warning"))
                st.markdown(
                    f"{sev_html}&nbsp; {iss.get('message', '')}",
                    unsafe_allow_html=True,
                )

            st.markdown("")  # spacer

            # Approve / Reject radio — index=None means NO pre-selection
            current = decisions.get(oid)
            choice = st.radio(
                "Decision",
                options=["Approve", "Reject"],
                index=(
                    ["Approve", "Reject"].index(current)
                    if current in ("Approve", "Reject")
                    else None          # ← no button selected by default
                ),
                horizontal=True,
                key=f"radio_{oid}",
                label_visibility="collapsed",
            )

            if choice != current:
                decisions[oid] = choice
                st.session_state["decisions"] = decisions

    st.divider()

    # ── Submit guard: every flagged order must have a decision ────────────────
    all_decided = all(
        decisions.get(item["order_id"]) in ("Approve", "Reject")
        for item in flagged
    )
    pending_count = sum(
        1 for item in flagged
        if decisions.get(item["order_id"]) not in ("Approve", "Reject")
    )

    if not all_decided:
        st.warning(f"{pending_count} order(s) still need a decision before you can submit.")

    if st.button(
        "Submit Decisions",
        type="primary",
        disabled=not all_decided,
    ):
        # Normalise to "Approved" / "Rejected" for the downstream agent
        final_decisions = {
            oid: ("Approved" if choice == "Approve" else "Rejected")
            for oid, choice in decisions.items()
            if choice in ("Approve", "Reject")
        }

        # Save review_decisions.json
        _save_decisions(final_decisions)

        # Write temp file for rejected orders
        rejected_ids = {oid for oid, d in final_decisions.items() if d == "Rejected"}
        if rejected_ids:
            rejected_records = [
                item["order"] for item in flagged
                if item["order_id"] in rejected_ids
            ]
            temp_path = _save_rejected_temp(rejected_records, run_id)
            st.toast(
                f"{len(rejected_records)} rejected order(s) saved to temp file "
                f"(auto-deleted after 30 days).",
                icon="🗂️",
            )
        else:
            temp_path = None

        # Write signal file so order_workflow.py --phase2 knows to continue
        SIGNAL_FILE.write_text("done", encoding="utf-8")

        st.session_state["review_submitted"] = True
        st.rerun()


if __name__ == "__main__":
    main()
