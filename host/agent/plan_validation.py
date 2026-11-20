"""Deterministic sanity checks for planner-generated acceptance criteria.

Check specs are postconditions evaluated after a worker finishes. This module
repairs one unambiguous planner mistake: using path_absent for a path the same
subgoal is explicitly creating. Contradictory absent + positive checks for the
same path are resolved in favour of the positive postcondition and logged.
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath
from typing import Any


_CREATE_ACTION = re.compile(
    r"\b(create|creates|creating|make|makes|making|write|writes|writing|"
    r"save|saves|saving|generate|generates|generating|build|builds|building|"
    r"produce|produces|producing|add|adds|adding)\b",
    re.IGNORECASE,
)
_DIRECTORY_WORDS = re.compile(r"\b(folder|directory|subfolder)\b", re.IGNORECASE)
_POSITIVE_KINDS = {
    "path_exists", "text_contains", "text_equals", "xlsx_sheet_exists",
    "xlsx_values_present", "xlsx_rows_present", "xlsx_cell_equals",
    "xlsx_formula_present", "xlsx_data_row_count", "xlsx_aggregate_matches",
}


def _norm_path(value: str) -> str:
    return "/".join(part.casefold() for part in PurePosixPath(value.replace("\\", "/")).parts)


def _created_target_context(path: str, text: str) -> str | None:
    """Return nearby text only when the basename is the object being created.

    Names used as a destination parent (e.g. "inside DemoProject") must not
    be mistaken for the newly-created target.
    """
    name = PurePosixPath(path.replace("\\", "/")).name
    candidates = [name]
    if "." in name:
        candidates.append(name.rsplit(".", 1)[0])
    for candidate in candidates:
        if not candidate:
            continue
        match = re.search(rf"(?<![\w]){re.escape(candidate.casefold())}(?![\w])", text.casefold())
        if not match:
            continue
        prefix = text.casefold()[max(0, match.start() - 24):match.start()]
        if re.search(r"\b(inside|within|under|into|beneath)\s+(?:the\s+)?$", prefix):
            continue
        return text[max(0, match.start() - 36):min(len(text), match.end() + 36)]
    return None


def _target_is_directory(context: str) -> bool | None:
    has_dir = bool(_DIRECTORY_WORDS.search(context))
    has_file = bool(re.search(r"\b(file|document|workbook|spreadsheet)\b", context, re.IGNORECASE))
    if has_dir and not has_file:
        return True
    if has_file and not has_dir:
        return False
    return None


def normalize_plan_check_specs(subgoals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Correct unambiguous create-vs-absent mistakes; return an audit trail.

    The normalizer intentionally does not invent missing check specs or guess
    arbitrary paths. It only changes a `path_absent` assertion when the
    subgoal explicitly describes creating the referenced item, or when a
    positive assertion for the exact same path already makes absence
    logically contradictory.
    """
    changes: list[dict[str, Any]] = []

    for subgoal in subgoals:
        spec = subgoal.get("check_spec")
        if not isinstance(spec, dict) or not isinstance(spec.get("checks"), list):
            continue

        checks = spec["checks"]
        positive_paths = {
            _norm_path(assertion["path"])
            for assertion in checks
            if isinstance(assertion, dict)
            and assertion.get("kind") in _POSITIVE_KINDS
            and isinstance(assertion.get("path"), str)
            and assertion["path"].strip()
        }
        description = " ".join((
            str(subgoal.get("instruction", "")),
            str(subgoal.get("expected_output", "")),
        ))
        is_creation_subgoal = bool(_CREATE_ACTION.search(description))

        retained: list[Any] = []
        for assertion in checks:
            if not isinstance(assertion, dict) or assertion.get("kind") != "path_absent":
                retained.append(assertion)
                continue

            path = assertion.get("path")
            if not isinstance(path, str) or not path.strip():
                retained.append(assertion)
                continue

            normalized = _norm_path(path)
            same_path_positive = normalized in positive_paths
            target_context = _created_target_context(path, description) if is_creation_subgoal else None
            explicit_create_target = target_context is not None

            if same_path_positive:
                changes.append({
                    "subgoal_id": str(subgoal.get("id", "?")),
                    "path": path,
                    "action": "removed_contradictory_path_absent",
                    "reason": "a positive postcondition for the same path already requires it to exist",
                })
                continue

            if explicit_create_target:
                replacement = {"kind": "path_exists", "path": path}
                target_is_dir = _target_is_directory(target_context or "")
                if target_is_dir is not None:
                    replacement["is_dir"] = target_is_dir
                retained.append(replacement)
                changes.append({
                    "subgoal_id": str(subgoal.get("id", "?")),
                    "path": path,
                    "action": "converted_path_absent_to_path_exists",
                    "reason": "check specs are postconditions; this subgoal explicitly creates the target",
                    "replacement": replacement,
                })
                continue

            retained.append(assertion)

        spec["checks"] = retained

    return changes
