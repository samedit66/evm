from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from evm.cli import main
from evm.errors import EvmError
from evm.lockfile import empty_lock
from evm.manifest import parse_manifest
from evm.model import Project, Task, TaskStep
from evm.tasks import run_task
from evm.testing import TestRequest as WorkflowTestRequest
from evm.testing import test_project as run_project_tests
from evm.toolchains import Toolchain
from evm.versioning import NumericVersion
from evm.workspace import load_workspace, workspace_tree_lines


def test_manifest_parses_structured_and_short_tasks(tmp_path: Path) -> None:
    project = parse_manifest(
        _manifest()
        + """
[scripts]
serve = "run --target server"

[scripts.ci]
steps = [
    { command = "check", configuration-only = true },
    { command = "build", release = true },
]
""",
        tmp_path / "Eiffel.toml",
    )

    assert project.tasks == (
        Task("serve", (TaskStep(command="run", arguments=("--target", "server")),)),
        Task(
            "ci",
            (
                TaskStep(command="check", arguments=("--configuration-only",)),
                TaskStep(command="build", arguments=("--release",)),
            ),
        ),
    )


@pytest.mark.parametrize(
    "command",
    [
        "check && test",
        "check|test",
        "check > result.txt",
        "check $EVM_OPTIONS",
        "check $(other-command)",
    ],
)
def test_short_task_rejects_shell_syntax(tmp_path: Path, command: str) -> None:
    with pytest.raises(EvmError, match="without shell operators"):
        parse_manifest(
            _manifest() + f'\n[scripts]\nunsafe = "{command}"\n',
            tmp_path / "Eiffel.toml",
        )


def test_task_detects_recursion(tmp_path: Path) -> None:
    project = parse_manifest(
        _manifest() + '\n[scripts]\nfirst = "task second"\nsecond = "task first"\n',
        tmp_path / "Eiffel.toml",
    )

    with pytest.raises(EvmError, match="recursive task invocation"):
        run_task(project, "first", lambda arguments: 0)


def test_shell_task_requires_explicit_permission(tmp_path: Path) -> None:
    project = parse_manifest(
        _manifest()
        + """
[scripts.generate]
steps = [{ shell = "exit 0" }]
""",
        tmp_path / "Eiffel.toml",
    )

    with pytest.raises(EvmError, match="--allow-build-scripts"):
        run_task(project, "generate", lambda arguments: 0)

    run_task(
        project,
        "generate",
        lambda arguments: 0,
        allow_build_scripts=True,
    )


def test_test_target_is_inferred_when_test_sources_exist(tmp_path: Path) -> None:
    tests = tmp_path / "tests"
    tests.mkdir()
    (tests / "test_application.e").write_text("class TEST_APPLICATION end")

    project = parse_manifest(_manifest(), tmp_path / "Eiffel.toml")

    assert project.test is not None
    assert project.test.target == "test"
    assert project.target("test").extends == "default"
    assert project.target("test").sources == ("tests",)


