"""Produce sanitized disposable project snapshots.

Only controlled snapshots are ever mounted into untrusted executors.
Applying a snapshot to the live repository is always an owner CLI action.
"""

from __future__ import annotations

import difflib
import os
import shutil
import stat
from pathlib import Path

from .runtime import (
    ALLOWED_SUFFIXES, MAX_SNAPSHOT_BYTES, MAX_SOURCE_FILE, sensitive, valid_name
)


class SnapshotError(ValueError):
    pass


def project_path(projects: Path, name: str) -> Path:
    if not valid_name(name):
        raise SnapshotError("Invalid project name")
    root = projects.resolve(strict=True)
    path = root / name
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        raise SnapshotError("Invalid project directory")
    if path.exists() and path.resolve() != path:
        raise SnapshotError("Project path is redirected")
    return path


def _eligible(rel: Path, is_dir: bool) -> bool:
    if any(sensitive(part) for part in rel.parts):
        return False
    if is_dir:
        return True
    return rel.name == ".gitignore" or rel.suffix.lower() in ALLOWED_SUFFIXES


def inventory(root: Path):
    if not root.is_dir() or root.is_symlink():
        raise SnapshotError("Missing or linked workspace")
    total = 0
    files = {}
    for base, dirs, names in os.walk(root, followlinks=False):
        bp = Path(base)
        dirs[:] = [d for d in dirs if
                   _eligible((bp / d).relative_to(root), True)
                   and not (bp / d).is_symlink()]
        for name in names:
            src = bp / name
            rel = src.relative_to(root)
            if not _eligible(rel, False) or src.is_symlink():
                continue
            try:
                info = src.lstat()
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_SOURCE_FILE:
                    continue
                total += info.st_size
                if total > MAX_SNAPSHOT_BYTES:
                    raise SnapshotError("Project exceeds snapshot limit (100 MiB)")
                files[str(rel)] = src
            except OSError as exc:
                raise SnapshotError(f"Cannot inspect {rel}: {exc}") from exc
    return files


def create(
    source: Path,
    target: Path,
    *,
    dir_mode: int = 0o700,
    file_mode: int = 0o600,
):
    if target.exists():
        raise SnapshotError("Snapshot destination already exists")
    target.mkdir(parents=True, mode=dir_mode)
    target.chmod(dir_mode)
    for rel, src in inventory(source).items():
        dst = target / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.parent.chmod(dir_mode)
        # Ignore symlinks and refuse unusual objects. Workers cannot see host files.
        if not src.is_file() or src.is_symlink():
            continue
        with src.open("rb") as inp, dst.open("xb") as out:
            shutil.copyfileobj(inp, out, MAX_SOURCE_FILE + 1)
        dst.chmod(file_mode)


def diff(source: Path, scratch: Path):
    original = inventory(source)
    current = inventory(scratch)
    changed = []
    preview = []
    for rel in sorted(set(original) | set(current)):
        # Deliberately ignore deletions in the automated apply flow.
        if rel not in current:
            continue
        with current[rel].open("rb") as f:
            new = f.read(MAX_SOURCE_FILE + 1)
        if len(new) > MAX_SOURCE_FILE:
            raise SnapshotError(f"File too large: {rel}")
        old = original[rel].read_bytes() if rel in original else b""
        if new != old:
            changed.append(rel)
            try:
                text = "".join(difflib.unified_diff(
                    old.decode("utf-8").splitlines(True),
                    new.decode("utf-8").splitlines(True),
                    fromfile="a/" + rel, tofile="b/" + rel,
                ))
                preview.append(text[:12000])
            except UnicodeError:
                preview.append(f"Binary/invalid UTF-8 content: {rel}\n")
    return changed, ("\n".join(preview))[:35000]


def apply(source: Path, scratch: Path, *, allow_deletions: bool = False):
    """Trusted owner-command-only copyback from an isolated workspace.

    Deletion remains disabled unless an approved project contract explicitly
    allows it. Symlink substitution is refused at every destination.
    """
    original = inventory(source)
    current = inventory(scratch)
    changed, _ = diff(source, scratch)
    deleted = sorted(set(original) - set(current)) if allow_deletions else []
    root = source.resolve(strict=True)
    for rel in changed:
        src = scratch / rel
        dst = root / rel
        if src.is_symlink() or not src.is_file():
            raise SnapshotError(f"Bad source: {rel}")
        parent = dst.parent
        parent.mkdir(parents=True, exist_ok=True)
        if not parent.resolve().is_relative_to(root) or dst.is_symlink():
            raise SnapshotError(f"Unsafe destination: {rel}")
        temp = parent / (".ctx-agent-" + os.urandom(8).hex())
        with src.open("rb") as inp, temp.open("xb") as out:
            shutil.copyfileobj(inp, out)
        os.replace(temp, dst)
    for rel in deleted:
        dst = root / rel
        if dst.is_symlink() or not dst.is_file():
            raise SnapshotError(f"Unsafe deletion target: {rel}")
        if not dst.resolve().is_relative_to(root):
            raise SnapshotError(f"Deletion leaves project: {rel}")
        dst.unlink()
    return changed + deleted


def import_files(source: Path, destination: Path) -> list[str]:
    """User-invoked import into a newly initialized project.

    The caller must have owner permission to write the destination. This
    deliberately runs in the interactive CLI, not the background service,
    so the daemon never needs access to the user's home directory.
    """
    if not source.is_dir() or source.is_symlink():
        raise SnapshotError("Source must be a real project directory")
    if not destination.is_dir() or destination.is_symlink():
        raise SnapshotError("Destination must be a real project directory")
    files = inventory(source)
    for rel, src in files.items():
        output = destination / rel
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.is_symlink() or output.exists():
            raise SnapshotError("Refusing to overwrite: " + rel)
        with src.open("rb") as stream, output.open("xb") as copied:
            shutil.copyfileobj(stream, copied)
    return sorted(files)
