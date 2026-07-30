from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from evm.model import BuildRequest
from evm.project import compile_project, create_project


@pytest.mark.toolchain
@pytest.mark.integration
@pytest.mark.parametrize(
    ("adapter", "executable"),
    [("ise", "ec"), ("gobo", "gec")],
)
def test_generated_application_builds_with_real_toolchain(
    tmp_path: Path,
    adapter: str,
    executable: str,
) -> None:
    if shutil.which(executable) is None:
        pytest.skip(f"{executable} is not installed")
    if adapter == "gobo" and "GOBO" not in os.environ:
        pytest.skip("GOBO is not defined")
    project = create_project(tmp_path / f"hello_{adapter}")

    selected = compile_project(
        project,
        BuildRequest(compiler=adapter),
    )

    assert selected.adapter == adapter
