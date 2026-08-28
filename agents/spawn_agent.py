"""
agents/spawn_agent.py
---------------------
Spawn Agent — the log watcher that continuously monitors
log_analysis/ for new workflow run logs, detects ERROR or WARNING
events, and dispatches a BobRepairAgent sub-process to perform root
cause analysis, apply fixes, and rerun order_workflow.py.

Architecture
    SpawnAgent
        └── watches  log_analysis/*.json  (polling loop)
        └── on new log with ERROR/WARNING → spawns BobRepairAgent
                └── analyse()   → build RCA report
                └── fix()       → apply one or more AgentFix actions
                └── rerun()     → invoke order_workflow.py
                └── verify()    → load post-run log, assert CLEAN or re-escalate

Usage
    python agents/spawn_agent.py                      # watch mode (infinite)
    python agents/spawn_agent.py --once               # process current logs and exit
    python agents/spawn_agent.py --log-dir log_analysis --poll-interval 10

---------------------------------------------------------------------------
Reporting Agent Prompt  (spawn_agent_report_prompt_2.txt)
---------------------------------------------------------------------------
Role
    You are a reporting agent. Your job is to read two JSON files produced by
    the order history workflow, combine their contents into a clean,
    customer-facing report, render that report as an interactive Streamlit app,
    and trigger an email alert when the run contains errors or warnings.

Inputs
    1. Log file   — the most recent file in log_analysis/ matching
                    workflow_log_*.json. Contains the events array (workflow
                    run audit trail) and metadata (run_id, level_counts,
                    overall_status).
    2. Output file — generated_output/order_history_processed.json.
                    Contains metadata (row_count, issue_count), orders (the
                    cleaned records), and aggregates.

    Locate both files, parse them as JSON, and use them together as described
    below.  Do not fabricate or infer data that isn't present in either file.

Step 1 — Build the Summary Section
    At the top of the report, write a short plain-language summary containing
    only:
    - Count of records in source: total rows originally read from the input
      CSV (before any rows were dropped as hard duplicates).
    - Count of records processed successfully: rows present in the final
      orders array with no unresolved error-level issue against them.
    - Count of rejected/flagged records: rows with at least one error or
      warning severity entry in the validation trail, followed by a one-line
      summary of why (group by issue_type, e.g. "3 formula mismatches,
      1 missing quantity, 2 business duplicates").

    Keep this section to a short paragraph or a few bullet points — no raw
    JSON, no internal field names, no stage-by-stage narration.

Step 2 — Build Table 1: Processing Summary
    Scan the log file's events array and produce one row per event that
    represents a validation/audit outcome (i.e. events under stage
    "validation" or any stage-level SUCCESS/WARNING/ERROR event).
    Columns, in this exact order:

        Stage | Level | Level Colour | Event | Message | Order Id

    - Stage       : the stage field.
    - Level       : the level field (SUCCESS, WARNING, ERROR; treat INFO as
                    SUCCESS for display purposes).
    - Level Colour: derived — Success → Green, Warning → Yellow, Error → Red.
                    Render as a colored badge/cell, not just the word.
    - Event       : the event field.
    - Message     : the message field, in plain language (no stack traces).
    - Order Id    : the order_id field from details if present, else blank.

    Only include rows a customer would find meaningful — omit purely internal
    stage_start bookkeeping events with no order-level relevance unless they
    carry a warning/error.

Step 3 — Build Table 2: Final Output
    From the output file's orders array, produce one row per order with
    columns in this exact order:

        OrderID | Customer ID | Customer Name | ProductID | ProductName |
        Quantity | UnitPrice | Total Amount | OrderDate | Status | Region |
        ExpectedTotalAmount

    Map directly from the corresponding fields in each order record.
    Format currency fields to 2 decimal places.

Step 4 — Formatting Rules
    - The report must be presentable to an external customer: no internal
      run IDs, file paths, stack traces, or engineering jargon.
    - Use a clean layout: Summary text → Table 1 → Table 2, in that order,
      with clear section headings.
    - Do not include any field, event, or note not explicitly requested above.

Step 5 — Render as a Streamlit App
    Build a Streamlit app (app.py) that displays the same report interactively:
    - Header      : report title (e.g. "Order Processing Report — Run <run_id>").
    - Summary     : the Step 1 counts shown as st.metric cards (Source Records /
                    Processed Successfully / Rejected) plus the reason summary
                    as text underneath.
    - Table 1     : render with the Level Colour column as an actual colored
                    badge/cell (Green/Yellow/Red), e.g. via a
                    pandas.Styler.applymap background-color function rendered
                    through st.dataframe (or st.write with
                    unsafe_allow_html=True if using HTML badges). Do not just
                    print the colour name as plain text.
    - Table 2     : render as a standard st.dataframe, with currency columns
                    (UnitPrice, Total Amount, ExpectedTotalAmount) formatted
                    to 2 decimal places.
    - Email banner: after the email logic in Step 6 runs, show
                    st.success("Alert email sent to soumya.roy.1982@gmail.com")
                    if an email was sent, or
                    st.info("No issues found — no alert email sent") if not.
    - Keep the same customer-facing constraint from Step 4: no run IDs beyond
      the header, no file paths, no stack traces, no internal field names.

Step 6 — Email Alert
    After building Table 1, check whether it contains any row with
    Level = Warning or Error.
    - If yes: send an email to soumya.roy.1982@gmail.com with:
        Subject : Order Workflow Alert — <count> issue(s) found (Run <run_id>)
        Body    : the Summary section text plus the full Table 1
                  (errors/warnings only, not the clean rows).
    - If no: do not send any email.
    - Trigger this automatically whenever the report is generated (i.e. on
      Streamlit app load / on each new run), not on a manual button click,
      unless the agent's environment requires a button to avoid duplicate
      sends on every page refresh — in that case, use st.session_state to
      ensure the email is sent at most once per run.
    - Use environment variables or the agent's existing secrets mechanism for
      SMTP/email credentials — never hardcode credentials in the app.

Output
    Deliver two things:
    1. The Streamlit app (app.py) showing Summary + Table 1 (colour-coded) +
       Table 2, ready to run with `streamlit run app.py`.
    2. Confirmation of whether the alert email was sent for this run, and to
       whom.
---------------------------------------------------------------------------
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import smtplib
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path
from typing import Any

# ── project root so we can run order_workflow from any cwd ──────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Make sure sibling modules (agents/) are importable
sys.path.insert(0, str(PROJECT_ROOT / "agents"))

from bob_repair_agent import BobRepairAgent  # noqa: E402

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | [SpawnAgent] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("spawn_agent")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _load_log(path: Path) -> dict[str, Any]:
    """Parse a workflow JSON audit log and return its dict."""
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def _needs_attention(log_data: dict[str, Any]) -> bool:
    """Return True when the log has any ERROR or WARNING events."""
    counts = log_data.get("level_counts", {})
    return counts.get("ERROR", 0) > 0 or counts.get("WARNING", 0) > 0


def _extract_issues(log_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Pull every ERROR/WARNING event out of the log."""
    return [
        e for e in log_data.get("events", [])
        if e.get("level") in ("ERROR", "WARNING")
    ]


