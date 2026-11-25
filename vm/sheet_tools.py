"""Spreadsheet engine for the Sheet worker (pure functions over an already-sandboxed Path).

Shared verbatim by the local backend (host/agent/local_runtime.py) and the VM tool server
(vm/sheet_tools.py is a byte-identical copy; tests/test_sheet_tools.py enforces that).

Design points
  * Formulas are evaluated by a small built-in evaluator (+ - * / ^ & comparisons, cell/range
    refs, SUM AVERAGE MIN MAX COUNT ROUND ABS IF). openpyxl never computes formulas, so a
    freshly written "=SUM(B2:B5)" has no cached value; the evaluator fills the gap and lets
    both the worker and the deterministic checker see real numbers without Excel/LibreOffice.
    Unsupported functions are reported as `uncomputed_formula_cells`, never guessed.
  * Observations are compact: bounded rows, truncated long strings, ISO dates, explicit
    `truncated` flags.
  * Calculator tools (xlsx_aggregate / xlsx_query) exist so a small local model never has to
    add or sort numbers "in its head".
"""
from __future__ import annotations

import csv
import datetime as dt
import hashlib
import io
import json
import math
import re
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from openpyxl import Workbook, load_workbook
from openpyxl.utils import column_index_from_string, get_column_letter
from openpyxl.utils.cell import range_boundaries

MAX_CELLS_PER_RANGE = 200_000
_CELL_RE = re.compile(r"^[A-Za-z]{1,3}[1-9]\d*$")
_BAD_SHEET_CHARS = set('[]:*?/\\')


# ============================================================ formula evaluator
class FormulaError(Exception):
    pass


class _Err:
    """A deferred error value, so IF() can ignore the branch it does not take."""
    def __init__(self, msg: str):
        self.msg = msg


_TOKEN = re.compile(r"""\s*(?:
    (?P<num>\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)
   |(?P<str>"(?:[^"]|"")*")
   |(?P<func>[A-Za-z][A-Za-z0-9.]*)(?=\()
   |(?P<ref>(?:(?:'(?:[^']|'')+'|[A-Za-z_][\w.]*)!)?\$?[A-Za-z]{1,3}\$?\d+(?::\$?[A-Za-z]{1,3}\$?\d+)?)
   |(?P<op><=|>=|<>|[-+*/^&=<>(),])
)""", re.X)


def _tokenize(text: str) -> list[tuple[str, str]]:
    text, pos, out = text.strip(), 0, []
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            raise FormulaError(f"cannot parse near {text[pos:pos + 12]!r}")
        out.append((m.lastgroup, m.group(m.lastgroup)))
        pos = m.end()
    return out


def _num(v: Any) -> float:
    if isinstance(v, _Err):
        raise FormulaError(v.msg)
    if v is None:
        return 0.0
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v)
        except ValueError:
            pass
    raise FormulaError("#VALUE!")


def _txt(v: Any) -> str:
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v)


def _numeric_values(args: list[Any]) -> list[float]:
    """Excel semantics: in ranges only real numbers count; direct scalars are coerced."""
    out: list[float] = []
    for a in args:
        if isinstance(a, _Err):
            raise FormulaError(a.msg)
        if isinstance(a, list):
            out += [float(x) for x in a if isinstance(x, (int, float)) and not isinstance(x, bool)]
        elif a is not None:
            out.append(_num(a))
    return out


def _round_half_away(x: float, digits: int) -> float:
    f = 10 ** digits
    return math.copysign(math.floor(abs(x) * f + 0.5) / f, x)


def _compare(op: str, a: Any, b: Any) -> bool:
    for v in (a, b):
        if isinstance(v, _Err):
            raise FormulaError(v.msg)
    numeric = all(isinstance(v, (int, float)) or v is None for v in (a, b))
    if numeric:
        x, y = _num(a), _num(b)
    else:
        x, y = _txt(a).casefold(), _txt(b).casefold()
    return {"=": x == y, "<>": x != y, "<": x < y, ">": x > y, "<=": x <= y, ">=": x >= y}[op]


