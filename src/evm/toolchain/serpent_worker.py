"""Isolated bridge between EVM and the Python 3.13 Serpent API."""

from __future__ import annotations

import json
import sys
from typing import Any

from serpent.build import build_class_files, run
from serpent.errors import ErrorCollector
from serpent.resources import get_resource_path


def main() -> int:
    request = _request_from_arguments(sys.argv)
    errors = ErrorCollector()
    if request["operation"] == "build":
        _build(request, errors)
    else:
        _run(request, errors)
    if errors.ok():
        return 0
    errors.show()
    return 1


def _request_from_arguments(arguments: list[str]) -> dict[str, Any]:
    if len(arguments) != 2:
        raise SystemExit("usage: serpent_worker.py REQUEST_JSON")
    request = json.loads(arguments[1])
    if not isinstance(request, dict) or request.get("operation") not in {"build", "run"}:
        raise SystemExit("invalid Serpent worker request")
    return request


def _build(request: dict[str, Any], errors: ErrorCollector) -> None:
    build_class_files(
        eiffel_source_dirs=[get_resource_path("stdlib"), *request["sources"]],
        java_source_dirs=[get_resource_path("rtl")],
        parser_path=get_resource_path("build") / "eiffelp",
        error_collector=errors,
        build_dir=request["output"],
        java_version=request["java_version"],
        main_class_name=request["main_class"],
        main_routine_name=request["main_routine"],
        eiffel_package="com.eiffel",
        verbose=False,
    )


def _run(request: dict[str, Any], errors: ErrorCollector) -> None:
    run(
        classpath=request["classpath"],
        error_collector=errors,
        main_class_name=request["main_class"],
        eiffel_package="com.eiffel",
        cmd_args=request["arguments"],
    )


if __name__ == "__main__":
    sys.exit(main())
