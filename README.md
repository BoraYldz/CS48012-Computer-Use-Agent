# Computer-Use Agent

A verifiable desktop assistant that turns a plain-language request into finished work in the browser, spreadsheets, documents and files inside an isolated sandbox or an explicitly selected local workspace (and later a Windows VM), and **checks the result in code before reporting success**.

CS48012 Agentic AI Systems · Sabancı University · Group 11

## Currently How it works

```
User request
   │
   ▼
Orchestrator (LangGraph)  ── plans subgoals, routes each to one specialist worker
   │
   ├─ Files worker        ┐
   ├─ Spreadsheet worker  │  each worker = ReAct loop with its OWN tool set and prompt
   ├─ (Doc, Web …)        ┘
   ├─ GUI worker          ── its own screen loop on this PC (gui_worker/), no VM needed
   │
   ▼
Check (deterministic code, not an LLM opinion)
   ├─ PASS ──→ next subgoal / finish
   ├─ FAIL with new evidence ──→ Quick Fix critic ──→ same specialist worker (max 2 repairs)
   └─ UNKNOWN or repeated failure ──→ stop without claiming success
   │
   ▼
Local tools (direct calls, sandbox or selected workspace) or VM tool server (MCP over HTTP)
VM mode can restore a clean snapshot before an attempt
```

- **Host** (`host/`): orchestrator, workers, checkers, CLI, run logs.
- **Local backend** (`host/agent/local_runtime.py`): runs tools directly, confined to one configured root. The default is `host/local_sandbox/`; optional workspace scope uses one dedicated existing folder selected by the user.
- **VM backend** (`vm/server.py`): the agent's "hands" over MCP; paths are sandboxed to the VM user's home folder.
- **Circuit breakers:** cumulative tool-step limit per subgoal, token cap per task, and at most two Quick Fix attempts per subgoal; the model never decides when to stop.
- **Verification:** each planned subgoal carries a `check_spec`; deterministic filesystem and spreadsheet checkers return `PASS`, `FAIL`, or `UNKNOWN`. Worker self-reports never establish success. Planner check specs are normalized as postconditions before execution (for example, creation requires existence, not absence), contradictory assertions are removed/logged, and outputs are relocated when they would violate an explicit empty-source-folder requirement.
- **File organization:** multi-file moves use `list_dir` plus `move_files(src_dir, filenames, dst_dir)`, which accepts exact basenames, preserves filenames, creates the destination directory, and refuses overwrites. `move_path` remains for one exact source-to-destination move or rename. Wildcards are rejected. The planner snapshots the matching input filenames and sizes before execution, and deterministic checks verify the moved files and required empty source directory afterward. The empty-directory check ignores common OS metadata files (`.DS_Store`, `Thumbs.db`, `desktop.ini`) but not ordinary user files.
- **Tool errors:** local worker tools return actionable `TOOL_ERROR` observations instead of letting an ordinary filesystem exception crash the whole run. The worker is instructed to use a supported alternative where safe.
- **GUI worker** (`gui_worker/`, see its README): operates apps and websites through this PC's screen (Windows only). The planner uses it when no tool worker can do the job. A GUI subgoal without a deterministic check passes only when the GUI worker's independent reviewer model confirms the final screen; that is reported as a model judgement. In VM mode, `run` still works without the VM tool server, with the GUI worker alone.

## Safety gate and evaluation

- **Human approval:** `delete_path` and any `overwrite=true` move/copy stop and ask on the terminal (`y/N`) before touching the disk; `--auto-approve` is for unattended runs only. With no approver configured the gate **fails closed**. Every decision is written to the run log (`approval` events).
- **Eval suites:** `python -m agent.cli eval files|sheet --runs 3` runs 12 core tasks (14 with `--tag all`) inside `<AGENT_ROOT>/_eval/<task>/`. Each task is judged by an independent exact-file-tree (SHA-256) check, not by the planner's `check_spec`. Reported: success rate, pass^k, false-success count, steps, tokens. `--bound on|off` toggles the planner-bound move tool for an ablation. Results are saved to `host/runs/eval-files-*.json|md`.
- `tests/test_files_suite.py` proves each eval check accepts a reference solution and rejects a do-nothing baseline, with no LLM.

