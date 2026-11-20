# GUI worker

The GUI fallback worker of the computer-use agent: given one subtask in plain language, it operates
the Windows desktop through the screen (screenshots + UI Automation), one checked action at a time,
and returns evidence for the orchestrator's checker. It never claims verified success on its own.

Status: works standalone (CLI), as a LangGraph node and as an MCP server (stdio). Wiring it into the
host orchestrator in `host/` is not done yet. Windows only; it controls the desktop it runs on.

## Quick start

```powershell
cd gui_worker
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env            # add OPENAI_API_KEY

# Proposal only: one model decision, no input sent to the desktop
.venv\Scripts\python.exe run_agent.py "Open Notepad and type hello"

# Live run: close private windows first, the whole primary screen is sent to the model
.venv\Scripts\python.exe run_agent.py "Open Chrome, visit youtube.com and search for lofi music" --execute --steps 16
```

Useful flags: `--trials 1-3` (retry with the new lesson), `--no-reflect`, `--no-done-check`,
`--approve "Button 'Send'"`, `--reflect-model` (default `gpt-5.5`; must differ from `--model`,
default `gpt-5.4-mini`).

## How one run works

observe -> decide -> validate -> guard -> act -> settle -> observe and measure the effect

1. **Observe**: screenshot with numbered boxes on the UI Automation elements (set-of-marks), the
   element list, window list and focus.
2. **Decide**: the planner model returns one action as strict JSON, plus a typed scratchpad
   (objective, confirmed facts, todos) and the ids of stored lessons it applied.
3. **Validate** (`gui_worker.validate_action`): unknown targets, container clicks, unsafe URLs,
   disallowed keys or apps are refused, and the reason goes back to the model.
4. **Guard**: an action that already did nothing twice on the same screen is refused; clicks such
   as Send / Delete / Submit / Gönder pause the run for a person's approval (`awaiting_approval`).
5. **Act**: mouse, keyboard (Unicode input, so `@` and Turkish letters work on any layout),
   app launch, browser address bar.
6. **Measure**: pixel/title/focus change per step ("NO visible change" is reported), and typed text
   is read back from the field ("TEXT MISMATCH" if it differs).
7. **Done check**: before `done` is accepted, a different reviewer model compares the final
   screenshot and the field read-backs with the goal; a failure goes back to the agent to fix.

## Learning across tasks (Lecture 3 Reflexion, Lecture 5 memory)

- After every live run, code credits or blames the lessons the run used (used / helped / failed).
- After a failed or wasteful run, the reviewer model (not the acting model) reads the trajectory
  and up to four screenshots and makes one evidence-backed change: add, revise or vote to retire
  a lesson. Lessons with run-specific IDs or coordinates are rejected.
- Lessons stay in the planner's system prompt across tasks (only those relevant to the current app)
  until rules retire them: contradicted in two runs, or followed by failure in most of 3+ uses.
  Retired lessons are archived, not deleted.

```powershell
.venv\Scripts\python.exe memory.py list --archived
.venv\Scripts\python.exe memory.py show L1a2b3c
.venv\Scripts\python.exe memory.py archive L1a2b3c     # restore / revert / flush also exist
```

## Integration points

- **LangGraph**: `langgraph_node.make_gui_node(planner_factory, execute=..., reflector=..., done_checker=...)`
  reads `gui_request` (fields of `worker_api.GuiTask`) and appends to `gui_results`.
- **MCP** (`mcp_server.py`, stdio): tools `run_gui_subtask`, `gui_reflect`, `gui_capabilities`,
  `gui_windows`; resources `gui://capabilities`, `gui://windows`, `gui://memory/lessons`,
  `gui://runs/{run_id}/result`, `gui://runs/{run_id}/screenshot`.
- Results always have `verified: false`; the orchestrator's checker decides success. Its verdict can
  be fed back with `gui_reflect(run_id, feedback)`.

## Files

| File | Role |
|---|---|
| `gui_worker.py` | the observe-decide-act loop, validation, approval gate |
| `worker_api.py` | task contract (`GuiTask`), desktop lock, scope, timeouts |
| `model_planner.py` | planner prompt, OpenAI/Gemini backends, scratchpad, lesson selection |
| `observations.py`, `ui_tree.py`, `annotate.py` | screenshots, UI Automation scan, numbered marks |
| `actions.py`, `desktopgui.py`, `app_tools.py`, `input_policy.py` | input, app launch, allowlists |
| `progress.py` | effect detection, loop guard, step summaries |
| `readback.py` | reads typed text back from the focused field |
| `verify.py` | reviewer-model done check |
| `reflect.py`, `memory.py` | reflection and self-updating lesson memory |
| `run_agent.py`, `mcp_server.py`, `langgraph_node.py` | entry points |

Run logs and screenshots go to `outputs/`, lessons to `memory/`; both are git-ignored.
