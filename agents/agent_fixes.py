"""
agents/agent_fixes.py
----------------------
Fix Library — maps known error / warning event codes produced by
order_workflow.py to concrete remediation actions that BobRepairAgent
can apply automatically.

Each AgentFix has:
    name        – human-readable label for reporting
    matches()   – predicate: does this fix apply to the given issue event?
    apply()     – mutate the CSV / config to resolve the issue
    reversible  – whether the fix can be rolled back (for safety)

Adding a new fix:
    1.  Subclass AgentFix (or instantiate a LambdaFix for simple cases).
    2.  Register it in FIX_REGISTRY at the bottom of this file.

Review-decision fix:
    FixRejectedOrders handles the synthetic "order_rejected_by_reviewer"
    event that BobRepairAgent injects after reading review_decisions.json.
    It removes the rejected rows from the source CSV so the next workflow
    run excludes them from the output entirely.
"""

from __future__ import annotations

import csv
import json
import logging
import shutil
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

log = logging.getLogger("bob_repair_agent.fixes")


# --------------------------------------------------------------------------- #
# Base class
# --------------------------------------------------------------------------- #

class AgentFix(ABC):
    """Abstract base for all auto-repair actions."""

    name: str = "unnamed_fix"
    reversible: bool = True

    @abstractmethod
    def matches(self, issue: dict[str, Any]) -> bool:
        """Return True if this fix should be applied to `issue`."""

    @abstractmethod
    def apply(self, issue: dict[str, Any], project_root: Path) -> str:
        """
        Apply the fix.

        Returns a one-line human-readable description of what was done,
        e.g. "Filled missing Quantity=1 for ORD1009 (row 8)".
        Raises on failure — SpawnAgent will catch and continue.
        """


# --------------------------------------------------------------------------- #
# Concrete fixes
# --------------------------------------------------------------------------- #

class FixMissingQuantity(AgentFix):
    """
    ERROR: missing_quantity
    Root cause: A row in the CSV has an empty Quantity cell.
    Fix: Back-fill the CSV cell with 1 (the same default the workflow itself
         uses at runtime) so subsequent reruns don't re-flag it.
    """

    name = "fill_missing_quantity"

    def matches(self, issue: dict[str, Any]) -> bool:
        return issue.get("event") == "missing_quantity"

    def apply(self, issue: dict[str, Any], project_root: Path) -> str:
        csv_path = project_root / "order_history.csv"
        row_index = issue.get("details", {}).get("row_index")
        order_id = issue.get("details", {}).get("order_id")

        if row_index is None:
            raise ValueError("missing_quantity issue has no row_index in details")

        _patch_csv_cell(csv_path, data_row=row_index, column="Quantity", new_value="1")
        return f"Filled missing Quantity=1 for {order_id} (CSV data row {row_index})"


class FixFormulaMismatch(AgentFix):
    """
    WARNING: formula_mismatch
    Root cause: TotalAmount ≠ Quantity * UnitPrice in the source CSV.
    Fix: Recompute TotalAmount from Quantity and UnitPrice in the CSV so
         the source is authoritative and reruns pass formula validation.
    """

    name = "recompute_total_amount"

    def matches(self, issue: dict[str, Any]) -> bool:
        return issue.get("event") == "formula_mismatch"

    def apply(self, issue: dict[str, Any], project_root: Path) -> str:
        csv_path = project_root / "order_history.csv"
        row_index = issue.get("details", {}).get("row_index")
        order_id = issue.get("details", {}).get("order_id")

        if row_index is None:
            raise ValueError("formula_mismatch issue has no row_index in details")

        rows = _read_csv(csv_path)
        # row_index is 0-based data row (0 = first data row, not header)
        data_row = rows[row_index]

        try:
            qty = float(str(data_row.get("Quantity", "0")).replace(",", "").strip() or "0")
            price = float(
                str(data_row.get("UnitPrice", "0"))
                .replace(",", "")
                .replace("$", "")
                .replace("₹", "")
                .strip()
                or "0"
            )
        except ValueError as exc:
            raise ValueError(
                f"Cannot recompute TotalAmount for row {row_index}: {exc}"
            ) from exc

        corrected = round(qty * price, 2)
        _patch_csv_cell(csv_path, data_row=row_index, column="TotalAmount",
                        new_value=str(corrected))
        return (
            f"Corrected TotalAmount={corrected} (was {data_row.get('TotalAmount')}) "
            f"for {order_id} (CSV data row {row_index})"
        )


class FixBusinessDuplicate(AgentFix):
    """
    WARNING: duplicate_business
    Root cause: Two rows share the same CustomerID / ProductID / Quantity /
                OrderDate — likely a double-entry in the upstream system.
    Fix: Flag-only (adds a note).  We do NOT auto-delete rows because a human
         must confirm.  This fix is intentionally non-destructive and marks
         the issue as "acknowledged" so the agent knows it is handled.
    """

    name = "acknowledge_business_duplicate"
    reversible = False  # no change made, so nothing to reverse

    def matches(self, issue: dict[str, Any]) -> bool:
        return issue.get("event") == "duplicate_business"

    def apply(self, issue: dict[str, Any], project_root: Path) -> str:
        order_id = issue.get("details", {}).get("order_id", "unknown")
        return (
            f"Business duplicate for {order_id} acknowledged. "
            "Auto-deletion skipped — manual review required."
        )


