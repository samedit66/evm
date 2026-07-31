from __future__ import annotations

from pathlib import Path

import pytest

from evm.autotest import (
    AutoTestCase,
    _filter_autotest_cases,
    _parse_descendants,
    _parse_test_features,
    _runner_ecf,
    _runner_source,
)
from evm.errors import EvmError


def test_parses_effective_autotest_descendants() -> None:
    output = """
Eiffel Compilation Manager

    EQA_TEST_SET*
        CALCULATOR_TESTS
        DEFERRED_TESTS*
        OTHER_TESTS
"""

    assert _parse_descendants(output) == ("CALCULATOR_TESTS", "OTHER_TESTS")


def test_parses_only_public_argumentless_procedures_from_short_view() -> None:
    output = """
class interface
    CALCULATOR_TESTS

feature -- Tests

    test_add

    test_with_argument (value: INTEGER)

    value: INTEGER

    test_subtract

end -- class CALCULATOR_TESTS
"""

    assert _parse_test_features(output) == ("test_add", "test_subtract")


def test_filters_autotest_cases_by_exact_case_insensitive_names() -> None:
    cases = (
        AutoTestCase("CALCULATOR_TESTS", "test_add"),
        AutoTestCase("CALCULATOR_TESTS", "test_subtract"),
        AutoTestCase("OTHER_TESTS", "test_add"),
    )

    assert _filter_autotest_cases(cases, "calculator_tests", "TEST_ADD") == (
        AutoTestCase("CALCULATOR_TESTS", "test_add"),
    )

    with pytest.raises(EvmError, match="no AutoTest tests matched"):
        _filter_autotest_cases(cases, "CALCULATOR", None)


def test_generated_runner_registers_each_test() -> None:
    source = _runner_source(
        (
            AutoTestCase("CALCULATOR_TESTS", "test_add"),
            AutoTestCase("CALCULATOR_TESTS", "test_subtract"),
        )
    )

    assert 'argument (2).same_string ("test_add")' in source
    assert "evaluator: EQA_TEST_EVALUATOR [CALCULATOR_TESTS]" in source
    assert "selected_test_set.test_subtract" in source
    assert "EVM_AUTOTEST_RESULT|" in source


def test_runner_ecf_rebases_relative_locations(tmp_path: Path) -> None:
    project_directory = tmp_path / "project"
    project_directory.mkdir()
    ecf = project_directory / "project.ecf"
    ecf.write_text(
        """<?xml version="1.0"?>
<system xmlns="http://www.eiffel.com/developers/xml/configuration-1-23-0"
        name="sample" uuid="a7dc72e4-94d9-4c44-97eb-f8fa1e5651ad">
  <target name="test">
    <root class="APPLICATION" feature="make"/>
    <cluster name="tests" location="tests"/>
  </target>
</system>
"""
    )
    generated = tmp_path / "state" / "generated"

    overlay = _runner_ecf(ecf, "test", generated).decode()

    assert f'location="{project_directory / "tests"}"' in overlay
    assert 'class="EVM_AUTOTEST_APPLICATION"' in overlay
    assert 'name="testing"' in overlay
    assert f'location="{generated}"' in overlay
