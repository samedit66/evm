from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from evm.errors import EvmError
from evm.toolchain.companion_tools import companion_tool, installed_gobo_tool
from evm.toolchain.selection import Toolchain
from evm.versioning import NumericVersion


def test_companion_tool_resolves_sibling_executable(tmp_path: Path) -> None:
    compiler = tmp_path / "bin" / "gec"
    compiler.parent.mkdir()
    compiler.touch()
    linter = compiler.with_name("gelint")
    linter.touch()
    toolchain = Toolchain(
        "gobo",
        compiler,
        NumericVersion.parse("26.07"),
        "explicit",
        "test",
    )

    assert companion_tool(toolchain, "gelint") == linter


def test_companion_tool_reports_incomplete_toolchain(tmp_path: Path) -> None:
    toolchain = Toolchain(
        "gobo",
        tmp_path / "bin" / "gec",
        NumericVersion.parse("26.07"),
        "explicit",
        "test",
    )

    with pytest.raises(EvmError, match="toolchain verify"):
        companion_tool(toolchain, "gedoc")


def test_installed_gobo_tool_uses_registered_installation(tmp_path: Path, monkeypatch) -> None:
    compiler = tmp_path / "bin" / "gec"
    compiler.parent.mkdir()
    compiler.touch()
    documentation = compiler.with_name("gedoc")
    documentation.touch()
    installation = SimpleNamespace(
        provider="gobo",
        platform=SimpleNamespace(identifier="test-platform"),
        executable=compiler,
    )
    monkeypatch.setattr("evm.toolchain.companion_tools.list_installations", lambda: (installation,))
    monkeypatch.setattr(
        "evm.toolchain.companion_tools.current_toolchain_platform",
        lambda: SimpleNamespace(identifier="test-platform"),
    )

    assert installed_gobo_tool("gedoc") == documentation


def test_installed_gobo_tool_does_not_trust_path(monkeypatch) -> None:
    monkeypatch.setattr("evm.toolchain.companion_tools.list_installations", lambda: ())
    monkeypatch.setattr(
        "evm.toolchain.companion_tools.current_toolchain_platform",
        lambda: SimpleNamespace(identifier="test-platform"),
    )

    with pytest.raises(EvmError, match="registered Gobo Eiffel installation"):
        installed_gobo_tool("gelint")