## Spreadsheet (Sheet) worker

A specialist, not a generic ReAct loop: its own prompt, tool list, step cap, observation format and checker.

| | Files worker | Sheet worker |
|---|---|---|
| Tools | list/move/copy/read/write/delete | `xlsx_info`, `xlsx_read` (range, computed values or formulas), `xlsx_query` (filter/sort), `xlsx_aggregate` (sum/avg/min/max/count, group_by), `xlsx_write`, `xlsx_append_rows`, `xlsx_set_cells` + read-only `list_dir`, `path_info`, `read_text`, `read_pdf_text` |
| Cannot | - | move, copy, delete, write arbitrary text files |
| Step cap | `MAX_STEPS_PER_SUBGOAL` (15) | `MAX_STEPS_SHEET` (12) |
| Observations | JSON listings | bounded rows, `truncated` flag, ISO dates, `uncomputed_formula_cells`, before -> after diff for edits |
| Human gate | `delete_path`, overwrite | `xlsx_write` that would replace a sheet that already has data |
| Checker | path / text / tree assertions | `xlsx_cell_equals`, `xlsx_formula_present`, `xlsx_data_row_count`, `xlsx_aggregate_matches` (expected value **recomputed from the source data in code**), plus the earlier sheet/values/rows kinds |

- **Calculator tools:** a small local model never adds up or sorts numbers itself; it calls `xlsx_aggregate` / `xlsx_query` and writes the returned numbers.
- **Formulas:** `agent/sheet_tools.py` contains a small evaluator (`+ - * / ^ &`, comparisons, `SUM AVERAGE MIN MAX COUNT ROUND ABS IF AND OR NOT`, cross-sheet refs). openpyxl never computes formulas, so a freshly written `=SUM(B2:B6)` has no cached value; the evaluator lets the worker and the checker see real numbers without Excel or LibreOffice. Unsupported functions are reported as `uncomputed_formula_cells`, never guessed, and the checker returns UNKNOWN for them.
- **Sheet-state contract** (`agent/sheet_contracts.py`): before the first attempt of a sheet subgoal the orchestrator digests every sheet the check_spec does *not* name (and every sheet of a read-only `source_path` workbook) and appends an `xlsx_sheets_preserved` assertion. If the worker touches anything else the subgoal fails with `COLLATERAL DAMAGE`, whatever the planner wrote. The snapshot is lazy, so a workbook created by an earlier subgoal (e.g. a copy made by the Files worker) is protected too.
- **Eval:** `python -m agent.cli eval sheet --runs 3` runs 13 core tasks (14 with `--tag all`), judged by an independent cell-by-cell check of the final workbook (types, formulas, sheet set). `tests/test_sheet_suite.py` proves every check accepts a reference solution, rejects do-nothing and rejects the typical wrong answers (numbers stored as text, replaced instead of appended, pasted value instead of formula, collateral damage, injected instruction followed).
- `vm/sheet_tools.py` is a byte-identical copy of `host/agent/sheet_tools.py` (a test enforces it); the VM server exposes the same tools. The VM server has no approval gate yet; the human gate currently exists only in local mode.

## Quick start (local mode; no VM required)

The default build runs against a **dedicated local sandbox**. Every local path is
confined to one configured root. By default, that is `host/local_sandbox/`.
For real local-workspace testing, you can explicitly select a dedicated directory
on your Mac; the agent still cannot escape that selected directory.

### 1. Install Python dependencies

macOS / Linux:

```bash
cd host
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

Windows PowerShell:

```powershell
cd host
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item .env.example .env
```

### 2. Install and start Ollama

Install Ollama for your operating system, then pull the development model:

```bash
ollama pull qwen3:8b
```

Make sure Ollama is running. Its OpenAI-compatible endpoint should be available at
`http://localhost:11434/v1`. The `.env.example` config uses Qwen3 8B for planner,
worker, and critic. Set `OLLAMA_THINKING=true` to leave thinking enabled; the
default `false` sends `reasoning_effort=none` for Qwen3 worker calls.