class _Parser:
    def __init__(self, tokens: list[tuple[str, str]], resolve: Callable[[str], Any]):
        self.t, self.i, self.resolve = tokens, 0, resolve

    def peek(self) -> tuple[str | None, str | None]:
        return self.t[self.i] if self.i < len(self.t) else (None, None)

    def take(self) -> tuple[str | None, str | None]:
        tok = self.peek()
        self.i += 1
        return tok

    def parse(self) -> Any:
        v = self.cmp()
        if self.i < len(self.t):
            raise FormulaError("unexpected trailing tokens")
        if isinstance(v, _Err):
            raise FormulaError(v.msg)
        return v

    def cmp(self) -> Any:
        v = self.concat()
        while self.peek()[1] in ("=", "<>", "<", ">", "<=", ">="):
            op = self.take()[1]
            v = _compare(op, v, self.concat())
        return v

    def concat(self) -> Any:
        v = self.add()
        while self.peek()[1] == "&":
            self.take()
            v = _txt(v) + _txt(self.add())
        return v

    def add(self) -> Any:
        v = self.mul()
        while self.peek()[1] in ("+", "-"):
            op = self.take()[1]
            r = self.mul()
            v = _num(v) + _num(r) if op == "+" else _num(v) - _num(r)
        return v

    def mul(self) -> Any:
        v = self.pow()
        while self.peek()[1] in ("*", "/"):
            op = self.take()[1]
            r = self.pow()
            if op == "*":
                v = _num(v) * _num(r)
            else:
                if _num(r) == 0:
                    raise FormulaError("#DIV/0!")
                v = _num(v) / _num(r)
        return v

    def pow(self) -> Any:
        v = self.unary()
        while self.peek()[1] == "^":
            self.take()
            v = _num(v) ** _num(self.unary())
        return v

    def unary(self) -> Any:
        if self.peek()[1] == "-":
            self.take()
            return -_num(self.unary())
        if self.peek()[1] == "+":
            self.take()
            return self.unary()
        return self.primary()

    def primary(self) -> Any:
        kind, text = self.take()
        if kind == "num":
            f = float(text)
            return int(f) if f.is_integer() and "." not in text and "e" not in text.lower() else f
        if kind == "str":
            return text[1:-1].replace('""', '"')
        if kind == "ref":
            return self.resolve(text)
        if kind == "func":
            return self.call(text.upper())
        if text == "(":
            v = self.cmp()
            if self.take()[1] != ")":
                raise FormulaError("missing )")
            return v
        raise FormulaError(f"unexpected token {text!r}")

    def arg(self) -> Any:
        try:
            return self.cmp()
        except FormulaError as exc:
            return _Err(str(exc))

    def call(self, name: str) -> Any:
        if self.take()[1] != "(":
            raise FormulaError("expected (")
        args: list[Any] = []
        if self.peek()[1] == ")":
            self.take()
        else:
            while True:
                args.append(self.arg())
                sep = self.take()[1]
                if sep == ")":
                    break
                if sep != ",":
                    raise FormulaError("expected , or )")
        if name == "IF":
            if len(args) not in (2, 3):
                raise FormulaError("IF needs 2 or 3 arguments")
            if isinstance(args[0], _Err):
                raise FormulaError(args[0].msg)
            cond = args[0]
            if isinstance(cond, str):
                cond = bool(cond)
            pick = args[1] if _num(cond) != 0 else (args[2] if len(args) == 3 else False)
            if isinstance(pick, _Err):
                raise FormulaError(pick.msg)
            return pick
        for a in args:
            if isinstance(a, _Err):
                raise FormulaError(a.msg)
        if name == "SUM":
            return sum(_numeric_values(args))
        if name == "COUNT":
            return len(_numeric_values(args))
        if name in ("AVERAGE", "MIN", "MAX"):
            vals = _numeric_values(args)
            if not vals:
                raise FormulaError("#DIV/0!" if name == "AVERAGE" else "#NUM!")
            return {"AVERAGE": sum(vals) / len(vals), "MIN": min(vals), "MAX": max(vals)}[name]
        if name in ("AND", "OR") and args:
            flags = [_num(x) != 0 for a in args for x in (a if isinstance(a, list) else [a]) if x is not None]
            if not flags:
                raise FormulaError("#VALUE!")
            return all(flags) if name == "AND" else any(flags)
        if name == "NOT" and len(args) == 1:
            return _num(args[0]) == 0
        if name == "ABS" and len(args) == 1:
            return abs(_num(args[0]))
        if name == "ROUND" and len(args) in (1, 2):
            return _round_half_away(_num(args[0]), int(_num(args[1])) if len(args) == 2 else 0)
        raise FormulaError(f"unsupported function {name}")


