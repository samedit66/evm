"""Resolve tools shipped beside registered Eiffel toolchains."""

from __future__ import annotations

import os
from pathlib import Path

from evm.errors import EvmError
from evm.toolchain_store import list_installations
from evm.toolchain_types import current_toolchain_platform
from evm.toolchains import Toolchain


def companion_tool(toolchain: Toolchain, name: str) -> Path:
    candidate = toolchain.executable.with_name(_executable_name(name))
    if candidate.is_file():
        return candidate
    raise EvmError(
        f"{name} was not found in the selected {toolchain.adapter} toolchain; "
        "run `evm toolchain verify`"
    )


def installed_gobo_tool(name: str) -> Path:
    platform = current_toolchain_platform().identifier
    for installation in list_installations():
        if installation.provider != "gobo" or installation.platform.identifier != platform:
            continue
        candidate = installation.executable.with_name(_executable_name(name))
        if candidate.is_file():
            return candidate
    suffix = ".exe" if os.name == "nt" else ""
    raise EvmError(
        f"{name}{suffix} requires a registered Gobo Eiffel installation; "
        "run `evm toolchain install gobo` or `evm toolchain link PATH`"
    )


def _executable_name(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")
