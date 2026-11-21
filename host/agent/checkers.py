"""Deterministic verifiers for planner-generated subgoal check specifications.

A check is deliberately conservative: it returns PASS only when the requested
postcondition was observed, FAIL when an inspected postcondition is false, and
UNKNOWN when the claim cannot be checked with the available tools/evidence.
"""
import csv
import io
import re
from pathlib import PurePosixPath
from typing import Any, Literal, TypedDict

from pydantic import BaseModel, ConfigDict, Field

from .sheet_tools import coerce_number, digest_rows


CellValue = str | int | float | bool | None


class CheckAssertion(BaseModel):
    """One deterministic assertion in a subgoal's acceptance specification."""

    model_config = ConfigDict(extra="forbid")

    kind: str = Field(description="Supported: path_exists, path_absent, file_content_matches, directory_empty, directory_contains_files, files_moved_by_extension, no_files_by_extension, text_contains, text_equals, xlsx_sheet_exists, xlsx_values_present, xlsx_rows_present, xlsx_cell_equals, xlsx_formula_present, xlsx_data_row_count, xlsx_aggregate_matches, xlsx_group_aggregate_matches, xlsx_sheets_preserved, xlsx_csv_matches")
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
    # spreadsheet assertions
    cell: str | None = None                  # A1-style address
    value: CellValue = None                  # expected cell value
    tolerance: float | None = None           # numeric tolerance for cell comparisons
    contains: str | None = None              # substring a formula must contain
    count: int | None = None                 # expected number of data rows (rows below the header)
    column: str | None = None                # header name or column letter in the source table
    group_by: str | None = None              # header of the grouping column (xlsx_group_aggregate_matches)
    op: str | None = None                    # sum | avg | min | max | count
    source_path: str | None = None           # source workbook or CSV path, depending on assertion kind
    source_sheet: str | None = None
    preserved_sheets: dict[str, str] | None = None   # orchestrator-filled sheet digests


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
    "files": COMMON_CHECKS | {"file_content_matches", "directory_empty", "directory_contains_files", "files_moved_by_extension", "no_files_by_extension", "text_contains", "text_equals"},
    "sheet": COMMON_CHECKS | {
        "xlsx_sheet_exists", "xlsx_values_present", "xlsx_rows_present",
        "xlsx_cell_equals", "xlsx_formula_present", "xlsx_data_row_count",
        "xlsx_aggregate_matches", "xlsx_group_aggregate_matches", "xlsx_sheets_preserved", "xlsx_csv_matches",
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


# ---------------------------------------------------------------- spreadsheet checks
XLSX_EXTRA_CHECKS = {"xlsx_cell_equals", "xlsx_formula_present", "xlsx_data_row_count",
                     "xlsx_aggregate_matches", "xlsx_group_aggregate_matches", "xlsx_sheets_preserved", "xlsx_csv_matches"}
_CELL_ADDR = re.compile(r"^[A-Za-z]{1,3}[1-9]\d*$")


def _isnum(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _txt_key(v: Any) -> str:
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip().casefold()


def _find_column(header: list[Any], ref: str) -> int | None:
    idx = next((i for i, h in enumerate(header) if h is not None and str(h).strip().casefold() == ref.strip().casefold()), None)
    if idx is None and re.fullmatch(r"[A-Za-z]{1,3}", ref.strip()):
        from openpyxl.utils import column_index_from_string
        cand = column_index_from_string(ref.strip().upper()) - 1
        idx = cand if cand < len(header) else None
    return idx


def _norm_formula(text: str) -> str:
    return re.sub(r"[\s$]", "", text).upper()


async def _workbook_state(vm, kind: str, path: str, sheet: str | None) -> tuple[str, str] | None:
    """Return a terminal (status, message) when the workbook/sheet is unusable, else None."""
    info = await vm.call("path_info", path=path)
    if not isinstance(info, dict) or "exists" not in info:
        return _unknown(f"{kind} {path!r}: path_info returned an unexpected result")
    if not info["exists"]:
        return _fail(f"{kind} {path!r}: workbook does not exist")
    if info.get("is_dir"):
        return _fail(f"{kind} {path!r}: expected a workbook file, found a directory")
    if sheet:
        head = await vm.call("xlsx_read", path=path, max_rows=1)
        names = head.get("sheets") if isinstance(head, dict) else None
        if not isinstance(names, list):
            return _unknown(f"{kind} {path!r}: xlsx_read returned an unexpected result")
        if sheet not in names:
            return _fail(f"{kind} {path!r}: sheet {sheet!r} is missing (sheets: {names})")
    return None


async def _read_one_cell(vm, path: str, sheet: str | None, cell: str, formulas: bool = False):
    kwargs: dict[str, Any] = {"path": path, "cell_range": cell, "max_rows": 1, "formulas": formulas}
    if sheet:
        kwargs["sheet"] = sheet
    book = await vm.call("xlsx_read", **kwargs)
    if not isinstance(book, dict) or not isinstance(book.get("rows"), list) or not book["rows"]:
        return None, False
    return book["rows"][0][0], bool(book.get("uncomputed_formula_cells"))


def _values_equal(actual: Any, expected: Any, tol: float) -> tuple[bool, str]:
    if _isnum(expected):
        if not _isnum(actual):
            hint = " (stored as text, not a number)" if isinstance(actual, str) and actual.strip() == str(expected) else ""
            return False, f"is {actual!r}{hint}, expected number {expected!r}"
        return abs(actual - expected) <= tol, f"is {actual!r}, expected {expected!r} (tolerance {tol})"
    return actual == expected and type(actual) is type(expected), f"is {actual!r}, expected {expected!r}"


async def _check_xlsx_extra(vm, kind: str, a: dict[str, Any]) -> tuple[str, str]:
    path, sheet = a.get("path"), a.get("sheet")
    sheet = sheet if isinstance(sheet, str) and sheet else None
    blocked = await _workbook_state(vm, kind, path, sheet)
    if blocked:
        return blocked

    if kind == "xlsx_sheets_preserved":
        saved = a.get("preserved_sheets")
        if not isinstance(saved, dict):
            return _unknown(f"xlsx_sheets_preserved {path!r}: no plan-time snapshot available")
        head = await vm.call("xlsx_read", path=path, max_rows=1)
        names = head.get("sheets", []) if isinstance(head, dict) else []
        lost, altered = [], []
        for name, digest in saved.items():
            if name not in names:
                lost.append(name)
                continue
            book = await vm.call("xlsx_read", path=path, sheet=name, formulas=True, max_rows=5000)
            if digest_rows(book.get("rows", [])) != digest:
                altered.append(name)
        if lost or altered:
            parts = []
            if lost:
                parts.append(f"COLLATERAL DAMAGE, sheets that must be kept were deleted: {sorted(lost)}")
            if altered:
                parts.append(f"COLLATERAL DAMAGE, sheets that must stay unchanged were modified: {sorted(altered)}")
            return _fail(f"xlsx_sheets_preserved {path!r}: " + "; ".join(parts))
        return _pass(f"xlsx_sheets_preserved {path!r}: {len(saved)} other sheet(s) verified unchanged")

    if kind == "xlsx_csv_matches":
        source = a.get("source_path")
        if not isinstance(source, str) or not source.lower().endswith(".csv"):
            return _unknown(f"xlsx_csv_matches {path!r}: source_path must name a CSV file")
        try:
            raw_text = await vm.call("read_text", path=source, max_chars=2_000_001)
        except Exception as exc:
            return _unknown(f"xlsx_csv_matches {source!r}: could not read source CSV ({type(exc).__name__})")
        if not isinstance(raw_text, str):
            return _unknown(f"xlsx_csv_matches {source!r}: read_text returned an unexpected result")
        if len(raw_text) > 2_000_000:
            return _unknown(f"xlsx_csv_matches {source!r}: source exceeds the 2 MB verification limit")
        try:
            sample = raw_text.lstrip("\ufeff")
            try:
                dialect = csv.Sniffer().sniff(sample[:8192], delimiters=",;\t|")
            except csv.Error:
                dialect = csv.excel
            parsed = list(csv.reader(io.StringIO(sample), dialect))
        except Exception as exc:
            return _unknown(f"xlsx_csv_matches {source!r}: CSV parsing failed ({type(exc).__name__})")
        if not parsed:
            return _unknown(f"xlsx_csv_matches {source!r}: CSV is empty")
        expected: list[list[Any]] = []
        for row_index, source_row in enumerate(parsed):
            converted_row: list[Any] = []
            for raw_value in source_row:
                if raw_value == "":
                    converted_row.append(None)
                elif row_index == 0:
                    converted_row.append(raw_value)
                else:
                    value, _ = coerce_number(raw_value)
                    converted_row.append(value)
            expected.append(converted_row)
        try:
            actual_book = await vm.call("xlsx_read", path=path, sheet=sheet, max_rows=5000, formulas=True)
        except Exception as exc:
            return _unknown(f"xlsx_csv_matches {path!r}: could not read imported sheet ({type(exc).__name__})")
        actual = actual_book.get("rows") if isinstance(actual_book, dict) else None
        if not isinstance(actual, list):
            return _unknown(f"xlsx_csv_matches {path!r}: xlsx_read returned an unexpected result")
        if actual_book.get("truncated"):
            return _unknown(f"xlsx_csv_matches {path!r}: imported sheet exceeds the 5000-row verification limit")
        if actual_book.get("sheets") != [sheet]:
            return _fail(f"xlsx_csv_matches {path!r}: expected exactly one sheet {sheet!r}, found {actual_book.get('sheets')!r}")
        actual_trimmed = []
        for row in actual:
            values = list(row)
            while values and values[-1] is None:
                values.pop()
            actual_trimmed.append(values)
        expected_trimmed = []
        for row in expected:
            values = list(row)
            while values and values[-1] is None:
                values.pop()
            expected_trimmed.append(values)
        while actual_trimmed and not actual_trimmed[-1]:
            actual_trimmed.pop()
        while expected_trimmed and not expected_trimmed[-1]:
            expected_trimmed.pop()
        if len(actual_trimmed) != len(expected_trimmed):
            return _fail(f"xlsx_csv_matches {path!r}: {len(actual_trimmed)} row(s), expected {len(expected_trimmed)} from {source!r}")
        for row_number, (actual_row, expected_row) in enumerate(zip(actual_trimmed, expected_trimmed), start=1):
            if len(actual_row) != len(expected_row):
                return _fail(f"xlsx_csv_matches {path!r}: row {row_number} has {len(actual_row)} cell(s), expected {len(expected_row)}")
            for column_number, (actual_value, expected_value) in enumerate(zip(actual_row, expected_row), start=1):
                if _isnum(expected_value):
                    if not _isnum(actual_value) or float(actual_value) != float(expected_value):
                        return _fail(f"xlsx_csv_matches {path!r}: row {row_number}, column {column_number} is {actual_value!r}; expected numeric {expected_value!r}")
                elif actual_value != expected_value or type(actual_value) is not type(expected_value):
                    return _fail(f"xlsx_csv_matches {path!r}: row {row_number}, column {column_number} is {actual_value!r}; expected {expected_value!r}")
        return _pass(f"xlsx_csv_matches {path!r}: all {len(expected_trimmed)} row(s) and cell types match {source!r}")

    if kind in {"xlsx_cell_equals", "xlsx_formula_present", "xlsx_aggregate_matches"}:
        cell = a.get("cell")
        if not isinstance(cell, str) or not _CELL_ADDR.match(cell):
            return _unknown(f"{kind} {path!r}: cell must be an A1-style address, got {cell!r}")

    if kind == "xlsx_formula_present":
        raw, _ = await _read_one_cell(vm, path, sheet, cell, formulas=True)
        if not (isinstance(raw, str) and raw.startswith("=")):
            return _fail(f"xlsx_formula_present {path!r} {cell}: holds {raw!r}, not a formula")
        needle = a.get("contains")
        if isinstance(needle, str) and needle and _norm_formula(needle) not in _norm_formula(raw):
            return _fail(f"xlsx_formula_present {path!r} {cell}: formula {raw!r} does not contain {needle!r}")
        return _pass(f"xlsx_formula_present {path!r} {cell}: formula {raw!r}")

    if kind == "xlsx_cell_equals":
        expected = a.get("value")
        if expected is None:
            return _unknown(f"xlsx_cell_equals {path!r} {cell}: expected value is missing")
        actual, uncomputed = await _read_one_cell(vm, path, sheet, cell)
        if uncomputed:
            return _unknown(f"xlsx_cell_equals {path!r} {cell}: formula could not be computed by the verifier")
        ok, msg = _values_equal(actual, expected, float(a.get("tolerance") if a.get("tolerance") is not None else 1e-6))
        return (_pass if ok else _fail)(f"xlsx_cell_equals {path!r} {cell}: {msg}")

    if kind == "xlsx_data_row_count":
        expected = a.get("count")
        if isinstance(expected, str) and expected.strip().isdigit():
            expected = int(expected.strip())
        elif isinstance(expected, float) and expected.is_integer():
            expected = int(expected)
        if not isinstance(expected, int) or isinstance(expected, bool) or expected < 0:
            return _unknown(f"xlsx_data_row_count {path!r}: count must be a non-negative integer")
        kwargs: dict[str, Any] = {"path": path, "max_rows": 5000}
        if sheet:
            kwargs["sheet"] = sheet
        book = await vm.call("xlsx_read", **kwargs)
        if not isinstance(book, dict) or not isinstance(book.get("rows"), list):
            return _unknown(f"xlsx_data_row_count {path!r}: xlsx_read returned an unexpected result")
        if book.get("truncated"):
            return _unknown(f"xlsx_data_row_count {path!r}: sheet larger than the 5000 inspected rows")
        actual = sum(1 for r in book["rows"][1:] if any(v not in (None, "") for v in r))
        if actual == expected:
            return _pass(f"xlsx_data_row_count {path!r}: {actual} data row(s) below the header")
        return _fail(f"xlsx_data_row_count {path!r}: {actual} data row(s) below the header, expected {expected}")

    if kind == "xlsx_group_aggregate_matches":
        src, op = a.get("source_path"), (a.get("op") or "").lower()
        column, group_by = a.get("column"), a.get("group_by")
        if not (isinstance(src, str) and src and op in {"sum", "avg", "min", "max", "count"}
                and isinstance(column, str) and column and isinstance(group_by, str) and group_by):
            return _unknown("xlsx_group_aggregate_matches: needs source_path, column, group_by and op")
        ssheet = a.get("source_sheet") if isinstance(a.get("source_sheet"), str) and a.get("source_sheet") else None
        blocked = await _workbook_state(vm, kind, src, ssheet)
        if blocked:
            return blocked
        kw: dict[str, Any] = {"path": src, "max_rows": 5000}
        if ssheet:
            kw["sheet"] = ssheet
        sb = await vm.call("xlsx_read", **kw)
        srows = sb.get("rows") if isinstance(sb, dict) else None
        if not isinstance(srows, list) or not srows:
            return _unknown(f"xlsx_group_aggregate_matches {src!r}: xlsx_read returned an unexpected result")
        if sb.get("truncated"):
            return _unknown(f"xlsx_group_aggregate_matches {src!r}: source larger than the 5000 inspected rows")
        ci, gi = _find_column(srows[0], column), _find_column(srows[0], group_by)
        if ci is None or gi is None:
            return _unknown(f"xlsx_group_aggregate_matches {src!r}: column {column!r} or group {group_by!r} not in header {srows[0]}")
        grouped: dict[str, list[Any]] = {}
        for source_row in srows[1:]:
            if gi < len(source_row) and source_row[gi] not in (None, ""):
                grouped.setdefault(str(source_row[gi]).strip(), []).append(
                    source_row[ci] if ci < len(source_row) else None
                )
        if not grouped:
            return _unknown(f"xlsx_group_aggregate_matches {src!r}: no groups found")

        expected_groups: dict[str, Any] = {}
        for key, values in grouped.items():
            numbers = [float(value) for value in values if _isnum(value)]
            if op == "count":
                expected_groups[key] = sum(1 for value in values if value not in (None, ""))
            elif numbers:
                result = {"sum": sum(numbers), "avg": sum(numbers) / len(numbers),
                          "min": min(numbers), "max": max(numbers)}[op]
                expected_groups[key] = int(result) if float(result).is_integer() else result
            else:
                expected_groups[key] = None
        expected_rows = [[srows[0][gi], srows[0][ci]]] + [
            [key, expected_groups[key]] for key in sorted(expected_groups, key=lambda value: value.casefold())
        ]

        target_kwargs: dict[str, Any] = {"path": path, "max_rows": 5000, "formulas": False}
        if sheet:
            target_kwargs["sheet"] = sheet
        target_book = await vm.call("xlsx_read", **target_kwargs)
        target_rows = target_book.get("rows") if isinstance(target_book, dict) else None
        if not isinstance(target_rows, list):
            return _unknown(f"xlsx_group_aggregate_matches {path!r}: xlsx_read returned an unexpected result")
        if target_book.get("truncated"):
            return _unknown(f"xlsx_group_aggregate_matches {path!r}: target larger than the 5000 inspected rows")
        if target_book.get("uncomputed_formula_cells"):
            return _unknown(
                f"xlsx_group_aggregate_matches {path!r}: unsupported target formula(s) at "
                f"{target_book['uncomputed_formula_cells']}"
            )
        actual_rows = []
        for row in target_rows:
            values = list(row)
            while values and values[-1] is None:
                values.pop()
            actual_rows.append(values)
        while actual_rows and not actual_rows[-1]:
            actual_rows.pop()
        tolerance = float(a.get("tolerance") if a.get("tolerance") is not None else 0.005)
        if not actual_rows or actual_rows[0] != expected_rows[0]:
            actual_header = actual_rows[0] if actual_rows else None
            return _fail(f"xlsx_group_aggregate_matches {path!r}: header is {actual_header!r}, expected {expected_rows[0]!r}")
        actual_order: list[str] = []
        actual_values: dict[str, Any] = {}
        duplicates: list[str] = []
        for row_number, row in enumerate(actual_rows[1:], start=2):
            if len(row) != 2:
                return _fail(f"xlsx_group_aggregate_matches {path!r}: row {row_number} has {len(row)} columns, expected 2")
            key = str(row[0]).strip()
            if key in actual_values:
                duplicates.append(key)
            actual_order.append(key)
            actual_values[key] = row[1]
        expected_order = sorted(expected_groups, key=lambda value: value.casefold())
        missing = [key for key in expected_order if key not in actual_values]
        extra = [key for key in actual_order if key not in expected_groups]
        if missing or extra or duplicates:
            parts = []
            if missing:
                parts.append(f"missing groups {missing}")
            if extra:
                parts.append(f"unexpected groups {extra}")
            if duplicates:
                parts.append(f"duplicate groups {duplicates}")
            return _fail(f"xlsx_group_aggregate_matches {path!r}: " + "; ".join(parts))
        if actual_order != expected_order:
            return _fail(f"xlsx_group_aggregate_matches {path!r}: groups are not in A-Z order; found {actual_order}, expected {expected_order}")
        wrong = []
        for key in expected_order:
            expected_value = expected_groups[key]
            actual_value = actual_values[key]
            if expected_value is None:
                if actual_value is not None:
                    wrong.append(f"{key}: sheet has [{actual_value}], expected blank")
            elif not _isnum(actual_value) or abs(float(actual_value) - float(expected_value)) > tolerance:
                wrong.append(f"{key}: sheet has [{actual_value}], recomputed {op}({column}) is {expected_value}")
        if wrong:
            return _fail(f"xlsx_group_aggregate_matches {path!r}: " + "; ".join(wrong))
        return _pass(
            f"xlsx_group_aggregate_matches {path!r}: all {len(expected_groups)} group(s) match "
            f"{op}({column}) recomputed from {src!r} and are ordered A-Z"
        )

    # xlsx_aggregate_matches: the expected number is recomputed from the SOURCE data in code.
    src, op, column = a.get("source_path"), (a.get("op") or "").lower(), a.get("column")
    if not isinstance(src, str) or not src or op not in {"sum", "avg", "min", "max", "count"} or not isinstance(column, str) or not column:
        return _unknown("xlsx_aggregate_matches: needs source_path, column and op (sum/avg/min/max/count)")
    source_sheet = a.get("source_sheet") if isinstance(a.get("source_sheet"), str) and a.get("source_sheet") else None
    blocked = await _workbook_state(vm, kind, src, source_sheet)
    if blocked:
        return blocked
    kwargs = {"path": src, "max_rows": 5000}
    if source_sheet:
        kwargs["sheet"] = source_sheet
    book = await vm.call("xlsx_read", **kwargs)
    rows = book.get("rows") if isinstance(book, dict) else None
    if not isinstance(rows, list) or not rows:
        return _unknown(f"xlsx_aggregate_matches {src!r}: xlsx_read returned an unexpected result")
    if book.get("truncated"):
        return _unknown(f"xlsx_aggregate_matches {src!r}: source larger than the 5000 inspected rows")
    header = rows[0]
    idx = next((i for i, h in enumerate(header) if h is not None and str(h).strip().casefold() == column.strip().casefold()), None)
    if idx is None and re.fullmatch(r"[A-Za-z]{1,3}", column.strip()):
        from openpyxl.utils import column_index_from_string
        cand = column_index_from_string(column.strip().upper()) - 1
        idx = cand if cand < len(header) else None
    if idx is None:
        return _unknown(f"xlsx_aggregate_matches {src!r}: column {column!r} not found in header {header}")
    col = [r[idx] for r in rows[1:] if idx < len(r)]
    nums = [float(v) for v in col if _isnum(v)]
    if op == "count":
        expected: float | None = float(sum(1 for v in col if v not in (None, "")))
    elif not nums:
        return _unknown(f"xlsx_aggregate_matches {src!r}: column {column!r} has no numeric values")
    else:
        expected = {"sum": sum(nums), "avg": sum(nums) / len(nums), "min": min(nums), "max": max(nums)}[op]
    actual, uncomputed = await _read_one_cell(vm, path, sheet, cell)
    if uncomputed:
        return _unknown(f"xlsx_aggregate_matches {path!r} {cell}: formula could not be computed by the verifier")
    tol = float(a.get("tolerance") if a.get("tolerance") is not None else 0.005)
    if not _isnum(actual):
        return _fail(f"xlsx_aggregate_matches {path!r} {cell}: holds {actual!r}, expected {op}({column}) = {expected:g}")
    if abs(actual - expected) <= tol:
        return _pass(f"xlsx_aggregate_matches {path!r} {cell}: {actual!r} equals {op}({column}) = {expected:g} recomputed from {src!r}")
    return _fail(f"xlsx_aggregate_matches {path!r} {cell}: holds {actual!r} but {op}({column}) recomputed from {src!r} is {expected:g}")


async def _check_assertion(vm, assertion: dict[str, Any], allowed_checks: set[str]) -> tuple[str, str]:
    kind = assertion.get("kind")
    path = assertion.get("path")

    if not isinstance(kind, str) or kind not in SUPPORTED_CHECKS:
        return _unknown(f"unsupported check kind: {kind!r}")
    if kind not in allowed_checks:
        return _unknown(f"check kind {kind!r} is not available to this worker")
    if kind in {"path_exists", "path_absent", "file_content_matches", "directory_empty", "directory_contains_files", "no_files_by_extension", "text_contains", "text_equals",
                "xlsx_sheet_exists", "xlsx_values_present", "xlsx_rows_present", *XLSX_EXTRA_CHECKS}:
        if not isinstance(path, str) or not path.strip():
            return _unknown(f"{kind}: missing or invalid path")
        if any(ch in path for ch in "*?[]"):
            return _unknown(
                f"{kind} {path!r}: wildcard paths are not valid deterministic assertions; "
                "enumerate exact files with list_dir"
            )

    try:
        if kind in XLSX_EXTRA_CHECKS:
            return await _check_xlsx_extra(vm, kind, assertion)

        if kind == "file_content_matches":
            source = assertion.get("source_path")
            if not isinstance(source, str) or not source.strip():
                return _unknown("file_content_matches: missing source_path")
            try:
                destination_info = await vm.call("path_info", path=path)
                source_info = await vm.call("path_info", path=source)
                if not isinstance(destination_info, dict) or not isinstance(source_info, dict):
                    return _unknown("file_content_matches: path_info returned an unexpected result")
                if not destination_info.get("exists") or not source_info.get("exists"):
                    missing = [candidate for candidate, info in ((path, destination_info), (source, source_info))
                               if not info.get("exists")]
                    return _fail(f"file_content_matches: file(s) missing: {missing}")
                if destination_info.get("is_dir") or source_info.get("is_dir"):
                    return _fail("file_content_matches: both paths must be files, not directories")
                def parent_and_name(candidate: str) -> tuple[str, str]:
                    normalized = candidate.replace("\\", "/")
                    p = PurePosixPath(normalized)
                    return (p.parent.as_posix() if p.parent.as_posix() != "." else ".", p.name)
                async def digest(candidate: str) -> str | None:
                    parent, name = parent_and_name(candidate)
                    entries = await vm.call("list_dir", path=parent)
                    if not isinstance(entries, list):
                        return None
                    matches = [entry for entry in entries if isinstance(entry, dict)
                               and entry.get("type") == "file"
                               and PurePosixPath(str(entry.get("path", "")).replace("\\", "/")).name == name]
                    return matches[0].get("sha256") if len(matches) == 1 else None
                destination_hash, source_hash = await digest(path), await digest(source)
            except Exception as exc:
                return _unknown(f"file_content_matches: could not inspect file hashes ({type(exc).__name__})")
            if not destination_hash or not source_hash:
                return _unknown("file_content_matches: SHA-256 unavailable for source or destination")
            if destination_hash == source_hash:
                return _pass(f"file_content_matches: {path!r} is byte-identical to {source!r}")
            return _fail(f"file_content_matches: {path!r} differs from source {source!r} (SHA-256 mismatch)")

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
