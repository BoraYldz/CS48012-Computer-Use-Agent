"""Generic, deterministic plan gates (no LLM). They close verifier holes the 8B planner leaves open.

Each gate encodes a rule that holds for ANY request, not for a particular eval task:
  A drop_unrequested_deletion_checks  a "no files left with extension X" postcondition needs a
                                       deletion verb in the request; otherwise the planner invented it.
  B ground_free_text_checks            expected file text must literally come from the request; text the
                                       planner made up (e.g. a guessed summary) cannot be a pass/fail oracle.
  C state_change_gap / neutralize      a plan made only of read/inspect subgoals cannot satisfy a request
                                       that asks for a change: replan once, else report UNKNOWN, never "done".
  D precheck_named_sources             a source file the request names by full path must exist, otherwise
                                       the subgoal is turned into an honest, failing postcondition.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any

_DELETION_WORDS = re.compile(
    r"\b(delet\w*|remov\w*|erase\w*|trash\w*|discard\w*|get\s+rid|clean(?:\s*up)?|purge\w*|wipe\w*|empty)\b",
    re.IGNORECASE)
_READ_ONLY_START = re.compile(
    r"^\s*(read|inspect|look|review|check|verify|examine|list|open|view|confirm|ensure|validate|find|"
    r"locate|identify|analy[sz]e|scan|load)\b", re.IGNORECASE)
_WRITE_INTENT = re.compile(
    r"\b(write|create|add|append|save|make|copy|duplicate|move|rename|put|sort|organi[sz]e|replace|update|"
    r"edit|insert|generate|compute|calculate|convert|import|export|set|delete|remove|collect|gather|place)\b",
    re.IGNORECASE)
_FILE_PATH = re.compile(r"[^\s\"'`,;()<>]+/[^\s\"'`,;()<>/]+\.[A-Za-z0-9]{1,6}")

PLAN_GAP_NOTE = ("\n\n[Planner note] The previous plan only read or inspected files, but the request asks "
                 "for a change. The plan MUST include the subgoal that performs the requested change "
                 "(write, create, copy, move, rename, edit ...) and its check.")


def _checks(sg: dict[str, Any]) -> list[dict[str, Any]]:
    spec = sg.get("check_spec")
    checks = spec.get("checks") if isinstance(spec, dict) else None
    return [c for c in (checks or []) if isinstance(c, dict)]


def _squash(text: str) -> str:
    return " ".join((text or "").casefold().split())


def _renumber(subgoals: list[dict[str, Any]]) -> None:
    for i, sg in enumerate(subgoals):
        sg["id"] = i


# ---------------------------------------------------------------- gate A
def drop_unrequested_deletion_checks(goal: str, subgoals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if _DELETION_WORDS.search(goal or ""):
        return []
    changes: list[dict[str, Any]] = []
    keep: list[dict[str, Any]] = []
    for sg in subgoals:
        checks = _checks(sg)
        kept = [c for c in checks if c.get("kind") != "no_files_by_extension"]
        if len(kept) == len(checks):
            keep.append(sg)
            continue
        changes.append({"subgoal_id": str(sg.get("id", "?")), "action": "dropped_deletion_check_not_requested"})
        if kept or len(subgoals) == 1:
            sg["check_spec"]["checks"] = kept
            keep.append(sg)
        else:                                   # the subgoal existed only to verify an invented deletion
            changes.append({"subgoal_id": str(sg.get("id", "?")), "action": "dropped_invented_verification_subgoal"})
    if len(keep) != len(subgoals):
        subgoals[:] = keep
        _renumber(subgoals)
    return changes


# ---------------------------------------------------------------- gate B
def ground_free_text_checks(goal: str, subgoals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """text_contains/text_equals whose text is not in the request become a plain existence check."""
    haystack = _squash(goal)
    changes: list[dict[str, Any]] = []
    for sg in subgoals:
        spec = sg.get("check_spec")
        raw = spec.get("checks") if isinstance(spec, dict) else None
        for i, c in enumerate(raw or []):
            if not isinstance(c, dict) or c.get("kind") not in ("text_contains", "text_equals"):
                continue
            text = _squash(str(c.get("text") or ""))
            if text and text in haystack:
                continue
            path = c.get("path")
            raw[i] = {"kind": "path_exists", "path": path, "is_dir": False}
            changes.append({"subgoal_id": str(sg.get("id", "?")), "path": path,
                            "action": "ungrounded_text_check_downgraded_to_path_exists",
                            "dropped_text": str(c.get("text"))[:80]})
    return changes


# ---------------------------------------------------------------- gate C
def state_change_gap(goal: str, subgoals: list[dict[str, Any]]) -> str | None:
    if not subgoals or not _WRITE_INTENT.search(goal or ""):
        return None
    if all(_READ_ONLY_START.match(str(sg.get("instruction", ""))) for sg in subgoals):
        return "plan has only read/inspect subgoals but the request asks for a change"
    return None


def neutralize_plan(subgoals: list[dict[str, Any]], reason: str) -> None:
    """Make every subgoal UNKNOWN (empty checks) so the run cannot end in 'done'."""
    for sg in subgoals:
        sg["check_spec"] = {"checks": [], "rationale": reason}


# ---------------------------------------------------------------- gate D
def _norm(path: str) -> str:
    p = path.replace("\\", "/")
    while p.startswith("./"):
        p = p[2:]
    return p.rstrip("/")


def named_file_paths(goal: str) -> list[str]:
    out = []
    for m in _FILE_PATH.finditer(goal or ""):
        p = _norm(m.group(0).rstrip(".,;:"))
        if p not in out:
            out.append(p)
    return out


async def precheck_named_sources(vm, goal: str, subgoals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """A file the request names by FULL path inside a file_ops source_dir must exist."""
    named = named_file_paths(goal)
    changes: list[dict[str, Any]] = []
    for sg in subgoals:
        for c in _checks(sg):
            if c.get("kind") != "file_ops" or not isinstance(c.get("source_dir"), str):
                continue
            src = _norm(c["source_dir"])
            ext = (c.get("extension") or "").casefold()
            missing = []
            for p in named:
                if PurePosixPath(p).parent.as_posix() != src or (ext and not p.casefold().endswith(ext)):
                    continue
                try:
                    info = await vm.call("path_info", path=p)
                except Exception:
                    continue
                if isinstance(info, dict) and not info.get("exists"):
                    missing.append(p)
            if missing:
                sg["check_spec"] = {
                    "checks": [{"kind": "path_exists", "path": missing[0], "is_dir": False}],
                    "rationale": "the request names this source file, which does not exist; the subgoal cannot succeed"}
                changes.append({"subgoal_id": str(sg.get("id", "?")), "missing": missing,
                                "action": "named_source_missing_subgoal_made_failing"})
                break
    return changes
