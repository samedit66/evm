from __future__ import annotations

import json

import httpx
import pytest
from click.testing import CliRunner

from evm.cli import main
from evm.toolchain.installation import available_artifacts
from evm.toolchain.types import ToolchainPlatform, current_toolchain_platform

ISE_PLATFORMS = (
    current_toolchain_platform("Linux", "x86_64"),
    current_toolchain_platform("Linux", "arm64"),
    current_toolchain_platform("Darwin", "x86_64"),
    current_toolchain_platform("Darwin", "arm64"),
    current_toolchain_platform("Windows", "AMD64"),
)


@pytest.mark.network
@pytest.mark.integration
@pytest.mark.parametrize("platform", ISE_PLATFORMS, ids=lambda value: value.identifier)
def test_real_ise_catalog_only_returns_downloadable_urls(
    platform: ToolchainPlatform,
) -> None:
    artifacts = available_artifacts("ise", platform=platform)

    assert artifacts, f"no downloadable EiffelStudio release for {platform.identifier}"
    for artifact in artifacts:
        _assert_url_can_start_downloading(artifact.url)
    if platform.operating_system == "windows":
        assert all(artifact.filename.endswith(".7z") for artifact in artifacts)


@pytest.mark.network
@pytest.mark.integration
def test_available_cli_json_only_publishes_reachable_sources() -> None:
    result = CliRunner().invoke(main, ["toolchain", "list", "--available", "--json"])

    assert result.exit_code == 0, result.output
    toolchains = json.loads(result.output)["toolchains"]
    assert toolchains
    for toolchain in toolchains:
        if toolchain["provider"] == "liberty":
            assert str(toolchain["url"]).endswith(f"#{toolchain['revision']}")
        else:
            _assert_url_can_start_downloading(str(toolchain["url"]))


def _assert_url_can_start_downloading(url: str) -> None:
    with (
        httpx.Client(
            timeout=60,
            follow_redirects=True,
            headers={"User-Agent": "evm-test"},
        ) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        assert next(response.iter_bytes(1), b""), f"empty download response from {url}"
