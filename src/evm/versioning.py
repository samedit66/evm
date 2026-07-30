"""Numeric Eiffel toolchain version constraints."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from evm.errors import EvmError

_VERSION = r"\d+(?:\.\d+)*"
_CONSTRAINT_RE = re.compile(rf"^(=|>=|>|<=|<)({_VERSION})$")


@dataclass(frozen=True, order=True)
class NumericVersion:
    parts: tuple[int, ...]
    text: str = field(compare=False)

    @classmethod
    def parse(cls, value: str) -> NumericVersion:
        if re.fullmatch(_VERSION, value) is None:
            raise EvmError(f"invalid numeric toolchain version: {value!r}")
        return cls(tuple(int(part) for part in value.split(".")), value)

    def __str__(self) -> str:
        return self.text


def validate_constraint(value: str) -> None:
    parse_constraint(value)


def parse_constraint(value: str) -> tuple[tuple[str, NumericVersion], ...]:
    if not value or any(char.isspace() for char in value):
        raise EvmError(f"invalid version constraint {value!r}; expected for example '>=25.12,<26'")
    result: list[tuple[str, NumericVersion]] = []
    for item in value.split(","):
        match = _CONSTRAINT_RE.fullmatch(item)
        if match is None:
            raise EvmError(
                f"invalid version constraint {value!r}; supported operators are =, >=, >, <=, <"
            )
        result.append((match.group(1), NumericVersion.parse(match.group(2))))
    return tuple(result)


def satisfies(version: NumericVersion, constraint: str | None) -> bool:
    if constraint is None:
        return True
    for operator, expected in parse_constraint(constraint):
        if operator == "=" and version != expected:
            return False
        if operator == ">=" and version < expected:
            return False
        if operator == ">" and version <= expected:
            return False
        if operator == "<=" and version > expected:
            return False
        if operator == "<" and version >= expected:
            return False
    return True
