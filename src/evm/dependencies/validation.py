"""Domain validation shared by dependency entry points."""

from __future__ import annotations

from evm.errors import EvmError
from evm.model import Dependency

_IMPLICIT_RUNTIME_LIBRARIES = {
    "gobo": "free_elks",
    "ise": "base",
}
_IMPLICIT_RUNTIME_GROUP_NAMES = frozenset(_IMPLICIT_RUNTIME_LIBRARIES.values())


def ensure_dependency_is_not_implicit_runtime(dependency: Dependency) -> None:
    runtime_library = _IMPLICIT_RUNTIME_LIBRARIES.get(dependency.source)
    selected_library = dependency.library or dependency.name
    if selected_library == runtime_library:
        raise EvmError(
            f"dependency {dependency.name!r} declares {dependency.source.upper()} runtime library "
            f"{selected_library!r}, which EVM provides automatically"
        )
    if dependency.name in _IMPLICIT_RUNTIME_GROUP_NAMES:
        raise EvmError(
            f"dependency name {dependency.name!r} is reserved for an EVM-provided runtime library"
        )
