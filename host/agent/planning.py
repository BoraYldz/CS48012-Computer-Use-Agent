"""Deterministic validation helpers for planner output."""
from __future__ import annotations

import re
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any


# A leading location phrase is an explicit destination anchor, not decorative
# context. These common home folders are represented as directories under the
# agent's sandbox root in both local and VM modes.
_EXPLICIT_HOME_LOCATION = re.compile(
    r"^\s*(?:in|inside|under)\s+"
    r"(Desktop|Documents|Downloads|Pictures|Music|Videos)\b",
    re.IGNORECASE,
)
_PATH_ASSERTION_KINDS = {
    "path_exists",
    "path_absent",
    "text_contains",
    "text_equals",
    "xlsx_sheet_exists",
    "xlsx_values_present",
    "xlsx_rows_present",
    "xlsx_cell_equals",
    "xlsx_formula_present",
    "xlsx_data_row_count",
    "xlsx_aggregate_matches",
}


def _anchor_key(assertion: dict[str, Any], key: str, anchor: str,
                subgoal: dict[str, Any], changes: list[dict[str, str]]) -> None:
    """Prefix one path-valued field of an assertion with the leading location anchor."""
    original = assertion.get(key)
    if not isinstance(original, str) or not original.strip():
        return
    normalized = original.replace("\\", "/")
    posix_path = PurePosixPath(normalized)
    windows_path = PureWindowsPath(original)
    if (posix_path.is_absolute() or windows_path.is_absolute()
            or windows_path.drive or ".." in posix_path.parts):
        return
    clean_path = normalized.lstrip("./")
    first_part = PurePosixPath(clean_path).parts[:1]
    if first_part and first_part[0].casefold() == anchor.casefold():
        return
    corrected = f"{anchor}/{clean_path}"
    assertion[key] = corrected
    changes.append({"from": original, "to": corrected, "subgoal_id": str(subgoal.get("id", "?"))})


def apply_explicit_location_anchor(
    goal: str,
    subgoals: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Prefix planner check paths with a location explicitly leading the goal.

    Example: "In Desktop, create Proje" must be checked at Desktop/Proje,
    not Proje at the sandbox root. The worker is separately shown the exact
    corrected check paths, so execution and verification share one contract.

    Only relative, non-traversing paths are normalized. Absolute paths and
    paths containing '..' are left unchanged so the normal sandbox policy can
    reject them rather than silently rewriting them.
    """
    match = _EXPLICIT_HOME_LOCATION.match(goal or "")
    if not match:
        return None

    anchor = match.group(1)
    changes: list[dict[str, str]] = []

    for subgoal in subgoals:
        spec = subgoal.get("check_spec") or {}
        assertions = spec.get("checks", []) if isinstance(spec, dict) else []
        if not isinstance(assertions, list):
            continue

        for assertion in assertions:
            if not isinstance(assertion, dict):
                continue
            if assertion.get("kind") not in _PATH_ASSERTION_KINDS:
                continue
            for key in ("path", "source_path"):
                _anchor_key(assertion, key, anchor, subgoal, changes)

    return {"anchor": anchor, "changes": changes}


def apply_plan_path_rules(
    goal: str,
    subgoals: list[dict[str, Any]],
    scope: str,
) -> dict[str, Any] | None:
    """Apply path normalization appropriate to the configured allowed root.

    In sandbox scope the root models the user's home folder, so a leading
    destination such as "In Desktop" must be reflected in verifier paths. In
    workspace scope the root is already the selected working directory; paths
    remain relative to it and must not receive an extra OS-folder prefix.
    """
    if scope == "workspace":
        return {"scope": scope, "changes": []}
    if scope != "sandbox":
        raise ValueError(f"unknown agent scope: {scope!r}")
    return apply_explicit_location_anchor(goal, subgoals)
