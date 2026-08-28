"""
agents/bob_repair_agent.py
---------------------------
IBM Bob Repair Agent — performs root cause analysis (RCA) on a failed or
degraded order_workflow run, applies one or more AgentFix actions from
agent_fixes.py, reruns order_workflow.py, and verifies the outcome.

This module is instantiated by SpawnAgent and can also be run standalone:

    python agents/bob_repair_agent.py --log log_analysis/workflow_log_<id>.json

Repair lifecycle
    1. analyse()  → inspect every ERROR/WARNING event, classify each by
                    severity, identify which AgentFix applies, and produce
                    a structured RCA report.
    2. fix(rca)   → iterate through the RCA's recommended fixes in priority
                    order and apply them.
    3. rerun()    → invoke order_workflow.py as a subprocess; capture the
                    path of the new log it writes.
    4. verify()   → parse the new log; return {"clean": True} if the run
                    either has no issues or only residual warnings that are
                    already acknowledged.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

# ── project root resolution ─────────────────────────────────────────────────
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "agents"))

from agent_fixes import AgentFix, FIX_REGISTRY  # noqa: E402

# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)-8s | [BobRepairAgent] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bob_repair_agent")


# --------------------------------------------------------------------------- #
# Severity & priority helpers
# --------------------------------------------------------------------------- #

# How we classify each event type for the RCA report
_SEVERITY_MAP: dict[str, str] = {
    # CRITICAL — workflow cannot continue, must fix before rerun
    "file_not_found":   "critical",
    "load_failed":      "critical",
    "export_failed":    "critical",
    "deploy_failed":    "critical",
    "aggregation_failed": "critical",
    "workflow_failed":  "critical",
    # HIGH — data is wrong / missing
    "missing_quantity": "high",
    "missing_unitprice": "high",
    "missing_totalamount": "high",
    "datatype_mismatch": "high",
    # MEDIUM — formula / calculation issue
    "formula_mismatch": "medium",
    # LOW — potential duplicate, human review needed
    "duplicate_business": "low",
    "duplicate_hard":   "medium",
    # INFORMATIONAL
    "missing_column":   "low",
    # HUMAN DECISION — reviewer explicitly rejected an order
    "order_rejected_by_reviewer": "high",
}

# Issues at these severities block a clean pass
_BLOCKING_SEVERITIES = {"critical", "high", "medium"}

# These events will cause us NOT to mark a rerun as "fixable" automatically
_MANUAL_ONLY_EVENTS = {"load_failed", "export_failed", "aggregation_failed",
                       "deploy_failed", "workflow_failed"}


# --------------------------------------------------------------------------- #
# RCA dataclass (plain dict for JSON-serialisability)
# --------------------------------------------------------------------------- #

def _build_rca(
    issues: list[dict[str, Any]],
    fix_registry: list[AgentFix],
) -> dict[str, Any]:
    """
    Build a Root Cause Analysis report from the issue list.

    Returns a dict with:
        summary         – one-line human-readable summary
        detail          – verbose multi-line breakdown
        fixable         – True if ALL critical/high/medium issues have a matching fix
        recommended_fixes – list of (fix_name, issue) pairs in priority order
        unresolvable    – issues for which no automatic fix exists
    """
    classified: list[dict[str, Any]] = []
    recommended: list[tuple[str, dict[str, Any]]] = []
    unresolvable: list[dict[str, Any]] = []

    for issue in issues:
        event = issue.get("event", "unknown")
        sev = _SEVERITY_MAP.get(event, "low")
        issue_enriched = {**issue, "_severity": sev}
        classified.append(issue_enriched)

        matched_fix: AgentFix | None = None
        for fix in fix_registry:
            if fix.matches(issue):
                matched_fix = fix
                break

        if matched_fix:
            recommended.append((matched_fix.name, issue))
        elif sev in _BLOCKING_SEVERITIES:
            unresolvable.append(issue_enriched)

    # Build a severity summary string
    sev_counts: dict[str, int] = {}
    for c in classified:
        s = c["_severity"]
        sev_counts[s] = sev_counts.get(s, 0) + 1

    sev_str = ", ".join(f"{v} {k}" for k, v in sev_counts.items())
    summary = f"{len(issues)} issue(s) detected ({sev_str})"
    if unresolvable:
        summary += f"; {len(unresolvable)} require(s) manual intervention"

    # Build detail text
    detail_lines = ["=== Root Cause Analysis ==="]
    for c in classified:
        detail_lines.append(
            f"  [{c['_severity'].upper():8s}] stage={c.get('stage','?')} "
            f"event={c.get('event','?')} | {c.get('message','')}"
        )
    if unresolvable:
        detail_lines.append("\n=== Unresolvable (manual fix required) ===")
        for u in unresolvable:
            detail_lines.append(f"  • {u.get('event')}: {u.get('message')}")

    fixable = len(unresolvable) == 0

    return {
        "summary": summary,
        "detail": "\n".join(detail_lines),
        "fixable": fixable,
        "recommended_fixes": recommended,
        "unresolvable": unresolvable,
        "classified_issues": classified,
    }


# --------------------------------------------------------------------------- #
# BobRepairAgent
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Review-decision loader
# --------------------------------------------------------------------------- #

def _load_review_decisions(project_root: Path) -> dict[str, str]:
    """
    Read generated_output/review_decisions.json and return {order_id: decision}.
    Returns an empty dict if the file does not exist or cannot be parsed.
    """
    review_path = project_root / "generated_output" / "review_decisions.json"
    if not review_path.exists():
        return {}
    try:
        return json.loads(review_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _build_rejection_issues(
    decisions: dict[str, str],
) -> list[dict[str, Any]]:
    """
    Convert every "Rejected" entry in review_decisions into a synthetic issue
    dict that the existing RCA / fix pipeline can handle.

    The synthetic event name "order_rejected_by_reviewer" is matched by
    FixRejectedOrders in agent_fixes.py.
    """
    return [
        {
            "level":   "ERROR",
            "stage":   "human_review",
            "event":   "order_rejected_by_reviewer",
            "message": (
                f"Order {oid} was marked 'Rejected' by a human reviewer "
                "and must be excluded from the processed output."
            ),
            "details": {"order_id": oid},
        }
        for oid, decision in decisions.items()
        if decision == "Rejected"
    ]


class BobRepairAgent:
    """
    IBM Bob Repair Agent.
    Instantiated by SpawnAgent once per detected problem run.
    """

    def __init__(
        self,
        run_id: str,
        log_data: dict[str, Any],
        issues: list[dict[str, Any]],
        project_root: Path = PROJECT_ROOT,
        max_attempts: int = 3,
    ):
        self.run_id = run_id
        self.log_data = log_data
        self.project_root = project_root
        self.max_attempts = max_attempts
        self._fix_registry = FIX_REGISTRY

        # Merge any human review rejections into the issue list so the
        # existing RCA/fix pipeline handles them alongside workflow errors.
        rejection_issues = _build_rejection_issues(
            _load_review_decisions(project_root)
        )
        if rejection_issues:
            log.info(
                "[%s] Found %d rejected order(s) in review_decisions.json — "
                "adding to issue list.",
                run_id,
                len(rejection_issues),
            )
        self.issues = rejection_issues + issues

    # ------------------------------------------------------------------ #
    # Step 1: Root Cause Analysis
    # ------------------------------------------------------------------ #

    def analyse(self) -> dict[str, Any]:
        """
        Build and return an RCA report for the current issue list.

        The report tells SpawnAgent whether the problems are auto-fixable
        and which fix actions to apply.
        """
        log.info("[%s] Running root cause analysis on %d issue(s)…", self.run_id, len(self.issues))
        rca = _build_rca(self.issues, self._fix_registry)
        log.info("[%s] RCA: %s", self.run_id, rca["summary"])
        if not rca["fixable"]:
            log.warning("[%s] %s", self.run_id, rca["detail"])
        return rca

    # ------------------------------------------------------------------ #
    # Step 2: Apply fixes
    # ------------------------------------------------------------------ #

    def fix(self, rca: dict[str, Any]) -> list[str]:
        """
        Apply every recommended fix from the RCA report.

        Returns a list of human-readable strings describing what was done.
        Failures for individual fixes are logged as warnings but do not abort
        the whole repair cycle — other fixes still run.
        """
        actions_taken: list[str] = []
        for fix_name, issue in rca["recommended_fixes"]:
            fix_obj = next((f for f in self._fix_registry if f.name == fix_name), None)
            if fix_obj is None:
                log.warning("[%s] Fix '%s' not found in registry — skipping.", self.run_id, fix_name)
                continue
            try:
                result = fix_obj.apply(issue, self.project_root)
                log.info("[%s] Fix '%s' applied: %s", self.run_id, fix_name, result)
                actions_taken.append(f"{fix_name}: {result}")
            except Exception as exc:  # noqa: BLE001
                log.warning(
                    "[%s] Fix '%s' failed: %s — continuing with remaining fixes.",
                    self.run_id, fix_name, exc,
                )
        return actions_taken

    # ------------------------------------------------------------------ #
    # Step 3: Rerun the workflow
    # ------------------------------------------------------------------ #

    def rerun(self) -> Path:
        """
        Re-invoke order_workflow.py and return the path of the new log file.

        Uses subprocess so the agent is completely decoupled from the
        workflow's internal state — each rerun starts with a fresh process.
        """
        workflow_path = self.project_root / "order_workflow.py"
        cmd = [
            sys.executable, str(workflow_path),
            "--input", str(self.project_root / "order_history.csv"),
            "--output-dir", str(self.project_root / "generated_output"),
            "--log-dir", str(self.project_root / "log_analysis"),
            "--deploy-dir", str(self.project_root / "deployed"),
        ]

        log.info("[%s] Rerunning: %s", self.run_id, " ".join(cmd))
        start_time = time.time()

        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            cwd=str(self.project_root),
        )

        elapsed = round(time.time() - start_time, 2)
        log.info("[%s] Rerun completed in %.2fs (exit code %d)", self.run_id, elapsed, result.returncode)

        if result.stdout:
            for line in result.stdout.strip().splitlines():
                log.info("[%s] [workflow stdout] %s", self.run_id, line)
        if result.stderr:
            for line in result.stderr.strip().splitlines():
                log.info("[%s] [workflow stderr] %s", self.run_id, line)

        # Find the newest log file written after our start time
        log_dir = self.project_root / "log_analysis"
        new_logs = sorted(
            (lf for lf in log_dir.glob("workflow_log_*.json") if lf.stat().st_mtime > start_time),
            key=lambda lf: lf.stat().st_mtime,
        )

        if not new_logs:
            raise RuntimeError(
                f"Rerun completed (exit {result.returncode}) but no new log file was found in {log_dir}"
            )

        new_log = new_logs[-1]
        log.info("[%s] New log file: %s", self.run_id, new_log)
        return new_log

    # ------------------------------------------------------------------ #
    # Step 4: Verify the rerun outcome
    # ------------------------------------------------------------------ #

    def verify(self, new_log_path: Path) -> dict[str, Any]:
        """
        Parse the new workflow log and return a verdict dict:
            clean            – True if no blocking issues remain
            new_run_id       – run_id from the new log
            remaining_issues – list of still-present ERROR/WARNING events
            status           – overall_status from the new log
        """
        with new_log_path.open(encoding="utf-8") as f:
            new_log = json.load(f)

        new_run_id = new_log.get("run_id", new_log_path.stem)
        status = new_log.get("overall_status", "UNKNOWN")
        counts = new_log.get("level_counts", {})

        remaining = [
            e for e in new_log.get("events", [])
            if e.get("level") in ("ERROR", "WARNING")
        ]

        # A rerun is "clean" if there are no ERRORs and warnings are either
        # zero or come only from acknowledged/low-severity events.
        error_count = counts.get("ERROR", 0)
        blocking_remaining = [
            e for e in remaining
            if _SEVERITY_MAP.get(e.get("event", ""), "low") in _BLOCKING_SEVERITIES
            and e.get("level") == "ERROR"
        ]
        clean = error_count == 0 and len(blocking_remaining) == 0

        log.info(
            "[%s] Verify → new_run=%s status=%s errors=%d warnings=%d clean=%s",
            self.run_id, new_run_id, status,
            error_count, counts.get("WARNING", 0), clean,
        )

        return {
            "clean": clean,
            "new_run_id": new_run_id,
            "status": status,
            "remaining_issues": remaining,
        }


# --------------------------------------------------------------------------- #
# Standalone CLI
# --------------------------------------------------------------------------- #

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="BobRepairAgent — RCA, fix, and rerun a failed order_workflow run"
    )
    parser.add_argument(
        "--log",
        required=True,
        help="Path to the workflow_log_*.json file to repair",
    )
    parser.add_argument(
        "--project-root",
        default=str(PROJECT_ROOT),
        help="Absolute path to the IBM Bob Project root directory",
    )
    parser.add_argument(
        "--max-attempts",
        type=int,
        default=3,
        help="Maximum repair+rerun cycles before giving up",
    )
    return parser.parse_args()


def _load_log(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as f:
        return json.load(f)


def main() -> int:
    args = _parse_args()
    log_path = Path(args.log)
    project_root = Path(args.project_root)

    if not log_path.exists():
        log.error("Log file not found: %s", log_path)
        return 1

    log_data = _load_log(log_path)
    run_id = log_data.get("run_id", log_path.stem)
    issues = [
        e for e in log_data.get("events", [])
        if e.get("level") in ("ERROR", "WARNING")
    ]

    if not issues:
        log.info("[%s] No ERROR or WARNING events — nothing to repair.", run_id)
        return 0

    agent = BobRepairAgent(
        run_id=run_id,
        log_data=log_data,
        issues=issues,
        project_root=project_root,
        max_attempts=args.max_attempts,
    )

    for attempt in range(1, args.max_attempts + 1):
        log.info("[%s] === Repair attempt %d / %d ===", run_id, attempt, args.max_attempts)

        rca = agent.analyse()
        print(rca["detail"])

        if not rca["fixable"]:
            log.error("[%s] Not auto-fixable. Manual intervention required.", run_id)
            return 2

        fixes = agent.fix(rca)
        log.info("[%s] Applied: %s", run_id, fixes)

        new_log_path = agent.rerun()
        verdict = agent.verify(new_log_path)

        if verdict["clean"]:
            log.info(
                "[%s] ✅ Repair successful after %d attempt(s). New run: %s",
                run_id, attempt, verdict["new_run_id"],
            )
            return 0

        log.warning("[%s] Attempt %d incomplete: %d issue(s) remain.", run_id, attempt,
                    len(verdict["remaining_issues"]))
        agent.issues = verdict["remaining_issues"]

    log.error("[%s] ❌ Repair failed after %d attempt(s). Escalate manually.", run_id, args.max_attempts)
    return 3


if __name__ == "__main__":
    sys.exit(main())
