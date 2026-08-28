# Order Processing Pipeline — IBM Bob Project

### End-to-end CSV → JSON order workflow with Human-in-the-Loop review, autonomous repair agents, and a Streamlit reporting dashboard

---

## Overview

This project automates the full lifecycle of order history data — from raw CSV ingestion through validation, human review, transformation, and deployment — with an autonomous repair agent that watches for issues and self-heals without manual intervention.

| Component | File | Role |
|---|---|---|
| **Order Workflow** | `order_workflow.py` | CSV → JSON pipeline: load, validate, clean, aggregate, export, deploy |
| **Review App** | `review_app.py` | Streamlit pop-up for human review of flagged orders **before** final output is generated |
| **Report App** | `app.py` | Streamlit dashboard showing the final processed report after review |
| **SpawnAgent** | `agents/spawn_agent.py` | Watches `log_analysis/` for new run logs; triggers repair when errors or warnings are found |
| **BobRepairAgent** | `agents/bob_repair_agent.py` | Performs RCA, applies fixes, reruns the workflow, verifies the result |
| **Fix Library** | `agents/agent_fixes.py` | Maps every known workflow event code to a concrete repair action |

---

## Architecture

```
                    ┌─────────────────────────────────┐
                    │   python order_workflow.py       │
                    │          --phase1                │
                    └──────────────┬──────────────────┘
                                   │ validates, transforms
                                   │ writes staging JSON + audit log
                                   ▼
                    ┌─────────────────────────────────┐
                    │   review_app.py  (Streamlit)     │
                    │                                  │
                    │  Section 1: Processing Summary   │
                    │  (colour-coded stage events)     │
                    │                                  │
                    │  Section 2: Flagged Orders       │
                    │  Approve / Reject per order      │
                    │  (no default selection)          │
                    │                                  │
                    │  [Submit Decisions]              │
                    └──────────────┬──────────────────┘
                                   │ writes review_decisions.json
                                   │ signals phase 2
                                   ▼
                    ┌─────────────────────────────────┐
                    │   python order_workflow.py       │
                    │          --phase2                │
                    │                                  │
                    │  Applies decisions:              │
                    │  • Approved → final JSON         │
                    │  • Rejected → temp file (30 day) │
                    │  Exports + deploys               │
                    └──────────────┬──────────────────┘
                                   │ writes final JSON + audit log
                                   ▼
                    ┌─────────────────────────────────┐
                    │   app.py  (Streamlit report)     │
                    │                                  │
                    │  Summary metrics                 │
                    │  Processing Summary table        │
                    │  Final Order Output table        │
                    │  Rejected Orders panel           │
                    └─────────────────────────────────┘
                                   │
                    ┌──────────────▼──────────────────┐
                    │   agents/spawn_agent.py          │
                    │   (runs separately / in watch)   │
                    │                                  │
                    │  Polls log_analysis/ for issues  │
                    │  → BobRepairAgent                │
                    │    1. analyse()  RCA report      │
                    │    2. fix()      apply fixes     │◀── HITL approval prompt
                    │    3. rerun()    rerun workflow  │
                    │    4. verify()   check result    │
                    └─────────────────────────────────┘
```

---

## Human-in-the-Loop (HITL) Controls

Five checkpoints ensure a human stays in control of every consequential action:

| # | Where | What it does |
|---|---|---|
| **1** | `spawn_agent.py` — before `agent.fix()` | Shows the full RCA and proposed fixes; operator must type `y` to proceed. Pass `--auto-approve` to skip in CI. |
| **2** | `spawn_agent.py` — after fixes applied | If any fix is only an acknowledgement (business duplicate), the rerun is blocked and a review file is written to `log_analysis/duplicate_review_<run_id>.txt`. |
| **3** | `review_app.py` + `order_workflow.py --phase1` | Streamlit pop-up shows flagged orders **before** the final JSON is generated. Reviewer approves or rejects each one. Submit is disabled until all have a decision. |
| **4** | `spawn_agent.py` — after max attempts exhausted | Sends an escalation e-mail via SMTP. Configure via env vars (`SMTP_HOST`, `SMTP_USER`, `SMTP_PASS`, `ESCALATION_EMAIL`). |
| **5** | `app.py` — final report | Read-only panel shows rejected orders and what was excluded. No decision UI — review happens before output is generated. |

