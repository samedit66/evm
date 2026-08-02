from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from evm.autotest import AutoTestCase, AutoTestDiagnostic, AutoTestExecution
from evm.cli import main
from evm.errors import EvmError
from evm.lockfile import empty_lock
from evm.manifest import parse_manifest
from evm.model import Project, Task, TaskStep
from evm.tasks import run_task
from evm.testing import TestRequest as WorkflowTestRequest
from evm.testing import _find_assertion_line, _parse_getest_summary
from evm.testing import test_project as run_project_tests
from evm.toolchains import Toolchain
from evm.versioning import NumericVersion
from evm.workspace import (
    is_workspace_member_dependency,
    load_project_context,
    load_workspace,
    workspace_tree_lines,
)


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
    process_options = []
    monkeypatch.setattr(
        "evm.testing.subprocess.run",
        lambda *arguments, **options: (
            process_options.append(options)
            or SimpleNamespace(
                returncode=7,
                stdout=(
                    "failure at TEST_APPLICATION.make:12\n" if options["capture_output"] else None
                ),
                stderr="runtime stack trace\n" if options["capture_output"] else None,
            )
        ),
    )

    result = run_project_tests(project, WorkflowTestRequest(compiler="ise"))

    assert result.status == "failed"
    assert result.exit_code == 7
    assert result.runner == "test-target"
    assert result.stdout == "failure at TEST_APPLICATION.make:12\n"
    assert result.stderr == "runtime stack trace\n"
    assert result.as_dict()["stdout"] == "failure at TEST_APPLICATION.make:12\n"

    raw_result = run_project_tests(
        project,
        WorkflowTestRequest(compiler="ise", raw=True),
    )

    assert raw_result.exit_code == 7
    assert process_options[-1]["capture_output"] is False


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


