# Spawn Agent + IBM Bob Repair Agent
### Automated log-watching, root-cause analysis, and self-healing for `order_workflow.py`

---

## What this does

| Component | File | Role |
|---|---|---|
| **SpawnAgent** | `agents/spawn_agent.py` | Watches `log_analysis/` for new run logs; triggers repair when errors or warnings are found |
| **BobRepairAgent** | `agents/bob_repair_agent.py` | Performs RCA, applies fixes, reruns the workflow, verifies the result |
| **Fix Library** | `agents/agent_fixes.py` | Maps every known `order_workflow` event code to a concrete repair action |

---

## Architecture overview

```
order_workflow.py
       │ writes
       ▼
log_analysis/workflow_log_<run_id>.json
       │ detected by
       ▼
┌──────────────────────────────────────────────┐
│              SpawnAgent (watch loop)         │
│  polls every N seconds for new log files     │
│  _needs_attention() → ERROR/WARNING present? │
└───────────────┬──────────────────────────────┘
                │ spawns
                ▼
┌──────────────────────────────────────────────┐
│            BobRepairAgent                    │
│                                              │
│  1. analyse()  → Root Cause Analysis report  │
│  2. fix(rca)   → apply AgentFix actions      │
│  3. rerun()    → subprocess: order_workflow  │
│  4. verify()   → check new log for issues    │
│                                              │
│  repeats up to max_repair_attempts times     │
└──────────────────────────────────────────────┘
```

---

## Step-by-step guide

### Step 1 — Prerequisites

Ensure you have the project dependencies installed:

```bash
# from the IBM Bob Project root
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

No extra dependencies are needed for the agents — they use only the Python
standard library plus `pandas` (already in `requirements.txt`).

---

### Step 2 — Understanding the log format

Every run of `order_workflow.py` writes a file like:

```
log_analysis/workflow_log_20260828T061035Z_18f06bcd.json
```

The key fields the agents inspect:

```json
{
  "overall_status": "SUCCESS_WITH_ISSUES",
  "level_counts": { "ERROR": 1, "WARNING": 6 },
  "events": [
    {
      "stage": "validation",
      "level": "ERROR",
      "event": "missing_quantity",
      "message": "Quantity is missing/blank",
      "details": { "row_index": 8, "order_id": "ORD1009" }
    }
  ]
}
```

`SpawnAgent` considers a log file *needing attention* when
`ERROR > 0 OR WARNING > 0`.

---

### Step 3 — The Fix Library (`agent_fixes.py`)

Each class in `FIX_REGISTRY` targets a specific `event` field value:

| Fix class | Targets `event` | What it does |
|---|---|---|
| `FixFileNotFound` | `file_not_found` | Searches for a backup CSV and restores it |
| `FixMissingQuantity` | `missing_quantity` | Writes `1` into the blank Quantity cell in `order_history.csv` |
| `FixFormulaMismatch` | `formula_mismatch` | Recomputes `TotalAmount = Quantity × UnitPrice` in the CSV |
| `FixBusinessDuplicate` | `duplicate_business` | Acknowledges the duplicate (human review required; no row deleted) |
| `FixDataTypeMismatch` | `datatype_mismatch` | Logs a recommendation; cell correction is left to the user |

**Adding your own fix:**

```python
# agents/agent_fixes.py

class FixInvalidDate(AgentFix):
    name = "fix_invalid_date"

    def matches(self, issue):
        return issue.get("event") == "invalid_date"

    def apply(self, issue, project_root):
        row_index = issue["details"]["row_index"]
        _patch_csv_cell(project_root / "order_history.csv",
                        data_row=row_index,
                        column="OrderDate",
                        new_value="1970-01-01")   # safe default
        return f"Reset OrderDate for row {row_index} to 1970-01-01"

# then add to FIX_REGISTRY at the bottom:
FIX_REGISTRY.append(FixInvalidDate())
```

---

### Step 4 — Running SpawnAgent in **watch mode** (recommended)

Start the agent once; it will keep monitoring forever:

```bash
# from the IBM Bob Project root
python agents/spawn_agent.py
```

Optional arguments:

```bash
python agents/spawn_agent.py \
    --log-dir      log_analysis \
    --poll-interval 10 \
    --max-repair-attempts 3
