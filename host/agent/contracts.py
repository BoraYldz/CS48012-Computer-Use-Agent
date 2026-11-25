"""Deletion contracts: bound what a delete subgoal may destroy, and prove nothing else was.

A `no_files_by_extension` assertion ("no .tmp files remain in Work") says what must be
GONE. It says nothing about what must SURVIVE, so a worker that deletes everything
satisfies it. Two deterministic guards close that gap:

  1. prevention  - the worker's delete_path is scoped to the contract; an out-of-contract
                   path is refused BEFORE the human is ever asked to approve it.
  2. detection   - a plan-time snapshot of the surviving files (`preserved_files`) is
                   re-verified by the checker (presence + SHA-256), so collateral damage
                   fails the subgoal even if a path slipped past the scope.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Any


def _norm(path: str) -> PurePosixPath:
    return PurePosixPath(str(path).replace("\\", "/").lstrip("./") or ".")


def deletion_contracts(spec: Any) -> list[dict[str, Any]]:
    checks = spec.get("checks", []) if isinstance(spec, dict) else []
    return [a for a in checks if isinstance(a, dict) and a.get("kind") == "no_files_by_extension"
            and isinstance(a.get("path"), str) and isinstance(a.get("extension"), str)]


def delete_allowed(contracts: list[dict[str, Any]], path: str, recursive: bool = False) -> bool:
    """True only for a single FILE with the contract's extension inside the contract directory."""
    if recursive or not isinstance(path, str) or ".." in _norm(path).parts:
        return False
    target = _norm(path)
    for c in contracts:
        base, ext = _norm(c["path"]), c["extension"].casefold()
        in_scope = base in target.parents if c.get("recursive") else target.parent == base
        if in_scope and target.name.casefold().endswith(ext):
            return True
    return False


def describe(contracts: list[dict[str, Any]]) -> str:
    return "; ".join(f"only files ending in {c['extension']} "
                     f"{'anywhere under' if c.get('recursive') else 'directly in'} {c['path']}" for c in contracts)


async def snapshot_deletion_contracts(vm, subgoals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Record every file that must SURVIVE each deletion subgoal (before any worker runs)."""
    out = []
    for sg in subgoals:
        for a in deletion_contracts(sg.get("check_spec")):
            try:
                entries = await vm.call("list_dir", path=a["path"], recursive=bool(a.get("recursive")))
                ext = a["extension"].casefold()
                keep = [{"path": e["path"], "sha256": e.get("sha256")} for e in entries
                        if isinstance(e, dict) and e.get("type") == "file"
                        and not str(e.get("path", "")).casefold().endswith(ext)]
                a["preserved_files"] = keep
                out.append({"subgoal_id": sg.get("id"), "path": a["path"], "preserved": len(keep)})
            except Exception as exc:  # no snapshot -> only the "gone" half is verifiable
                a["preserved_files"] = None
                out.append({"subgoal_id": sg.get("id"), "path": a["path"], "error": f"{type(exc).__name__}: {str(exc)[:160]}"})
    return out