---

## Pipeline Stages

`order_workflow.py` runs these stages in order:

| Stage | What it does |
|---|---|
| **1. Load** | Reads the raw CSV; all columns kept as strings for clean-room parsing |
| **2. Data type check** | Infers actual types cell-by-cell; flags mismatches against the expected schema |
| **3. Duplicate check** | Removes hard duplicates (same OrderID); flags business duplicates (same customer/product/qty/date) for human review |
| **4. Formatting** | Normalises text case, strips whitespace, strips currency symbols, parses dates |
| **5. Formula check** | Verifies `TotalAmount == Quantity × UnitPrice`; auto-corrects mismatches and logs them |
| **6. Aggregation** | Rolls up totals by region, product, customer, status |
| **7. Export** | Writes cleaned records + aggregates + validation report to `generated_output/` |
| **8. Deploy** | Copies the JSON to `deployed/` (Azure Blob Storage hook included, commented out) |

---

## Quick Start

### Prerequisites

```bash
# from the project root
python -m venv .venv
.venv\Scripts\activate          # Windows
# source .venv/bin/activate     # macOS / Linux
pip install -r requirements.txt
```

### Run with Human Review (recommended)

```bash
# Phase 1: validate + transform, then open the review pop-up
python order_workflow.py --phase1
```

The terminal blocks with:
```
Waiting for human review to complete in Streamlit…
(submit decisions in the browser to continue)
```

A browser tab opens automatically with the **Order Review** screen. Once you submit decisions, Phase 2 fires automatically — it applies your decisions, writes the final JSON, deploys, and opens the final report.

### Run without Human Review (single-pass)

```bash
python order_workflow.py
```

Validates, transforms, exports, and deploys in one shot. No pop-up. Useful for automated batch runs or when data quality is already verified.

---

## Detailed Usage

### `order_workflow.py`

```
python order_workflow.py [options]

Options:
  --input FILE        Path to input CSV (default: order_history.csv)
  --output-dir DIR    Output folder for processed JSON (default: generated_output)
  --log-dir DIR       Folder for audit logs (default: log_analysis)
  --deploy-dir DIR    Deploy target folder (default: deployed)
  --phase1            Run phase 1 only: validate, transform, launch review pop-up
  --phase2            Run phase 2 only: apply review decisions, export + deploy
```

### `review_app.py`

Launched automatically by `--phase1`. Can also be run manually if the staging file already exists:

```bash
streamlit run review_app.py
```

### `app.py`

Launched automatically after phase 2 completes. Can also be run manually:

```bash
streamlit run app.py
```

### `agents/spawn_agent.py`

```
python agents/spawn_agent.py [options]

Options:
  --once                    Process existing logs once then exit
  --log-dir DIR             Directory to watch (default: log_analysis)
  --poll-interval N         Seconds between scans in watch mode (default: 10)
  --max-repair-attempts N   Repair cycles before escalating (default: 3)
  --auto-approve            Skip y/N prompt; apply fixes automatically (CI use)
```

**Watch mode** (runs forever, processes new logs as they arrive):
```bash
python agents/spawn_agent.py
```

**One-shot** (process current logs then exit — good for scheduled jobs):
```bash
python agents/spawn_agent.py --once
```

### `agents/bob_repair_agent.py`

Target a specific log file directly:

```bash
python agents/bob_repair_agent.py \
    --log log_analysis/workflow_log_20260828T061035Z_18f06bcd.json
```

Return codes: `0` = repaired, `1` = log not found, `2` = not auto-fixable, `3` = attempts exhausted.

---

## Output Files

| File | Description |
|---|---|
| `generated_output/order_history_staging.json` | Temporary staging file written by phase 1; deleted automatically after phase 2 completes |
| `generated_output/order_history_processed.json` | Final processed output (approved orders only) |
| `generated_output/review_decisions.json` | Human review decisions: `{ "ORD1007": "Approved", "ORD1012": "Rejected" }` |
| `generated_output/rejected_temp/rejected_orders_<id>_<date>.json` | Rejected order records; auto-deleted after 30 days |
| `deployed/order_history_processed.json` | Deployed copy (mirrors `generated_output/`) |
| `log_analysis/workflow_log_<run_id>.json` | Full audit log for every run |
| `log_analysis/duplicate_review_<run_id>.txt` | Step-by-step instructions when business duplicates need manual resolution |

