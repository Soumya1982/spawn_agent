"""
order_workflow.py
------------------
An end-to-end CSV -> JSON transformation workflow for order history data.

Pipeline stages
    1. Load            - read the raw CSV into a DataFrame
    2. Data type check - infer each column/cell's actual data type and flag
                          mismatches against the expected schema
    3. Duplicate check  - flag exact and "business" duplicates
    4. Formatting       - clean text case/whitespace and numeric formats
    5. Formula check    - verify TotalAmount == Quantity * UnitPrice
    6. Aggregation      - roll up sales by region / product / customer / status
    7. Export           - write cleaned records + aggregates + validation
                           report to <project>/generated_output/
    8. Deploy           - copy the JSON output to a "storage" location
                          (local folder by default; Azure Blob hook included
                          but commented out, ready for your Azure stack)

Logging
    Every stage emits both:
      - a normal human-readable console log line (via the standard
        `logging` module), and
      - a structured JSON event appended to an in-memory audit trail.
    The full audit trail (successes AND failures, per stage) is written at
    the end of the run to <project>/log_analysis/workflow_log_<run_id>.json,
    regardless of whether the run succeeded or raised an exception.

Usage (VS Code / terminal)
    python -m venv .venv
    source .venv/bin/activate        # (Windows: .venv\\Scripts\\activate)
    pip install -r requirements.txt
    python order_workflow.py --input order_history.csv

    Defaults (relative to wherever you run the script from, i.e. your
    VS Code project root):
        --output-dir  generated_output
        --log-dir     log_analysis
        --deploy-dir  deployed

Author: generated for Soumya
"""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

# --------------------------------------------------------------------------- #
# Console logging setup (human-readable, separate from the JSON audit trail)
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("order_workflow")


# --------------------------------------------------------------------------- #
# JSON Audit Logger
# --------------------------------------------------------------------------- #
class JsonAuditLogger:
    """
    Collects structured (success + failure) events for every stage of the
    workflow and writes them out as a single JSON file. This is separate
    from the per-row `validation_report` embedded in the output data file --
    this log is about the *workflow run itself* (what stages ran, what
    succeeded, what failed, and why).
    """

    def __init__(self, log_dir: Path, run_id: str):
        self.log_dir = log_dir
        self.run_id = run_id
        self.started_at = datetime.now(timezone.utc)
        self.entries: list[dict[str, Any]] = []
        # Distinguishes "the workflow crashed" from "the data had quality
        # issues" -- a missing quantity or a formula mismatch is a WARNING
        # entry, not a reason to mark the whole run as failed.
        self.workflow_failed = False

    def log(self, level: str, stage: str, event: str, message: str, **details: Any) -> None:
        """
        level: "INFO" | "SUCCESS" | "WARNING" | "ERROR"
        stage: pipeline stage name, e.g. "load", "datatype_check"
        event: short machine-readable event name, e.g. "stage_start"
        """
        entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "run_id": self.run_id,
            "stage": stage,
            "level": level,
            "event": event,
            "message": message,
        }
        if details:
            entry["details"] = details
        self.entries.append(entry)

        if event in (
            "workflow_failed", "load_failed", "file_not_found",
            "export_failed", "deploy_failed", "aggregation_failed",
        ):
            self.workflow_failed = True

        console_fn = {
            "ERROR": logger.error,
            "WARNING": logger.warning,
        }.get(level, logger.info)
        console_fn("[%s] %s: %s", stage, event, message)

    def write(self) -> Path:
        self.log_dir.mkdir(parents=True, exist_ok=True)
        out_path = self.log_dir / f"workflow_log_{self.run_id}.json"

        level_counts = {"INFO": 0, "SUCCESS": 0, "WARNING": 0, "ERROR": 0}
        for e in self.entries:
            level_counts[e["level"]] = level_counts.get(e["level"], 0) + 1

        if self.workflow_failed:
            overall_status = "FAILED"
        elif level_counts["ERROR"] > 0 or level_counts["WARNING"] > 0:
            overall_status = "SUCCESS_WITH_ISSUES"
        else:
            overall_status = "SUCCESS"

        payload = {
            "run_id": self.run_id,
            "started_at": self.started_at.isoformat(),
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "total_events": len(self.entries),
            "level_counts": level_counts,
            # "FAILED" = the workflow itself crashed (e.g. file not found,
            # export failed). "SUCCESS_WITH_ISSUES" = it completed, but
            # row-level data quality problems were flagged (see events with
            # stage="validation"). "SUCCESS" = clean run, no issues at all.
            "overall_status": overall_status,
            "events": self.entries,
        }
        with out_path.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2, ensure_ascii=False, default=str)

        logger.info("JSON audit log written to %s", out_path)
        return out_path


