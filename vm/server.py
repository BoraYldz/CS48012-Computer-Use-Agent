"""Tool server that runs INSIDE the Windows VM.

The agent's "hands": every action the agent takes goes through these tools.
Start it at logon (Task Scheduler), never as a Windows service.

    python server.py            # listens on 0.0.0.0:8765, MCP at /mcp, health at /health
"""
import base64
import hashlib
import json
import os
import shutil
import sys
from pathlib import Path

# pythonw (no console) sets stdout/stderr to None, which crashes uvicorn's logging.
# Send output to C:\agent\server.log instead; also handy for debugging.
if sys.stdout is None or sys.stderr is None:
    _log = open(Path(__file__).with_name("server.log"), "a", buffering=1, encoding="utf-8")
    sys.stdout = sys.stdout or _log
    sys.stderr = sys.stderr or _log

from mcp.server.fastmcp import FastMCP
from starlette.requests import Request
from starlette.responses import JSONResponse

import sheet_tools  # sits next to this file in the VM; copy of host/agent/sheet_tools.py

# Every path the agent touches must live under ROOT (the agent user's home by default).
ROOT = Path(os.environ.get("AGENT_ROOT", Path.home())).resolve()
PORT = int(os.environ.get("AGENT_PORT", "8765"))

mcp = FastMCP("cua-vm", host="0.0.0.0", port=PORT)


def _json_text(value) -> str:
    """Serialize structured tool output as plain text for OpenAI-compatible clients."""
    return json.dumps(value, ensure_ascii=False, default=str)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _p(path: str) -> Path:
    """Resolve a path relative to ROOT and refuse anything outside it."""
    p = Path(path)
    p = (p if p.is_absolute() else ROOT / p).resolve()
    if p != ROOT and ROOT not in p.parents:
        raise ValueError(f"path outside allowed root: {p}")
    return p


@mcp.custom_route("/health", methods=["GET"])
async def health(_: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "root": str(ROOT)})


# ---------- files ----------
@mcp.tool()
def list_dir(path: str = ".", recursive: bool = False) -> str:
    """List files and folders as JSON text, including file size and SHA-256."""
    base = _p(path)
    if not base.is_dir():
        raise NotADirectoryError(f"not a directory: {path}")
    it = base.rglob("*") if recursive else base.iterdir()
    result = []
    for item in sorted(it):
        is_file = item.is_file()
        result.append({
            "path": str(item.relative_to(ROOT)),
            "type": "file" if is_file else "dir",
            "size": item.stat().st_size if is_file else None,
            "sha256": _sha256(item) if is_file else None,
        })
    return _json_text(result)


@mcp.tool()
def path_info(path: str) -> str:
    """Return path metadata as JSON text."""
    p = _p(path)
    return _json_text({"exists": p.exists(), "is_dir": p.is_dir(),
                       "size": p.stat().st_size if p.is_file() else None})


@mcp.tool()
def make_dirs(path: str) -> str:
    """Create a folder (and any missing parents). Existing content is untouched."""
    _p(path).mkdir(parents=True, exist_ok=True)
    return "ok"


@mcp.tool()
def move_path(src: str, dst: str, overwrite: bool = False) -> str:
    """Move one literal file/folder path. No wildcard patterns; list_dir and move each matching file separately."""
    if any(ch in src for ch in "*?[") or any(ch in dst for ch in "*?["):
        raise ValueError(
            "move_path accepts one literal path and does not expand wildcards. "
            "Call list_dir on the source folder, then move each matching file using its exact returned path."
        )
    s, d = _p(src), _p(dst)
    if d.exists() and not overwrite:
        raise FileExistsError(f"destination exists: {d}")
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return "ok"


