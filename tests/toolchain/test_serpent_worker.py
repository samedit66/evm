"""Contract tests for the isolated Serpent API bridge."""

from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest


class FakeErrorCollector:
    def __init__(self) -> None:
        self.failed = False
        self.shown = False

    def ok(self) -> bool:
        return not self.failed

    def show(self) -> None:
        self.shown = True


@pytest.fixture
def serpent_worker(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    build_module = ModuleType("serpent.build")
    errors_module = ModuleType("serpent.errors")
    resources_module = ModuleType("serpent.resources")
    build_module.build_class_files = lambda **options: None
    build_module.run = lambda **options: None
    errors_module.ErrorCollector = FakeErrorCollector
    resources_module.get_resource_path = lambda name: Path("/serpent") / name
    monkeypatch.setitem(sys.modules, "serpent", ModuleType("serpent"))
    monkeypatch.setitem(sys.modules, "serpent.build", build_module)
    monkeypatch.setitem(sys.modules, "serpent.errors", errors_module)
    monkeypatch.setitem(sys.modules, "serpent.resources", resources_module)
    sys.modules.pop("evm.toolchain.serpent_worker", None)
    return importlib.import_module("evm.toolchain.serpent_worker")


def test_worker_build_maps_request_to_serpent_api(
    serpent_worker: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    monkeypatch.setattr(
        serpent_worker,
        "build_class_files",
        lambda **options: observed.update(options),
    )
    request = {
        "operation": "build",
        "sources": ["/project/src"],
        "output": "/project/build",
        "java_version": 11,
        "main_class": "APPLICATION",
        "main_routine": "make",
    }
    monkeypatch.setattr(serpent_worker.sys, "argv", ["worker", json.dumps(request)])

    assert serpent_worker.main() == 0
    assert observed["eiffel_source_dirs"] == [Path("/serpent/stdlib"), "/project/src"]
    assert observed["java_source_dirs"] == [Path("/serpent/rtl")]
    assert observed["parser_path"] == Path("/serpent/build/eiffelp")
    assert observed["build_dir"] == "/project/build"


def test_worker_run_reports_serpent_errors(
    serpent_worker: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def run(**options: object) -> None:
        observed.update(options)
        options["error_collector"].failed = True

    monkeypatch.setattr(serpent_worker, "run", run)
    request = {
        "operation": "run",
        "classpath": "/project/build",
        "main_class": "APPLICATION",
        "arguments": ["first"],
    }
    monkeypatch.setattr(serpent_worker.sys, "argv", ["worker", json.dumps(request)])

    assert serpent_worker.main() == 1
    assert observed["classpath"] == "/project/build"
    assert observed["cmd_args"] == ["first"]
    assert observed["error_collector"].shown is True


@pytest.mark.parametrize("arguments", [["worker"], ["worker", "{}"]])
def test_worker_rejects_invalid_requests(
    serpent_worker: ModuleType,
    arguments: list[str],
) -> None:
    with pytest.raises(SystemExit):
        serpent_worker._request_from_arguments(arguments)
