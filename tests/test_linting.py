from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from evm.errors import EvmError
from evm.linting import LintRequest, _lint_command, lint_project
from evm.manifest import parse_manifest
from evm.toolchains import Toolchain
from evm.versioning import NumericVersion


def _project(tmp_path: Path):
    return parse_manifest(
        """
[project]
name = "sample"
version = "1.0.0"
type = "application"
uuid = "00000000-0000-4000-8000-000000000000"
default-target = "default"
ecf = "sample.ecf"
ecf-managed = true

[root]
class = "APPLICATION"
feature = "make"

[sources]
clusters = ["src"]
""",
        tmp_path / "Eiffel.toml",
    )


def _toolchain(adapter: str) -> Toolchain:
    name = "ec" if adapter == "ise" else "gec"
    version = "25.12" if adapter == "ise" else "26.07"
    return Toolchain(
        adapter,
        Path(f"/tools/{name}"),
        NumericVersion.parse(version),
        "explicit",
        "test",
    )


def test_gelint_command_maps_analysis_options(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    toolchain = _toolchain("gobo")
    monkeypatch.setattr("evm.linting.companion_tool", lambda selected, name: Path("/tools/gelint"))

    command = _lint_command(
        project,
        toolchain,
        "library",
        "gelint",
        LintRequest(catcall=True, flat=True, standard="ecma", threads=3),
    )

    assert command == [
        "/tools/gelint",
        str(project.ecf_path),
        "--target=library",
        "--ecma",
        "--catcall",
        "--flat",
        "--thread=3",
    ]


def test_gelint_uses_registered_gobo_for_ise_semantics(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    toolchain = _toolchain("ise")
    monkeypatch.setattr("evm.linting.installed_gobo_tool", lambda name: Path("/gobo/gelint"))

    command = _lint_command(
        project,
        toolchain,
        "default",
        "gelint",
        LintRequest(),
    )

    assert command[-1] == "--ise=25.12"
    assert command[0] == "/gobo/gelint"


def test_gelint_uses_latest_ise_semantics_when_version_is_not_required(
    tmp_path: Path, monkeypatch
) -> None:
    project = _project(tmp_path)
    monkeypatch.setattr("evm.linting.companion_tool", lambda selected, name: Path("/tools/gelint"))

    command = _lint_command(
        project,
        _toolchain("gobo"),
        "default",
        "gelint",
        LintRequest(standard="ise"),
    )

    assert command[-1] == "--ise"


def test_eiffelstudio_analyzer_command_accepts_profile_and_rules(tmp_path: Path) -> None:
    project = _project(tmp_path)

    command = _lint_command(
        project,
        _toolchain("ise"),
        "default",
        "code-analyzer",
        LintRequest(rules=("CA001", "CA033"), profile=Path("analyzer.xml")),
    )

    assert command[-4:] == ["-ca_setting", "analyzer.xml", "-ca_rule", "CA001;CA033"]
    assert command[-6:-4] == ["-ca_class", "-all"]


def test_eiffelstudio_backend_rejects_gobo_toolchain(tmp_path: Path) -> None:
    with pytest.raises(EvmError, match="requires an ISE Eiffel toolchain"):
        _lint_command(
            _project(tmp_path),
            _toolchain("gobo"),
            "default",
            "code-analyzer",
            LintRequest(),
        )


def test_lint_project_runs_selected_backend(tmp_path: Path, monkeypatch) -> None:
    project = _project(tmp_path)
    toolchain = _toolchain("gobo")
    observed = []
    monkeypatch.setattr("evm.linting.select_toolchain", lambda project, compiler: toolchain)
    monkeypatch.setattr("evm.linting.prepare_project", lambda *args, **kwargs: None)
    monkeypatch.setattr("evm.linting.companion_tool", lambda selected, name: Path("/tools/gelint"))
    monkeypatch.setattr(
        "evm.linting.toolchain_environment_values", lambda command: (("GOBO", "/gobo"),)
    )
    monkeypatch.setattr(
        "evm.linting.subprocess.run",
        lambda command, **options: (
            observed.append((command, options))
            or SimpleNamespace(returncode=2, stdout="finding\n", stderr="problem\n")
        ),
    )

    result = lint_project(project, LintRequest())

    assert result.status == "failed"
    assert result.exit_code == 2
    assert result.as_dict()["stderr"] == "problem\n"
    assert observed[0][1]["env"]["GOBO"] == "/gobo"


@pytest.mark.parametrize(
    ("lint_request", "message"),
    [
        (LintRequest(backend="unknown"), "backend must be one of"),
        (LintRequest(backend="code-analyzer", catcall=True), "require --backend gelint"),
        (LintRequest(backend="gelint", rules=("CA001",)), "require --backend code-analyzer"),
    ],
)
def test_lint_project_rejects_incompatible_options(
    tmp_path: Path,
    monkeypatch,
    lint_request: LintRequest,
    message: str,
) -> None:
    monkeypatch.setattr("evm.linting.select_toolchain", lambda project, compiler: _toolchain("ise"))

    with pytest.raises(EvmError, match=message):
        lint_project(_project(tmp_path), lint_request)
