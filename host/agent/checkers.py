"""Deterministic verifiers for planner-generated subgoal check specifications.

A check is deliberately conservative: it returns PASS only when the requested
postcondition was observed, FAIL when an inspected postcondition is false, and
UNKNOWN when the claim cannot be checked with the available tools/evidence.
"""
from pathlib import PurePosixPath
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field


CellValue = str | int | float | bool | None


class CheckAssertion(BaseModel):
    """One deterministic assertion in a subgoal's acceptance specification."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="Supported: path_exists, path_absent, directory_empty, directory_contains_files, files_moved_by_extension, no_files_by_extension, text_contains, text_equals, xlsx_sheet_exists, xlsx_values_present, xlsx_rows_present")
    path: str | None = None
    is_dir: bool | None = None
    text: str | None = None
    sheet: str | None = None
    values: list[CellValue] | None = None
    rows: list[list[CellValue]] | None = None
    source_dir: str | None = None
    destination_dir: str | None = None
    extension: str | None = None
    expected_files: list[dict[str, Any]] | None = None
    snapshot_error: str | None = None
    recursive: bool | None = None
    preserved_files: list[dict[str, Any]] | None = None


class CheckSpec(BaseModel):
    """All assertions must pass for the subgoal to be considered verified."""

    model_config = ConfigDict(extra="forbid")

    checks: list[CheckAssertion] = Field(default_factory=list)
    rationale: str = Field(default="", description="Briefly state what the checks prove")


class CheckResult(TypedDict):
    status: Literal["pass", "fail", "unknown"]
    detail: str
    evidence: str


COMMON_CHECKS = {"path_exists", "path_absent"}
CHECKS_BY_CAPABILITY = {
    "files": COMMON_CHECKS | {"directory_empty", "directory_contains_files", "files_moved_by_extension", "no_files_by_extension", "text_contains", "text_equals"},
    "sheet": COMMON_CHECKS | {
        "xlsx_sheet_exists", "xlsx_values_present", "xlsx_rows_present"
    },
}
SUPPORTED_CHECKS = set().union(*CHECKS_BY_CAPABILITY.values())
# Common Finder/Explorer metadata can remain in an otherwise empty folder after
# moving every user file. Treat these as system noise, not user content.
_IGNORABLE_DIRECTORY_METADATA = {".ds_store", "thumbs.db", "desktop.ini"}


def _unknown(message: str) -> tuple[str, str]:
    return "unknown", message


def _pass(message: str) -> tuple[str, str]:
    return "pass", message


def _fail(message: str) -> tuple[str, str]:
    return "fail", message



def _row_matches(actual: list[Any], expected: list[Any]) -> bool:
    """Match expected cells against the start of a worksheet row."""
    if len(actual) < len(expected):
        return False
    return actual[:len(expected)] == expected


async def _check_assertion(vm, assertion: dict[str, Any], allowed_checks: set[str]) -> tuple[str, str]:
    kind = assertion.get("kind")
    path = assertion.get("path")

    if not isinstance(kind, str) or kind not in SUPPORTED_CHECKS:
        return _unknown(f"unsupported check kind: {kind!r}")
    if kind not in allowed_checks:
        return _unknown(f"check kind {kind!r} is not available to this worker")
    if kind in {"path_exists", "path_absent", "directory_empty", "directory_contains_files", "no_files_by_extension", "text_contains", "text_equals",
                "xlsx_sheet_exists", "xlsx_values_present", "xlsx_rows_present"}:
        if not isinstance(path, str) or not path.strip():
            return _unknown(f"{kind}: missing or invalid path")
        if any(ch in path for ch in "*?[]"):
            return _unknown(
                f"{kind} {path!r}: wildcard paths are not valid deterministic assertions; "
                "enumerate exact files with list_dir"
            )

    try:
        if kind == "directory_contains_files":
            entries = await vm.call("list_dir", path=path)
            if not isinstance(entries, list):
                return _unknown(f"directory_contains_files {path!r}: list_dir returned an unexpected result")
            files = [
                item for item in entries
                if isinstance(item, dict) and item.get("type") == "file"
                and PurePosixPath(str(item.get("path", "")).replace("\\", "/")).name.casefold()
                not in _IGNORABLE_DIRECTORY_METADATA
            ]
            if files:
                names = [PurePosixPath(str(item.get("path", "")).replace("\\", "/")).name for item in files]
                return _pass(f"directory_contains_files {path!r}: found {len(files)} file(s): {names}")
            return _fail(f"directory_contains_files {path!r}: no user files found")

        if kind == "directory_empty":
            entries = await vm.call("list_dir", path=path)
            if not isinstance(entries, list):
                return _unknown(f"directory_empty {path!r}: list_dir returned an unexpected result")
            remaining = [
                item for item in entries
                if not (isinstance(item, dict)
                        and PurePosixPath(str(item.get("path", "")).replace("\\", "/")).name.casefold()
                        in _IGNORABLE_DIRECTORY_METADATA)
            ]
            ignored = len(entries) - len(remaining)
            if remaining:
                return _fail(
                    f"directory_empty {path!r}: directory still contains {len(remaining)} user item(s)"
                )
            suffix = f" (ignored {ignored} OS metadata item(s))" if ignored else ""
            return _pass(f"directory_empty {path!r}: no user entries remain{suffix}")

        if kind == "no_files_by_extension":
            extension = assertion.get("extension")
            if not isinstance(extension, str) or not extension.startswith(".") or len(extension) < 2:
                return _unknown(f"no_files_by_extension {path!r}: missing or invalid extension")
            entries = await vm.call("list_dir", path=path, recursive=bool(assertion.get("recursive")))
            if not isinstance(entries, list):
                return _unknown(f"no_files_by_extension {path!r}: list_dir returned an unexpected result")
            left = [PurePosixPath(str(i.get("path", "")).replace("\\", "/")).name for i in entries
                    if isinstance(i, dict) and i.get("type") == "file"
                    and str(i.get("path", "")).casefold().endswith(extension.casefold())]
            parts = []
            if left:
                parts.append(f"still present: {sorted(left)}")
            # Collateral-damage guard: everything outside the deleted set must be intact.
            now = {str(i.get("path")): i.get("sha256") for i in entries if isinstance(i, dict) and i.get("type") == "file"}
            lost, altered = [], []
            for kept in assertion.get("preserved_files") or []:
                if kept.get("path") not in now:
                    lost.append(PurePosixPath(str(kept.get("path"))).name)
                elif kept.get("sha256") and now[kept["path"]] != kept["sha256"]:
                    altered.append(PurePosixPath(str(kept.get("path"))).name)
            if lost:
                parts.append(f"COLLATERAL DAMAGE, files that must be kept were deleted: {sorted(lost)}")
            if altered:
                parts.append(f"COLLATERAL DAMAGE, files that must be kept were modified: {sorted(altered)}")
            if parts:
                return _fail(f"no_files_by_extension {path!r} {extension}: " + "; ".join(parts))
            guarded = "; kept files verified intact" if assertion.get("preserved_files") else ""
            return _pass(f"no_files_by_extension {path!r} {extension}: none remain{guarded}")

        if kind == "files_moved_by_extension":
            source_dir = assertion.get("source_dir")
            destination_dir = assertion.get("destination_dir")
            extension = assertion.get("extension")
            expected_files = assertion.get("expected_files")
            snapshot_error = assertion.get("snapshot_error")
            if snapshot_error:
                return _unknown(f"files_moved_by_extension: could not snapshot source files ({snapshot_error})")
            if (not isinstance(source_dir, str) or not source_dir
                    or not isinstance(destination_dir, str) or not destination_dir
                    or not isinstance(extension, str) or not extension.startswith(".")
                    or not isinstance(expected_files, list)):
                return _unknown("files_moved_by_extension: missing source snapshot or valid paths/extension")
            if not expected_files:
                return _unknown(
                    f"files_moved_by_extension {extension}: no matching source files were present at planning time"
                )
            source_entries = await vm.call("list_dir", path=source_dir)
            destination_entries = await vm.call("list_dir", path=destination_dir)
            if not isinstance(source_entries, list) or not isinstance(destination_entries, list):
                return _unknown("files_moved_by_extension: list_dir returned an unexpected result")
            def file_map(entries):
                return {
                    PurePosixPath(str(item.get("path", "")).replace("\\", "/")).name: item
                    for item in entries
                    if isinstance(item, dict) and item.get("type") == "file"
                }
            source_files = file_map(source_entries)
            destination_files = file_map(destination_entries)
            expected_names = [item.get("name") for item in expected_files if isinstance(item, dict)]
            if len(expected_names) != len(expected_files) or any(not isinstance(n, str) or not n for n in expected_names):
                return _unknown("files_moved_by_extension: invalid planning-time file snapshot")
            missing = [name for name in expected_names if name not in destination_files]
            remaining = [name for name in expected_names if name in source_files]
            size_mismatches = []
            hash_mismatches = []
            hash_unavailable = []
            for item in expected_files:
                name = item["name"]
                original_size = item.get("size")
                actual_entry = destination_files.get(name)
                actual_size = actual_entry.get("size") if actual_entry is not None else None
                # A missing file is already reported by `missing`; it is not a
                # size mismatch. Only compare metadata after finding the file.
                if actual_entry is not None and original_size is not None and actual_size != original_size:
                    size_mismatches.append(name)
                original_hash = item.get("sha256")
                actual_hash = actual_entry.get("sha256") if actual_entry is not None else None
                if actual_entry is not None and original_hash and not actual_hash:
                    hash_unavailable.append(name)
                elif actual_entry is not None and original_hash and actual_hash != original_hash:
                    hash_mismatches.append(name)
            extra_source = [name for name in source_files if name.casefold().endswith(extension.casefold())]
            if missing or remaining or size_mismatches or hash_mismatches or extra_source:
                parts = []
                if missing:
                    parts.append(f"missing at destination: {missing}")
                if remaining:
                    parts.append(f"still in source: {remaining}")
                if extra_source and not remaining:
                    parts.append(f"unexpected matching source files remain: {extra_source}")
                if size_mismatches:
                    parts.append(f"file sizes changed: {size_mismatches}")
                if hash_mismatches:
                    parts.append(f"file contents changed (SHA-256 mismatch): {hash_mismatches}")
                return _fail(f"files_moved_by_extension {extension}: " + "; ".join(parts))
            if hash_unavailable:
                return _unknown(
                    f"files_moved_by_extension {extension}: destination SHA-256 unavailable for {hash_unavailable}; content preservation cannot be verified"
                )
            return _pass(
                f"files_moved_by_extension {extension}: verified {len(expected_names)} file(s) moved; source files absent and file metadata/content hashes preserved where available"
            )

        if kind == "path_exists":
            info = await vm.call("path_info", path=path)
            if not isinstance(info, dict) or "exists" not in info:
                return _unknown(f"path_exists {path!r}: path_info returned an unexpected result")
            if not info["exists"]:
                return _fail(f"path_exists {path!r}: path does not exist")
            expected_is_dir = assertion.get("is_dir")
            if expected_is_dir is not None and info.get("is_dir") is not expected_is_dir:
                expected = "directory" if expected_is_dir else "file"
                actual = "directory" if info.get("is_dir") else "file"
                return _fail(f"path_exists {path!r}: expected {expected}, found {actual}")
            return _pass(f"path_exists {path!r}: exists")

        if kind == "path_absent":
            info = await vm.call("path_info", path=path)
            if not isinstance(info, dict) or "exists" not in info:
                return _unknown(f"path_absent {path!r}: path_info returned an unexpected result")
            if info["exists"]:
                return _fail(f"path_absent {path!r}: path still exists")
            return _pass(f"path_absent {path!r}: absent")

        if kind in {"text_contains", "text_equals"}:
            expected = assertion.get("text")
            if not isinstance(expected, str) or (kind == "text_contains" and not expected):
                return _unknown(f"{kind} {path!r}: missing or invalid expected text")
            info = await vm.call("path_info", path=path)
            if not isinstance(info, dict) or "exists" not in info:
                return _unknown(f"{kind} {path!r}: path_info returned an unexpected result")
            if not info["exists"]:
                return _fail(f"{kind} {path!r}: file does not exist")
            if info.get("is_dir"):
                return _fail(f"{kind} {path!r}: expected a file, found a directory")
            # Read one character beyond the supported verification limit so a
            # truncated read is never mistaken for an exact whole-file match.
            content = await vm.call("read_text", path=path, max_chars=200_001)
            if not isinstance(content, str):
                return _unknown(f"{kind} {path!r}: read_text returned a non-text result")
            if len(content) > 200_000:
                return _unknown(f"{kind} {path!r}: file exceeds the 200000-character verification limit")
            if kind == "text_equals":
                if content == expected:
                    return _pass(f"text_equals {path!r}: exact file content matches")
                return _fail(f"text_equals {path!r}: file content does not exactly match expected text")
            if expected in content:
                return _pass(f"text_contains {path!r}: expected text found")
            return _fail(f"text_contains {path!r}: expected text was not found")

        if kind in {"xlsx_sheet_exists", "xlsx_values_present", "xlsx_rows_present"}:
            info = await vm.call("path_info", path=path)
            if not isinstance(info, dict) or "exists" not in info:
                return _unknown(f"{kind} {path!r}: path_info returned an unexpected result")
            if not info["exists"]:
                return _fail(f"{kind} {path!r}: workbook does not exist")
            if info.get("is_dir"):
                return _fail(f"{kind} {path!r}: expected a workbook file, found a directory")
            if kind == "xlsx_sheet_exists":
                sheet = assertion.get("sheet")
                if not isinstance(sheet, str) or not sheet:
                    return _unknown(f"xlsx_sheet_exists {path!r}: missing sheet name")
                book = await vm.call("xlsx_read", path=path, max_rows=1)
                sheets = book.get("sheets") if isinstance(book, dict) else None
                if not isinstance(sheets, list):
                    return _unknown(f"xlsx_sheet_exists {path!r}: xlsx_read returned an unexpected result")
                if sheet in sheets:
                    return _pass(f"xlsx_sheet_exists {path!r}: sheet {sheet!r} exists")
                return _fail(f"xlsx_sheet_exists {path!r}: sheet {sheet!r} is missing")

            sheet = assertion.get("sheet")
            kwargs = {"path": path, "max_rows": 5000}
            if isinstance(sheet, str) and sheet:
                kwargs["sheet"] = sheet
            book = await vm.call("xlsx_read", **kwargs)
            if not isinstance(book, dict) or not isinstance(book.get("rows"), list):
                return _unknown(f"{kind} {path!r}: xlsx_read returned an unexpected result")
            rows = book["rows"]
            # Older VM servers do not expose truncation metadata; be conservative
            # when exactly the requested number of rows was returned.
            truncated = bool(book.get("truncated", len(rows) >= 5000))

            if kind == "xlsx_values_present":
                values = assertion.get("values")
                if not isinstance(values, list) or not values:
                    return _unknown(f"xlsx_values_present {path!r}: values must be a non-empty list")
                if any(value is None for value in values):
                    return _unknown(f"xlsx_values_present {path!r}: None cannot prove a meaningful cell value")
                cells = {cell for row in rows if isinstance(row, list) for cell in row if cell is not None}
                missing = [value for value in values if value not in cells]
                if not missing:
                    return _pass(f"xlsx_values_present {path!r}: all expected values found")
                if truncated:
                    return _unknown(f"xlsx_values_present {path!r}: values {missing!r} not found in inspected rows; sheet may be truncated")
                return _fail(f"xlsx_values_present {path!r}: missing values {missing!r}")

            expected_rows = assertion.get("rows")
            if not isinstance(expected_rows, list) or not expected_rows or any(not isinstance(row, list) for row in expected_rows):
                return _unknown(f"xlsx_rows_present {path!r}: rows must be a non-empty list of rows")
            missing_rows = [expected for expected in expected_rows
                            if not any(isinstance(actual, list) and _row_matches(actual, expected) for actual in rows)]
            if not missing_rows:
                return _pass(f"xlsx_rows_present {path!r}: all expected rows found")
            if truncated:
                return _unknown(f"xlsx_rows_present {path!r}: expected rows {missing_rows!r} not found in inspected rows; sheet may be truncated")
            return _fail(f"xlsx_rows_present {path!r}: missing rows {missing_rows!r}")

    except Exception as exc:
        # A tool/server error is not proof that the requested postcondition failed.
        message = str(exc).replace("\n", " ")[:180]
        return _unknown(f"{kind} {path!r}: checker could not inspect the resource ({type(exc).__name__}: {message})")

    return _unknown(f"no verifier implemented for {kind!r}")


async def check_spec(
    vm,
    raw_spec: CheckSpec | dict[str, Any] | None,
    capability: str | None = None,
) -> CheckResult:
    """Run the selected worker's deterministic checks; never trust worker self-report."""
    if isinstance(raw_spec, CheckSpec):
        spec = raw_spec.model_dump()
    elif isinstance(raw_spec, dict):
        spec = raw_spec
    else:
        spec = {}

    assertions = spec.get("checks")
    if not isinstance(assertions, list) or not assertions:
        reason = spec.get("rationale") or "planner supplied no deterministic assertions"
        return {"status": "unknown", "detail": f"No verifiable check_spec: {reason}", "evidence": ""}

    allowed_checks = CHECKS_BY_CAPABILITY.get(capability, set()) if capability else SUPPORTED_CHECKS
    outcomes = []
    for assertion in assertions:
        if not isinstance(assertion, dict):
            outcomes.append(("unknown", "invalid assertion: expected an object"))
        else:
            outcomes.append(await _check_assertion(vm, assertion, allowed_checks))

    failures = [detail for status, detail in outcomes if status == "fail"]
    unknowns = [detail for status, detail in outcomes if status == "unknown"]
    if failures:
        evidence = " | ".join(failures)
        detail = evidence
        if unknowns:
            detail += " | Also unverified: " + " | ".join(unknowns)
        return {"status": "fail", "detail": detail[:1500], "evidence": evidence[:1200]}
    if unknowns:
        return {"status": "unknown", "detail": " | ".join(unknowns)[:1500], "evidence": ""}
    passed_details = [detail for status, detail in outcomes if status == "pass"]
    detail = "All deterministic assertions passed"
    if passed_details:
        detail += ": " + " | ".join(passed_details)
    return {"status": "pass", "detail": detail[:1500], "evidence": ""}