### 3. Check the local backend

Run these commands from `host/` with the virtual environment activated:

```bash
python -m agent.cli ping
python -m agent.cli task 16
```

`ping` prints the active mode, scope, allowed root, and available tools. Task 16
creates `Desktop/Proje/{Girdiler,Çıktılar,Raporlar}` inside the sandbox, then runs
the deterministic subgoal checks and the task-level preservation check. Its setup
removes the previous `Desktop/Proje` output plus the known incorrect root-level
`Proje` output from the earlier planner-location bug, then recreates its own
sentinel file. It does not reset or delete the rest of the sandbox. When a request
starts with an explicit home-folder destination such as `In Desktop`, the planner
path checks are deterministically anchored to that folder and the worker receives
the exact sandbox-relative paths used by the checker.

For a free-form request:

```bash
python -m agent.cli run "Create a folder named Demo with a notes.txt file inside it."
```

The files will appear under `host/local_sandbox/`, not on your actual Desktop.


### Use a real local workspace (recommended after sandbox tests)

Workspace scope lets the same tools work with real files in one directory on your
Mac. It does **not** grant access to your whole home folder or Desktop. Create a
separate directory dedicated to agent testing:

```bash
mkdir -p "$HOME/Desktop/AgentWorkspace"
```

Then inspect the selected root before allowing any work:

```bash
AGENT_MODE=local \
AGENT_SCOPE=workspace \
AGENT_ROOT="$HOME/Desktop/AgentWorkspace" \
python -m agent.cli ping
```

The command must report `scope: workspace` and the exact
`.../Desktop/AgentWorkspace` path. Workspace mode requires an existing absolute
directory and rejects broad roots such as your home folder, Desktop itself,
Documents, Downloads, or the filesystem root. All worker file operations remain
confined to that one directory; `..` traversal and symlink escapes are rejected.

Now try an ordinary request that is not tied to Task 16:

```bash
AGENT_MODE=local \
AGENT_SCOPE=workspace \
AGENT_ROOT="$HOME/Desktop/AgentWorkspace" \
python -m agent.cli run "Create a folder named ClientDemo and put a README.txt inside it containing exactly: Workspace mode works."
```

Expected output location:

```text
~/Desktop/AgentWorkspace/
└── ClientDemo/
    └── README.txt
```

In workspace scope, the selected folder is the working root. A plain request such
as "create a folder named ClientDemo" creates it directly there. Do not say
"in Desktop" unless you actually want a subfolder named `Desktop` inside
`AgentWorkspace`. The special Task 16 fixture is designed for sandbox/home-folder
semantics; use `run` for these generic workspace tests. Keep valuable files out
of the workspace while testing, because worker tools can create and overwrite
files within the selected directory.

For file organization, the agent should inspect the source with `list_dir` and
use `move_files` with the exact basenames discovered there (no `*.txt` wildcard
arguments). `move_files` preserves the filenames, creates the destination folder
if necessary, and refuses to overwrite an existing destination file. Use
`move_path` when one item is being renamed or moved to an exact destination path.
If the request says the source folder must be empty, the organized destination
must be outside that source folder. The planner captures the initial matching
file set so deterministic checks can verify the move rather than trusting the
worker's summary. The final empty-folder check ignores standard OS metadata files
such as `.DS_Store`, `Thumbs.db`, and `desktop.ini`.

To return to the isolated sandbox, omit the inline workspace variables or set
`AGENT_SCOPE=sandbox` and `AGENT_ROOT=local_sandbox`.

### 4. Switch back to the VM later

Set `AGENT_MODE=vm` in `host/.env`, start the updated `vm/server.py` MCP server,
and run the same CLI commands. The VM server must include the harness-only
`harness_remove_path` tool used by Task 16 setup. In VM mode, `task 16 --reset`
continues to invoke the Windows VM reset script.

### Exercise the Quick Fix loop