class FixFileNotFound(AgentFix):
    """
    ERROR: file_not_found
    Root cause: The input CSV is missing entirely.
    Fix: Check whether a backup copy exists in generated_output/ or
         a common typo path and restore it if found.
    """

    name = "restore_missing_input_csv"

    def matches(self, issue: dict[str, Any]) -> bool:
        return issue.get("event") == "file_not_found"

    def apply(self, issue: dict[str, Any], project_root: Path) -> str:
        target = project_root / "order_history.csv"
        if target.exists():
            return "order_history.csv already present — no action needed."

        # Look for a backup in generated_output or common alternatives
        candidates = [
            project_root / "generated_output" / "order_history.csv",
            project_root / "order_history.csv.bak",
            project_root / "Order_History.csv",
        ]
        for candidate in candidates:
            if candidate.exists():
                shutil.copy2(candidate, target)
                return f"Restored order_history.csv from backup at {candidate}"

        raise FileNotFoundError(
            "order_history.csv is missing and no backup was found. "
            "Manual restore required."
        )


class FixRejectedOrders(AgentFix):
    """
    SYNTHETIC event: order_rejected_by_reviewer
    Root cause: A human reviewer marked one or more orders as "Rejected"
                in the Streamlit review panel (HITL #5).  The decision is
                stored in generated_output/review_decisions.json.

    Fix:
        1. Read review_decisions.json.
        2. Collect every OrderID whose decision is "Rejected".
        3. Remove those rows from order_history.csv so the next workflow
           run will not include them in the output or the deploy artefact.
        4. Update review_decisions.json — mark each acted-on order with
           "Excluded" so the dashboard can show it was handled.

    reversible = True because _write_csv() always creates a .csv.bak backup
    before overwriting the source file.
    """

    name = "exclude_rejected_orders"

    def matches(self, issue: dict[str, Any]) -> bool:
        return issue.get("event") == "order_rejected_by_reviewer"

    def apply(self, issue: dict[str, Any], project_root: Path) -> str:
        review_path = project_root / "generated_output" / "review_decisions.json"
        csv_path    = project_root / "order_history.csv"

        # Load decisions
        if not review_path.exists():
            raise FileNotFoundError(
                f"review_decisions.json not found at {review_path}"
            )
        decisions: dict[str, str] = json.loads(
            review_path.read_text(encoding="utf-8")
        )

        rejected_ids = {
            oid for oid, decision in decisions.items() if decision == "Rejected"
        }
        if not rejected_ids:
            return "No rejected orders found in review_decisions.json — nothing to remove."

        # Remove rejected rows from CSV
        rows = _read_csv(csv_path)
        original_count = len(rows)
        kept_rows = [r for r in rows if r.get("OrderID", "").strip() not in rejected_ids]
        removed_count = original_count - len(kept_rows)

        if removed_count == 0:
            return (
                f"Rejected OrderIDs {rejected_ids} not found in CSV "
                "(may have been removed in a prior run)."
            )

        fieldnames = list(rows[0].keys())
        _write_csv(csv_path, kept_rows, fieldnames)

        # Update decisions file: mark acted-on orders as "Excluded"
        for oid in rejected_ids:
            decisions[oid] = "Excluded"
        review_path.write_text(
            json.dumps(decisions, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        removed_list = ", ".join(sorted(rejected_ids))
        return (
            f"Removed {removed_count} rejected order(s) from CSV "
            f"(OrderIDs: {removed_list}). "
            "review_decisions.json updated to 'Excluded'."
        )


class FixDataTypeMismatch(AgentFix):
    """
    WARNING: datatype_mismatch
    Root cause: A cell value doesn't match the expected schema type.
    Fix: Record the mismatch in the repair log. Actual cell correction
         requires domain knowledge, so we surface a recommendation but
         do not blindly overwrite data.
    """

    name = "log_datatype_mismatch_recommendation"
    reversible = False

    def matches(self, issue: dict[str, Any]) -> bool:
        return issue.get("event") == "datatype_mismatch"

    def apply(self, issue: dict[str, Any], project_root: Path) -> str:
        msg = issue.get("message", "")
        return (
            f"Data-type mismatch noted: '{msg}'. "
            "Recommend manual correction of source CSV before next run."
        )


# --------------------------------------------------------------------------- #
# CSV utility helpers
# --------------------------------------------------------------------------- #

def _read_csv(csv_path: Path) -> list[dict[str, str]]:
    with csv_path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _write_csv(csv_path: Path, rows: list[dict[str, str]], fieldnames: list[str]) -> None:
    backup = csv_path.with_suffix(".csv.bak")
    shutil.copy2(csv_path, backup)
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)
    log.info("CSV updated. Backup saved to %s", backup)


def _patch_csv_cell(csv_path: Path, data_row: int, column: str, new_value: str) -> None:
    """
    Overwrite a single cell in the CSV identified by its 0-based data row
    index (i.e. 0 = the first non-header row) and column name.
    """
    rows = _read_csv(csv_path)
    if data_row >= len(rows):
        raise IndexError(
            f"data_row={data_row} out of range for CSV with {len(rows)} data rows"
        )
    fieldnames = list(rows[0].keys())
    if column not in fieldnames:
        raise KeyError(f"Column '{column}' not found in CSV. Available: {fieldnames}")
    rows[data_row][column] = new_value
    _write_csv(csv_path, rows, fieldnames)


# --------------------------------------------------------------------------- #
# Fix Registry — ordered by specificity (most specific first)
# --------------------------------------------------------------------------- #

FIX_REGISTRY: list[AgentFix] = [
    FixRejectedOrders(),        # highest priority — human decision overrides all
    FixFileNotFound(),
    FixMissingQuantity(),
    FixFormulaMismatch(),
    FixBusinessDuplicate(),
    FixDataTypeMismatch(),
]
