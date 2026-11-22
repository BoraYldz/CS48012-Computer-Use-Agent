"""`file_ops`: one generic, state-grounded contract for move / copy / rename.

The planner (LLM) only supplies PARAMETERS of the intent:

    {"kind": "file_ops", "op": "move|copy|rename", "source_dir": ..., "destination_dir": ...,
     "extension": ".csv" | null, "recursive": true|false,
     "rename_prefix": ..., "rename_suffix": ...}

Code, not the model, then turns the parameters into exact expectations from the REAL files:

  expand_file_ops()  at worker start  -> `expected_ops` (src -> dst, SHA-256) + `preserved_files`
                                          (everything else in scope, with hash)
  check_file_ops()   after the worker -> every dst exists with the same hash; sources absent
                                          (copy: still present, unchanged); nothing else in scope
                                          changed, vanished or appeared.

There are no request-phrase regexes here: the same code verifies a sort, a rename, a recursive
collection or a backup copy, and an empty match set is reported as UNKNOWN, never as success.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any

VALID_OPS = {"move", "copy", "rename"}
_IGNORED = {".ds_store", "thumbs.db", "desktop.ini"}
MAX_SCOPE_FILES = 2000
MANIFEST_LINES = 40


def _n(path: Any) -> str:
    return PurePosixPath(str(path).replace("\\", "/").strip("/")).as_posix()


def _under(path: str, base: str) -> bool:
    return path == base or path.startswith(base.rstrip("/") + "/")


async def _files(vm, directory: str) -> dict[str, dict]:
    """Every file below `directory` (recursive): normalized path -> list_dir entry."""
    info = await vm.call("path_info", path=directory)
    if not isinstance(info, dict) or not info.get("exists") or not info.get("is_dir"):
        return {}
    entries = await vm.call("list_dir", path=directory, recursive=True)
    out: dict[str, dict] = {}
    for e in entries if isinstance(entries, list) else []:
        if isinstance(e, dict) and e.get("type") == "file":
            p = _n(e.get("path", ""))
            if PurePosixPath(p).name.casefold() not in _IGNORED:
                out[p] = e
    return out


def _new_name(name: str, prefix: str, suffix: str) -> str:
    p = PurePosixPath(name)
    return f"{prefix}{p.stem}{suffix}{p.suffix}"


async def expand_file_ops(vm, a: dict[str, Any]) -> dict[str, Any]:
    """Fill `expected_ops` / `preserved_files` from the current real state (idempotent per assertion)."""
    op = str(a.get("op") or "move").lower()
    src = a.get("source_dir")
    dst = a.get("destination_dir")
    ext = a.get("extension") or None
    recursive = bool(a.get("recursive"))
    prefix, suffix = a.get("rename_prefix") or "", a.get("rename_suffix") or ""

    def fail(message: str) -> dict[str, Any]:
        a["expected_ops"], a["preserved_files"], a["snapshot_error"] = [], None, message
        return {"op": op, "source_dir": src, "error": message}

    if op not in VALID_OPS:
        return fail(f"op must be one of {sorted(VALID_OPS)}, got {op!r}")
    if not isinstance(src, str) or not src.strip():
        return fail("source_dir is required")
    if op != "rename" and (not isinstance(dst, str) or not dst.strip()):
        return fail("destination_dir is required for move and copy")
    if op == "rename" and not (prefix or suffix):
        return fail("rename needs rename_prefix and/or rename_suffix")
    if ext is not None and not str(ext).startswith("."):
        return fail(f"extension must start with '.', got {ext!r}")
    src_n = _n(src)
    dst_n = _n(dst) if op != "rename" else None

    scope = await _files(vm, src_n)
    if dst_n and not _under(dst_n, src_n):
        scope.update(await _files(vm, dst_n))
    if len(scope) > MAX_SCOPE_FILES:
        return fail(f"scope too large to snapshot ({len(scope)} files)")

    candidates: dict[str, dict] = {}
    for p, e in scope.items():
        if not _under(p, src_n):
            continue
        if dst_n and _under(p, dst_n):          # already collected: not a source
            continue
        if not recursive and PurePosixPath(p).parent.as_posix() != src_n:
            continue
        if ext and not p.casefold().endswith(str(ext).casefold()):
            continue
        candidates[p] = e

    ops, problems, seen = [], [], {}
    for p in sorted(candidates):
        name = PurePosixPath(p).name
        if op == "rename":
            target = (PurePosixPath(p).parent / _new_name(name, prefix, suffix)).as_posix()
        else:
            target = f"{dst_n}/{name}"
        if target in seen:
            problems.append(f"{p} and {seen[target]} would both become {target}")
        elif target in scope and target not in candidates:
            problems.append(f"destination already exists: {target}")
        seen[target] = p
        e = candidates[p]
        ops.append({"op": op, "src": p, "dst": target, "sha256": e.get("sha256"), "size": e.get("size")})
    if problems:
        return fail("cannot build a collision-free plan: " + "; ".join(problems[:5]))

    sources = {o["src"] for o in ops}
    a["expected_ops"] = ops
    a["preserved_files"] = [{"path": p, "sha256": e.get("sha256")} for p, e in scope.items() if p not in sources]
    a["snapshot_error"] = None
    return {"op": op, "source_dir": src, "count": len(ops), "preserved": len(a["preserved_files"])}


async def snapshot_file_ops(vm, subgoal: dict[str, Any]) -> list[dict[str, Any]]:
    """Lazily expand every file_ops assertion of a subgoal ONCE, at the moment its worker starts."""
    spec = subgoal.get("check_spec")
    events = []
    for a in (spec.get("checks") if isinstance(spec, dict) else None) or []:
        if isinstance(a, dict) and a.get("kind") == "file_ops" and a.get("expected_ops") is None:
            try:
                events.append({"subgoal_id": subgoal.get("id"), **await expand_file_ops(vm, a)})
            except Exception as exc:                       # no snapshot -> UNKNOWN, never a pass
                a["expected_ops"], a["snapshot_error"] = [], f"{type(exc).__name__}: {str(exc)[:160]}"
                events.append({"subgoal_id": subgoal.get("id"), "error": a["snapshot_error"]})
    return events


def describe_ops(a: dict[str, Any]) -> str:
    """Worker-facing manifest of the exact operations (from the orchestrator's snapshot)."""
    if a.get("snapshot_error"):
        return (f"The operation contract for {a.get('source_dir')} could not be built "
                f"({a['snapshot_error']}). Change nothing and report this.")
    ops = a.get("expected_ops") or []
    if not ops:
        return (f"No files in {a.get('source_dir')} match this operation"
                f"{' (' + a['extension'] + ')' if a.get('extension') else ''}. Change nothing and report that nothing matched.")
    shown = [f"- {o['op']} {o['src']} -> {o['dst']}" for o in ops[:MANIFEST_LINES]]
    if len(ops) > MANIFEST_LINES:
        shown.append(f"- ... and {len(ops) - MANIFEST_LINES} more, following the same pattern")
    return ("Planned file operations (exact, computed by the orchestrator from the real files):\n"
            + "\n".join(shown)
            + "\nPerform exactly these and nothing else. Use move_files/copy_files when all files come from one "
              "directory, otherwise move_path/copy_path one file at a time. Never move or copy a whole folder; "
              "keep file names exactly as listed.")


def _cap(items: list[str], n: int = 6) -> str:
    items = sorted(items)
    return str(items[:n]) + (f" (+{len(items) - n} more)" if len(items) > n else "")


async def check_file_ops(vm, a: dict[str, Any]) -> tuple[str, str]:
    """(status, detail) for one file_ops assertion. FAIL details are deterministic evidence."""
    label = f"file_ops {a.get('op') or 'move'}" + (f" {a['extension']}" if a.get("extension") else "")
    if a.get("snapshot_error"):
        return "unknown", f"{label}: cannot verify ({a['snapshot_error']})"
    ops = a.get("expected_ops")
    if ops is None:
        return "unknown", f"{label}: the operation contract was never expanded"
    if not ops:
        return "unknown", f"{label}: no matching files existed when the worker started; nothing to verify"

    op = str(a.get("op") or "move").lower()
    src_n = _n(a["source_dir"])
    now = await _files(vm, src_n)
    if op != "rename" and a.get("destination_dir"):
        now.update(await _files(vm, _n(a["destination_dir"])))

    missing, changed, remaining, copy_lost = [], [], [], []
    for o in ops:
        got = now.get(o["dst"])
        if got is None:
            missing.append(o["dst"])
        elif o.get("sha256") and got.get("sha256") != o["sha256"]:
            changed.append(o["dst"])
        s = now.get(o["src"])
        if op in {"move", "rename"} and s is not None:
            remaining.append(o["src"])
        if op == "copy" and (s is None or (o.get("sha256") and s.get("sha256") != o["sha256"])):
            copy_lost.append(o["src"])

    lost, altered = [], []
    for kept in a.get("preserved_files") or []:
        cur = now.get(_n(kept.get("path")))
        if cur is None:
            lost.append(kept.get("path"))
        elif kept.get("sha256") and cur.get("sha256") != kept["sha256"]:
            altered.append(kept.get("path"))
    known = {_n(k.get("path")) for k in a.get("preserved_files") or []}
    known |= {o["src"] for o in ops} | {o["dst"] for o in ops}
    unexpected = [p for p in now if p not in known]

    parts = []
    if missing:
        parts.append(f"expected file(s) missing: {_cap(missing)}")
    if changed:
        parts.append(f"content changed (SHA-256 mismatch): {_cap(changed)}")
    if remaining:
        parts.append(f"source file(s) still present: {_cap(remaining)}")
    if copy_lost:
        parts.append(f"original(s) of a copy were removed or changed: {_cap(copy_lost)}")
    if lost:
        parts.append(f"COLLATERAL DAMAGE, other file(s) removed: {_cap(lost)}")
    if altered:
        parts.append(f"COLLATERAL DAMAGE, other file(s) modified: {_cap(altered)}")
    if unexpected:
        parts.append(f"unexpected new file(s): {_cap(unexpected)}")
    if parts:
        return "fail", f"{label}: " + "; ".join(parts)
    return "pass", f"{label}: verified {len(ops)} file(s); contents intact and nothing else in scope changed"