# --------------------------------------------------------------------------- #
# SpawnAgent
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Escalation e-mail helper (HITL #4)
# --------------------------------------------------------------------------- #

def _send_escalation_email(run_id: str, detail: str) -> None:
    """
    Send a plain-text escalation e-mail when all repair attempts are exhausted.

    Required environment variables (all optional -- if absent, email is skipped):
        SMTP_HOST        SMTP server hostname  (default: smtp.gmail.com)
        SMTP_PORT        SMTP port             (default: 587)
        SMTP_USER        Sender address / login
        SMTP_PASS        Sender password / app-password
        ESCALATION_EMAIL Recipient address
    """
    recipient = os.environ.get("ESCALATION_EMAIL", "")
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")
    if not (recipient and smtp_user and smtp_pass):
        log.warning(
            "[%s] Escalation e-mail skipped -- SMTP_USER / SMTP_PASS / "
            "ESCALATION_EMAIL env vars not set.",
            run_id,
        )
        return

    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "587"))

    subject = (
        f"[ORDER WORKFLOW] Repair exhausted -- manual intervention required ({run_id})"
    )
    body = (
        f"Run ID : {run_id}\n\n"
        "All automatic repair attempts have been exhausted.\n"
        "Manual review is required.\n\n"
        f"{detail}"
    )

    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = smtp_user
    msg["To"] = recipient

    try:
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, [recipient], msg.as_string())
        log.info("[%s] Escalation e-mail sent to %s", run_id, recipient)
    except Exception as exc:  # noqa: BLE001
        log.warning("[%s] Failed to send escalation e-mail: %s", run_id, exc)


# --------------------------------------------------------------------------- #
# SpawnAgent
# --------------------------------------------------------------------------- #