```

What happens when a bad log arrives:
1. `SpawnAgent` detects the new `workflow_log_*.json`.
2. Extracts all `ERROR` / `WARNING` events.
3. Instantiates `BobRepairAgent`.
4. Drives the **analyse → fix → rerun → verify** cycle.
5. Marks the run as ✅ repaired or ❌ escalates after `max_repair_attempts`.

---

### Step 5 — Running SpawnAgent **once** (CI / scheduled job)

```bash
python agents/spawn_agent.py --once
```

Useful for nightly batch jobs or CI pipelines where you want to process
whatever logs exist and exit with a proper return code.

---

### Step 6 — Running BobRepairAgent **standalone**

You can target a specific log file directly:

```bash
python agents/bob_repair_agent.py \
    --log log_analysis/workflow_log_20260828T061035Z_18f06bcd.json
```

Return codes:
- `0` — repair successful (or nothing to repair)
- `1` — log file not found
- `2` — not auto-fixable (manual intervention required)
- `3` — exhausted all repair attempts

---

### Step 7 — What a repair cycle looks like (console output)

```
2026-08-28 06:15:00 | INFO     | [SpawnAgent] Seeded 2 existing log(s); waiting for new runs…
2026-08-28 06:15:10 | WARNING  | [SpawnAgent] [20260828T061035Z_18f06bcd] status=SUCCESS_WITH_ISSUES
                                  — 7 issue(s) detected. Spawning BobRepairAgent…
2026-08-28 06:15:10 | INFO     | [BobRepairAgent] Running root cause analysis on 7 issue(s)…
2026-08-28 06:15:10 | INFO     | [BobRepairAgent] RCA: 7 issue(s) detected (1 high, 3 medium, 2 low)

=== Root Cause Analysis ===
  [HIGH    ] stage=validation event=missing_quantity | Quantity is missing/blank
  [MEDIUM  ] stage=validation event=formula_mismatch | TotalAmount 150.0 != 136.5
  ...

2026-08-28 06:15:10 | INFO     | [BobRepairAgent] Fix 'fill_missing_quantity' applied:
                                  Filled missing Quantity=1 for ORD1009 (CSV data row 8)
2026-08-28 06:15:10 | INFO     | [BobRepairAgent] Fix 'recompute_total_amount' applied:
                                  Corrected TotalAmount=136.5 for ORD1004 (CSV data row 3)
2026-08-28 06:15:10 | INFO     | [BobRepairAgent] Rerunning: python order_workflow.py …
2026-08-28 06:15:11 | INFO     | [BobRepairAgent] Rerun completed in 0.31s (exit code 0)
2026-08-28 06:15:11 | INFO     | [BobRepairAgent] Verify → new_run=20260828T061510Z_abc12345
                                  status=SUCCESS errors=0 warnings=2 clean=True
2026-08-28 06:15:11 | INFO     | [SpawnAgent] ✅ Repair successful after 1 attempt(s).
```

---

### Step 8 — Escalation (when repair is not possible)

If `BobRepairAgent` cannot resolve an issue automatically (e.g. the source
CSV file is completely missing and no backup exists), it logs:

```
❌ [run_id] Issues are not auto-fixable. Manual intervention required.
```

At that point you should:
1. Check `log_analysis/workflow_log_<failed_run_id>.json` for the exact error.
2. Restore or correct `order_history.csv` manually.
3. Rerun: `python order_workflow.py --input order_history.csv`

---

## File structure after setup

```
IBM Bob Project/
├── order_workflow.py
├── order_history.csv
├── requirements.txt
├── log_analysis/
│   └── workflow_log_*.json
├── generated_output/
│   └── order_history_processed.json
├── deployed/
│   └── order_history_processed.json
└── agents/                          ← NEW
    ├── __init__.py
    ├── spawn_agent.py               ← Spawn Agent (watcher + orchestrator)
    ├── bob_repair_agent.py          ← IBM Bob Repair Agent (RCA + fix + rerun + verify)
    ├── agent_fixes.py               ← Fix Library (pluggable repair actions)
    └── README.md                    ← This file
```

---

## Extending the system

| Goal | Where to change |
|---|---|
| Add a new fix for a new error type | `agent_fixes.py` — add a class + append to `FIX_REGISTRY` |
| Change retry behaviour | Pass `--max-repair-attempts N` to `spawn_agent.py` |
| Integrate email/Slack alerting on escalation | Add a call in `SpawnAgent._spawn_repair_agent()` after the final failed attempt |
| Watch a different workflow | Replace the `subprocess.run` command in `BobRepairAgent.rerun()` |
| Run on a schedule (Windows Task Scheduler) | Schedule `python agents/spawn_agent.py --once` every N minutes |
