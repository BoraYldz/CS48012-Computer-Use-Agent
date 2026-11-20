"""Local tool backend with the same interface as the MCP VM backend.

All agent-visible paths are resolved beneath one configured allowed root. The
root can be an isolated sandbox or an explicitly selected local workspace.
Workers receive the same tool names as in VM mode; harness-only tools remain
unavailable to workers.
"""
from __future__ import annotations

import base64
import hashlib
import json
import shutil
from functools import wraps
from pathlib import Path
from typing import Any, Callable

from .tool_output import serialize_tool_result


def _safe_tool_callable(fn, name: str):
    """Return tool errors as observations so ReAct can recover instead of crashing."""
    @wraps(fn)
    def invoke(*args, **kwargs):
        try:
            result = fn(*args, **kwargs)
            # OpenAI-compatible APIs expect plain-text tool observations. JSON
            # encode structured returns instead of passing list/dict content blocks.
            return serialize_tool_result(result)
        except PermissionError as exc:
            # Human denial is final: do not invite the model to look for a workaround.
            return f"TOOL_ERROR [{name}]: {str(exc)[:300]}"
        except Exception as exc:
            return (
                f"TOOL_ERROR [{name}]: {type(exc).__name__}: {str(exc)[:300]}. "
                "The operation was not completed. Review the error and use a supported alternative."
            )
    return invoke


def resolve_local_root(
    raw_root: str | Path | None,
    scope: str,
    default_root: str | Path,
) -> Path:
    """Resolve the local allowed root and validate explicit workspace mode.

    Sandbox mode retains the project-local default and accepts relative/absolute
    overrides for compatibility. Workspace mode requires an existing absolute
    directory chosen by the user and rejects broad root/home directories.
    """
    if scope not in {"sandbox", "workspace"}:
        raise ValueError("AGENT_SCOPE must be 'sandbox' or 'workspace'")

    if scope == "workspace":
        if raw_root is None or not str(raw_root).strip():
            raise ValueError(
                "AGENT_SCOPE=workspace requires AGENT_ROOT to name an existing dedicated directory"
            )
        candidate = Path(raw_root).expanduser()
        if not candidate.is_absolute():
            raise ValueError(
                "AGENT_SCOPE=workspace requires an absolute AGENT_ROOT path; relative roots are ambiguous"
            )
        if not candidate.exists() or not candidate.is_dir():
            raise ValueError(f"workspace root must already exist and be a directory: {candidate}")
        resolved = candidate.resolve(strict=True)
        home = Path.home().resolve()
        blocked = {Path(resolved.anchor), home}
        blocked.update(home / name for name in (
            "Desktop", "Documents", "Downloads", "Pictures", "Music", "Movies"
        ))
        if Path(resolved.anchor) == Path("/"):
            blocked.update(Path(p) for p in (
                "/Users", "/Applications", "/System", "/Library", "/Volumes",
                "/private", "/usr", "/opt", "/var", "/tmp",
            ))
        if resolved in blocked:
            raise ValueError(
                f"refusing broad workspace root {resolved}; choose a dedicated subdirectory, "
                "for example ~/Desktop/AgentWorkspace"
            )
        return resolved

    candidate = Path(raw_root).expanduser() if raw_root else Path(default_root)
    if candidate.is_absolute():
        return candidate.resolve()
    return (Path(default_root).resolve().parent / candidate).resolve()