_EMPTY_FOLDER_RE = re.compile(
    r"\b(?:leave|keep|ensure|make)\s+(?:the\s+)?([A-Za-z0-9_.-]+)\s+"
    r"(?:folder|directory)\s+empty\b",
    re.IGNORECASE,
)
_GLOB_BASENAME_RE = re.compile(r"^\*\.([A-Za-z0-9]+)$")


def find_explicitly_empty_directories(goal: str, subgoals: list[dict[str, Any]]) -> list[str]:
    """Resolve folder names explicitly required to be empty to planned relative paths."""
    names = [m.group(1).casefold() for m in _EMPTY_FOLDER_RE.finditer(goal or "")]
    if not names:
        return []

    path_values: list[str] = []
    path_values.extend(re.findall(
        r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)", goal or ""
    ))
    for subgoal in subgoals:
        spec = subgoal.get("check_spec") or {}
        for assertion in spec.get("checks", []) if isinstance(spec, dict) else []:
            if isinstance(assertion, dict):
                for key in ("path", "source_dir"):
                    if isinstance(assertion.get(key), str):
                        path_values.append(assertion[key])

    resolved: list[str] = []
    for name in names:
        candidates: list[str] = []
        for value in path_values:
            normalized = value.replace("\\", "/").strip("/")
            parts = list(PurePosixPath(normalized).parts)
            if parts:
                parts[-1] = parts[-1].rstrip(".,;:")
            for index, part in enumerate(parts):
                if part.casefold() == name:
                    candidates.append("/".join(parts[:index + 1]))
        if candidates:
            selected = max(candidates, key=lambda item: (len(PurePosixPath(item).parts), item))
            if selected not in resolved:
                resolved.append(selected)
    return resolved


