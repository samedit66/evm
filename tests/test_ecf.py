from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import pytest
from lxml import etree

from evm.ecf import (
    ECF_NAMESPACE,
    generate_ecf,
    prepare_legacy_ecf,
    semantic_diff,
    semantic_summary,
    validate_ecf,
)
from evm.errors import EvmError
from evm.lockfile import LockedPackage, LockFile, manifest_fingerprint
from evm.manifest import load_manifest
from evm.model import Dependency
from evm.project import create_project, import_ecf


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


def test_prepare_legacy_ecf_stages_portable_locations(tmp_path: Path) -> None:
    (tmp_path / "library").mkdir()
    ecf = tmp_path / "legacy.ecf"
    ecf.write_text(
        '<system xmlns="http://www.eiffel.com/developers/xml/configuration-1-18-0" '
        'name="legacy" uuid="00000000-0000-4000-8000-000000000000" '
        'library_target="legacy"><target name="legacy"><root all_classes="true"/>'
        '<library name="base" location="$LEGACY_LIBRARY\\base\\base-safe.ecf"/>'
        '<cluster name="legacy" location="library\\"/></target></system>'
    )
    before = ecf.read_bytes()
    project, _, _ = import_ecf(ecf, tmp_path)

    staged = prepare_legacy_ecf(project)

    assert staged == tmp_path / ".evm" / "tmp" / "legacy" / "legacy.ecf"
    assert ecf.read_bytes() == before
    locations = etree.parse(str(staged)).xpath("//*[@location]/@location")
    assert "$LEGACY_LIBRARY/base/base-safe.ecf" in locations
    assert "../../../library" in locations


def test_prepare_legacy_ecf_uses_available_modern_library_name(
    tmp_path: Path,
    monkeypatch,
) -> None:
    library = tmp_path / "ise" / "library" / "base"
    library.mkdir(parents=True)
    (library / "base.ecf").write_text("modern")
    (tmp_path / "src").mkdir()
    ecf = tmp_path / "legacy.ecf"
    ecf.write_text(
        f'<system xmlns="{ECF_NAMESPACE}" name="legacy" '
        'uuid="00000000-0000-4000-8000-000000000000" library_target="legacy">'
        '<target name="legacy"><root all_classes="true"/>'
        '<library name="base" location="$ISE_LIBRARY\\library\\base\\base-safe.ecf"/>'
        '<cluster name="legacy" location="src"/></target></system>'
    )
    project, _, _ = import_ecf(ecf, tmp_path)

    staged = prepare_legacy_ecf(project, {"ISE_LIBRARY": tmp_path / "ise"})

    locations = etree.parse(str(staged)).xpath("//*[@location]/@location")
    assert "$ISE_LIBRARY/library/base/base.ecf" in locations


def test_ecf_fragment_include_adds_target_constructs(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    config = project.directory / "config"
    config.mkdir()
    (config / "options.xml").write_text(
        f'<ecf-overlay xmlns="{ECF_NAMESPACE}">'
        '<target name="default"><option warning="true"/></target>'
        "</ecf-overlay>"
    )
    project.manifest_path.write_text(
        project.manifest_path.read_text() + '\n[ecf]\ninclude = ["config/options.xml"]\n'
    )

    generated = generate_ecf(load_manifest(project.manifest_path))

    root = etree.fromstring(generated)
    options = root.xpath("./*[local-name()='target']/*[local-name()='option']")
    assert any(option.get("warning") == "true" for option in options)


def test_ecf_fragment_rejects_high_level_conflict(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    config = project.directory / "config"
    config.mkdir()
    (config / "conflict.xml").write_text(
        f'<ecf-overlay xmlns="{ECF_NAMESPACE}">'
        '<target name="default" extends="other"/></ecf-overlay>'
    )
    project.manifest_path.write_text(
        project.manifest_path.read_text() + '\n[ecf]\ninclude = ["config/conflict.xml"]\n'
    )

    with pytest.raises(EvmError, match="conflicts with target"):
        generate_ecf(load_manifest(project.manifest_path))


def test_ecf_fragment_rejects_duplicate_runtime_group_name(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    config = project.directory / "config"
    config.mkdir()
    (config / "duplicate.xml").write_text(
        f'<ecf-overlay xmlns="{ECF_NAMESPACE}">'
        '<target name="default">'
        '<library name="base" location="custom/base.ecf"/>'
        "</target>"
        "</ecf-overlay>"
    )
    project.manifest_path.write_text(
        project.manifest_path.read_text() + '\n[ecf]\ninclude = ["config/duplicate.xml"]\n'
    )

    with pytest.raises(EvmError, match="duplicate group name 'base' for library and library"):
        generate_ecf(load_manifest(project.manifest_path))


def test_ecf_rejects_dependency_name_conflicting_with_cluster(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    dependency = Dependency(name="default_src_0", source="path", path=".")
    project_with_dependency = replace(project, dependencies=(dependency,))
    package = LockedPackage(
        name=dependency.name,
        version="0.0.0",
        source="path+.",
        path=".",
        ecf="hello.ecf",
    )
    lock = LockFile(manifest_fingerprint(project_with_dependency), (package,))

    with pytest.raises(
        EvmError,
        match="duplicate group name 'default_src_0' for library and cluster",
    ):
        generate_ecf(project_with_dependency, lock)


def test_ecf_allows_same_group_names_in_different_targets(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    project.manifest_path.write_text(
        project.manifest_path.read_text()
        + '\n[targets.alternate]\nroot = "APPLICATION.make"\nsources = ["src"]\n'
    )

    generated = generate_ecf(load_manifest(project.manifest_path))

    root = etree.fromstring(generated)
    targets = root.xpath("./*[local-name()='target']")
    assert len(targets) == 2
    assert all(target.xpath("./*[local-name()='library'][@name='base']") for target in targets)


def test_autotest_target_includes_ise_testing_library(tmp_path: Path) -> None:
    project = create_project(tmp_path / "hello")
    project.manifest_path.write_text(
        project.manifest_path.read_text()
        + '\n[targets.test]\nroot = "APPLICATION.make"\nsources = ["tests"]\n'
        + '\n[test]\ntarget = "test"\nrunner = "autotest"\n'
    )

    generated = generate_ecf(load_manifest(project.manifest_path))

    root = etree.fromstring(generated)
    testing = root.xpath(
        "./*[local-name()='target'][@name='test']/*[local-name()='library'][@name='testing']"
    )
    assert len(testing) == 1
    assert testing[0].get("location") == "${ISE_LIBRARY}/library/testing/testing.ecf"
