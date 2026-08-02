"""Shared Click boundary behavior for EVM command modules."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import wraps
from typing import ParamSpec, TypeVar

import click

from evm.errors import EvmError

P = ParamSpec("P")
R = TypeVar("R")


def command_errors(function: Callable[P, R]) -> Callable[P, R]:
    """Render domain errors consistently without Python tracebacks."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except EvmError as error:
            if kwargs.get("output_json"):
                click.echo(
                    json.dumps(
                        {
                            "status": "error",
                            "diagnostics": [{"level": "error", "message": str(error)}],
                        },
                        sort_keys=True,
                    )
                )
                raise click.exceptions.Exit(1) from error
            raise click.ClickException(str(error)) from error

    return wrapped