def evaluate(formula: str, resolve: Callable[[str], Any]) -> Any:
    """Evaluate '=...' (leading '=' optional). `resolve(ref)` returns a scalar or a list."""
    text = formula[1:] if formula.startswith("=") else formula
    v = _Parser(_tokenize(text), resolve).parse()
    if isinstance(v, list):
        raise FormulaError("#VALUE!")
    if isinstance(v, float) and v.is_integer() and abs(v) < 1e15:
        return int(v)
    return round(v, 12) if isinstance(v, float) else v


# ================================================================== workbook
def _jsonable(v: Any, limit: int = 300) -> Any:
    if isinstance(v, (dt.datetime, dt.date, dt.time)):
        return v.isoformat()
    if isinstance(v, Decimal):
        return float(v)
    if isinstance(v, str) and len(v) > limit:
        return v[:limit] + "…"
    return v


_NUMERIC_TEXT = re.compile(r"^-?(?:0|[1-9]\d{0,14})(?:\.\d+)?$")


def coerce_number(v: Any) -> tuple[Any, bool]:
    """Canonical numeric text ("42", "-3.5") -> number. Leading zeros ("007"), dates, phone-like
    or >15-digit strings stay text. Small models often quote numbers; this keeps columns numeric."""
    if isinstance(v, str) and _NUMERIC_TEXT.match(v.strip()):
        t = v.strip()
        return (float(t) if "." in t else int(t)), True
    return v, False


def coerce_rows(rows: list[list]) -> tuple[list[list], int]:
    out, n = [], 0
    for r in rows:
        new = []
        for v in r:
            v2, hit = coerce_number(v)
            n += hit
            new.append(v2)
        out.append(new)
    return out, n


def _note(n: int) -> str:
    return f" (converted {n} numeric-looking text value(s) to numbers)" if n else ""


def _is_formula(raw: Any) -> bool:
    return isinstance(raw, str) and raw.startswith("=") and len(raw) > 1


class _Book:
    """Workbook with computed-value access (cached value first, evaluator as fallback)."""

    def __init__(self, path: Path):
        if not Path(path).is_file():
            raise FileNotFoundError(f"workbook does not exist: {Path(path).name}")
        self.wf = load_workbook(path)                    # formulas as written
        self.wv = load_workbook(path, data_only=True)    # cached values (None if never computed)
        self._memo: dict[tuple[str, str], Any] = {}
        self._stack: set[tuple[str, str]] = set()

    def sheet(self, name: str | None):
        if name and name not in self.wf.sheetnames:
            raise KeyError(f"worksheet not found: {name!r}; available sheets: {self.wf.sheetnames}")
        return self.wf[name] if name else self.wf.active

    def value(self, sheet: str, coord: str) -> Any:
        key = (sheet, coord)
        if key in self._memo:
            return self._memo[key]
        cell = self.wf[sheet][coord]
        raw = cell.value
        # A CSV/text value beginning with '=' is still data when the importer
        # explicitly stored it as a string. Only evaluate genuine XLSX formulas.
        if _is_formula(raw) and cell.data_type == "f":
            cached = self.wv[sheet][coord].value
            if cached is not None:
                v = cached
            else:
                if key in self._stack:
                    raise FormulaError("#CIRC!")
                self._stack.add(key)
                try:
                    v = evaluate(raw, lambda ref, _s=sheet: self._resolve(_s, ref))
                finally:
                    self._stack.discard(key)
        else:
            v = raw
        self._memo[key] = v
        return v

    def _resolve(self, sheet: str, ref: str) -> Any:
        target, coord = sheet, ref.replace("$", "")
        if "!" in ref:
            sname, coord = ref.rsplit("!", 1)
            coord = coord.replace("$", "")
            target = sname[1:-1].replace("''", "'") if sname.startswith("'") else sname
            if target not in self.wf.sheetnames:
                raise FormulaError("#REF!")
        if ":" in coord:
            c1, r1, c2, r2 = range_boundaries(coord)
            if (c2 - c1 + 1) * (r2 - r1 + 1) > MAX_CELLS_PER_RANGE:
                raise FormulaError("range too large")
            return [self.value(target, f"{get_column_letter(c)}{r}")
                    for r in range(r1, r2 + 1) for c in range(c1, c2 + 1)]
        return self.value(target, coord)

    def safe_value(self, sheet: str, coord: str) -> tuple[Any, bool]:
        """(value, uncomputed). A formula the evaluator cannot compute yields (None, True)."""
        try:
            return self.value(sheet, coord), False
        except Exception:
            return None, True


def _used_rows(ws) -> int:
    """Index of the last row holding any value (0 for an empty sheet)."""
    for r in range(ws.max_row, 0, -1):
        if any(c.value is not None for c in ws[r]):
            return r
    return 0