class LocalVM:
    """Run the filesystem and spreadsheet tools directly on this machine."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        # One-shot fault injection is available only to the local test harness.
        # It is deliberately not exposed as an agent tool.
        self._write_fault: dict[str, Any] | None = None
        # Human-approval gate for high-blast-radius actions (delete, overwrite).
        # approver(request) -> bool. None means deny: the gate fails closed.
        self.approver: Callable[[dict], bool] | None = None
        self.approval_log: list[dict] = []
        self.event_sink: Callable[..., Any] | None = None  # e.g. RunLog.write
        self._functions = {
            "list_dir": self.list_dir,
            "path_info": self.path_info,
            "make_dirs": self.make_dirs,
            "move_path": self.move_path,
            "move_files": self.move_files,
            "copy_path": self.copy_path,
            "delete_path": self.delete_path,
            "read_text": self.read_text,
            "write_text": self.write_text,
            "read_pdf_text": self.read_pdf_text,
            "xlsx_read": self.xlsx_read,
            "xlsx_write": self.xlsx_write,
            # Harness-only tools: intentionally absent from WORKER_TOOLS.
            "harness_put_file": self.harness_put_file,
            "harness_get_file": self.harness_get_file,
            "harness_remove_path": self.harness_remove_path,
        }
        self.tools: dict[str, Any] = {}

    def _p(self, path: str) -> Path:
        """Resolve an allowed-root-relative path and reject traversal/symlink escapes."""
        if not isinstance(path, str) or not path.strip():
            raise ValueError("path must be a non-empty string")
        candidate = Path(path).expanduser()
        resolved = (candidate if candidate.is_absolute() else self.root / candidate).resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise ValueError(f"path outside configured allowed root: {resolved}") from exc
        return resolved

    # ---------- approval gate ----------
    def _require_approval(self, tool: str, args: dict, summary: str) -> None:
        """Block until a human approves; raise PermissionError on denial (fail closed)."""
        request = {"tool": tool, "args": args, "summary": summary}
        try:
            approved = bool(self.approver(request)) if self.approver else False
        except Exception:
            approved = False
        record = {**request, "approved": approved}
        self.approval_log.append(record)
        if self.event_sink:
            try:
                self.event_sink("approval", **record)
            except Exception:
                pass
        if not approved:
            raise PermissionError(
                f"DENIED: the user did not approve {tool}. Do not retry or look for another "
                "way to do this; stop and report that approval was denied."
            )

    # ---------- worker tools: files ----------
    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def list_dir(self, path: str = ".", recursive: bool = False) -> list[dict]:
        """List files and folders, including size and SHA-256 for files."""
        base = self._p(path)
        if not base.is_dir():
            raise NotADirectoryError(f"not a directory: {path}")
        items = base.rglob("*") if recursive else base.iterdir()
        result = []
        for item in sorted(items):
            is_file = item.is_file()
            result.append({
                "path": item.relative_to(self.root).as_posix(),
                "type": "file" if is_file else "dir",
                "size": item.stat().st_size if is_file else None,
                "sha256": self._sha256(item) if is_file else None,
            })
        return result

    def path_info(self, path: str) -> dict:
        """Check whether a path exists and whether it is a file or directory."""
        p = self._p(path)
        return {
            "exists": p.exists(),
            "is_dir": p.is_dir(),
            "size": p.stat().st_size if p.is_file() else None,
        }

    def make_dirs(self, path: str) -> str:
        """Create a folder and any missing parents; preserve existing content."""
        self._p(path).mkdir(parents=True, exist_ok=True)
        return "ok"

    def move_path(self, src: str, dst: str, overwrite: bool = False) -> str:
        """Move one literal file/folder path; no wildcard patterns. Use list_dir then move each matching file separately."""
        if any(ch in src for ch in "*?[") or any(ch in dst for ch in "*?["):
            raise ValueError(
                "move_path accepts one literal path and does not expand wildcards. "
                "Call list_dir on the source folder, then move each matching file using its exact returned path."
            )
        source, destination = self._p(src), self._p(dst)
        if not source.exists():
            raise FileNotFoundError(f"source does not exist: {src}")
        if destination.exists() and not overwrite:
            raise FileExistsError(f"destination exists: {dst}")
        if destination.exists() and overwrite:
            self._require_approval(
                "move_path", {"src": src, "dst": dst, "overwrite": True},
                f"OVERWRITE {dst}: the existing {'folder' if destination.is_dir() else 'file'} will be destroyed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() and overwrite and destination.is_dir():
            shutil.rmtree(destination)
        elif destination.exists() and overwrite:
            destination.unlink()
        shutil.move(str(source), str(destination))
        return "ok"

    def move_files(self, src_dir: str, filenames: list[str], dst_dir: str) -> dict[str, Any]:
        """Move an explicit list of files from one directory into another, preserving filenames.

        The destination directory is created when needed. Every source must be a
        direct child file of src_dir. Wildcards and nested paths are rejected, and
        no destination is overwritten. All paths remain inside the configured root.
        """
        source_dir = self._p(src_dir)
        destination_dir = self._p(dst_dir)
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
            source = self._p((source_dir / filename).relative_to(self.root).as_posix())
            if not source.is_file():
                raise FileNotFoundError(f"source file does not exist: {src_dir}/{filename}")
            destination = self._p((destination_dir / filename).relative_to(self.root).as_posix())
            if destination.exists():
                raise FileExistsError(f"destination file already exists: {dst_dir}/{filename}")
            source_paths.append((filename, source, destination))

        destination_dir.mkdir(parents=True, exist_ok=True)
        # Validate all entries before moving any file to avoid predictable partial moves.
        moved: list[str] = []
        for filename, source, destination in source_paths:
            shutil.move(str(source), str(destination))
            moved.append(filename)
        return {"moved": moved, "count": len(moved), "destination": dst_dir}

    def copy_path(self, src: str, dst: str, overwrite: bool = False) -> str:
        """Copy a file; refuse to overwrite by default."""
        source, destination = self._p(src), self._p(dst)
        if not source.is_file():
            raise FileNotFoundError(f"source file does not exist: {src}")
        if destination.exists() and not overwrite:
            raise FileExistsError(f"destination exists: {dst}")
        if destination.exists() and overwrite:
            self._require_approval(
                "copy_path", {"src": src, "dst": dst, "overwrite": True},
                f"OVERWRITE {dst}: the existing file will be destroyed")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        return "ok"

    def delete_path(self, path: str, recursive: bool = False) -> str:
        """PERMANENTLY delete one exact file or folder. Needs human approval. Folders need recursive=true. No wildcards."""
        if any(ch in path for ch in "*?["):
            raise ValueError("delete_path accepts one literal path; no wildcards. Use list_dir, then delete each exact path.")
        target = self._p(path)
        if target == self.root:
            raise ValueError("refusing to delete the allowed root")
        if not target.exists():
            raise FileNotFoundError(f"path does not exist: {path}")
        if target.is_dir():
            inside = sum(1 for _ in target.rglob("*"))
            if inside and not recursive:
                raise OSError(f"directory is not empty ({inside} item(s)); pass recursive=true only if the user asked to delete the whole folder")
            summary = f"DELETE folder {path} and the {inside} item(s) inside it"
        else:
            summary = f"DELETE file {path} ({target.stat().st_size} bytes)"
        self._require_approval("delete_path", {"path": path, "recursive": recursive}, summary)
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        return "ok"

    def read_text(self, path: str, max_chars: int = 20000) -> str:
        """Read a UTF-8 text file (CSV, TXT, Markdown, and similar)."""
        return self._p(path).read_text(encoding="utf-8", errors="replace")[:max_chars]

    def write_text(self, path: str, content: str) -> str:
        """Write a UTF-8 text file, creating parent folders when needed."""
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        fault = self._write_fault
        if fault and not fault["triggered"] and p == fault["target"]:
            fault["write_observed"] = True
        return "ok"

    def read_pdf_text(self, path: str, max_chars: int = 20000) -> str:
        """Extract text from a PDF file."""
        from pypdf import PdfReader

        reader = PdfReader(self._p(path))
        return "\n".join(page.extract_text() or "" for page in reader.pages)[:max_chars]

    # ---------- worker tools: spreadsheets ----------
    def xlsx_read(self, path: str, sheet: str | None = None, max_rows: int = 500) -> dict:
        """Read an XLSX sheet as rows of values, including truncation metadata."""
        from openpyxl import load_workbook

        workbook = load_workbook(self._p(path), data_only=True, read_only=True)
        try:
            if sheet and sheet not in workbook.sheetnames:
                raise KeyError(f"worksheet not found: {sheet}")
            worksheet = workbook[sheet] if sheet else workbook.active
            total_rows = worksheet.max_row or 0
            rows = [list(row) for row in worksheet.iter_rows(values_only=True, max_row=max_rows)]
            return {
                "sheets": workbook.sheetnames,
                "sheet": worksheet.title,
                "rows": rows,
                "total_rows": total_rows,
                "truncated": total_rows > max_rows,
            }
        finally:
            workbook.close()

    def xlsx_write(
        self,
        path: str,
        rows: list[list],
        sheet: str = "Sheet1",
        replace_sheet: bool = True,
    ) -> str:
        """Write rows to an XLSX worksheet; preserve other worksheets."""
        from openpyxl import Workbook, load_workbook

        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        if p.exists():
            workbook = load_workbook(p)
            if sheet in workbook.sheetnames and replace_sheet:
                del workbook[sheet]
            worksheet = workbook[sheet] if sheet in workbook.sheetnames else workbook.create_sheet(sheet)
        else:
            workbook = Workbook()
            worksheet = workbook.active
            worksheet.title = sheet

        for row in rows:
            worksheet.append(row)
        workbook.save(p)
        workbook.close()
        return f"wrote {len(rows)} rows to {p.name}!{sheet}"

    # ---------- harness-only tools: never exposed to workers ----------
    def harness_put_file(self, path: str, b64: str) -> str:
        """HARNESS ONLY: write a base64-encoded fixture file."""
        p = self._p(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(base64.b64decode(b64, validate=True))
        return "ok"

    def harness_get_file(self, path: str) -> str:
        """HARNESS ONLY: return a file's contents as base64."""
        return base64.b64encode(self._p(path).read_bytes()).decode("ascii")

    def harness_remove_path(self, path: str) -> str:
        """HARNESS ONLY: remove one task-owned fixture/output path."""
        p = self._p(path)
        if p == self.root:
            raise ValueError("refusing to remove the sandbox root")
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
        return "ok"

    # ---------- local test-harness controls: never exposed to workers ----------
    def arm_write_fault(self, path: str, replacement_content: str) -> None:
        """Corrupt the next write to exactly one sandbox path (test harness only)."""
        target = self._p(path)
        if not isinstance(replacement_content, str):
            raise TypeError("replacement_content must be text")
        self._write_fault = {
            "target": target,
            "replacement_content": replacement_content,
            "triggered": False,
            "write_observed": False,
        }

    def apply_pending_write_fault(self) -> dict[str, str] | None:
        """Apply the armed corruption after a worker attempt, before checking."""
        fault = self._write_fault
        if not fault or fault["triggered"] or not fault["write_observed"]:
            return None
        # Re-resolve at application time as well, so a path replaced by a
        # symlink after arming cannot escape the sandbox.
        target = self._p(fault["target"].relative_to(self.root).as_posix())
        if not target.is_file():
            return None
        target.write_text(fault["replacement_content"], encoding="utf-8")
        fault["triggered"] = True
        return {
            "kind": "one_shot_post_worker_write_corruption",
            "path": target.relative_to(self.root).as_posix(),
            "replacement_content": fault["replacement_content"],
        }

    @property
    def fault_injection_triggered(self) -> bool:
        return bool(self._write_fault and self._write_fault["triggered"])

    async def connect(self) -> "LocalVM":
        """Create LangChain tool wrappers for the specialist ReAct agents."""
        try:
            from langchain_core.tools import StructuredTool
        except ImportError as exc:
            raise RuntimeError(
                "Local agent tools require the host dependencies. Run: "
                "python -m pip install -r requirements.txt"
            ) from exc

        self.tools = {
            name: StructuredTool.from_function(
                func=_safe_tool_callable(fn, name),
                name=name,
                description=fn.__doc__ or name,
            )
            for name, fn in self._functions.items()
        }
        return self

    def for_worker(self, capability: str) -> list[Any]:
        from .runtime import WORKER_TOOLS

        if capability not in WORKER_TOOLS:
            raise ValueError(f"unknown worker capability: {capability}")
        return [self.tools[name] for name in WORKER_TOOLS[capability]]

    async def call(self, name: str, **args):
        """Direct async-compatible call used by the CLI, harness and checkers."""
        if name not in self._functions:
            raise KeyError(f"unknown local tool: {name}")
        return self._functions[name](**args)
