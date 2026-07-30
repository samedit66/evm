"""Filesystem operations shared by EVM components."""

import os
import tempfile
from pathlib import Path


def atomic_write(path: Path, data: bytes) -> None:
    """Replace a file without exposing partially written content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_bytes(data)
    temporary_path.replace(path)


def atomic_write_many(files: dict[Path, bytes]) -> None:
    """Replace related files as one recoverable filesystem transaction."""
    staged: dict[Path, Path] = {}
    backups: dict[Path, Path | None] = {}
    committed: list[Path] = []
    try:
        for path, data in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_name = tempfile.mkstemp(
                prefix=f".{path.name}.",
                dir=path.parent,
            )
            temporary = Path(temporary_name)
            with open(descriptor, "wb", closefd=True) as stream:
                stream.write(data)
            staged[path] = temporary
        for path in files:
            if path.exists():
                descriptor, backup_name = tempfile.mkstemp(
                    prefix=f".{path.name}.backup.",
                    dir=path.parent,
                )
                os.close(descriptor)
                backup = Path(backup_name)
                backup.unlink()
                path.replace(backup)
                backups[path] = backup
            else:
                backups[path] = None
            staged[path].replace(path)
            committed.append(path)
        for backup in backups.values():
            if backup is not None:
                backup.unlink(missing_ok=True)
    except OSError:
        for path in reversed(committed):
            path.unlink(missing_ok=True)
            backup = backups[path]
            if backup is not None and backup.exists():
                backup.replace(path)
        for path, backup in backups.items():
            if path not in committed and backup is not None and backup.exists():
                backup.replace(path)
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