def _check_sheet_name(name: str) -> None:
    if not isinstance(name, str) or not name.strip():
        raise ValueError("sheet name must be a non-empty string")
    if len(name) > 31 or _BAD_SHEET_CHARS & set(name):
        raise ValueError(f"invalid sheet name {name!r}: max 31 chars and none of []:*?/\\")


# ----------------------------------------------------------------- read tools
def info(path: Path) -> dict:
    """Workbook overview: every sheet with used rows, columns and its header row."""
    wb = load_workbook(path)
    try:
        out = []
        for ws in wb.worksheets:
            used = _used_rows(ws)
            header = [_jsonable(c.value, 60) for c in ws[1]] if used else []
            while header and header[-1] is None:
                header.pop()
            out.append({"name": ws.title, "used_rows": used, "columns": ws.max_column if used else 0,
                        "header": header})
        return {"sheets": out}
    finally:
        wb.close()


def read(path: Path, sheet: str | None = None, cell_range: str | None = None,
         max_rows: int = 100, formulas: bool = False) -> dict:
    book = _Book(path)
    names = book.wf.sheetnames
    ws = book.sheet(sheet)
    title = ws.title
    if cell_range:
        c1, r1, c2, r2 = range_boundaries(cell_range.replace("$", ""))
        if None in (c1, r1, c2, r2):
            raise ValueError("cell_range must be bounded, like A1:D20")
    else:
        c1, r1, c2, r2 = 1, 1, max(ws.max_column or 1, 1), max(ws.max_row or 1, 1)
    last = min(r2, r1 + max(1, max_rows) - 1)
    rows: list[list[Any]] = []
    uncomputed: list[str] = []
    for r in range(r1, last + 1):
        row = []
        for c in range(c1, c2 + 1):
            coord = f"{get_column_letter(c)}{r}"
            if formulas:
                v = ws[coord].value
            else:
                v, bad = book.safe_value(title, coord)
                if bad:
                    uncomputed.append(coord)
            row.append(_jsonable(v))
        rows.append(row)
    out = {"sheets": names, "sheet": title,
           "range": f"{get_column_letter(c1)}{r1}:{get_column_letter(c2)}{last}",
           "rows": rows, "total_rows": ws.max_row or 0, "truncated": last < r2}
    if uncomputed:
        out["uncomputed_formula_cells"] = uncomputed[:10]
    return out


# ---------------------------------------------------------------- table tools
def _table(path: Path, sheet: str | None, cap: int = 20_000) -> tuple[list[Any], list[list[Any]], str]:
    book = _Book(path)
    ws = book.sheet(sheet)
    title, ncols, last = ws.title, max(ws.max_column or 1, 1), min(_used_rows(ws), cap)
    letters = [get_column_letter(c) for c in range(1, ncols + 1)]
    header = [book.safe_value(title, f"{L}1")[0] for L in letters]
    rows = []
    for r in range(2, last + 1):
        row = [book.safe_value(title, f"{L}{r}")[0] for L in letters]
        if any(v is not None for v in row):
            rows.append(row)
    return header, rows, title


def _col(header: list[Any], ref: str) -> int:
    if not isinstance(ref, str) or not ref.strip():
        raise KeyError(f"column must be a header name or a letter; columns: {header}")
    for i, h in enumerate(header):
        if h is not None and str(h).strip().casefold() == ref.strip().casefold():
            return i
    if re.fullmatch(r"[A-Za-z]{1,3}", ref.strip()):
        idx = column_index_from_string(ref.strip().upper()) - 1
        if idx < len(header):
            return idx
    raise KeyError(f"unknown column {ref!r}; columns: {header}")