# --------------------------------------------------------------------------- #
# Config
# --------------------------------------------------------------------------- #
@dataclass
class WorkflowConfig:
    input_path: Path
    output_dir: Path = Path("generated_output")
    log_dir: Path = Path("log_analysis")
    deploy_dir: Path = Path("deployed")

    # Columns that uniquely identify a row (hard duplicate check)
    unique_key: list[str] = field(default_factory=lambda: ["OrderID"])
    # Columns that identify a "business" duplicate (same order placed twice
    # under different OrderIDs)
    business_key: list[str] = field(
        default_factory=lambda: ["CustomerID", "ProductID", "Quantity", "OrderDate"]
    )
    # Tolerance (in currency units) allowed between stated TotalAmount and
    # the recomputed Quantity * UnitPrice before it's flagged as a mismatch
    formula_tolerance: float = 0.01
    # If True, TotalAmount is overwritten with the recomputed value whenever
    # a mismatch is found (after logging it in the validation report)
    auto_correct_totals: bool = True

    # ── Two-phase HITL pipeline ──────────────────────────────────────────────
    # Phase 1 (--phase1): validate + transform, write staging JSON, launch
    #   review_app.py so a human can approve/reject flagged rows.
    # Phase 2 (--phase2): read review_decisions.json, apply decisions,
    #   write the final processed JSON + deploy.
    # Running without a phase flag executes the old single-pass behaviour
    # (no human review pop-up) for backward compatibility.
    phase: str = ""   # "" | "phase1" | "phase2"

    # How long to wait (seconds) between polls when --phase1 blocks waiting
    # for the human to finish reviewing (used only in the CLI main()).
    review_poll_interval: int = 3

    # Expected data type per column, used by the data-type validation stage.
    # Allowed values: "string", "integer", "float", "date"
    expected_schema: dict[str, str] = field(
        default_factory=lambda: {
            "OrderID": "string",
            "CustomerID": "string",
            "CustomerName": "string",
            "ProductID": "string",
            "ProductName": "string",
            "Quantity": "integer",
            "UnitPrice": "float",
            "TotalAmount": "float",
            "OrderDate": "date",
            "Status": "string",
            "Region": "string",
        }
    )


