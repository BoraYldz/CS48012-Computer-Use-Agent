# Computer-Use Agent

A verifiable desktop assistant that turns a plain-language request into finished work in the browser, spreadsheets, documents and files inside a Windows VM, and **checks the result in code before reporting success**.

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
Check (code, not an LLM opinion) ── pass → next subgoal · fail → stop (reflection in Milestone 2)
   │
   ▼
Tool server inside the Windows VM (MCP over HTTP)
VM restored from a clean snapshot before every attempt
```

- **Host** (`host/`): orchestrator, workers, checkers, CLI, run logs.
- **VM** (`vm/server.py`): the agent's "hands". Every action goes through these tools, and paths are sandboxed to the VM user's home folder.
- **GUI worker** (`gui_worker/`, see its README): operates apps and websites through this PC's screen. The planner uses it when no tool worker can do the job. Without the VM tool server, `run` still works with the GUI worker alone.
- **Circuit breakers:** step limit per subgoal and token cap per task; the model never decides when to stop.

## Quick start

Step-by-step setup (VM, snapshot, models): **[SETUP.md](SETUP.md)**

```powershell
cd host
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
copy .env.example .env            # choose the model endpoint

.venv\Scripts\python.exe -m agent.cli ping              # reach the VM tool server
.venv\Scripts\python.exe -m agent.cli task 16 --reset   # reset VM -> run task -> code check
.venv\Scripts\python.exe -m agent.cli run "your request"
```

GUI worker from the orchestrator (no VM): put `OPENAI_API_KEY` in `gui_worker/.env` (copy
`gui_worker/.env.example`), point `host/.env` at a planner model, close private windows, then
`.venv\Scripts\python.exe -m agent.cli run "Open youtube.com in Chrome and search for lofi music"`.
The GUI settings (`GUI_MODEL`, `GUI_REVIEW_MODEL`, `GUI_EXECUTE`, ...) are in `host/.env.example`.

## Status

To Be Updated

## Team

| Member | Area |
|---|---|
| Bora Yıldız | Architecture, orchestrator, VM environment, interface |
| Ali Durmuş Abay | GUI fallback, checking & evaluation |
| Eren Cebeci | Tools, task suite scope, reflection & memory |