def _isnum(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _match(v: Any, op: str, target: Any) -> bool:
    if op == "contains":
        return v is not None and str(target).casefold() in str(v).casefold()
    if _isnum(v) and isinstance(target, str):
        try:
            target = float(target)
        except ValueError:
            pass
    if _isnum(v) and _isnum(target):
        a, b = float(v), float(target)
    else:
        a, b = ("" if v is None else str(v)).casefold(), str(target).casefold()
    ops = {"==": a == b, "=": a == b, "!=": a != b, "<>": a != b}
    if op in ops:
        return ops[op]
    if op in (">", ">=", "<", "<=") and type(a) is type(b):
        return {">": a > b, ">=": a >= b, "<": a < b, "<=": a <= b}[op]
    return False


def _filter(header: list[Any], rows: list[list[Any]], where: list[dict] | None) -> list[list[Any]]:
    conds = []
    for w in where or []:
        if not isinstance(w, dict) or "column" not in w or "op" not in w or "value" not in w:
            raise ValueError('each where item must be {"column":..., "op":"==|!=|>|>=|<|<=|contains", "value":...}')
        if w["op"] not in ("==", "=", "!=", "<>", ">", ">=", "<", "<=", "contains"):
            raise ValueError(f"unsupported where op {w['op']!r}")
        conds.append((_col(header, w["column"]), w["op"], w["value"]))
    return [r for r in rows if all(_match(r[i], op, val) for i, op, val in conds)]


def _clean(x: float) -> int | float:
    x = round(x, 10)
    return int(x) if float(x).is_integer() else x


def aggregate(path: Path, sheet: str | None, column: str | None, op: str,
              where: list[dict] | None = None, group_by: str | None = None) -> dict:
    """sum / avg / min / max / count of a column (header row = row 1), optional filter/grouping."""
    op = (op or "").lower()
    if op not in ("sum", "avg", "min", "max", "count"):
        raise ValueError("op must be one of sum, avg, min, max, count")
    header, rows, title = _table(path, sheet)
    rows = _filter(header, rows, where)
    ci = _col(header, column) if column else None
    if ci is None and op != "count":
        raise ValueError("column is required unless op is count")

    def reduce(rs: list[list[Any]]) -> tuple[Any, int]:
        if op == "count":
            vals = [r for r in rs if ci is None or r[ci] not in (None, "")]
            return len(vals), len(vals)
        nums = [float(r[ci]) for r in rs if _isnum(r[ci])]
        if not nums:
            return None, 0
        val = {"sum": sum(nums), "avg": sum(nums) / len(nums), "min": min(nums), "max": max(nums)}[op]
        return _clean(val), len(nums)

    out: dict[str, Any] = {"sheet": title, "op": op, "column": header[ci] if ci is not None else None}
    if group_by:
        gi = _col(header, group_by)
        groups: dict[str, list[list[Any]]] = {}
        for r in rows:
            groups.setdefault(str(r[gi]), []).append(r)
        out["group_by"] = header[gi]
        out["groups"] = {k: reduce(v)[0] for k, v in groups.items()}
    else:
        out["value"], out["rows_used"] = reduce(rows)
    return out


def _sort_key(v: Any) -> tuple:
    if _isnum(v):
        return (0, float(v), "")
    return (1, 0.0, str(v).casefold())


def query(path: Path, sheet: str | None, where: list[dict] | None = None,
          order_by: str | None = None, descending: bool = False,
          columns: list[str] | None = None, limit: int = 50) -> dict:
    """Filter / sort / project rows of a table (header row = row 1). Returns header + rows."""
    header, rows, title = _table(path, sheet)
    rows = _filter(header, rows, where)
    if order_by:
        oi = _col(header, order_by)
        have = [r for r in rows if r[oi] is not None]
        none = [r for r in rows if r[oi] is None]
        rows = sorted(have, key=lambda r: _sort_key(r[oi]), reverse=descending) + none
    idx = [_col(header, c) for c in columns] if columns else list(range(len(header)))
    matched = len(rows)
    rows = rows[:max(1, limit)]
    return {"sheet": title, "header": [header[i] for i in idx],
            "rows": [[_jsonable(r[i]) for i in idx] for r in rows],
            "matched": matched, "returned": len(rows)}


# ---------------------------------------------------------------- write tools
def sheet_has_data(path: Path, sheet: str) -> int:
    """Used rows in an existing sheet (0 when the workbook or sheet is missing/empty)."""
    if not Path(path).is_file():
        return 0
    wb = load_workbook(path)
    try:
        return _used_rows(wb[sheet]) if sheet in wb.sheetnames else 0
    finally:
        wb.close()


def write(path: Path, rows: list[list], sheet: str = "Sheet1", replace_sheet: bool = True) -> str:
    """Create a workbook/sheet. replace_sheet=True rebuilds an existing sheet in place."""
    _check_sheet_name(sheet)
    if not isinstance(rows, list) or any(not isinstance(r, list) for r in rows):
        raise ValueError("rows must be a list of lists, e.g. [[\"Name\",\"Score\"],[\"A\",10]]")
    rows, converted = coerce_rows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        wb = load_workbook(path)
        if sheet in wb.sheetnames:
            existing_rows = _used_rows(wb[sheet])
            if existing_rows and not replace_sheet:
                wb.close()
                raise FileExistsError(
                    f"sheet {sheet!r} already contains {existing_rows} row(s); "
                    "xlsx_write with replace_sheet=False will not append or modify it. "
                    "Use xlsx_set_cells to edit cells or xlsx_append_rows to append rows."
                )
            if replace_sheet:
                idx = wb.sheetnames.index(sheet)
                del wb[sheet]
                ws = wb.create_sheet(sheet, idx)      # keep the sheet's position
            else:
                ws = wb[sheet]                         # existing but empty
        else:
            ws = wb.create_sheet(sheet)
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = sheet
    start = 0                                           # write starts at A1; append has its own tool
    for offset, r in enumerate(rows, start=1):
        for c, v in enumerate(r, start=1):
            ws.cell(start + offset, c, v)
    wb.save(path)
    wb.close()
    return f"wrote {len(rows)} rows to {path.name}!{sheet}{_note(converted)}"


def summarize(path: Path, source_sheet: str | None, group_by: str, column: str, op: str,
              target_sheet: str, replace_sheet: bool = True) -> str:
    """Compute a per-group aggregate AND write it as a table (header, one row per group, A-Z)."""
    agg = aggregate(path, source_sheet, column, op, None, group_by)
    def key(k: str):
        v, is_num = coerce_number(k)
        return (0, float(v), "") if is_num else (1, 0.0, k.casefold())
    rows = [[agg["group_by"], agg["column"]]] + [[k, agg["groups"][k]] for k in sorted(agg["groups"], key=key)]
    msg = write(path, rows, target_sheet, replace_sheet)
    return f"{msg}: {json.dumps(rows, ensure_ascii=False, default=str)}"


def filter_to_sheet(path: Path, source_sheet: str | None, target_sheet: str, where: list[dict] | None = None,
                    order_by: str | None = None, descending: bool = False, columns: list[str] | None = None,
                    replace_sheet: bool = True) -> str:
    """Filter / sort / project a source table AND write the result as a sheet (header + matching rows)."""
    res = query(path, source_sheet, where, order_by, descending, columns, limit=100_000)
    rows = [res["header"]] + res["rows"]
    msg = write(path, rows, target_sheet, replace_sheet)
    preview = json.dumps(rows[:6], ensure_ascii=False, default=str)
    return f"{msg}: {res['matched']} matching row(s) from {res['sheet']}; first rows {preview}"


def add_column(path: Path, sheet: str, column: str, header: str, value: Any = None,
                values: list | None = None, first_data_row: int = 2, last_data_row: int | None = None) -> str:
    """Add a new column: header in row 1, then one constant `value` for every data row or an explicit `values` list."""
    if not re.fullmatch(r"[A-Za-z]{1,3}", column or ""):
        raise ValueError("column must be a column letter such as C")
    if not isinstance(header, str) or not header.strip():
        raise ValueError("header must be non-empty text")
    if (value is None) == (values is None):
        raise ValueError("give exactly one of value (same for every data row) or values (one per data row)")
    if first_data_row < 2:
        raise ValueError("first_data_row must be 2 or greater")
    if not Path(path).is_file():
        raise FileNotFoundError(f"workbook does not exist: {Path(path).name}")
    target_col = column_index_from_string(column.upper())
    wb = load_workbook(path)
    try:
        if sheet not in wb.sheetnames:
            raise KeyError(f"worksheet not found: {sheet!r}; available sheets: {wb.sheetnames}")
        ws = wb[sheet]
        last = _used_rows(ws) if last_data_row is None else last_data_row
        if last < first_data_row:
            raise ValueError("no data rows found for the requested column")
        occupied = [ws.cell(r, target_col).coordinate for r in range(1, last + 1)
                    if ws.cell(r, target_col).value not in (None, "")]
        if occupied:
            raise ValueError(f"target column {column.upper()} is not empty; refusing to overwrite {occupied[:8]}")
        data_rows = [r for r in range(first_data_row, last + 1)
                     if any(ws.cell(r, c).value not in (None, "") for c in range(1, target_col))]
        if values is not None:
            if len(values) != len(data_rows):
                raise ValueError(f"values has {len(values)} item(s) but the table has {len(data_rows)} data row(s)")
            fill = list(values)
        else:
            fill = [value] * len(data_rows)
        ws.cell(1, target_col, header)
        for r, v in zip(data_rows, fill):
            ws.cell(r, target_col, coerce_number(v)[0])
        wb.save(path)
        return f"created {column.upper()} ({header}) and wrote {len(data_rows)} value(s) in {sheet}"
    finally:
        wb.close()


def append_rows(path: Path, sheet: str, rows: list[list]) -> str:
    """Append rows after the last used row of an EXISTING sheet; nothing already there changes."""
    if not isinstance(rows, list) or not rows or any(not isinstance(r, list) for r in rows):
        raise ValueError("rows must be a non-empty list of lists")
    if not path.is_file():
        raise FileNotFoundError(f"workbook does not exist: {path.name}. Use xlsx_write to create it.")
    rows, converted = coerce_rows(rows)
    wb = load_workbook(path)
    try:
        if sheet not in wb.sheetnames:
            raise KeyError(f"worksheet not found: {sheet!r}; available sheets: {wb.sheetnames}. "
                           "Use xlsx_write to create a new sheet.")
        ws = wb[sheet]
        first = _used_rows(ws) + 1
        for offset, r in enumerate(rows):
            for c, v in enumerate(r, start=1):
                ws.cell(first + offset, c, v)
        wb.save(path)
        return f"appended {len(rows)} row(s) to {sheet} at rows {first}-{first + len(rows) - 1}{_note(converted)}"
    finally:
        wb.close()


def set_cells(path: Path, sheet: str, cells: dict[str, Any]) -> str:
    """Set individual cells (values or '=formulas') and report every before -> after change."""
    if not isinstance(cells, dict) or not cells:
        raise ValueError('cells must be a non-empty object like {"B3": 99, "D2": "=B2*C2"}')
    bad = [k for k in cells if not isinstance(k, str) or not _CELL_RE.match(k)]
    if bad:
        raise ValueError(f"invalid cell address(es) {bad}; use A1-style like B3")
    if not path.is_file():
        raise FileNotFoundError(f"workbook does not exist: {path.name}")
    wb = load_workbook(path)
    try:
        if sheet not in wb.sheetnames:
            raise KeyError(f"worksheet not found: {sheet!r}; available sheets: {wb.sheetnames}")
        ws = wb[sheet]
        changes, converted = [], 0
        for addr, value in cells.items():
            addr = addr.upper()
            value, hit = coerce_number(value)
            converted += hit
            changes.append(f"{addr}: {json.dumps(_jsonable(ws[addr].value, 60), ensure_ascii=False)} -> "
                           f"{json.dumps(value, ensure_ascii=False, default=str)}")
            ws[addr] = value
        wb.save(path)
        shown = "; ".join(changes[:20]) + (f"; … +{len(changes) - 20} more" if len(changes) > 20 else "")
        return f"set {len(changes)} cell(s) in {sheet}: {shown}{_note(converted)}"
    finally:
        wb.close()


def import_csv(csv_path: Path, output_path: Path, sheet: str = "Sheet1") -> str:
    """Import a CSV as a new one-sheet workbook, preserving rows and typing numeric data cells."""
    _check_sheet_name(sheet)
    csv_path, output_path = Path(csv_path), Path(output_path)
    if not csv_path.is_file():
        raise FileNotFoundError(f"CSV source does not exist: {csv_path}")
    if output_path.exists():
        raise FileExistsError(f"destination workbook already exists: {output_path}")
    if csv_path.resolve() == output_path.resolve():
        raise ValueError("CSV source and XLSX destination must be different files")

    rows: list[list[Any]] = []
    converted = 0
    with csv_path.open("r", encoding="utf-8-sig", newline="") as stream:
        content = stream.read()
    try:
        dialect = csv.Sniffer().sniff(content[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    for row_index, raw_row in enumerate(csv.reader(io.StringIO(content), dialect)):
        row: list[Any] = []
        for raw in raw_row:
            if raw == "":
                row.append(None)
            elif row_index == 0:
                # Header labels are text even when they look numeric.
                row.append(raw)
            else:
                value, changed = coerce_number(raw)
                converted += int(changed)
                row.append(value)
        rows.append(row)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb = Workbook()
    ws = wb.active
    ws.title = sheet
    for r, values in enumerate(rows, start=1):
        for c, value in enumerate(values, start=1):
            cell = ws.cell(r, c, value)
            # Prevent CSV data such as '=HYPERLINK(...)' from becoming a formula.
            if isinstance(value, str) and value.startswith("="):
                cell.data_type = "s"
    wb.save(output_path)
    wb.close()
    return (f"imported {len(rows)} CSV row(s) to {output_path.name}!{sheet} "
            f"({converted} numeric data value(s) converted)")


def update_row_by_value(path: Path, sheet: str, match_column: str,
                        match_value: Any, updates: dict[str, Any]) -> str:
    """Update columns in exactly one row selected by an exact header/value match."""
    if not isinstance(updates, dict) or not updates:
        raise ValueError("updates must map one or more column headers to new values")
    if not Path(path).is_file():
        raise FileNotFoundError(f"workbook does not exist: {Path(path).name}")
    match_value, _ = coerce_number(match_value)
    wb = load_workbook(path)
    try:
        if sheet not in wb.sheetnames:
            raise KeyError(f"worksheet not found: {sheet!r}; available sheets: {wb.sheetnames}")
        ws = wb[sheet]
        header = [ws.cell(1, col).value for col in range(1, max(ws.max_column, 1) + 1)]
        match_idx = _col(header, match_column) + 1
        update_cols = { _col(header, name) + 1: value for name, value in updates.items() }
        if len(update_cols) != len(updates):
            raise ValueError("updates resolve to duplicate columns")

        def equal(actual: Any, expected: Any) -> bool:
            if _isnum(actual) and _isnum(expected):
                return float(actual) == float(expected)
            if isinstance(actual, str) and isinstance(expected, str):
                return actual.strip().casefold() == expected.strip().casefold()
            return actual == expected

        matches = [r for r in range(2, _used_rows(ws) + 1)
                   if equal(ws.cell(r, match_idx).value, match_value)]
        if len(matches) != 1:
            raise ValueError(
                f"expected exactly one row where {match_column!r}={match_value!r}; found {len(matches)}"
            )

        row = matches[0]
        changes, converted = [], 0
        for col, raw_value in update_cols.items():
            value, hit = coerce_number(raw_value)
            converted += int(hit)
            cell = ws.cell(row, col)
            changes.append(f"{cell.coordinate}: {cell.value!r} -> {value!r}")
            cell.value = value
        wb.save(path)
        return f"updated row {row} in {sheet}: " + "; ".join(changes) + _note(converted)
    finally:
        wb.close()


def add_formula_column(path: Path, sheet: str, column: str, header: str,
                       formula_template: str, first_data_row: int = 2,
                       last_data_row: int | None = None) -> str:
    """Add a new empty column and fill formulas using a {row} template, without touching other cells."""
    if not re.fullmatch(r"[A-Za-z]{1,3}", column or ""):
        raise ValueError("column must be a column letter such as D")
    if not isinstance(header, str) or not header.strip():
        raise ValueError("header must be non-empty text")
    if not isinstance(formula_template, str) or not formula_template.startswith("=") or "{row}" not in formula_template:
        raise ValueError("formula_template must start with '=' and contain the {row} placeholder")
    if first_data_row < 2:
        raise ValueError("first_data_row must be 2 or greater")
    if not Path(path).is_file():
        raise FileNotFoundError(f"workbook does not exist: {Path(path).name}")

    target_col = column_index_from_string(column.upper())
    wb = load_workbook(path)
    try:
        if sheet not in wb.sheetnames:
            raise KeyError(f"worksheet not found: {sheet!r}; available sheets: {wb.sheetnames}")
        ws = wb[sheet]
        last = _used_rows(ws) if last_data_row is None else last_data_row
        if last < first_data_row - 1:
            raise ValueError("no data rows found for the requested formula column")
        occupied = [ws.cell(r, target_col).coordinate for r in range(1, last + 1)
                    if ws.cell(r, target_col).value not in (None, "")]
        if occupied:
            raise ValueError(f"target column {column.upper()} is not empty; refusing to overwrite {occupied[:8]}")

        ws.cell(1, target_col, header)
        written = 0
        for row in range(first_data_row, last + 1):
            if not any(ws.cell(row, col).value not in (None, "")
                       for col in range(1, target_col)):
                continue
            formula = formula_template.replace("{row}", str(row))
            ws.cell(row, target_col, formula)
            written += 1
        wb.save(path)
        return f"created {column.upper()} ({header}) and wrote {written} formula(s) in {sheet}"
    finally:
        wb.close()


# ------------------------------------------------------------------ digests
def digest_rows(rows: list[list[Any]]) -> str:
    """Stable digest of a sheet's cell contents (trailing blanks ignored)."""
    norm = []
    for r in rows:
        r = list(r)
        while r and r[-1] is None:
            r.pop()
        norm.append(r)
    while norm and not norm[-1]:
        norm.pop()
    return hashlib.sha256(json.dumps(norm, ensure_ascii=False, default=str).encode()).hexdigest()