def test_autotest_runner_reports_structured_counts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    tests_directory = tmp_path / "tests"
    tests_directory.mkdir()
    (tests_directory / "legacy_suite.e").write_text(
        """class STRING_TESTS
inherit EQA_TEST_SET
feature
    test_failure
        do
            assert ("expected_value", False)
        end
end
"""
    )
    project = parse_manifest(
        _manifest()
        + """
[targets.test]
root = "TEST_APPLICATION.make"
sources = ["tests"]

[test]
target = "test"
runner = "autotest"
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
    monkeypatch.setattr(
        "evm.testing.run_autotest",
        lambda *arguments: AutoTestExecution(
            4,
            2,
            1,
            1,
            diagnostics=(
                AutoTestDiagnostic(
                    AutoTestCase("STRING_TESTS", "test_failure"),
                    "failed",
                    assertion="expected_value",
                    exception_tag="assertion violated",
                    stderr="runtime diagnostic",
                    trace="trace line",
                    trace_valid=True,
                ),
                AutoTestDiagnostic(
                    AutoTestCase("STRING_TESTS", "test_unresolved"),
                    "unresolved",
                ),
            ),
        ),
    )

    result = run_project_tests(project, WorkflowTestRequest(compiler="ise"))

    assert result.status == "failed"
    assert result.exit_code == 1
    assert result.runner == "autotest"
    assert result.tests == 4
    assert result.passed == 2
    assert result.failed == 1
    assert result.unresolved == 1
    assert result.failed_tests == ("STRING_TESTS.test_failure",)
    assert result.unresolved_tests == ("STRING_TESTS.test_unresolved",)
    assert result.as_dict()["failed_tests"] == ["STRING_TESTS.test_failure"]
    assert result.as_dict()["test_details"][0]["assertion"] == "expected_value"
    assert result.as_dict()["test_details"][0]["exception_tag"] == "assertion violated"
    assert result.as_dict()["test_details"][0]["stderr"] == "runtime diagnostic"
    assert result.details[0].source_path == "tests/legacy_suite.e"
    assert result.details[0].source_line == 6
    assert result.details[0].source_text == 'assert ("expected_value", False)'


def test_assertion_source_line_requires_a_unique_literal_tag() -> None:
    lines = [
        'assert ("same_tag", first)',
        'assert ("unique_tag", value)',
        'assert ("same_tag", second)',
    ]

    assert _find_assertion_line(lines, "unique_tag") == 2
    assert _find_assertion_line(lines, "same_tag") is None
    assert _find_assertion_line(lines, None) is None


def test_getest_uses_matching_configuration_and_exact_filters(
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
    configuration = tmp_path / "getest.ge"
    configuration.touch()
    toolchain = Toolchain(
        "gobo",
        Path("/tools/gec"),
        NumericVersion.parse("26.07"),
        "explicit",
        "--compiler",
    )
    commands: list[list[str]] = []
    process_options = []
    monkeypatch.setattr("evm.testing.select_toolchain", lambda project, compiler: toolchain)
    monkeypatch.setattr("evm.testing.shutil.which", lambda executable: "/tools/getest")
    monkeypatch.setattr("evm.testing.prepare_project", lambda *arguments, **options: None)
    monkeypatch.setattr(
        "evm.testing.subprocess.run",
        lambda command, **options: (
            commands.append(command)
            or process_options.append(options)
            or SimpleNamespace(
                returncode=0,
                stdout="3 tests, 3 passed\n" if options["capture_output"] else None,
                stderr="" if options["capture_output"] else None,
            )
        ),
    )

    result = run_project_tests(
        project,
        WorkflowTestRequest(
            compiler="gobo",
            class_name="STRING.TESTS",
            feature="test_append+unicode",
        ),
    )

    assert result.runner == "getest"
    assert result.stdout == "3 tests, 3 passed\n"
    assert commands == [
        [
            "/tools/getest",
            str(configuration),
            r"--class=^STRING\.TESTS$",
            r"--feature=^test_append\+unicode$",
        ]
    ]

    raw_result = run_project_tests(
        project,
        WorkflowTestRequest(compiler="gobo", raw=True),
    )

    assert raw_result.runner == "getest"
    assert process_options[-1]["capture_output"] is False


def test_getest_is_not_selected_without_its_configuration(
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
        "gobo",
        Path("/tools/gec"),
        NumericVersion.parse("26.07"),
        "explicit",
        "--compiler",
    )
    monkeypatch.setattr("evm.testing.select_toolchain", lambda project, compiler: toolchain)
    monkeypatch.setattr("evm.testing.shutil.which", lambda executable: "/tools/getest")

    with pytest.raises(EvmError, match="filter unsupported"):
        run_project_tests(
            project,
            WorkflowTestRequest(compiler="gobo", feature="test_append"),
        )


def test_getest_summary_is_normalized() -> None:
    output = """
Test Summary for sample

# Passed: 3 tests
# FAILED: 1 test
# ABORTED: 2 tests
# Total: 6 tests (10 assertions)
"""

    assert _parse_getest_summary(output) == (6, 3, 1, 2)
    assert _parse_getest_summary("incomplete") is None
    assert _parse_getest_summary(None) is None


@pytest.mark.parametrize("runner", ["autotest", "target"])
def test_getest_only_options_report_incompatible_runner(
    tmp_path: Path,
    monkeypatch,
    runner: str,
) -> None:
    project = parse_manifest(
        _manifest()
        + f"""
[targets.test]
root = "TEST_APPLICATION.make"
sources = ["tests"]

[test]
target = "test"
runner = "{runner}"
""",
        tmp_path / "Eiffel.toml",
    )
    monkeypatch.setattr(
        "evm.testing.select_toolchain",
        lambda project, compiler: Toolchain(
            "ise",
            Path("/tools/ec"),
            NumericVersion.parse("25.12"),
            "explicit",
            "test",
        ),
    )

    with pytest.raises(EvmError, match=r"supported by.*getest|select the getest runner"):
        run_project_tests(project, WorkflowTestRequest(default_test=True))


def test_getest_only_option_requires_configuration_in_auto_mode(
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
    monkeypatch.setattr(
        "evm.testing.select_toolchain",
        lambda project, compiler: Toolchain(
            "gobo",
            Path("/tools/gec"),
            NumericVersion.parse("26.07"),
            "explicit",
            "test",
        ),
    )
    monkeypatch.setattr("evm.testing.shutil.which", lambda executable: "/tools/getest")

    with pytest.raises(EvmError, match="no getest configuration"):
        run_project_tests(project, WorkflowTestRequest(default_test=True))


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


def test_project_context_finds_parent_workspace_and_selected_dependencies(tmp_path: Path) -> None:
    core = tmp_path / "core"
    app = tmp_path / "app"
    _write_package(core, "core")
    _write_package(app, "app", dependencies='core = { path = "../core" }')
    (tmp_path / "Eiffel.toml").write_text('[workspace]\nmembers = ["app", "core"]\n')

    context = load_project_context(app)

    assert context.project is not None
    assert context.project.name == "app"
    assert context.workspace is not None
    assert [item.name for item in context.workspace.ordered_packages("app")] == ["core", "app"]
    assert is_workspace_member_dependency(context.project, "../core")
    assert not is_workspace_member_dependency(context.project, "../missing")
    with pytest.raises(EvmError, match="unknown workspace package"):
        context.workspace.package("missing")
    with pytest.raises(EvmError, match="unknown workspace package"):
        context.workspace.ordered_packages("missing")


@pytest.mark.parametrize(
    ("workspace", "message"),
    [
        ("workspace = 1\n", "workspace must be a table"),
        ("[workspace]\nunknown = true\n", "unknown manifest field"),
        ("[workspace]\nmembers = []\n", "non-empty array"),
        ('[workspace]\nmembers = [""]\n', "non-empty string"),
        ('[workspace]\nmembers = ["../outside"]\n', "escapes the workspace root"),
    ],
)
def test_workspace_rejects_invalid_boundaries(
    tmp_path: Path,
    workspace: str,
    message: str,
) -> None:
    manifest = tmp_path / "Eiffel.toml"
    manifest.write_text(workspace)

    with pytest.raises(EvmError, match=message):
        load_workspace(manifest)


def test_workspace_rejects_duplicate_and_mismatched_package_names(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    _write_package(first, "same")
    _write_package(second, "same")
    manifest = tmp_path / "Eiffel.toml"
    manifest.write_text('[workspace]\nmembers = ["first", "second"]\n')
    with pytest.raises(EvmError, match="must be unique"):
        load_workspace(manifest)

    (second / "Eiffel.toml").write_text(_manifest("second"))
    (first / "Eiffel.toml").write_text(
        _manifest("first") + '\n[dependencies]\nwrong = { path = "../second" }\n'
    )
    workspace = load_workspace(manifest)
    assert workspace is not None
    with pytest.raises(EvmError, match="points to package"):
        workspace.ordered_packages()


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
