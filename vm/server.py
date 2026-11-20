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
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    return "ok"


@mcp.tool()
def read_pdf_text(path: str, max_chars: int = 20000) -> str:
    """Extract the text of a PDF."""
    from pypdf import PdfReader
    return "\n".join((pg.extract_text() or "") for pg in PdfReader(_p(path)).pages)[:max_chars]


# ---------- spreadsheets ----------
@mcp.tool()
def xlsx_read(path: str, sheet: str | None = None, max_rows: int = 500) -> str:
    """Read an .xlsx sheet as rows of values. Returns sheet names too."""
    from openpyxl import load_workbook
    wb = load_workbook(_p(path), data_only=True)
    ws = wb[sheet] if sheet else wb.active
    total_rows = ws.max_row
    rows = [list(r) for r in ws.iter_rows(values_only=True, max_row=max_rows)]
    return _json_text({"sheets": wb.sheetnames, "sheet": ws.title, "rows": rows,
                       "total_rows": total_rows, "truncated": total_rows > max_rows})


@mcp.tool()
def xlsx_write(path: str, rows: list[list], sheet: str = "Sheet1", replace_sheet: bool = True) -> str:
    """Write rows to a sheet. Creates the file if missing; other sheets are kept."""
    from openpyxl import Workbook, load_workbook
    p = _p(path)
    if p.exists():
        wb = load_workbook(p)
        if sheet in wb.sheetnames and replace_sheet:
            del wb[sheet]
        ws = wb[sheet] if sheet in wb.sheetnames else wb.create_sheet(sheet)
    else:
        wb = Workbook()
        ws = wb.active
        ws.title = sheet
    for r in rows:
        ws.append(r)
    p.parent.mkdir(parents=True, exist_ok=True)
    wb.save(p)
    return f"wrote {len(rows)} rows to {p.name}!{sheet}"


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
