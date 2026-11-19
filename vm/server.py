"""Tool server that runs INSIDE the Windows VM.

The agent's "hands": every action the agent takes goes through these tools.
Start it at logon (Task Scheduler), never as a Windows service.

    python server.py            # listens on 0.0.0.0:8765, MCP at /mcp, health at /health
"""
import base64
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
def list_dir(path: str = ".", recursive: bool = False) -> list[dict]:
    """List files and folders. Paths are relative to the user's home folder."""
    base = _p(path)
    it = base.rglob("*") if recursive else base.iterdir()
    return [
        {"path": str(x.relative_to(ROOT)), "type": "dir" if x.is_dir() else "file",
         "size": x.stat().st_size if x.is_file() else None}
        for x in sorted(it)
    ]


@mcp.tool()
def path_info(path: str) -> dict:
    """Does a path exist, is it a file or folder, and its size."""
    p = _p(path)
    return {"exists": p.exists(), "is_dir": p.is_dir(),
            "size": p.stat().st_size if p.is_file() else None}


@mcp.tool()
def make_dirs(path: str) -> str:
    """Create a folder (and any missing parents). Existing content is untouched."""
    _p(path).mkdir(parents=True, exist_ok=True)
    return "ok"


@mcp.tool()
def move_path(src: str, dst: str, overwrite: bool = False) -> str:
    """Move or rename a file/folder. Refuses to overwrite unless overwrite=True."""
    s, d = _p(src), _p(dst)
    if d.exists() and not overwrite:
        raise FileExistsError(f"destination exists: {d}")
    d.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(s), str(d))
    return "ok"


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
def xlsx_read(path: str, sheet: str | None = None, max_rows: int = 500) -> dict:
    """Read an .xlsx sheet as rows of values. Returns sheet names too."""
    from openpyxl import load_workbook
    wb = load_workbook(_p(path), data_only=True)
    ws = wb[sheet] if sheet else wb.active
    rows = [list(r) for r in ws.iter_rows(values_only=True, max_row=max_rows)]
    return {"sheets": wb.sheetnames, "sheet": ws.title, "rows": rows}


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


if __name__ == "__main__":
    print(f"cua-vm tool server on :{PORT}, root={ROOT}")
    mcp.run(transport="streamable-http")