# --------------------------------------------------------------------------- #
# Workflow
# --------------------------------------------------------------------------- #
class OrderWorkflow:
    """Encapsulates every stage of the CSV -> JSON transformation pipeline."""

    def __init__(self, config: WorkflowConfig, audit: JsonAuditLogger):
        self.config = config
        self.audit = audit
        self.df: pd.DataFrame | None = None
        # Every validation stage appends structured issue dicts here so the
        # final output's validation_report captures the full per-row audit
        # trail, not just pass/fail.
        self.issues: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    # Stage 1: Load
    # ------------------------------------------------------------------ #
    def load(self) -> "OrderWorkflow":
        stage = "load"
        self.audit.log("INFO", stage, "stage_start", f"Loading CSV from {self.config.input_path}")

        if not self.config.input_path.exists():
            self.audit.log(
                "ERROR", stage, "file_not_found",
                f"Input file not found: {self.config.input_path}",
            )
            raise FileNotFoundError(f"Input file not found: {self.config.input_path}")

        try:
            # dtype=str keeps everything as raw text so formatting/cleaning has
            # full control instead of pandas guessing types (and mangling
            # currency-formatted numbers) at load time.
            self.df = pd.read_csv(self.config.input_path, dtype=str, keep_default_na=True)
            self.df.columns = [c.strip() for c in self.df.columns]
        except Exception as exc:
            self.audit.log("ERROR", stage, "load_failed", f"Failed to read CSV: {exc}")
            raise

        self.audit.log(
            "SUCCESS", stage, "stage_complete",
            f"Loaded {len(self.df)} rows, {len(self.df.columns)} columns",
            row_count=len(self.df), column_count=len(self.df.columns),
        )
        return self

    # ------------------------------------------------------------------ #
    # Stage 2: Data type identification & validation
    # ------------------------------------------------------------------ #
    def check_data_types(self) -> "OrderWorkflow":
        """
        Infers the actual data type of every cell (before cleaning) and
        compares it against `config.expected_schema`. This catches things
        like text in a numeric column, or a date that doesn't parse, so the
        formatting stage's fixes are visible in the audit trail rather than
        silently "fixed."
        """
        stage = "datatype_check"
        self.audit.log("INFO", stage, "stage_start", "Identifying and validating column data types")
        df = self.df
        mismatch_count = 0
        detected_types: dict[str, str] = {}

        for col, expected_type in self.config.expected_schema.items():
            if col not in df.columns:
                self.audit.log(
                    "WARNING", stage, "missing_column",
                    f"Expected column '{col}' not found in input",
                )
                continue

            col_mismatches = 0
            for idx, raw_value in df[col].items():
                inferred = self._infer_type(raw_value)
                if inferred == "empty":
                    continue  # missing-value checks are handled in the formatting stage
                if not self._type_compatible(inferred, expected_type):
                    col_mismatches += 1
                    mismatch_count += 1
                    self._log_issue(
                        "datatype_mismatch",
                        idx,
                        f"Column '{col}' expected type '{expected_type}' but value "
                        f"'{raw_value}' looks like '{inferred}'",
                        severity="warning",
                    )

            detected_types[col] = expected_type if col_mismatches == 0 else "mixed"

        if mismatch_count:
            self.audit.log(
                "WARNING", stage, "mismatches_found",
                f"Found {mismatch_count} data-type mismatch(es) across "
                f"{len(self.config.expected_schema)} column(s)",
                mismatch_count=mismatch_count, detected_types=detected_types,
            )
        else:
            self.audit.log(
                "SUCCESS", stage, "stage_complete",
                "All columns match their expected data types",
                detected_types=detected_types,
            )
        return self

    @staticmethod
    def _infer_type(value: Any) -> str:
        """Best-effort type inference on a raw (pre-cleaning) CSV cell."""
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return "empty"
        s = str(value).strip()
        if s == "" or s.lower() in ("nan", "none", "null"):
            return "empty"

        # Numeric check, tolerant of currency symbols / thousands separators
        numeric_candidate = s.replace("$", "").replace("₹", "").replace(",", "").strip()
        try:
            f = float(numeric_candidate)
            return "integer" if f.is_integer() and "." not in numeric_candidate else "float"
        except ValueError:
            pass

        # Date check
        try:
            pd.to_datetime(s, errors="raise", dayfirst=True)
            # Only treat as a date if it "looks like" one, to avoid classifying
            # plain numbers-as-strings (already caught above) as dates.
            if any(ch in s for ch in ("-", "/")) and any(ch.isdigit() for ch in s):
                return "date"
        except (ValueError, TypeError):
            pass

        return "string"

    @staticmethod
    def _type_compatible(inferred: str, expected: str) -> bool:
        if expected == "string":
            return True  # any non-empty value is valid as a string
        if expected == "integer":
            return inferred in ("integer", "float")  # "2.0" is acceptable for a qty column
        if expected == "float":
            return inferred in ("integer", "float")
        if expected == "date":
            return inferred == "date"
        return True

    # ------------------------------------------------------------------ #
    # Stage 3: Duplicate check
    # ------------------------------------------------------------------ #
    def check_duplicates(self) -> "OrderWorkflow":
        stage = "duplicate_check"
        self.audit.log("INFO", stage, "stage_start", "Checking for duplicates")
        df = self.df

        # Hard duplicates: identical unique key (e.g. same OrderID twice)
        hard_mask = df.duplicated(subset=self.config.unique_key, keep="first")
        for idx in df[hard_mask].index:
            self._log_issue(
                "duplicate_hard",
                idx,
                f"Duplicate {self.config.unique_key} value: "
                f"{df.loc[idx, self.config.unique_key].to_dict()}",
            )

        if hard_mask.any():
            self.audit.log(
                "WARNING", stage, "hard_duplicates_dropped",
                f"Dropping {int(hard_mask.sum())} hard duplicate row(s)",
                dropped_count=int(hard_mask.sum()),
            )
            self.df = df[~hard_mask].copy()
            df = self.df

        # Business duplicates: same customer/product/qty/date but a
        # different OrderID -- likely the same order entered twice.
        # These are flagged, not dropped, since a human should confirm.
        biz_mask = df.duplicated(subset=self.config.business_key, keep=False)
        for idx in df[biz_mask].index:
            self._log_issue(
                "duplicate_business",
                idx,
                f"Possible duplicate order (same {self.config.business_key}): "
                f"OrderID={df.loc[idx, 'OrderID']}",
                severity="warning",
            )

        self.audit.log(
            "SUCCESS", stage, "stage_complete",
            f"Duplicate check complete: {int(hard_mask.sum())} hard duplicate(s) removed, "
            f"{int(biz_mask.sum())} business duplicate(s) flagged",
            hard_duplicates=int(hard_mask.sum()), business_duplicates=int(biz_mask.sum()),
        )
        return self

    # ------------------------------------------------------------------ #
    # Stage 4: Character & number formatting
    # ------------------------------------------------------------------ #
    def clean_formatting(self) -> "OrderWorkflow":
        stage = "formatting"
        self.audit.log("INFO", stage, "stage_start", "Cleaning text and numeric formatting")
        df = self.df

        # --- Text columns: trim whitespace, normalize case ---
        text_title_cols = ["CustomerName", "ProductName", "Region"]
        for col in text_title_cols:
            if col in df.columns:
                df[col] = df[col].astype(str).str.strip().str.title()

        if "Status" in df.columns:
            df["Status"] = df["Status"].astype(str).str.strip().str.capitalize()

        for col in ["OrderID", "CustomerID", "ProductID"]:
            if col in df.columns:
                df[col] = df[col].astype(str).str.strip().str.upper()

        # --- Numeric columns: strip currency symbols / thousands separators ---
        for col in ["Quantity", "UnitPrice", "TotalAmount"]:
            df[col] = df[col].apply(self._clean_number)

        # Quantity should be an integer count
        missing_qty = df["Quantity"].isna()
        for idx in df[missing_qty].index:
            self._log_issue("missing_quantity", idx, "Quantity is missing/blank")
        # Default missing quantity to 1 so downstream formula checks can run;
        # flagged above so it's visible in the audit trail.
        df["Quantity"] = df["Quantity"].fillna(1)
        df["Quantity"] = df["Quantity"].astype(int)

        for col in ["UnitPrice", "TotalAmount"]:
            missing = df[col].isna()
            for idx in df[missing].index:
                self._log_issue(f"missing_{col.lower()}", idx, f"{col} is missing/blank")
            df[col] = df[col].fillna(0.0)

        # --- Date normalization ---
        bad_date_count = 0
        if "OrderDate" in df.columns:
            parsed = pd.to_datetime(df["OrderDate"], errors="coerce", dayfirst=True)
            bad_dates = parsed.isna() & df["OrderDate"].notna()
            bad_date_count = int(bad_dates.sum())
            for idx in df[bad_dates].index:
                self._log_issue(
                    "invalid_date", idx, f"Unparseable OrderDate: {df.loc[idx, 'OrderDate']}"
                )
            df["OrderDate"] = parsed.dt.strftime("%Y-%m-%d")

        self.df = df
        self.audit.log(
            "SUCCESS", stage, "stage_complete",
            f"Formatting complete: {int(missing_qty.sum())} missing quantity, "
            f"{bad_date_count} invalid date(s)",
            missing_quantity=int(missing_qty.sum()), invalid_dates=bad_date_count,
        )
        return self

    @staticmethod
    def _clean_number(value: Any) -> float | None:
        """Strip currency symbols/thousand separators and coerce to float."""
        if pd.isna(value):
            return None
        s = str(value).strip()
        if s == "":
            return None
        s = s.replace("$", "").replace("₹", "").replace(",", "").strip()
        try:
            return float(s)
        except ValueError:
            return None

    # ------------------------------------------------------------------ #
    # Stage 5: Formula check
    # ------------------------------------------------------------------ #
    def check_formulas(self) -> "OrderWorkflow":
        stage = "formula_check"
        self.audit.log(
            "INFO", stage, "stage_start", "Validating TotalAmount = Quantity * UnitPrice"
        )
        df = self.df
        expected = (df["Quantity"] * df["UnitPrice"]).round(2)
        actual = df["TotalAmount"].round(2)
        mismatch = (expected - actual).abs() > self.config.formula_tolerance

        for idx in df[mismatch].index:
            self._log_issue(
                "formula_mismatch",
                idx,
                f"TotalAmount {actual[idx]} != Quantity*UnitPrice {expected[idx]}",
                severity="warning",
            )

        df["ExpectedTotalAmount"] = expected
        if self.config.auto_correct_totals:
            df.loc[mismatch, "TotalAmount"] = expected[mismatch]

        self.df = df
        self.audit.log(
            "SUCCESS" if not mismatch.any() else "WARNING",
            stage, "stage_complete",
            f"Formula check complete: {int(mismatch.sum())} mismatch(es) "
            f"{'auto-corrected' if self.config.auto_correct_totals else 'flagged'}",
            mismatch_count=int(mismatch.sum()),
        )
        return self

    # ------------------------------------------------------------------ #
    # Stage 6: Aggregation
    # ------------------------------------------------------------------ #
    def aggregate(self) -> dict[str, Any]:
        stage = "aggregation"
        self.audit.log("INFO", stage, "stage_start", "Aggregating totals")
        df = self.df

        def agg_by(col: str) -> list[dict[str, Any]]:
            g = (
                df.groupby(col, dropna=False)
                .agg(
                    order_count=("OrderID", "count"),
                    total_quantity=("Quantity", "sum"),
                    total_sales=("TotalAmount", "sum"),
                )
                .reset_index()
            )
            g["total_sales"] = g["total_sales"].round(2)
            return g.to_dict(orient="records")

        try:
            aggregates = {
                "by_region": agg_by("Region"),
                "by_status": agg_by("Status"),
                "by_product": agg_by("ProductName"),
                "by_customer": agg_by("CustomerName"),
                "overall": {
                    "total_orders": int(len(df)),
                    "total_quantity": int(df["Quantity"].sum()),
                    "total_sales": round(float(df["TotalAmount"].sum()), 2),
                },
            }
        except Exception as exc:
            self.audit.log("ERROR", stage, "aggregation_failed", f"Aggregation failed: {exc}")
            raise

        self.audit.log(
            "SUCCESS", stage, "stage_complete",
            f"Aggregated {len(df)} orders across "
            f"{len(aggregates['by_region'])} region(s)",
            overall=aggregates["overall"],
        )
        return aggregates

    # ------------------------------------------------------------------ #
    # Stage 7: Export to JSON
    # ------------------------------------------------------------------ #
    def export(
        self,
        aggregates: dict[str, Any],
        filename: str = "order_history_processed.json",
    ) -> Path:
        stage = "export"
        self.audit.log(
            "INFO", stage, "stage_start", f"Exporting JSON to {self.config.output_dir}"
        )
        try:
            self.config.output_dir.mkdir(parents=True, exist_ok=True)

            payload = {
                "metadata": {
                    "run_id": self.audit.run_id,
                    "source_file": str(self.config.input_path.name),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                    "row_count": int(len(self.df)),
                    "issue_count": len(self.issues),
                },
                "orders": json.loads(self.df.to_json(orient="records")),
                "aggregates": aggregates,
                "validation_report": self.issues,
            }

            out_path = self.config.output_dir / filename
            with out_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2, ensure_ascii=False)
        except Exception as exc:
            self.audit.log("ERROR", stage, "export_failed", f"Failed to export JSON: {exc}")
            raise

        self.audit.log(
            "SUCCESS", stage, "stage_complete",
            f"Wrote {out_path} ({out_path.stat().st_size} bytes)",
            output_path=str(out_path), size_bytes=out_path.stat().st_size,
        )
        return out_path

    # ------------------------------------------------------------------ #
    # Stage 8: Deploy
    # ------------------------------------------------------------------ #
    def deploy(self, file_path: Path) -> Path:
        stage = "deploy"
        self.audit.log(
            "INFO", stage, "stage_start", f"Deploying output to {self.config.deploy_dir}"
        )
        try:
            self.config.deploy_dir.mkdir(parents=True, exist_ok=True)
            dest = self.config.deploy_dir / file_path.name
            shutil.copy2(file_path, dest)

            # --- Azure Blob Storage hook (uncomment and configure to go live) ---
            # import os
            # from azure.storage.blob import BlobServiceClient
            # conn_str = os.environ["AZURE_STORAGE_CONNECTION_STRING"]
            # container = "order-history-processed"
            # blob_service = BlobServiceClient.from_connection_string(conn_str)
            # blob_client = blob_service.get_blob_client(container=container, blob=file_path.name)
            # with open(file_path, "rb") as data:
            #     blob_client.upload_blob(data, overwrite=True)
        except Exception as exc:
            self.audit.log("ERROR", stage, "deploy_failed", f"Failed to deploy output: {exc}")
            raise

        self.audit.log(
            "SUCCESS", stage, "stage_complete", f"Deployed to {dest}", deployed_path=str(dest)
        )
        return dest

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _log_issue(self, issue_type: str, row_index: int, message: str, severity: str = "error"):
        order_id = None
        if self.df is not None and "OrderID" in self.df.columns:
            try:
                order_id = self.df.loc[row_index, "OrderID"]
            except KeyError:
                pass
        entry = {
            "row_index": int(row_index),
            "order_id": order_id,
            "issue_type": issue_type,
            "severity": severity,
            "message": message,
        }
        self.issues.append(entry)
        # Mirror into the JSON audit trail as well, so the run-level log
        # captures every per-row validation failure alongside stage events.
        self.audit.log(
            "ERROR" if severity == "error" else "WARNING",
            "validation", issue_type, message,
            row_index=int(row_index), order_id=order_id,
        )

    # ------------------------------------------------------------------ #
    # Orchestration
    # ------------------------------------------------------------------ #
    def run(self) -> Path:
        """
        Single-pass run (no human review).  Validates, transforms, exports,
        and deploys in one shot.  Used when --phase1 / --phase2 are not set.
        """
        self.load()
        self.check_data_types()
        self.check_duplicates()
        self.clean_formatting()
        self.check_formulas()
        aggregates = self.aggregate()
        out_path   = self.export(aggregates)
        self.deploy(out_path)

        self.audit.log(
            "SUCCESS", "workflow", "workflow_complete",
            f"Workflow complete: {len(self.df)} rows processed, "
            f"{len(self.issues)} validation issue(s) logged",
            row_count=len(self.df), issue_count=len(self.issues),
        )
        return out_path

    # ------------------------------------------------------------------ #
    # Phase 1: validate + transform → write staging JSON
    # ------------------------------------------------------------------ #
    def run_phase1(self) -> Path:
        """
        Validate and transform the CSV, then write a staging JSON file.
        Does NOT write the final processed JSON or deploy anything.
        Returns the path to the staging file.
        """
        self.load()
        self.check_data_types()
        self.check_duplicates()
        self.clean_formatting()
        self.check_formulas()
        aggregates = self.aggregate()

        staging_path = self.export(
            aggregates,
            filename="order_history_staging.json",
        )

        self.audit.log(
            "INFO", "workflow", "phase1_complete",
            f"Phase 1 complete: {len(self.df)} rows processed, "
            f"{len(self.issues)} issue(s) flagged. "
            "Staging file written — awaiting human review.",
            row_count=len(self.df),
            issue_count=len(self.issues),
            staging_path=str(staging_path),
        )
        return staging_path

    # ------------------------------------------------------------------ #
    # Phase 2: apply review decisions → write final JSON + deploy
    # ------------------------------------------------------------------ #
    def run_phase2(self) -> Path:
        """
        Read the staging JSON and review_decisions.json, filter out rejected
        orders, rebuild aggregates from approved rows only, then export and
        deploy the final processed JSON.
        """
        output_dir   = self.config.output_dir
        staging_path = output_dir / "order_history_staging.json"
        review_path  = output_dir / "review_decisions.json"

        if not staging_path.exists():
            raise FileNotFoundError(
                f"Staging file not found: {staging_path}. "
                "Run --phase1 first."
            )
        if not review_path.exists():
            raise FileNotFoundError(
                f"review_decisions.json not found: {review_path}. "
                "Human review must be completed first."
            )

        # Load staging data
        with staging_path.open(encoding="utf-8") as f:
            staging = json.load(f)

        decisions: dict[str, str] = json.loads(
            review_path.read_text(encoding="utf-8")
        )

        rejected_ids = {
            oid for oid, dec in decisions.items() if dec == "Rejected"
        }
        approved_ids = {
            oid for oid, dec in decisions.items() if dec == "Approved"
        }

        self.audit.log(
            "INFO", "phase2", "decisions_loaded",
            f"Review decisions loaded: {len(approved_ids)} approved, "
            f"{len(rejected_ids)} rejected.",
            approved=list(approved_ids),
            rejected=list(rejected_ids),
        )

        # Reconstruct the DataFrame from staging — apply decisions
        all_orders = staging.get("orders", [])
        kept_orders = [
            o for o in all_orders
            if str(o.get("OrderID", "")) not in rejected_ids
        ]

        self.df = pd.DataFrame(kept_orders)
        # Re-cast numeric columns that json round-tripped as float/int
        for col in ["Quantity"]:
            if col in self.df.columns:
                self.df[col] = pd.to_numeric(self.df[col], errors="coerce").fillna(1).astype(int)
        for col in ["UnitPrice", "TotalAmount", "ExpectedTotalAmount"]:
            if col in self.df.columns:
                self.df[col] = pd.to_numeric(self.df[col], errors="coerce").fillna(0.0)

        # Keep only validation issues for the rows that were NOT rejected
        full_val_report = staging.get("validation_report", [])
        self.issues = [
            v for v in full_val_report
            if str(v.get("order_id", "")) not in rejected_ids
        ]

        if rejected_ids:
            self.audit.log(
                "INFO", "phase2", "rejected_orders_excluded",
                f"Excluded {len(rejected_ids)} rejected order(s) from output: "
                f"{sorted(rejected_ids)}",
                rejected=sorted(rejected_ids),
            )

        # Re-aggregate with the filtered rows
        aggregates = self.aggregate()

        # Export final JSON
        out_path = self.export(aggregates)

        # Clean up staging file — it's no longer needed
        try:
            staging_path.unlink()
        except OSError:
            pass

        # Clean up the signal file
        signal_path = output_dir / ".review_complete"
        try:
            signal_path.unlink(missing_ok=True)
        except OSError:
            pass

        self.deploy(out_path)

        self.audit.log(
            "SUCCESS", "workflow", "workflow_complete",
            f"Phase 2 complete: {len(self.df)} rows in final output, "
            f"{len(rejected_ids)} rejected, "
            f"{len(self.issues)} issue(s) in validation report.",
            row_count=len(self.df),
            rejected_count=len(rejected_ids),
            issue_count=len(self.issues),
        )
        return out_path


