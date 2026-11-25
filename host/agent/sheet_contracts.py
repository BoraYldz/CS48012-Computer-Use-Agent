"""Sheet-state contract: prove a spreadsheet subgoal changed only what it was asked to change.

Mirror of the Files worker's deletion contract, but planner-independent. Before the FIRST attempt
of a sheet subgoal the orchestrator looks at every workbook its check_spec names:

  * sheets the check_spec does not name  -> must be byte-for-byte (cell-for-cell) unchanged
  * a source workbook (`source_path`)    -> every sheet must stay unchanged (inputs are read-only)

It appends one synthetic `xlsx_sheets_preserved` assertion per workbook, carrying a digest of each
protected sheet. The deterministic checker re-reads and compares after the worker finishes, so a
worker that overwrote the wrong sheet fails the subgoal even if the planner never asked for that.
The snapshot runs lazily (at worker start, not plan time) so a workbook created by an earlier
subgoal (e.g. a copy made by the files worker) is protected too.
"""
from __future__ import annotations

import json
import re
from typing import Any

from .sheet_tools import digest_rows

_XLSX_IN_TEXT = re.compile(r"[^\s\"'`,;()<>]+\.xlsx\b", re.IGNORECASE)


def _assertions(subgoal: dict[str, Any]) -> list[dict[str, Any]]:
    spec = subgoal.get("check_spec")
    checks = spec.get("checks") if isinstance(spec, dict) else None
    return [a for a in checks if isinstance(a, dict)] if isinstance(checks, list) else []


def named_workbooks(subgoal: dict[str, Any]) -> dict[str, set[str | None]]:
    """workbook path -> sheet names the check_spec touches (None = 'whatever the active sheet is')."""
    out: dict[str, set[str | None]] = {}
    for a in _assertions(subgoal):
        kind = str(a.get("kind", ""))
        if not kind.startswith("xlsx_") or kind == "xlsx_sheets_preserved":
            continue
        if isinstance(a.get("path"), str) and a["path"]:
            out.setdefault(a["path"], set()).add(a.get("sheet") or None)
    return out


def source_workbooks(subgoal: dict[str, Any]) -> set[str]:
    """Return only XLSX inputs; CSV/text sources are not openpyxl workbooks."""
    return {a["source_path"] for a in _assertions(subgoal)
            if isinstance(a.get("source_path"), str)
            and a["source_path"].casefold().endswith((".xlsx", ".xlsm", ".xltx", ".xltm"))}


async def _sheet_digests(vm, path: str, names: list[str]) -> dict[str, str]:
    out = {}
    for name in names:
        book = await vm.call("xlsx_read", path=path, sheet=name, formulas=True, max_rows=5000)
        out[name] = digest_rows(book.get("rows", []))
    return out


async def snapshot_sheet_contract(vm, subgoal: dict[str, Any]) -> list[dict[str, Any]]:
    """Append `xlsx_sheets_preserved` assertions to the subgoal (idempotent). Returns log events."""
    spec = subgoal.get("check_spec")
    if not isinstance(spec, dict) or not isinstance(spec.get("checks"), list):
        return []
    already = {a.get("path") for a in _assertions(subgoal) if a.get("kind") == "xlsx_sheets_preserved"}
    sources = source_workbooks(subgoal)
    events = []
    # Source-only workbooks touch nothing (all sheets protected); a workbook the check_spec names
    # as a target protects only the sheets it does not name.
    for path, touched in {**{s: set() for s in sources}, **named_workbooks(subgoal)}.items():
        if path in already:
            continue
        try:
            info = await vm.call("path_info", path=path)
            if not (isinstance(info, dict) and info.get("exists") and not info.get("is_dir")):
                continue                                   # nothing to protect yet
            head = await vm.call("xlsx_read", path=path, max_rows=1)
            names, active = head["sheets"], head["sheet"]
            touched_names = {active if t is None else t for t in touched}
            keep = [n for n in names if n not in touched_names]
            if not keep:
                continue
            digests = await _sheet_digests(vm, path, keep)
            spec["checks"].append({"kind": "xlsx_sheets_preserved", "path": path, "preserved_sheets": digests})
            events.append({"subgoal_id": subgoal.get("id"), "path": path, "protected_sheets": keep})
        except Exception as exc:                           # unreadable workbook: only the positive checks apply
            events.append({"subgoal_id": subgoal.get("id"), "path": path,
                           "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    return events


async def workbook_overview(vm, subgoal: dict[str, Any], limit_chars: int = 1800) -> str:
    """Compact xlsx_info for every existing workbook the subgoal mentions (saves the worker a step)."""
    paths = list(dict.fromkeys(
        [*named_workbooks(subgoal), *source_workbooks(subgoal),
         *_XLSX_IN_TEXT.findall(str(subgoal.get("instruction", "")))]))
    chunks = []
    for path in paths:
        try:
            info = await vm.call("path_info", path=path)
            if not (isinstance(info, dict) and info.get("exists") and not info.get("is_dir")):
                chunks.append(f"{path}: does not exist")
                continue
            chunks.append(f"{path}: " + json.dumps(await vm.call("xlsx_info", path=path), ensure_ascii=False, default=str))
        except Exception as exc:
            chunks.append(f"{path}: not readable as a workbook ({type(exc).__name__})")
    return "\n".join(chunks)[:limit_chars]
