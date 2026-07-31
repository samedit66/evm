from __future__ import annotations

from pathlib import Path

import pytest

from evm.errors import EvmError
from evm.lockfile import LockedPackage, LockFile, load_lock, serialize_lock


def test_load_lock_reports_missing_file(tmp_path: Path) -> None:
    with pytest.raises(EvmError, match=r"lock file not found.*run `evm update`"):
        load_lock(tmp_path / "Eiffel.lock")


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("not = [valid", "cannot parse"),
        ('format-version = 2\nmanifest-fingerprint = "abc"\n', "unsupported lock format 2"),
        ("format-version = 1\nmanifest-fingerprint = 7\n", "must be a string"),
        ('format-version = 1\nmanifest-fingerprint = "abc"\npackage = {}\n', "array of tables"),
        (
            'format-version = 1\nmanifest-fingerprint = "abc"\npackage = ["bad"]\n',
            r"package\[0\].*table",
        ),
    ],
)
def test_load_lock_rejects_invalid_document(
    tmp_path: Path,
    content: str,
    message: str,
) -> None:
    path = tmp_path / "Eiffel.lock"
    path.write_text(content)

    with pytest.raises(EvmError, match=message):
        load_lock(path)


def test_load_lock_rejects_non_utf8_content(tmp_path: Path) -> None:
    path = tmp_path / "Eiffel.lock"
    path.write_bytes(b"\xff")

    with pytest.raises(EvmError, match="cannot parse"):
        load_lock(path)


@pytest.mark.parametrize("field", ["name", "version", "source"])
@pytest.mark.parametrize("value", [None, "", 1])
def test_load_lock_requires_non_empty_package_identity(
    tmp_path: Path,
    field: str,
    value: object,
) -> None:
    package = {"name": "json", "version": "1.0.0", "source": "iron+repo"}
    if value is None:
        package.pop(field)
    else:
        package[field] = value
    path = _write_lock(tmp_path, _package_table(package))

    with pytest.raises(EvmError, match=rf"package\[0\].{field} must be a non-empty string"):
        load_lock(path)


@pytest.mark.parametrize(
    "dependencies",
    ["base", ["base", 1], [""]],
)
def test_load_lock_rejects_invalid_dependency_names(
    tmp_path: Path,
    dependencies: object,
) -> None:
    path = _write_lock(
        tmp_path,
        _package_table(
            {
                "name": "json",
                "version": "1.0.0",
                "source": "iron+repo",
                "dependencies": dependencies,
            }
        ),
    )

    with pytest.raises(EvmError, match=r"package\[0\].dependencies"):
        load_lock(path)


def test_load_lock_rejects_unknown_package_field(tmp_path: Path) -> None:
    path = _write_lock(
        tmp_path,
        _package_table(
            {
                "name": "json",
                "version": "1.0.0",
                "source": "iron+repo",
                "mystery": "value",
            }
        ),
    )

    with pytest.raises(EvmError, match=r"unknown Eiffel.lock field: package\[0\].mystery"):
        load_lock(path)


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("checksum", 1, "non-empty string"),
        ("checksum", "", "non-empty string"),
        ("development", "yes", "boolean"),
        ("patched", 1, "boolean"),
    ],
)
def test_load_lock_validates_optional_package_fields(
    tmp_path: Path,
    field: str,
    value: object,
    expected: str,
) -> None:
    path = _write_lock(
        tmp_path,
        _package_table(
            {
                "name": "json",
                "version": "1.0.0",
                "source": "iron+repo",
                field: value,
            }
        ),
    )

    with pytest.raises(EvmError, match=rf"package\[0\].{field} must be a {expected}"):
        load_lock(path)


def test_load_lock_rejects_duplicate_package_names(tmp_path: Path) -> None:
    package = {"name": "json", "version": "1.0.0", "source": "iron+repo"}
    path = _write_lock(tmp_path, _package_table(package) + _package_table(package))

    with pytest.raises(EvmError, match="duplicate package names"):
        load_lock(path)


def test_lock_serialization_is_sorted_and_round_trips_all_fields(tmp_path: Path) -> None:
    json = LockedPackage(
        name="json",
        version="1.0.0",
        source="iron+repo",
        dependencies=("base",),
        checksum="sha256:abc",
        requested="version:1",
        revision="a" * 40,
        tree="b" * 40,
        manifest="Eiffel.toml",
        ecf="json.ecf",
        subdir="library/json",
        path="../json",
        path_kind="external",
        distribution="gobo",
        library="json",
        archive="https://example.invalid/json.tar.bz2",
        development=True,
        patched=True,
    )
    base = LockedPackage(name="base", version="1.0.0", source="eiffelstudio")
    lock = LockFile("fingerprint", (json, base))
    path = tmp_path / "Eiffel.lock"

    path.write_bytes(serialize_lock(lock))

    assert path.read_text().index('name = "base"') < path.read_text().index('name = "json"')
    assert load_lock(path) == LockFile("fingerprint", (base, json))


def _write_lock(tmp_path: Path, packages: str) -> Path:
    path = tmp_path / "Eiffel.lock"
    path.write_text(f'format-version = 1\nmanifest-fingerprint = "abc"\n{packages}')
    return path


def _package_table(package: dict[str, object]) -> str:
    lines = ["[[package]]"]
    for key, value in package.items():
        if isinstance(value, str):
            rendered = f'"{value}"'
        elif isinstance(value, bool):
            rendered = str(value).lower()
        elif isinstance(value, list):
            items = (f'"{item}"' if isinstance(item, str) else str(item) for item in value)
            rendered = "[" + ", ".join(items) + "]"
        elif isinstance(value, dict):
            rendered = "{}"
        else:
            rendered = str(value)
        lines.append(f"{key} = {rendered}")
    return "\n".join(lines) + "\n"
