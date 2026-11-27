"""Snapshot and rollback of the local allowed root (Reflexion trials). No LLM, local mode only.

The copy lives outside the root. Rollback restores only differences (files and folders, empty ones
included) and verifies the result against a SHA-256 manifest. Links and special files are unsupported.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import stat
import tempfile
from dataclasses import dataclass
from pathlib import Path

MAX_FILES = 5000
MAX_BYTES = 200 * 1024 * 1024
LISTED_PATHS = 10


class RollbackError(Exception):
    """The root cannot be snapshotted, or cannot be restored exactly."""


def _sha256(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _scan(root: Path) -> dict[str, int | None]:
    """Relative posix path -> file size, or None for a folder. Never follows links."""
    entries: dict[str, int | None] = {}
    pending = [root]
    while pending:
        with os.scandir(pending.pop()) as listing:
            for entry in listing:
                path = Path(entry.path)
                rel = path.relative_to(root).as_posix()
                if entry.is_symlink() or os.path.isjunction(entry.path):
                    raise RollbackError(f"symlink or junction in root: {rel}")
                if entry.is_dir(follow_symlinks=False):
                    entries[rel] = None
                    pending.append(path)
                elif entry.is_file(follow_symlinks=False):
                    entries[rel] = entry.stat(follow_symlinks=False).st_size
                else:
                    raise RollbackError(f"special file in root: {rel}")
    return entries


def _remove(path: Path) -> None:
    if path.is_dir():
        path.rmdir()          # never recursive: only listed items are removed
        return
    try:
        path.unlink()
    except PermissionError:   # Windows refuses to delete read-only files
        os.chmod(path, path.stat().st_mode | stat.S_IWRITE)
        path.unlink()


def _make_writable_and_retry(func, path, _exc) -> None:
    os.chmod(path, os.stat(path).st_mode | stat.S_IWRITE)
    func(path)


@dataclass(frozen=True)
class Diff:
    added: tuple[str, ...]      # not in the snapshot: will be removed
    changed: tuple[str, ...]    # different content or type: will be overwritten
    missing: tuple[str, ...]    # gone: will be recreated

    def __bool__(self) -> bool:
        return bool(self.added or self.changed or self.missing)

    def counts(self) -> dict[str, int]:
        return {"added": len(self.added), "changed": len(self.changed), "missing": len(self.missing)}

    def summary(self, root: Path) -> str:
        lines = [f"ROLLBACK {root} to its state before the run (edits made by hand since then are reverted too)"]
        for label, paths in (("remove", self.added), ("overwrite", self.changed), ("recreate", self.missing)):
            if paths:
                more = f" (+{len(paths) - LISTED_PATHS} more)" if len(paths) > LISTED_PATHS else ""
                lines.append(f"    {label} {len(paths)}: {', '.join(paths[:LISTED_PATHS])}{more}")
        return "\n".join(lines)


@dataclass(frozen=True)
class Snapshot:
    root: Path
    store: Path
    manifest: dict[str, str | None]   # relative path -> SHA-256, or None for a folder
    total_bytes: int

    @classmethod
    def take(cls, root: Path) -> Snapshot:
        entries = _scan(root)
        sizes = [size for size in entries.values() if size is not None]
        if len(sizes) > MAX_FILES:
            raise RollbackError(f"too many files to snapshot: {len(sizes)} > {MAX_FILES}")
        if sum(sizes) > MAX_BYTES:
            raise RollbackError(f"root too large to snapshot: {sum(sizes)} > {MAX_BYTES} bytes")
        store = Path(tempfile.mkdtemp(prefix="cua-snapshot-")).resolve()
        try:
            if store.is_relative_to(root):
                raise RollbackError("temp directory is inside the allowed root")
            manifest: dict[str, str | None] = {}
            for rel in sorted(entries):               # parents sort before their children
                if entries[rel] is None:
                    (store / rel).mkdir()
                    manifest[rel] = None
                else:
                    shutil.copy2(root / rel, store / rel)
                    manifest[rel] = _sha256(root / rel)
        except BaseException:
            shutil.rmtree(store, onexc=_make_writable_and_retry)
            raise
        return cls(root, store, manifest, sum(sizes))

    def stats(self) -> dict[str, int]:
        files = sum(1 for digest in self.manifest.values() if digest is not None)
        return {"files": files, "folders": len(self.manifest) - files, "bytes": self.total_bytes}

    def diff(self) -> Diff:
        current = {rel: None if size is None else _sha256(self.root / rel)
                   for rel, size in _scan(self.root).items()}
        saved = self.manifest
        return Diff(
            added=tuple(sorted(current.keys() - saved.keys())),
            changed=tuple(sorted(r for r in current.keys() & saved.keys() if current[r] != saved[r])),
            missing=tuple(sorted(saved.keys() - current.keys())),
        )

    def restore(self, listed: Diff) -> None:
        """Apply exactly the listed differences, then verify the whole root."""
        if self.diff() != listed:
            raise RollbackError("root changed after the differences were listed; nothing restored")
        rewrite = sorted(listed.changed + listed.missing)
        for rel in rewrite:                           # check the copy before touching the root
            if self.manifest[rel] is not None and _sha256(self.store / rel) != self.manifest[rel]:
                raise RollbackError(f"snapshot copy is corrupt: {rel}")
        for rel in sorted(listed.added + listed.changed, reverse=True):     # children first
            _remove(self.root / rel)
        for rel in rewrite:                                                 # parents first
            if self.manifest[rel] is None:
                (self.root / rel).mkdir()
            else:
                shutil.copy2(self.store / rel, self.root / rel)
        left = self.diff()
        if left:
            raise RollbackError(f"manifest mismatch after restore: {left.counts()}")

    def discard(self) -> None:
        if self.store.exists():
            shutil.rmtree(self.store, onexc=_make_writable_and_retry)