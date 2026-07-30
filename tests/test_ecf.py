from __future__ import annotations

from pathlib import Path

import pytest
from lxml import etree

from evm.ecf import ECF_NAMESPACE, semantic_diff, semantic_summary, validate_ecf
from evm.errors import EvmError
from evm.project import create_project


def test_semantic_diff_ignores_xml_formatting_and_management_comment(
    tmp_path: Path,
) -> None:
    project = create_project(tmp_path / "hello")
    document = etree.parse(str(project.ecf_path))
    project.ecf_path.write_bytes(
        etree.tostring(document.getroot(), encoding="UTF-8", xml_declaration=True)
    )

    assert semantic_diff(project) == "No semantic differences.\n"


def test_semantic_summary_does_not_resolve_external_entities(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("must-not-be-read")
    ecf = (
        f'<!DOCTYPE system [<!ENTITY secret SYSTEM "{secret.as_uri()}">]>'
        f'<system xmlns="{ECF_NAMESPACE}" name="safe">'
        "<description>&secret;</description>"
        '<target name="default"><root all_classes="true"/></target>'
        "</system>"
    ).encode()

    summary = semantic_summary(ecf)

    assert "must-not-be-read" not in str(summary)


def test_validate_ecf_rejects_unsupported_namespace(tmp_path: Path) -> None:
    ecf = tmp_path / "legacy.ecf"
    ecf.write_text('<system xmlns="https://example.invalid/ecf"><target name="x"/></system>')

    with pytest.raises(EvmError, match="unsupported ECF namespace"):
        validate_ecf(ecf)