class SpawnAgent:
    """
    Watches the log directory for new workflow run logs.
    Spawns a BobRepairAgent whenever errors or warnings are detected.
    """

    def __init__(
        self,
        log_dir: Path = PROJECT_ROOT / "log_analysis",
        poll_interval: int = 10,
        max_repair_attempts: int = 3,
        auto_approve: bool = False,
    ):
        self.log_dir = log_dir
        self.poll_interval = poll_interval
        self.max_repair_attempts = max_repair_attempts
        # When True, skip the interactive y/N approval prompt (CI / unattended use).
        self.auto_approve = auto_approve
        # Keep track of log files we have already processed so we only act
        # on genuinely new logs.
        self._processed: set[str] = set()

    # ------------------------------------------------------------------ #
    # Public interface
    # ------------------------------------------------------------------ #

    def run_once(self) -> None:
        """Process every unprocessed log file in log_dir exactly once."""
        log_files = sorted(self.log_dir.glob("workflow_log_*.json"))
        if not log_files:
            log.info("No workflow log files found in %s", self.log_dir)
            return
        for lf in log_files:
            self._handle_log_file(lf)

    def watch(self) -> None:
        """
        Poll log_dir indefinitely.  New log files that contain ERROR /
        WARNING events are dispatched to BobRepairAgent.
        Press Ctrl+C to stop.
        """
        log.info(
            "SpawnAgent started — watching %s every %ds",
            self.log_dir,
            self.poll_interval,
        )
        # Seed _processed with whatever already exists so we don't re-process
        # old logs on first boot.
        for lf in sorted(self.log_dir.glob("workflow_log_*.json")):
            self._processed.add(lf.name)
        log.info("Seeded %d existing log(s); waiting for new runs…", len(self._processed))

        try:
            while True:
                for lf in sorted(self.log_dir.glob("workflow_log_*.json")):
                    if lf.name not in self._processed:
                        self._handle_log_file(lf)
                time.sleep(self.poll_interval)
        except KeyboardInterrupt:
            log.info("SpawnAgent stopped by user.")

    # ------------------------------------------------------------------ #
    # Internal helpers
    # ------------------------------------------------------------------ #

    def _handle_log_file(self, log_path: Path) -> None:
        """Decide whether a log file needs repair, and if so dispatch an agent."""
        self._processed.add(log_path.name)

        try:
            log_data = _load_log(log_path)
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("Could not parse %s: %s", log_path.name, exc)
            return

        run_id = log_data.get("run_id", log_path.stem)
        status = log_data.get("overall_status", "UNKNOWN")

        if not _needs_attention(log_data):
            log.info("[%s] status=%s — clean run, no action needed.", run_id, status)
            return

        issues = _extract_issues(log_data)
        log.warning(
            "[%s] status=%s — %d issue(s) detected. Spawning BobRepairAgent…",
            run_id,
            status,
            len(issues),
        )

        self._spawn_repair_agent(run_id, log_data, issues)

    def _spawn_repair_agent(
        self,
        run_id: str,
        log_data: dict[str, Any],
        issues: list[dict[str, Any]],
    ) -> None:
        """Instantiate BobRepairAgent and drive the analyse → fix → rerun → verify cycle."""
        agent = BobRepairAgent(
            run_id=run_id,
            log_data=log_data,
            issues=issues,
            project_root=PROJECT_ROOT,
            max_attempts=self.max_repair_attempts,
        )

        for attempt in range(1, self.max_repair_attempts + 1):
            log.info("[%s] Repair attempt %d / %d", run_id, attempt, self.max_repair_attempts)

            # 1. Root Cause Analysis
            rca = agent.analyse()
            log.info("[%s] RCA summary: %s", run_id, rca["summary"])

            if not rca["fixable"]:
                log.error(
                    "[%s] Issues are not auto-fixable. Manual intervention required.\n%s",
                    run_id,
                    rca["detail"],
                )
                return

            # ── HITL #1: Approval gate ────────────────────────────────────────
            # Show the operator the full RCA and ask for explicit confirmation
            # before any fix mutates source files or reruns the workflow.
            print("\n" + rca["detail"])
            print(
                f"\n[{run_id}] Proposed fixes ({len(rca['recommended_fixes'])}):"
            )
            for fix_name, issue in rca["recommended_fixes"]:
                print(f"  * {fix_name}  |  {issue.get('message', '')}")

            if self.auto_approve:
                log.info("[%s] --auto-approve set -- skipping interactive prompt.", run_id)
                approved = True
            else:
                try:
                    answer = input(
                        f"\n[{run_id}] Apply these fixes and rerun? [y/N]: "
                    ).strip().lower()
                except EOFError:
                    # Non-interactive environment (e.g. piped stdin) -- default to N
                    answer = "n"
                approved = answer == "y"

            if not approved:
                log.info(
                    "[%s] Repair skipped by operator at attempt %d. "
                    "No changes made.",
                    run_id,
                    attempt,
                )
                return
            # ── end HITL #1 ───────────────────────────────────────────────────

            # 2. Apply fixes
            fixes_applied = agent.fix(rca)
            log.info("[%s] Applied %d fix(es): %s", run_id, len(fixes_applied), fixes_applied)

            # ── HITL #2: Business-duplicate hold ─────────────────────────────
            # If any "fix" was purely an acknowledgement (no data change), the
            # duplicate rows still exist in the CSV.  Rerunning now would just
            # re-flag them.  Block and ask the operator to resolve them first.
            acknowledge_only = [
                f for f in fixes_applied if "business_duplicate" in f.lower()
            ]
            if acknowledge_only:
                log.warning(
                    "[%s] Business duplicate(s) require manual review before "
                    "rerun.  The following orders were flagged:\n  %s\n"
                    "  Review the source CSV, remove or correct the duplicate "
                    "rows, then rerun order_workflow.py manually.",
                    run_id,
                    "\n  ".join(acknowledge_only),
                )
                # Write a human-readable review file next to the log for
                # the operator to consult.
                review_path = (
                    PROJECT_ROOT / "log_analysis"
                    / f"duplicate_review_{run_id}.txt"
                )
                review_path.write_text(
                    f"Run ID: {run_id}\n\n"
                    "The following business duplicates require manual resolution "
                    "before the workflow can be rerun cleanly:\n\n"
                    + "\n".join(f"  - {item}" for item in acknowledge_only)
                    + "\n\nSteps:\n"
                    "  1. Open order_history.csv\n"
                    "  2. Identify the duplicate OrderIDs listed above\n"
                    "  3. Delete or correct the erroneous row\n"
                    "  4. Re-run:  python order_workflow.py\n",
                    encoding="utf-8",
                )
                log.info("[%s] Review instructions written to %s", run_id, review_path)
                return
            # ── end HITL #2 ───────────────────────────────────────────────────

            # 3. Rerun the workflow
            new_log_path = agent.rerun()
            log.info("[%s] Workflow rerun complete. New log: %s", run_id, new_log_path)

            # 4. Verify the rerun result
            verdict = agent.verify(new_log_path)
            if verdict["clean"]:
                log.info(
                    "[%s] Repair successful after %d attempt(s). "
                    "New run_id: %s",
                    run_id,
                    attempt,
                    verdict["new_run_id"],
                )
                # Mark the new log as already processed so we don't loop
                self._processed.add(Path(new_log_path).name)
                return

            log.warning(
                "[%s] Repair attempt %d did not fully resolve issues: %s",
                run_id,
                attempt,
                verdict["remaining_issues"],
            )
            # Update the issue list for the next iteration
            agent.issues = verdict["remaining_issues"]

        # ── HITL #4: Escalation on exhaustion ────────────────────────────────
        # All automated attempts exhausted.  Send an e-mail so the problem
        # never silently disappears.  If SMTP env vars are not set the helper
        # logs a warning and moves on -- no hard failure.
        log.error(
            "[%s] Exhausted %d repair attempt(s). "
            "Escalating to manual review.",
            run_id,
            self.max_repair_attempts,
        )
        rca_detail = agent.analyse().get("detail", "No detail available.")
        _send_escalation_email(run_id, rca_detail)
        # ── end HITL #4 ───────────────────────────────────────────────────────


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="SpawnAgent -- monitors workflow logs and auto-repairs issues"
    )
    parser.add_argument(
        "--once",
        action="store_true",
        help="Process existing logs once then exit (instead of watching)",
    )
    parser.add_argument(
        "--log-dir",
        default=str(PROJECT_ROOT / "log_analysis"),
        help="Directory containing workflow_log_*.json files",
    )
    parser.add_argument(
        "--poll-interval",
        type=int,
        default=10,
        help="Seconds between directory scans in watch mode (default: 10)",
    )
    parser.add_argument(
        "--max-repair-attempts",
        type=int,
        default=3,
        help="How many repair+rerun cycles to attempt before escalating",
    )
    parser.add_argument(
        "--auto-approve",
        action="store_true",
        help=(
            "Skip the interactive y/N approval prompt and apply fixes "
            "automatically.  Use in CI / unattended pipelines."
        ),
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    agent = SpawnAgent(
        log_dir=Path(args.log_dir),
        poll_interval=args.poll_interval,
        max_repair_attempts=args.max_repair_attempts,
        auto_approve=args.auto_approve,
    )
    if args.once:
        agent.run_once()
    else:
        agent.watch()
    return 0


if __name__ == "__main__":
    sys.exit(main())