def test_test_target_adapter_preserves_runner_exit_code(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = parse_manifest(
        _manifest()
        + """
[targets.test]
root = "TEST_APPLICATION.make"
sources = ["tests"]
""",
        tmp_path / "Eiffel.toml",
    )
    executable = tmp_path / "tests-runner"
    executable.touch()
    toolchain = Toolchain(
        "ise",
        Path("/tools/ec"),
        NumericVersion.parse("25.12"),
        "explicit",
        "--compiler",
    )
    monkeypatch.setattr("evm.testing.select_toolchain", lambda project, compiler: toolchain)
    monkeypatch.setattr(
        "evm.testing.compile_project",
        lambda project, request, announce: toolchain,
    )
    monkeypatch.setattr(
        "evm.testing.artifact_candidates",
        lambda toolchain, project, request: (executable,),
    )
    monkeypatch.setattr(
        "evm.testing.subprocess.run",
        lambda *arguments, **options: SimpleNamespace(returncode=7),
    )

    result = run_project_tests(project, WorkflowTestRequest(compiler="ise"))

    assert result.status == "failed"
    assert result.exit_code == 7
    assert result.runner == "test-target"


def test_test_filter_is_rejected_when_adapter_cannot_apply_it(
    tmp_path: Path,
    monkeypatch,
) -> None:
    project = parse_manifest(
        _manifest()
        + """
[targets.test]
root = "TEST_APPLICATION.make"
sources = ["tests"]
""",
        tmp_path / "Eiffel.toml",
    )
    toolchain = Toolchain(
        "ise",
        Path("/tools/ec"),
        NumericVersion.parse("25.12"),
        "explicit",
        "--compiler",
    )
    monkeypatch.setattr("evm.testing.select_toolchain", lambda project, compiler: toolchain)

    with pytest.raises(EvmError, match="filter unsupported"):
        run_project_tests(
            project,
            WorkflowTestRequest(compiler="ise", class_name="STRING_TESTS"),
        )


def test_workspace_orders_dependencies_and_uses_shared_state(tmp_path: Path) -> None:
    core = tmp_path / "packages" / "core"
    compiler = tmp_path / "apps" / "compiler"
    _write_package(core, "core")
    _write_package(
        compiler,
        "compiler",
        dependencies='core = { path = "../../packages/core" }',
    )
    workspace_manifest = tmp_path / "Eiffel.toml"
    workspace_manifest.write_text('[workspace]\nmembers = ["apps/compiler", "packages/core"]\n')

    workspace = load_workspace(workspace_manifest)

    assert workspace is not None
    assert [package.name for package in workspace.ordered_packages()] == [
        "core",
        "compiler",
    ]
    assert workspace_tree_lines(workspace) == ["compiler", "└── core"]
    assert all(package.state_directory == tmp_path / ".evm" for package in workspace.packages)


def test_workspace_rejects_dependency_cycle(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_package(first, "first", dependencies='second = { path = "../second" }')
    _write_package(second, "second", dependencies='first = { path = "../first" }')
    manifest = tmp_path / "Eiffel.toml"
    manifest.write_text('[workspace]\nmembers = ["first", "second"]\n')
    workspace = load_workspace(manifest)
    assert workspace is not None

    with pytest.raises(EvmError, match="workspace dependency cycle"):
        workspace.ordered_packages()


def test_workspace_dependency_ecf_path_is_relative_to_package(tmp_path: Path) -> None:
    package_directory = tmp_path / "packages" / "app"
    _write_package(package_directory, "app")
    manifest = tmp_path / "Eiffel.toml"
    manifest.write_text('[workspace]\nmembers = ["packages/app"]\n')
    workspace = load_workspace(manifest)
    assert workspace is not None
    project = workspace.package("app")

    assert project.state_directory == tmp_path / ".evm"
    assert empty_lock(project).packages == ()


def test_check_json_is_machine_readable(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "hello"
    runner = CliRunner()
    assert runner.invoke(main, ["new", str(project)]).exit_code == 0
    monkeypatch.chdir(project)

    result = runner.invoke(main, ["check", "--configuration-only", "--json"])

    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == {
        "packages": [{"package": "hello", "status": "passed"}],
        "status": "passed",
    }


def test_task_command_runs_nested_evm_command(tmp_path: Path, monkeypatch) -> None:
    project = tmp_path / "hello"
    runner = CliRunner()
    assert runner.invoke(main, ["new", str(project)]).exit_code == 0
    manifest = project / "Eiffel.toml"
    manifest.write_text(manifest.read_text() + '\n[scripts]\nci = "check --configuration-only"\n')
    monkeypatch.chdir(project)

    result = runner.invoke(main, ["task", "ci"])

    assert result.exit_code == 0, result.output
    assert "hello: project check completed." in result.output
    assert "Task 'ci' completed." in result.output


def _manifest(name: str = "sample") -> str:
    return f"""
[project]
name = "{name}"
version = "0.1.0"
type = "application"
uuid = "00000000-0000-4000-8000-000000000000"

[root]
class = "APPLICATION"
feature = "make"

[sources]
clusters = ["src"]

[targets.server]
root = "APPLICATION.make"
sources = ["src"]
"""


def _write_package(
    directory: Path,
    name: str,
    *,
    dependencies: str | None = None,
) -> Project:
    directory.mkdir(parents=True)
    (directory / "src").mkdir()
    content = _manifest(name)
    if dependencies is not None:
        content += f"\n[dependencies]\n{dependencies}\n"
    manifest = directory / "Eiffel.toml"
    manifest.write_text(content)
    return parse_manifest(content, manifest)