@mcp.tool()
def move_files(src_dir: str, filenames: list[str], dst_dir: str) -> str:
    """Move an explicit list of files into a destination folder, preserving filenames.

    The destination folder is created when needed. Each filename must be an exact
    basename (not a path or wildcard). Existing destination files are never overwritten.
    """
    source_dir = _p(src_dir)
    destination_dir = _p(dst_dir)
    if not source_dir.is_dir():
        raise NotADirectoryError(f"source directory does not exist or is not a directory: {src_dir}")
    if not isinstance(filenames, list) or not filenames:
        raise ValueError("filenames must be a non-empty list of exact file names returned by list_dir")
    if destination_dir == source_dir:
        raise ValueError("source and destination directories must be different")

    source_paths: list[tuple[str, Path, Path]] = []
    seen: set[str] = set()
    for filename in filenames:
        if not isinstance(filename, str) or not filename.strip():
            raise ValueError("each filename must be a non-empty string")
        candidate_name = Path(filename)
        if candidate_name.name != filename or filename in {".", ".."} or any(ch in filename for ch in "*?[]"):
            raise ValueError(f"expected an exact basename, not a path or wildcard: {filename!r}")
        if filename in seen:
            raise ValueError(f"duplicate filename in move request: {filename}")
        seen.add(filename)
        source = _p((source_dir / filename).relative_to(ROOT).as_posix())
        if not source.is_file():
            raise FileNotFoundError(f"source file does not exist: {src_dir}/{filename}")
        destination = _p((destination_dir / filename).relative_to(ROOT).as_posix())
        if destination.exists():
            raise FileExistsError(f"destination file already exists: {dst_dir}/{filename}")
        source_paths.append((filename, source, destination))

    destination_dir.mkdir(parents=True, exist_ok=True)
    moved: list[str] = []
    for filename, source, destination in source_paths:
        shutil.move(str(source), str(destination))
        moved.append(filename)
    return _json_text({"moved": moved, "count": len(moved), "destination": dst_dir})


@mcp.tool()
def copy_path(src: str, dst: str, overwrite: bool = False) -> str:
    """Copy a file. Refuses to overwrite unless overwrite=True."""
    s, d = _p(src), _p(dst)
    if d.exists() and not overwrite:
        raise FileExistsError(f"destination exists: {d}")
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(s, d)
    return "ok"


@mcp.tool()
def read_text(path: str, max_chars: int = 20000) -> str:
    """Read a text file (csv, txt, md...)."""
    return _p(path).read_text(encoding="utf-8", errors="replace")[:max_chars]


@mcp.tool()
def write_text(path: str, content: str) -> str:
    """Write a text file, creating parent folders."""
    p = _p(path)
    if p.suffix.casefold() in {".xlsx", ".xlsm", ".xltx", ".xltm"}:
        raise ValueError(
            f"Refusing to write plain text to Excel workbook path {path!r}. "
            "Use xlsx_write or another spreadsheet tool to create or modify a workbook."
        )
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return "ok"


@mcp.tool()
def read_pdf_text(path: str, max_chars: int = 20000) -> str:
    """Extract the text of a PDF."""
    from pypdf import PdfReader
    return "\n".join((pg.extract_text() or "") for pg in PdfReader(_p(path)).pages)[:max_chars]


# ---------- spreadsheets (engine shared with the host: sheet_tools.py, keep byte-identical) ----------
def _xlsx(path: str) -> Path:
    p = _p(path)
    if not p.is_file():
        raise FileNotFoundError(f"workbook does not exist: {path}")
    return p


@mcp.tool()
def xlsx_info(path: str) -> str:
    """Overview of a workbook: every sheet with its used rows, column count and header row. Call this first."""
    return _json_text(sheet_tools.info(_xlsx(path)))


@mcp.tool()
def xlsx_read(path: str, sheet: str | None = None, cell_range: str | None = None,
              max_rows: int = 100, formulas: bool = False) -> str:
    """Read cells as rows of COMPUTED values (formulas are evaluated). Optional cell_range like "A1:D20";
    formulas=true returns the formula text instead. At most max_rows rows; check `truncated`."""
    return _json_text(sheet_tools.read(_xlsx(path), sheet, cell_range, max_rows, formulas))


@mcp.tool()
def xlsx_import_csv(csv_path: str, path: str, sheet: str = "Sheet1") -> str:
    """Create a new XLSX workbook by importing every row from a CSV; numeric data cells become numbers."""
    return sheet_tools.import_csv(_p(csv_path), _p(path), sheet)


