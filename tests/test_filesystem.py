from pathlib import Path

import pytest

from evm.filesystem import atomic_write, atomic_write_many


def test_atomic_write_replaces_content_without_leaving_temporary_file(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "state.toml"
    destination.write_bytes(b"old")

    atomic_write(destination, b"new")

    assert destination.read_bytes() == b"new"
    assert not (tmp_path / ".state.toml.tmp").exists()


def test_atomic_write_many_restores_every_file_when_commit_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.write_bytes(b"old-first")
    second.write_bytes(b"old-second")
    original_replace = Path.replace

    def fail_second_staged_replace(source: Path, target: Path) -> Path:
        if (
            Path(target) == second
            and source.name.startswith(".second.")
            and ".backup." not in source.name
        ):
            raise OSError("simulated commit failure")
        return original_replace(source, target)

    monkeypatch.setattr(Path, "replace", fail_second_staged_replace)

    with pytest.raises(OSError, match="simulated"):
        atomic_write_many({first: b"new-first", second: b"new-second"})

    assert first.read_bytes() == b"old-first"
    assert second.read_bytes() == b"old-second"