# --------------------------------------------------------------------------- #
# CLI entry point
# --------------------------------------------------------------------------- #
def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="CSV -> JSON order history workflow",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Two-phase HITL usage:\n"
            "  python order_workflow.py --phase1   # validate, open review pop-up\n"
            "  # (review_app.py auto-launched; approve/reject flagged orders)\n"
            "  python order_workflow.py --phase2   # apply decisions, export, deploy\n\n"
            "Single-pass (no review pop-up):\n"
            "  python order_workflow.py\n"
        ),
    )
    parser.add_argument("--input", type=str, default="order_history.csv",
                        help="Path to input CSV")
    parser.add_argument("--output-dir", type=str, default="generated_output",
                        help="Folder for processed JSON output")
    parser.add_argument("--log-dir", type=str, default="log_analysis",
                        help="Folder for JSON audit logs")
    parser.add_argument("--deploy-dir", type=str, default="deployed",
                        help="Deploy target folder")
    parser.add_argument("--phase1", action="store_true",
                        help="Run phase 1: validate + transform, launch review pop-up")
    parser.add_argument("--phase2", action="store_true",
                        help="Run phase 2: apply review decisions, export final JSON + deploy")
    return parser.parse_args()


def _wait_for_review(signal_path: Path, poll_interval: int = 3) -> None:
    """Block until review_app.py writes the .review_complete signal file."""
    logger.info(
        "Waiting for human review to complete in Streamlit… "
        "(submit decisions in the browser to continue)"
    )
    while not signal_path.exists():
        time.sleep(poll_interval)
    logger.info("Review complete signal received.")


