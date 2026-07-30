"""Filesystem operations shared by EVM components."""

from pathlib import Path


def atomic_write(path: Path, data: bytes) -> None:
    """Replace a file without exposing partially written content."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name(f".{path.name}.tmp")
    temporary_path.write_bytes(data)
    temporary_path.replace(path)