Run this diagnostic in local mode:

```bash
AGENT_MODE=local AGENT_ROOT=local_sandbox_step2_test python -m agent.cli task quickfix
```

The diagnostic uses a fixed, explicit plan to isolate the repair path from
planner variance. Its local-only harness injects a one-time bad persisted write
to `QuickFixProbe.txt` after the first worker attempt finishes. The
deterministic checker should detect the missing expected marker, the critic
should produce a narrow repair instruction, and the same files worker should
write the correct marker on its retry. The task passes only if the injected
fault actually fired and the final file content is exactly correct. The fault
injection is a harness control, not an agent-visible tool, and is unavailable
in VM mode. It only affects `QuickFixProbe.txt` inside the configured sandbox.

## Local testing without an LLM

The local backend and Task 16 fixture/checker integration can be tested without
Ollama or an MCP server:

```bash
cd host
python -m unittest discover -s tests -v
```

The ReAct planner/worker run itself still requires the model and the LangGraph
runtime dependencies installed from `requirements.txt`.

### Run the GUI worker

GUI worker from the orchestrator (no VM): put `OPENAI_API_KEY` in `gui_worker/.env` (copy
`gui_worker/.env.example`), point `host/.env` at a planner model, close private windows, then
`.venv\Scripts\python.exe -m agent.cli run "Open youtube.com in Chrome and search for lofi music"`.
The GUI settings (`GUI_MODEL`, `GUI_REVIEW_MODEL`, `GUI_EXECUTE`, ...) are in `host/.env.example`.

## Status

- Local sandbox and workspace scopes implemented.
- Deterministic filesystem/spreadsheet checks include `text_equals` for exact file contents.
- Task 16 passed in the local sandbox.
- Controlled Quick Fix integration test passed with an LLM-critic-generated repair.
- Browser worker and evaluation script remain to be implemented.

## Team

| Member | Area |
|---|---|
| Bora Yıldız | Architecture, orchestrator, VM environment, interface |
| Ali Durmuş Abay | GUI fallback, checking & evaluation |
| Eren Cebeci | Tools, task suite scope, reflection & memory |

## Recent robustness changes: structured tool output and file organization

The local worker-tool adapter JSON-encodes list/dictionary results as plain text before returning them to the chat model. The Windows MCP server follows the same contract for `list_dir`, `path_info`, `move_files`, and `xlsx_read`; the host VM client decodes JSON text for deterministic checkers. This avoids passing arbitrary Python list/dict structures as OpenAI-compatible `ToolMessage` content, which can be rejected by some Ollama-compatible endpoints.

For explicit extension-based organization requests, the planner boundary adds snapshot-backed `files_moved_by_extension` assertions from the user's source directory, extension-to-folder mapping, and planned destination paths. Source file manifests are captured before workers run and include SHA-256 where available. Verification confirms that every original file arrived in the correct destination, is absent from the source, and retains its content hash. A request to leave the source folder empty adds a `directory_empty` postcondition. Inventory-only subgoals use `directory_contains_files` instead of merely checking that the source directory exists.

Structured tool errors remain observable as plain-text tool results. `list_dir` and other structured local results are JSON strings to workers; direct checker calls retain their native Python data structures. The VM client decodes JSON-text structured content before passing it to checkers.

### Tool-trace callback robustness

Worker trace callbacks accept all LangChain message-like objects, including
`ToolMessage`, without depending on a concrete class list. Tool-result text is
normalized through the shared `_text` helper. Trace logging failures are captured
as warnings and must never interrupt the worker graph; callback observability is
best-effort and is not part of task execution correctness. Regression coverage is
in `host/tests/test_graph_trace.py`.

### Bound extension-move operations

For generic file-sorting requests, the planner normalizes each extension move into a
canonical source/destination contract and captures an exact pre-execution file manifest.
The worker receives a temporary `move_expected_files(filenames)` tool whose source and
destination are bound to that contract. It cannot supply alternate paths or move files
outside the manifest/extension. The deterministic checker verifies destination presence,
source absence, size, and SHA-256 where available.
