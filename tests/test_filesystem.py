from pathlib import Path

from evm.filesystem import atomic_write


def test_atomic_write_replaces_content_without_leaving_temporary_file(
    tmp_path: Path,
) -> None:
    destination = tmp_path / "state.toml"
    destination.write_bytes(b"old")

    atomic_write(destination, b"new")

    assert destination.read_bytes() == b"new"
    assert not (tmp_path / ".state.toml.tmp").exists()