def main() -> int:
    args = parse_args()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "_" + uuid.uuid4().hex[:8]

    # Determine which phase to run
    if args.phase1 and args.phase2:
        logger.error("Cannot pass both --phase1 and --phase2. Choose one.")
        return 1

    phase = "phase1" if args.phase1 else ("phase2" if args.phase2 else "")

    # Always resolve paths to absolute so subprocess invocations and
    # review_app.py (which anchors to its own __file__) all agree on the
    # same locations regardless of the working directory.
    project_root = Path(__file__).resolve().parent
    input_path   = (project_root / args.input).resolve()
    output_dir   = (project_root / args.output_dir).resolve()
    log_dir      = (project_root / args.log_dir).resolve()
    deploy_dir   = (project_root / args.deploy_dir).resolve()

    config = WorkflowConfig(
        input_path=input_path,
        output_dir=output_dir,
        log_dir=log_dir,
        deploy_dir=deploy_dir,
        phase=phase,
    )
    audit = JsonAuditLogger(config.log_dir, run_id)

    # Signal file lives in the same folder as review_app.py expects it
    signal_file = output_dir / ".review_complete"
    # Clean up any stale signal from a previous interrupted run
    signal_file.unlink(missing_ok=True)

    exit_code = 0
    try:
        workflow = OrderWorkflow(config, audit)

        if phase == "phase1":
            # ── Phase 1: validate, transform, write staging, open review UI ──
            workflow.run_phase1()
            audit.write()   # flush log before blocking

            review_app = project_root / "review_app.py"
            logger.info("Launching review app: streamlit run %s", review_app)
            subprocess.Popen(
                [sys.executable, "-m", "streamlit", "run", str(review_app)],
                cwd=str(project_root),
            )

            # Block here in the terminal until the reviewer clicks Submit
            _wait_for_review(signal_file, config.review_poll_interval)

            # Kick off phase 2 in a new process (fresh run_id + audit log)
            logger.info("Review complete — launching phase 2…")
            subprocess.Popen(
                [
                    sys.executable, str(project_root / "order_workflow.py"),
                    "--phase2",
                    "--input",       str(input_path),
                    "--output-dir",  str(output_dir),
                    "--log-dir",     str(log_dir),
                    "--deploy-dir",  str(deploy_dir),
                ],
                cwd=str(project_root),
            )
            return 0

        elif phase == "phase2":
            # ── Phase 2: apply decisions, export final JSON, deploy ───────────
            workflow.run_phase2()

        else:
            # ── Single-pass (no human review) — backward compatible ───────────
            workflow.run()

    except Exception:
        exit_code = 1
        audit.log(
            "ERROR", "workflow", "workflow_failed",
            "Workflow terminated due to an unhandled exception",
            traceback=traceback.format_exc(),
        )
        logger.error("Workflow failed. See the JSON audit log for details.")
    finally:
        audit.write()

    if exit_code == 0 and phase != "phase1":
        app_path = project_root / "app.py"
        logger.info("Launching Streamlit report: streamlit run %s", app_path)
        subprocess.Popen([sys.executable, "-m", "streamlit", "run", str(app_path)])

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