def normalize_empty_directory_destinations(
    goal: str, subgoals: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Rebase output roots outside directories the user explicitly requires empty."""
    changes: list[dict[str, str]] = []
    for empty_dir in find_explicitly_empty_directories(goal, subgoals):
        source_parts = PurePosixPath(empty_dir).parts
        destination_counts: dict[str, int] = {}
        for subgoal in subgoals:
            spec = subgoal.get("check_spec") or {}
            assertions = spec.get("checks", []) if isinstance(spec, dict) else []
            for assertion in assertions:
                if not isinstance(assertion, dict):
                    continue
                value = assertion.get("path")
                if not isinstance(value, str):
                    continue
                parts = PurePosixPath(value.replace("\\", "/")).parts
                if (len(parts) > len(source_parts)
                        and tuple(p.casefold() for p in parts[:len(source_parts)])
                        == tuple(p.casefold() for p in source_parts)):
                    next_part = parts[len(source_parts)]
                    if not any(ch in next_part for ch in "*?[]"):
                        destination_counts[next_part] = destination_counts.get(next_part, 0) + 1

        if not destination_counts:
            continue
        destination_name = sorted(
            destination_counts, key=lambda k: (-destination_counts[k], k.casefold())
        )[0]
        old_prefix = f"{empty_dir.rstrip('/')}/{destination_name}"
        new_prefix = (PurePosixPath(empty_dir).parent / destination_name).as_posix()
        if old_prefix == new_prefix:
            continue

        for subgoal in subgoals:
            for field in ("instruction", "expected_output"):
                value = subgoal.get(field)
                if isinstance(value, str):
                    if old_prefix in value:
                        value = value.replace(old_prefix, new_prefix)
                    # A creation instruction may name the parent folder but omit
                    # the complete target path. Keep it consistent with the rebase.
                    parent_pattern = re.compile(
                        rf"\b(inside|within|under|in)\s+{re.escape(empty_dir)}\b",
                        re.IGNORECASE,
                    )
                    if destination_name.casefold() in value.casefold():
                        value = parent_pattern.sub(f"at {new_prefix}", value)
                    subgoal[field] = value
            spec = subgoal.get("check_spec") or {}
            assertions = spec.get("checks", []) if isinstance(spec, dict) else []
            for assertion in assertions:
                if isinstance(assertion, dict) and isinstance(assertion.get("path"), str):
                    if assertion["path"].startswith(old_prefix):
                        assertion["path"] = new_prefix + assertion["path"][len(old_prefix):]

        changes.append({
            "empty_directory": empty_dir,
            "old_output_root": old_prefix,
            "new_output_root": new_prefix,
            "reason": "output directories must not be children of a directory required to be empty",
        })
    return changes


def normalize_glob_move_checks(subgoals: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Replace wildcard move checks with snapshot-based assertions.

    Handles both paired wildcard assertions (source ``*.txt`` absent and
    destination ``*.txt`` present) and the common planner output where the
    destination is asserted as an existing directory.
    """
    changes: list[dict[str, str]] = []
    for subgoal in subgoals:
        spec = subgoal.get("check_spec")
        if not isinstance(spec, dict) or not isinstance(spec.get("checks"), list):
            continue
        checks = spec["checks"]
        absent_by_ext: dict[str, list[dict[str, Any]]] = {}
        exists_by_ext: dict[str, list[dict[str, Any]]] = {}
        for assertion in checks:
            if not isinstance(assertion, dict) or assertion.get("kind") not in {"path_absent", "path_exists"}:
                continue
            raw_path = assertion.get("path")
            if not isinstance(raw_path, str):
                continue
            parts = PurePosixPath(raw_path.replace("\\", "/")).parts
            if not parts:
                continue
            match = _GLOB_BASENAME_RE.fullmatch(parts[-1])
            if not match:
                continue
            ext = "." + match.group(1).casefold()
            target = absent_by_ext if assertion["kind"] == "path_absent" else exists_by_ext
            target.setdefault(ext, []).append(assertion)

        replacements: list[dict[str, Any]] = []
        removed_ids: set[int] = set()
        for ext, absent_checks in absent_by_ext.items():
            for absent in absent_checks:
                absent_path = PurePosixPath(absent["path"].replace("\\", "/"))
                source_dir = absent_path.parent.as_posix()
                destination_dir = None
                paired_present = None

                # Preferred form: destination wildcard directly identifies the folder.
                for present in exists_by_ext.get(ext, []):
                    present_path = PurePosixPath(present["path"].replace("\\", "/"))
                    candidate = present_path.parent.as_posix()
                    if source_dir != candidate:
                        destination_dir = candidate
                        paired_present = present
                        break

                # Common form: planner verifies the destination folder itself, not
                # each wildcard file. Use it only when exactly one unambiguous
                # destination directory assertion exists in this subgoal.
                if destination_dir is None:
                    directory_candidates = [
                        item for item in checks
                        if isinstance(item, dict)
                        and item.get("kind") == "path_exists"
                        and item.get("is_dir") is True
                        and isinstance(item.get("path"), str)
                        and _norm_path(item["path"]) != _norm_path(source_dir)
                    ]
                    if len(directory_candidates) == 1 and re.search(
                        r"\b(move|moving|sort|organize)\b",
                        " ".join((str(subgoal.get("instruction", "")),
                                  str(subgoal.get("expected_output", "")))),
                        re.IGNORECASE,
                    ):
                        destination_dir = directory_candidates[0]["path"].replace("\\", "/")

                if not destination_dir:
                    continue

                already_added = any(
                    isinstance(item, dict) and item.get("kind") == "files_moved_by_extension"
                    and item.get("source_dir") == source_dir
                    and item.get("destination_dir") == destination_dir
                    and str(item.get("extension", "")).casefold() == ext.casefold()
                    for item in checks
                )
                if not already_added:
                    replacements.append({
                        "kind": "files_moved_by_extension",
                        "source_dir": source_dir,
                        "destination_dir": destination_dir,
                        "extension": ext,
                        "expected_files": None,
                    })
                removed_ids.add(id(absent))
                if paired_present is not None:
                    removed_ids.add(id(paired_present))
                changes.append({
                    "subgoal_id": str(subgoal.get("id", "?")),
                    "extension": ext,
                    "source_dir": source_dir,
                    "destination_dir": destination_dir,
                    "action": "replaced_wildcard_checks_with_file_move_snapshot",
                })

        if replacements:
            checks[:] = [item for item in checks if id(item) not in removed_ids]
            checks.extend(replacements)

    return changes


def add_empty_directory_postcondition(
    goal: str, subgoals: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """Attach an empty-directory assertion to the final extension-move subgoal."""
    empty_dirs = find_explicitly_empty_directories(goal, subgoals)
    if not empty_dirs:
        return []
    move_subgoals = []
    for subgoal in subgoals:
        spec = subgoal.get("check_spec") or {}
        checks = spec.get("checks", []) if isinstance(spec, dict) else []
        if any(isinstance(item, dict) and item.get("kind") == "files_moved_by_extension"
               for item in checks):
            move_subgoals.append(subgoal)
    if not move_subgoals:
        return []
    target = move_subgoals[-1]
    checks = target["check_spec"]["checks"]
    changes = []
    for empty_dir in empty_dirs:
        if not any(isinstance(item, dict) and item.get("kind") == "directory_empty"
                   and item.get("path") == empty_dir for item in checks):
            checks.append({"kind": "directory_empty", "path": empty_dir})
            changes.append({
                "subgoal_id": str(target.get("id", "?")),
                "path": empty_dir,
                "action": "added_final_directory_empty_postcondition",
            })
    return changes



_SORT_SOURCE_RE = re.compile(
    r"\b(?:inside|from)\s+([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+)",
    re.IGNORECASE,
)
_SORT_MAPPING_RE = re.compile(
    r"\.([A-Za-z0-9]+)\s+files?\s+(?:in|to|into)\s+([A-Za-z][A-Za-z0-9_-]*)",
    re.IGNORECASE,
)
_SORT_OUTPUT_RE = re.compile(
    r"\bcreate\s+(?:an?\s+)?([A-Za-z0-9_-]+)\s+(?:folder|directory)\b",
    re.IGNORECASE,
)


def normalize_file_sort_assertions(
    goal: str, subgoals: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Add snapshot-backed move postconditions for explicit extension sorting requests.

    The LLM planner often describes the destination directories but supplies only
    `path_exists` checks. Those checks can pass even when no files were moved.
    When the user explicitly maps extensions to folder names, add one independent
    files_moved_by_extension assertion per mapping before any worker is run.
    """
    goal = goal or ""
    source_match = _SORT_SOURCE_RE.search(goal)
    mappings = [
        ("." + match.group(1).casefold(), match.group(2))
        for match in _SORT_MAPPING_RE.finditer(goal)
    ]
    output_match = _SORT_OUTPUT_RE.search(goal)
    if not source_match or not mappings or not output_match:
        return []

    source_dir = source_match.group(1).rstrip(".,;:")
    output_name = output_match.group(1)
    source_parent = PurePosixPath(source_dir).parent

    # If the source must be empty, the destination root is unambiguously its
    # sibling. Do not trust a stale planner path here: the planner may have put
    # Organized inside Incoming, and later normalization may have corrected
    # checker paths without correcting every natural-language instruction.
    explicitly_empty = find_explicitly_empty_directories(goal, subgoals)
    source_is_required_empty = any(
        _norm_path(item) == _norm_path(source_dir) for item in explicitly_empty
    )

    # Otherwise prefer a destination path proposed by the planner. If none is
    # available, derive the named output folder beside the source.
    destination_roots: list[str] = []
    if not source_is_required_empty:
        for extension, folder_name in mappings:
            for subgoal in subgoals:
                spec = subgoal.get("check_spec") or {}
                checks = spec.get("checks", []) if isinstance(spec, dict) else []
                for assertion in checks:
                    if not isinstance(assertion, dict) or assertion.get("kind") != "path_exists":
                        continue
                    path = assertion.get("path")
                    if (isinstance(path, str) and assertion.get("is_dir") is True
                            and PurePosixPath(path.replace("\\", "/")).name.casefold()
                            == folder_name.casefold()):
                        destination_roots.append(
                            PurePosixPath(path.replace("\\", "/")).parent.as_posix()
                        )
    destination_root = (
        (source_parent / output_name).as_posix()
        if source_is_required_empty
        else (destination_roots[0] if destination_roots
              else (source_parent / output_name).as_posix())
    )

    changes: list[dict[str, Any]] = []
    next_id = max((int(s.get("id", -1)) for s in subgoals), default=-1) + 1
    claimed_subgoals: set[str] = set()
    for extension, folder_name in mappings:
        destination_dir = (PurePosixPath(destination_root) / folder_name).as_posix()
        matching_subgoal = None
        for subgoal in subgoals:
            subgoal_key = str(subgoal.get("id", "?"))
            if subgoal_key in claimed_subgoals:
                continue
            description = " ".join((str(subgoal.get("instruction", "")),
                                    str(subgoal.get("expected_output", ""))))
            if (extension.casefold() in description.casefold()
                    and re.search(r"\b(move|moving|sort|organize|transfer)\b", description, re.IGNORECASE)):
                matching_subgoal = subgoal
                break

        if matching_subgoal is None:
            matching_subgoal = {
                "id": next_id,
                "capability": "files",
                "instruction": (
                    f"Move every {extension} file directly inside {source_dir} "
                    f"into {destination_dir}, preserving each filename and file content. "
                    "Use move_files with the exact basenames from list_dir."
                ),
                "expected_output": f"All original {extension} files are in {destination_dir} and absent from {source_dir}.",
                "check_spec": {"checks": [], "rationale": "Verify the requested extension-based file moves."},
            }
            subgoals.append(matching_subgoal)
            next_id += 1
            action = "added_missing_extension_move_subgoal"
        else:
            action = "added_snapshot_backed_extension_move_check"

        # The normalized move contract is authoritative. Rewrite the planner's
        # prose as well as its check_spec so the worker cannot receive conflicting
        # directions (for example, a stale Incoming/Organized destination while
        # the checker expects the sibling Organized directory).
        matching_subgoal["instruction"] = (
            f"Move only the {extension} files listed in the planning-time manifest "
            f"from {source_dir} into {destination_dir}. Preserve each original "
            "filename and content. Use the scoped move_expected_files tool with "
            "the exact basenames from the manifest. Do not move other extensions "
            "or use a different destination."
        )
        matching_subgoal["expected_output"] = (
            f"Every snapshotted {extension} file is in {destination_dir}, absent "
            f"from {source_dir}, with its original content preserved."
        )
        claimed_subgoals.add(str(matching_subgoal.get("id", "?")))

        spec = matching_subgoal.setdefault("check_spec", {"checks": [], "rationale": ""})
        checks = spec.setdefault("checks", [])
        # Remove the planner's wildcard checks for this extension. They are not
        # valid filesystem paths and are superseded by the exact snapshot check.
        kept = []
        for assertion in checks:
            if not isinstance(assertion, dict):
                kept.append(assertion)
                continue
            raw_path = assertion.get("path")
            if (assertion.get("kind") in {"path_absent", "path_exists"}
                    and isinstance(raw_path, str)):
                base = PurePosixPath(raw_path.replace("\\", "/")).name
                if (base.casefold() == ("*" + extension).casefold()
                        or (assertion.get("kind") == "path_exists"
                            and assertion.get("is_dir") is True
                            and base.casefold() == folder_name.casefold()
                            and _norm_path(raw_path) != _norm_path(destination_dir))):
                    continue
            if (isinstance(assertion, dict)
                    and assertion.get("kind") == "files_moved_by_extension"
                    and str(assertion.get("extension", "")).casefold() == extension.casefold()):
                continue
            kept.append(assertion)
        checks[:] = kept

        if not any(
            isinstance(item, dict)
            and item.get("kind") == "files_moved_by_extension"
            and item.get("source_dir") == source_dir
            and item.get("destination_dir") == destination_dir
            and str(item.get("extension", "")).casefold() == extension.casefold()
            for item in checks
        ):
            checks.append({
                "kind": "files_moved_by_extension",
                "source_dir": source_dir,
                "destination_dir": destination_dir,
                "extension": extension,
                "expected_files": None,
            })
        if not any(
            isinstance(item, dict) and item.get("kind") == "path_exists"
            and item.get("path") == destination_dir
            for item in checks
        ):
            checks.append({"kind": "path_exists", "path": destination_dir, "is_dir": True})
        changes.append({
            "subgoal_id": str(matching_subgoal.get("id", "?")),
            "extension": extension,
            "source_dir": source_dir,
            "destination_dir": destination_dir,
            "action": action,
        })

    return changes


def normalize_bare_empty_globs(subgoals: list[dict[str, Any]]) -> list[dict[str, str]]:
    """Convert `path_absent: folder/*` into a direct directory_empty assertion."""
    changes: list[dict[str, str]] = []
    for subgoal in subgoals:
        spec = subgoal.get("check_spec")
        if not isinstance(spec, dict) or not isinstance(spec.get("checks"), list):
            continue
        kept = []
        for assertion in spec["checks"]:
            if (isinstance(assertion, dict) and assertion.get("kind") == "path_absent"
                    and isinstance(assertion.get("path"), str)):
                candidate = PurePosixPath(assertion["path"].replace("\\", "/"))
                if candidate.name == "*":
                    directory = candidate.parent.as_posix()
                    replacement = {"kind": "directory_empty", "path": directory}
                    if not any(isinstance(item, dict) and item.get("kind") == "directory_empty"
                               and item.get("path") == directory for item in kept):
                        kept.append(replacement)
                    changes.append({
                        "subgoal_id": str(subgoal.get("id", "?")),
                        "path": assertion["path"],
                        "action": "converted_bare_wildcard_to_directory_empty",
                    })
                    continue
            kept.append(assertion)
        spec["checks"] = kept
    return changes


def deduplicate_file_move_assertions(subgoals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep one snapshot-backed assertion for each source/destination/extension.

    Several planner-normalization passes can recognize the same wildcard move.
    Duplicate assertions cause repeated snapshots and make the plan harder to
    reason about, so retain the first canonical occurrence only.
    """
    seen: set[tuple[str, str, str]] = set()
    changes: list[dict[str, Any]] = []
    for subgoal in subgoals:
        spec = subgoal.get("check_spec")
        if not isinstance(spec, dict) or not isinstance(spec.get("checks"), list):
            continue
        kept = []
        for assertion in spec["checks"]:
            if not isinstance(assertion, dict) or assertion.get("kind") != "files_moved_by_extension":
                kept.append(assertion)
                continue
            key = (
                _norm_path(str(assertion.get("source_dir", ""))),
                _norm_path(str(assertion.get("destination_dir", ""))),
                str(assertion.get("extension", "")).casefold(),
            )
            if key in seen:
                changes.append({
                    "subgoal_id": str(subgoal.get("id", "?")),
                    "source_dir": assertion.get("source_dir"),
                    "destination_dir": assertion.get("destination_dir"),
                    "extension": assertion.get("extension"),
                    "action": "removed_duplicate_file_move_assertion",
                })
                continue
            seen.add(key)
            kept.append(assertion)
        spec["checks"] = kept
    return changes


def build_bound_move_request(
    assertion: dict[str, Any], filenames: list[str]
) -> dict[str, Any]:
    """Validate filenames and return only the canonical, planner-bound move args.

    Callers do not supply source/destination paths. Those are taken from the
    normalized acceptance assertion, so the worker cannot redirect a move to
    a stale or conflicting destination.
    """
    source_dir = assertion.get("source_dir")
    destination_dir = assertion.get("destination_dir")
    extension = assertion.get("extension")
    manifest = assertion.get("expected_files")
    if not isinstance(source_dir, str) or not source_dir:
        raise ValueError("the move contract has no source directory")
    if not isinstance(destination_dir, str) or not destination_dir:
        raise ValueError("the move contract has no destination directory")
    if not isinstance(extension, str) or not extension.startswith("."):
        raise ValueError("the move contract has no valid extension")
    if not isinstance(manifest, list) or not manifest:
        raise ValueError("there are no snapshotted source files available to move")
    if not isinstance(filenames, list) or not filenames:
        raise ValueError("filenames must be a non-empty list of exact snapshotted basenames")

    allowed = {
        item.get("name") for item in manifest
        if isinstance(item, dict) and isinstance(item.get("name"), str)
    }
    if not allowed:
        raise ValueError("the planning-time file manifest contains no valid basenames")
    if any(not isinstance(name, str) or not name for name in filenames):
        raise ValueError("every filename must be a non-empty string")
    if len(set(filenames)) != len(filenames):
        raise ValueError("duplicate filenames are not allowed")
    unexpected = [name for name in filenames if name not in allowed]
    if unexpected:
        raise ValueError(f"files are not in the verified planning-time manifest: {unexpected}")
    wrong_extension = [name for name in filenames if not name.casefold().endswith(extension.casefold())]
    if wrong_extension:
        raise ValueError(f"only {extension} files may be moved by this operation: {wrong_extension}")
    return {
        "src_dir": source_dir,
        "filenames": filenames,
        "dst_dir": destination_dir,
    }


async def execute_bound_move(vm, assertion: dict[str, Any], filenames: list[str]) -> dict[str, Any]:
    """Execute an idempotent move using the assertion's fixed paths and manifest.

    Files already at the correct destination are treated as completed. Files
    still in the source are moved in one batch. A file missing from both places,
    or present in both, is reported rather than guessed/recreated.
    """
    args = build_bound_move_request(assertion, filenames)
    source_dir = args["src_dir"]
    destination_dir = args["dst_dir"]
    requested = args["filenames"]

    source_entries = await vm.call("list_dir", path=source_dir)
    if not isinstance(source_entries, list):
        raise RuntimeError("source inventory returned an unexpected result")
    destination_info = await vm.call("path_info", path=destination_dir)
    if not isinstance(destination_info, dict) or "exists" not in destination_info:
        raise RuntimeError("destination path inspection returned an unexpected result")
    if destination_info.get("exists") and not destination_info.get("is_dir"):
        raise NotADirectoryError(f"destination exists but is not a directory: {destination_dir}")
    destination_entries = (
        await vm.call("list_dir", path=destination_dir)
        if destination_info.get("exists") else []
    )
    if not isinstance(destination_entries, list):
        raise RuntimeError("destination inventory returned an unexpected result")

    def direct_file_names(entries: list[Any]) -> set[str]:
        result = set()
        for item in entries:
            if not isinstance(item, dict) or item.get("type") != "file":
                continue
            result.add(PurePosixPath(str(item.get("path", "")).replace("\\", "/")).name)
        return result

    source_names = direct_file_names(source_entries)
    destination_names = direct_file_names(destination_entries)
    at_source = [name for name in requested if name in source_names]
    at_destination = [name for name in requested if name in destination_names]
    in_both = sorted(set(at_source) & set(at_destination))
    if in_both:
        raise FileExistsError(f"files exist in both source and destination: {in_both}")
    missing_everywhere = sorted(set(requested) - set(at_source) - set(at_destination))
    if missing_everywhere:
        raise FileNotFoundError(
            f"snapshotted files are missing from both source and expected destination: {missing_everywhere}"
        )

    moved: list[str] = []
    if at_source:
        move_result = await vm.call(
            "move_files", src_dir=source_dir, filenames=at_source, dst_dir=destination_dir
        )
        if isinstance(move_result, dict):
            moved = move_result.get("moved", at_source)
        else:
            moved = at_source
    return {
        "moved": moved,
        "already_at_destination": at_destination,
        "count": len(moved) + len(at_destination),
        "destination": destination_dir,
    }


def strengthen_directory_inventory_checks(subgoals: list[dict[str, Any]], source_dir: str) -> list[dict[str, str]]:
    """Make a read-only inventory subgoal prove the source contains files, not just the folder."""
    changes: list[dict[str, str]] = []
    for subgoal in subgoals:
        description = " ".join((str(subgoal.get("instruction", "")),
                                str(subgoal.get("expected_output", ""))))
        if not re.search(r"\b(list|inspect|review|identify|inventory|enumerate|discover)\b", description, re.IGNORECASE):
            continue
        spec = subgoal.get("check_spec")
        if not isinstance(spec, dict) or not isinstance(spec.get("checks"), list):
            continue
        checks = spec["checks"]
        replaced = False
        for index, assertion in enumerate(checks):
            if (isinstance(assertion, dict) and assertion.get("kind") == "path_exists"
                    and assertion.get("path") == source_dir):
                checks[index] = {"kind": "directory_contains_files", "path": source_dir}
                replaced = True
        if replaced:
            changes.append({
                "subgoal_id": str(subgoal.get("id", "?")),
                "path": source_dir,
                "action": "strengthened_inventory_postcondition",
            })
    return changes


async def snapshot_move_assertions(vm, subgoals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Capture exact matching input files before any worker starts moving them."""
    snapshots: list[dict[str, Any]] = []
    for subgoal in subgoals:
        spec = subgoal.get("check_spec") or {}
        checks = spec.get("checks", []) if isinstance(spec, dict) else []
        for assertion in checks:
            if not isinstance(assertion, dict) or assertion.get("kind") != "files_moved_by_extension":
                continue
            try:
                source_dir = assertion["source_dir"]
                extension = assertion["extension"]
                entries = await vm.call("list_dir", path=source_dir)
                expected = [
                    {
                        **{
                            "name": PurePosixPath(str(item.get("path", "")).replace("\\", "/")).name,
                            "size": item.get("size"),
                        },
                        **({"sha256": item.get("sha256")} if item.get("sha256") else {}),
                    }
                    for item in entries
                    if isinstance(item, dict) and item.get("type") == "file"
                    and PurePosixPath(str(item.get("path", "")).replace("\\", "/")).name.casefold().endswith(
                        str(extension).casefold()
                    )
                ]
                assertion["expected_files"] = expected
                snapshots.append({
                    "subgoal_id": subgoal.get("id"),
                    "source_dir": source_dir,
                    "extension": extension,
                    "files": expected,
                })
            except Exception as exc:
                assertion["expected_files"] = []
                assertion["snapshot_error"] = f"{type(exc).__name__}: {str(exc)[:180]}"
                snapshots.append({
                    "subgoal_id": subgoal.get("id"),
                    "source_dir": assertion.get("source_dir"),
                    "extension": assertion.get("extension"),
                    "error": assertion["snapshot_error"],
                })
    return snapshots
