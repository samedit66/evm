from __future__ import annotations

from pathlib import Path

import pytest

from evm.errors import EvmError
from evm.manifest import load_manifest, parse_manifest
from evm.project import (
    create_project,
    effective_sources,
    target_chain,
    validate_configuration,
)


def test_manifest_rejects_unknown_requires_key(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(path.read_text() + '\n[requires]\ndebugger = "yes"\n')

    with pytest.raises(EvmError, match="allowed keys"):
        load_manifest(path)


@pytest.mark.parametrize(
    ("section", "declaration"),
    [
        ("dependencies", 'base = { source = "ise" }'),
        ("dependencies", 'eiffel_base = { source = "ise", library = "base" }'),
        ("dependencies", 'free_elks = { source = "gobo", library = "free_elks" }'),
        ("dependencies", 'runtime = { source = "gobo", library = "free_elks" }'),
        ("dev-dependencies", 'runtime = { source = "ise", library = "base" }'),
    ],
)
def test_manifest_rejects_implicit_runtime_dependencies(
    tmp_path: Path,
    section: str,
    declaration: str,
) -> None:
    project = create_project(tmp_path / "hello")
    project.manifest_path.write_text(
        project.manifest_path.read_text() + f"\n[{section}]\n{declaration}\n"
    )

    with pytest.raises(EvmError, match=r"runtime library .* EVM provides automatically"):
        load_manifest(project.manifest_path)


@pytest.mark.parametrize(
    "declaration",
    [
        'base = { git = "https://example.invalid/base.git", tag = "v1" }',
        'base = { source = "ise", library = "time" }',
        'free_elks = { source = "gobo", library = "xml" }',
    ],
)
def test_manifest_rejects_reserved_runtime_group_names(
    tmp_path: Path,
    declaration: str,
) -> None:
    project = create_project(tmp_path / "hello")
    project.manifest_path.write_text(
        project.manifest_path.read_text() + f"\n[dependencies]\n{declaration}\n"
    )

    with pytest.raises(EvmError, match=r"dependency name .* is reserved"):
        load_manifest(project.manifest_path)


@pytest.mark.parametrize(
    "declaration",
    [
        'foundation = { git = "https://example.invalid/base.git", tag = "v1" }',
        'time_adapter = { source = "ise", library = "time" }',
        'xml_adapter = { source = "gobo", library = "xml" }',
    ],
)
def test_manifest_allows_non_runtime_dependency_aliases(
    tmp_path: Path,
    declaration: str,
) -> None:
    project = create_project(tmp_path / "hello")
    project.manifest_path.write_text(
        project.manifest_path.read_text() + f"\n[dependencies]\n{declaration}\n"
    )

    loaded = load_manifest(project.manifest_path)

    assert len(loaded.dependencies) == 1


@pytest.mark.parametrize("constraint", ["^25.12", "~25.12", "25.12", ">=25.x"])
def test_manifest_rejects_invalid_version_constraints(tmp_path: Path, constraint: str) -> None:
    project = create_project(tmp_path / constraint.replace(".", "_").replace(">", "x"))
    path = project.manifest_path
    path.write_text(path.read_text() + f'\n[compatibility]\ncompilers = ["ise {constraint}"]\n')

    with pytest.raises(EvmError, match="invalid version constraint"):
        load_manifest(path)


def test_target_inheritance_is_normalized(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(
        path.read_text()
        + '\n[targets.server]\nextends = "default"\n'
        + 'root = "SERVER.start"\nsources = ["server"]\n'
    )
    (project.directory / "server").mkdir()

    loaded = load_manifest(path)

    assert [item.name for item in target_chain(loaded, "server")] == [
        "default",
        "server",
    ]
    assert effective_sources(loaded, "server") == ("src", "server")
    assert validate_configuration(loaded) == []


def test_manifest_uses_declared_default_target(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(
        path.read_text()
        .replace("ecf-managed = true\n", 'ecf-managed = true\ndefault-target = "app"\n')
        .replace('extends = "default"', 'extends = "app"')
    )

    loaded = load_manifest(path)

    assert loaded.default_target == "app"
    assert loaded.target("app").sources == ("src",)


def test_target_cycle_is_rejected(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(
        path.read_text() + '\n[targets.a]\nextends = "b"\n' + '\n[targets.b]\nextends = "a"\n'
    )

    with pytest.raises(EvmError, match="cycle"):
        load_manifest(path)


def test_condition_values_are_closed_where_portability_requires_it(
    tmp_path: Path,
) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(
        path.read_text() + '\n[[conditions]]\nwhen = { compiler = "gec" }\n' + 'sources = ["src"]\n'
    )

    with pytest.raises(EvmError, match="allowed: gobo, ise"):
        load_manifest(path)


def test_parse_manifest_reports_invalid_toml_with_source_path(tmp_path: Path) -> None:
    manifest = tmp_path / "Eiffel.toml"

    with pytest.raises(EvmError, match=r"cannot parse .*Eiffel\.toml"):
        parse_manifest("[project", manifest)


def test_managed_ecf_must_stay_inside_project(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(path.read_text().replace('ecf = "hello.ecf"', 'ecf = "../outside.ecf"'))

    with pytest.raises(EvmError, match="must stay inside"):
        load_manifest(path)


def test_ecf_include_must_stay_inside_project(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(path.read_text() + '\n[ecf]\ninclude = ["../outside.xml"]\n')

    with pytest.raises(EvmError, match=r"ecf\.include\[0\] must stay inside"):
        load_manifest(path)


def test_release_is_a_valid_explicit_target_name(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(path.read_text() + '\n[targets.release]\nextends = "default"\n')

    loaded = load_manifest(path)

    assert loaded.target("release").extends == "default"


def test_manifest_parses_explicit_autotest_runner(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(
        path.read_text()
        + '\n[targets.test]\nroot = "APPLICATION.make"\nsources = ["tests"]\n'
        + '\n[test]\ntarget = "test"\nrunner = "autotest"\n'
    )

    loaded = load_manifest(path)

    assert loaded.test is not None
    assert loaded.test.runner == "autotest"


def test_manifest_rejects_unknown_test_runner(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    path = project.manifest_path
    path.write_text(
        path.read_text()
        + '\n[targets.test]\nroot = "APPLICATION.make"\nsources = ["tests"]\n'
        + '\n[test]\ntarget = "test"\nrunner = "unknown"\n'
    )

    with pytest.raises(EvmError, match=r"test\.runner must be one of"):
        load_manifest(path)


@pytest.mark.parametrize(
    ("addition", "message"),
    [
        ("unknown = 1\n", "unknown manifest field"),
        ("package = 1\n", "package must be a table"),
        ("[package]\niron = 1\n", "package.iron must be a table"),
        ("[package]\nlinks = 1\n", "package.links must be a table"),
        ("test = 1\n", "test must be a table"),
        ("scripts = 1\n", "scripts must be a table"),
        ("workspace = 1\n", "workspace must be a table"),
        ("compatibility = 1\n", "compatibility must be a table"),
        ("toolchain = 1\n", "toolchain must be a table"),
        ("requires = 1\n", "requires must be a table"),
        ("conditions = 1\n", "conditions must use"),
        ("compiler = 1\n", "compiler must be a table"),
        ("ecf = 1\n", "ecf must be a table"),
        ("dependencies = 1\n", "dependencies must be a table"),
        ("patch = 1\n", "patch must be a table"),
        ('[test]\ntarget = "missing"\n', "unknown target"),
        ("[scripts]\n'bad name' = \"check\"\n", "not a valid task name"),
        ("[scripts]\nci = 1\n", "command string or table"),
        ("[scripts.ci]\nsteps = []\n", "non-empty array"),
        ('[scripts]\nci = ""\n', "must contain one EVM command"),
        ('[scripts]\nci = "check && build"\n', "without shell operators"),
        (
            '[scripts.ci]\nsteps = [{ command = "check", release = 1 }]\n',
            "must be a string or boolean",
        ),
        (
            '[scripts.ci]\nsteps = [{ command = "task" }]\n',
            "unknown task",
        ),
        ('[targets.other]\nroot = "INVALID"\n', "must have the form"),
        ("[targets.other]\nextends = 1\n", "extends must be a string"),
        ('[targets.other]\nsources = ["src"]\n', "requires root or extends"),
        ('[compatibility]\ncompilers = ["other"]\n', "unknown compiler adapter"),
        (
            '[compatibility]\ncompilers = ["gobo", "gobo"]\n',
            "duplicate compiler adapter",
        ),
        ("[requires]\nconcurrency = 1\n", "must be a string"),
        ('[requires]\nconcurrency = "parallel"\n', "invalid requires"),
        ('[[conditions]]\ntarget = "missing"\nwhen = { os = "unix" }\n', "unknown target"),
        ('[[conditions]]\nwhen = {}\nsources = ["src"]\n', "non-empty inline"),
        (
            '[[conditions]]\nwhen = { unknown = "x" }\nsources = ["src"]\n',
            "allowed",
        ),
        ('[[conditions]]\nwhen = { os = 1 }\nsources = ["src"]\n', "non-empty string"),
        ('[[conditions]]\nwhen = { os = "plan9" }\nsources = ["src"]\n', "allowed"),
        ('[[conditions]]\nwhen = { os = "unix" }\n', "must define"),
        ("[compiler]\nother = {}\n", "unknown compiler adapter"),
        ("[compiler]\ngobo = 1\n", "compiler.gobo must be a table"),
        ("[dependencies]\n'bad name' = \"1.0\"\n", "invalid dependency name"),
        ("[dependencies]\nfoo = 1\n", "version string or inline table"),
        ('[dependencies]\nfoo = { source = "git", git = "x" }\n', "exactly one"),
        (
            '[dependencies]\nfoo = { source = "iron", version = "1", tag = "x" }\n',
            "only valid for Git",
        ),
        ('[dependencies]\nfoo = { source = "git", tag = "x" }\n', "git is required"),
        ('[dependencies]\nfoo = { source = "path" }\n', "path is required"),
        ('[dependencies]\nfoo = { source = "gobo" }\n', "library is required"),
        ('[dependencies]\nfoo = { source = "iron" }\n', "version is required"),
        ('[dependencies]\nfoo = { source = "other" }\n', "source must be one of"),
        (
            '[dependencies]\nfoo = { git = "x", path = "y", tag = "v" }\n',
            "conflicting dependency sources",
        ),
        ('[patch]\nfoo = { path = "local" }\n', "does not match"),
    ],
)
def test_manifest_rejects_invalid_section_boundaries(
    tmp_path: Path,
    addition: str,
    message: str,
) -> None:
    project = create_project(tmp_path / "hello")
    project.manifest_path.write_text(addition + "\n" + project.manifest_path.read_text())

    with pytest.raises(EvmError, match=message):
        load_manifest(project.manifest_path)


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('name = "hello"', 'name = "1bad"', "project.name"),
        ('version = "0.1.0"', 'version = "one"', "project.version"),
        ('type = "application"', 'type = "unknown"', "project.type"),
        (
            "uuid = ",
            'uuid = "not-a-uuid" # ',
            "project.uuid",
        ),
        ("ecf-managed = true", 'ecf-managed = "yes"', "ecf-managed"),
        ('ecf = "hello.ecf"', 'ecf = ""', "project.ecf"),
    ],
)
def test_manifest_rejects_invalid_project_metadata(
    tmp_path: Path,
    old: str,
    new: str,
    message: str,
) -> None:
    project = create_project(tmp_path / "hello")
    content = project.manifest_path.read_text()
    if old == "uuid = ":
        line = next(line for line in content.splitlines() if line.startswith("uuid = "))
        content = content.replace(line, new + line)
    else:
        content = content.replace(old, new)
    project.manifest_path.write_text(content)

    with pytest.raises(EvmError, match=message):
        load_manifest(project.manifest_path)