---

## Fix Library (`agents/agent_fixes.py`)

Each class in `FIX_REGISTRY` targets a specific `event` code from the workflow log:

| Fix class | Targets event | What it does |
|---|---|---|
| `FixRejectedOrders` | `order_rejected_by_reviewer` | Removes rows rejected in the review pop-up from `order_history.csv`; marks decisions as `Excluded` |
| `FixFileNotFound` | `file_not_found` | Searches for a backup CSV and restores it |
| `FixMissingQuantity` | `missing_quantity` | Writes `1` into the blank Quantity cell in the CSV |
| `FixFormulaMismatch` | `formula_mismatch` | Recomputes `TotalAmount = Quantity × UnitPrice` in the CSV |
| `FixBusinessDuplicate` | `duplicate_business` | Acknowledges the duplicate; blocks rerun until human resolves manually |
| `FixDataTypeMismatch` | `datatype_mismatch` | Logs a recommendation; cell correction is left to the operator |

**Adding your own fix:**

```python
# agents/agent_fixes.py

class FixInvalidDate(AgentFix):
    name = "fix_invalid_date"

    def matches(self, issue):
        return issue.get("event") == "invalid_date"

    def apply(self, issue, project_root):
        row_index = issue["details"]["row_index"]
        _patch_csv_cell(
            project_root / "order_history.csv",
            data_row=row_index,
            column="OrderDate",
            new_value="1970-01-01",
        )
        return f"Reset OrderDate for row {row_index} to 1970-01-01"

# Add to FIX_REGISTRY at the bottom of the file:
FIX_REGISTRY.append(FixInvalidDate())
```

---

## Escalation Email Setup (HITL #4)

When all repair attempts are exhausted, `spawn_agent.py` sends a plain-text escalation email. Set these environment variables (or add them to a `.env` file):

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=your.address@gmail.com
SMTP_PASS=your-app-password
ESCALATION_EMAIL=recipient@example.com
```

If any variable is missing the email is silently skipped — no hard failure.

---

## File Structure

```
IBM Bob Project/
│
├── order_workflow.py              # Main pipeline (single-pass or two-phase)
├── review_app.py                  # Streamlit HITL review pop-up (phase 1)
├── app.py                         # Streamlit final report (phase 2)
├── order_history.csv              # Source data
├── requirements.txt
│
├── generated_output/
│   ├── order_history_processed.json     # Final output (approved orders)
│   ├── order_history_staging.json       # Temp — phase 1 only, deleted after phase 2
│   ├── review_decisions.json            # Human review decisions
│   └── rejected_temp/
│       └── rejected_orders_<id>_<date>.json   # Auto-deleted after 30 days
│
├── deployed/
│   └── order_history_processed.json     # Deployed copy
│
├── log_analysis/
│   ├── workflow_log_<run_id>.json        # Audit log per run
│   └── duplicate_review_<run_id>.txt    # Manual review instructions (when needed)
│
└── agents/
    ├── __init__.py
    ├── spawn_agent.py                   # Log watcher + repair orchestrator
    ├── bob_repair_agent.py              # RCA + fix + rerun + verify
    └── agent_fixes.py                   # Pluggable fix library
```

---

## Extending the System

| Goal | Where to change |
|---|---|
| Add a new auto-fix for a new error type | `agents/agent_fixes.py` — subclass `AgentFix`, add to `FIX_REGISTRY` |
| Change the review pop-up layout | `review_app.py` |
| Add more columns to the final report | `app.py` — update `_ORDER_COLS` / `_DISPLAY_COLS` |
| Change retry behaviour | `--max-repair-attempts N` on `spawn_agent.py` |
| Enable Azure Blob deploy | Uncomment the Azure block in `OrderWorkflow.deploy()` in `order_workflow.py` |
| Watch a different workflow | Replace the subprocess command in `BobRepairAgent.rerun()` |
| Run on a schedule (Windows Task Scheduler) | Schedule `python agents/spawn_agent.py --once` every N minutes |
| Adjust rejected-record retention | Change `retention_days=30` in `review_app.py` → `_cleanup_old_rejected()` |