@mcp.tool()
def xlsx_query(path: str, sheet: str | None = None, where: list[dict] | None = None,
               order_by: str | None = None, descending: bool = False,
               columns: list[str] | None = None, limit: int = 50) -> str:
    """Filter/sort/select rows of a table whose header is row 1. where=[{"column":"Amount","op":">","value":100}]
    (ops: == != > >= < <= contains). Use this instead of sorting or filtering by hand."""
    return _json_text(sheet_tools.query(_xlsx(path), sheet, where, order_by, descending, columns, limit))


@mcp.tool()
def xlsx_aggregate(path: str, sheet: str | None = None, column: str | None = None, op: str = "sum",
                   where: list[dict] | None = None, group_by: str | None = None) -> str:
    """Exact sum/avg/min/max/count of a column (header = row 1), optionally filtered (same where as
    xlsx_query) or grouped. Use this instead of adding numbers yourself."""
    return _json_text(sheet_tools.aggregate(_xlsx(path), sheet, column, op, where, group_by))


@mcp.tool()
def xlsx_write(path: str, rows: list[list], sheet: str = "Sheet1", replace_sheet: bool = True) -> str:
    """Create a workbook or a sheet from rows (list of lists; strings starting with '=' become formulas).
    replace_sheet=true DESTROYS an existing sheet of that name; to add to existing data use
    xlsx_append_rows or xlsx_set_cells. Other sheets are kept."""
    return sheet_tools.write(_p(path), rows, sheet, replace_sheet)


@mcp.tool()
def xlsx_group_summary(path: str, group_by: str, column: str, op: str = "sum", target_sheet: str = "Summary",
                       source_sheet: str | None = None, replace_sheet: bool = True) -> str:
    """Compute AND write a per-group table in one call: header (group column, value column), one row per
    group sorted A-Z, values = sum/avg/min/max/count of `column`. Use for 'total X per Y'."""
    return sheet_tools.summarize(_xlsx(path), source_sheet, group_by, column, op, target_sheet, replace_sheet)


@mcp.tool()
def xlsx_append_rows(path: str, sheet: str, rows: list[list]) -> str:
    """Append rows below the last used row of an existing sheet. Existing cells are never changed."""
    return sheet_tools.append_rows(_p(path), sheet, rows)


@mcp.tool()
def xlsx_set_cells(path: str, sheet: str, cells: dict) -> str:
    """Set specific cells, e.g. {"B3": 99, "D2": "=B2*C2"}. Returns each cell's old -> new value."""
    return sheet_tools.set_cells(_p(path), sheet, cells)


@mcp.tool()
def xlsx_update_row(path: str, sheet: str, match_column: str,
                    match_value: str | int | float, updates: dict) -> str:
    """Update columns in exactly one row selected by its label/value, e.g. Item=Coffee -> Price=25."""
    return sheet_tools.update_row_by_value(_xlsx(path), sheet, match_column, match_value, updates)


@mcp.tool()
def xlsx_add_formula_column(path: str, sheet: str, column: str, header: str,
                            formula_template: str, first_data_row: int = 2,
                            last_data_row: int | None = None) -> str:
    """Add a formula column using a template such as =B{row}+C{row}, without changing other cells."""
    return sheet_tools.add_formula_column(
        _xlsx(path), sheet, column, header, formula_template, first_data_row, last_data_row
    )


# ---------- harness only (test setup / checkers); never given to workers ----------
@mcp.tool()
def harness_put_file(path: str, b64: str) -> str:
    """HARNESS ONLY: place a fixture file."""
    p = _p(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(base64.b64decode(b64))
    return "ok"


@mcp.tool()
def harness_get_file(path: str) -> str:
    """HARNESS ONLY: fetch a file's bytes (base64) for checking."""
    return base64.b64encode(_p(path).read_bytes()).decode()


@mcp.tool()
def harness_remove_path(path: str) -> str:
    """HARNESS ONLY: remove one task-owned fixture/output path."""
    p = _p(path)
    if p == ROOT:
        raise ValueError("refusing to remove the sandbox root")
    if p.is_dir():
        shutil.rmtree(p)
    elif p.exists():
        p.unlink()
    return "ok"


if __name__ == "__main__":
    print(f"cua-vm tool server on :{PORT}, root={ROOT}")
    mcp.run(transport="streamable-http")
