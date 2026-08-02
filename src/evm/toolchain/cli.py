"""Command-line boundary for installing and managing Eiffel toolchains."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from evm.cli_support import command_errors
from evm.errors import EvmError
from evm.lockfile import LOCK_NAME, ensure_lock_matches, load_lock
from evm.model import Project
from evm.toolchain.commands import (
    configure_project_toolchains,
    locked_toolchains_for_current_platform,
    project_toolchain_selectors,
    record_installed_toolchains,
)
from evm.toolchain.compilers import compiler_adapter_names
from evm.toolchain.installation import (
    InstallationProgress,
    available_artifacts,
    find_serpent_bison,
    find_serpent_python,
    homebrew_can_install_bison,
    install_bison_with_homebrew,
    install_locked_toolchain,
    install_serpent_python_with_uv,
    install_toolchain,
    serpent_bison_requirement_error,
    uv_can_install_serpent_python,
)
from evm.toolchain.store import (
    list_installations,
    probe_linked_installation,
    register_linked_installation,
    remove_installation,
    render_environment,
    select_installation,
    toolchain_environment,
    verify_installation,
)
from evm.toolchain.types import (
    ToolchainArtifact,
    ToolchainInstallation,
    ToolchainSelector,
)
from evm.workspace import ProjectContext, load_project_context


@click.group("toolchain")
def toolchain_group() -> None:
    """Install and manage user-level Eiffel toolchains."""


@toolchain_group.command("list")
@click.argument("provider", required=False, type=click.Choice(compiler_adapter_names()))
@click.option("--available", is_flag=True, help="Show releases available for download.")
@click.option("--json", "output_json", is_flag=True, help="Emit stable JSON for CI.")
@command_errors
def toolchain_list_command(
    provider: str | None,
    available: bool,
    output_json: bool,
) -> None:
    """List installed toolchains or official releases."""
    if available:
        _list_available_toolchains(provider, output_json)
        return
    _list_installed_toolchains(provider, output_json)


def _list_available_toolchains(provider: str | None, output_json: bool) -> None:
    providers = (provider,) if provider else compiler_adapter_names()
    artifacts = tuple(
        artifact
        for selected_provider in providers
        for artifact in available_artifacts(selected_provider)
    )
    if output_json:
        click.echo(
            json.dumps(
                {"toolchains": [_artifact_data(item) for item in artifacts]},
                sort_keys=True,
            )
        )
        return
    for artifact in artifacts:
        click.echo(
            f"{artifact.provider}@{artifact.version}\t{artifact.channel}\t"
            f"{artifact.platform.identifier}"
        )
    click.echo(
        "You can specify an exact toolchain, such as ise@latest, ise@stable, "
        "gobo@beta, or serpent@0.1.0."
    )


def _list_installed_toolchains(provider: str | None, output_json: bool) -> None:
    installations = tuple(
        item for item in list_installations() if provider is None or item.provider == provider
    )
    if output_json:
        click.echo(
            json.dumps(
                {"toolchains": [_installation_data(item) for item in installations]},
                sort_keys=True,
            )
        )
        return
    if not installations:
        click.echo("No EVM-managed or linked toolchains installed.")
        return
    click.echo("TOOLCHAIN\tREVISION\tKIND\tPLATFORM\tLOCATION")
    for installation in installations:
        click.echo(
            f"{installation.selector}\t{installation.revision}\t{installation.kind.value}\t"
            f"{installation.platform.identifier}\t{installation.root}"
        )


@toolchain_group.command("install")
@click.argument("selectors", nargs=-1)
@click.option("--project", "project_mode", is_flag=True, help="Install the project matrix.")
@click.option("--locked", is_flag=True, help="Require exact artifacts from Eiffel.lock.")
@click.option("--offline", is_flag=True, help="Use only the download cache.")
@command_errors
def toolchain_install_command(
    selectors: tuple[str, ...],
    project_mode: bool,
    locked: bool,
    offline: bool,
) -> None:
    """Install toolchains into the shared user store."""
    if project_mode and selectors:
        raise EvmError("SELECTOR and --project are mutually exclusive")
    if locked and not project_mode:
        raise EvmError("--locked requires --project")
    progress = InstallationProgressRenderer()
    try:
        if project_mode:
            project = _require_project(load_project_context())
            _prepare_project_serpent_installation(project, locked, offline, progress)
            installed = _install_project_toolchains(
                project, locked=locked, offline=offline, progress=progress
            )
            record_installed_toolchains(project, installed)
        else:
            if not selectors:
                raise EvmError("provide at least one SELECTOR or use --project")
            parsed = tuple(ToolchainSelector.parse(value) for value in selectors)
            _prepare_serpent_installation(parsed, offline, progress)
            installed = tuple(
                install_toolchain(selector, offline=offline, progress=progress)
                for selector in parsed
            )
    finally:
        progress.finish()
    for installation in installed:
        click.echo(f"Installed {installation.identity} at {installation.root}")


@toolchain_group.command("use")
@click.argument("selectors", nargs=-1, required=True)
@click.option("--install", "install_missing", is_flag=True, help="Install configured releases.")
@command_errors
def toolchain_use_command(selectors: tuple[str, ...], install_missing: bool) -> None:
    """Set the default and compilation matrix for the current project."""
    project = _require_project(load_project_context())
    parsed = tuple(ToolchainSelector.parse(value) for value in selectors)
    proposed, _ = configure_project_toolchains(project, parsed)
    click.echo(f"Updated {proposed.manifest_path}")
    click.echo(f"Updated {proposed.directory / LOCK_NAME}")
    if install_missing:
        progress = InstallationProgressRenderer()
        configured = project_toolchain_selectors(proposed)
        installed = []
        try:
            _prepare_serpent_installation(configured, False, progress)
            for selector in configured:
                installation = install_toolchain(selector, progress=progress)
                installed.append(installation)
                click.echo(f"Installed {installation.identity} at {installation.root}")
        finally:
            progress.finish()
        record_installed_toolchains(proposed, tuple(installed))


@toolchain_group.command("link")
@click.argument("path", type=click.Path(exists=True, file_okay=False, path_type=Path))
@command_errors
def toolchain_link_command(path: Path) -> None:
    """Register an existing EiffelStudio or Gobo installation."""
    installation = probe_linked_installation(path)
    register_linked_installation(installation)
    click.echo(f"Linked {installation.identity} at {installation.root}")


@toolchain_group.command("remove")
@click.argument("selector")
@click.option("--force", is_flag=True, help="Remove a toolchain selected by this project.")
@command_errors
def toolchain_remove_command(selector: str, force: bool) -> None:
    """Remove a managed toolchain or linked registration."""
    installation = select_installation(ToolchainSelector.parse(selector))
    project = _optional_project()
    if not force and project is not None and project.toolchain is not None:
        configured = {ToolchainSelector.parse(item) for item in project.toolchain.matrix}
        if any(installation.matches(item) for item in configured):
            raise EvmError(
                f"{installation.selector} is selected by the current project; use --force"
            )
    remove_installation(installation)
    action = "Unlinked" if installation.kind.value == "linked" else "Removed"
    click.echo(f"{action} {installation.identity}")


@toolchain_group.command("verify")
@click.argument("selector", required=False)
@click.option("--project", "project_mode", is_flag=True, help="Verify the project matrix.")
@click.option("--json", "output_json", is_flag=True, help="Emit stable JSON for CI.")
@command_errors
def toolchain_verify_command(
    selector: str | None,
    project_mode: bool,
    output_json: bool,
) -> None:
    """Verify compiler executables and reported versions."""
    if selector is not None and project_mode:
        raise EvmError("SELECTOR and --project are mutually exclusive")
    results: list[dict[str, object]] = []
    failed = False
    for installation in _verification_installations(selector, project_mode):
        diagnostics = verify_installation(installation)
        failed = failed or bool(diagnostics)
        results.append(
            {
                "toolchain": installation.identity,
                "status": "failed" if diagnostics else "passed",
                "diagnostics": list(diagnostics),
            }
        )
        if not output_json:
            click.echo(f"{installation.identity}: {'FAILED' if diagnostics else 'passed'}")
            for diagnostic in diagnostics:
                click.echo(f"  {diagnostic}")
    if output_json:
        click.echo(json.dumps({"status": "failed" if failed else "passed", "toolchains": results}))
    if failed:
        raise click.exceptions.Exit(1)


@toolchain_group.command("env")
@click.argument("selector", required=False)
@click.option(
    "--shell",
    type=click.Choice(["bash", "zsh", "sh", "powershell", "dotenv"]),
    default="sh",
    show_default=True,
)
@command_errors
def toolchain_env_command(selector: str | None, shell: str) -> None:
    """Print environment changes for a selected toolchain."""
    installation = _selected_or_project_installation(selector)
    environment = toolchain_environment(installation, {})
    click.echo(render_environment(environment, shell), nl=False)


def _install_project_toolchains(
    project: Project,
    *,
    locked: bool,
    offline: bool,
    progress: InstallationProgressRenderer | None = None,
) -> tuple[ToolchainInstallation, ...]:
    if locked:
        lock = load_lock(project.directory / LOCK_NAME)
        ensure_lock_matches(project, lock)
        locked_toolchains = locked_toolchains_for_current_platform(lock)
        if not locked_toolchains:
            raise EvmError("Eiffel.lock has no toolchain artifact for the current platform")
        return tuple(
            install_locked_toolchain(item, offline=offline, progress=progress)
            for item in locked_toolchains
        )
    return tuple(
        install_toolchain(selector, offline=offline, progress=progress)
        for selector in project_toolchain_selectors(project)
    )


class InstallationProgressRenderer:
    def __init__(self) -> None:
        self.interactive = click.get_text_stream("stdout").isatty()
        self.last_stage: str | None = None
        self.activity_line_open = False

    def __call__(self, progress: InstallationProgress) -> None:
        if progress.completed is not None and progress.total is not None:
            self._render_download(progress)
            return
        if progress.completed is not None:
            self._render_activity(progress)
            return
        self._finish_activity_line()
        if not self.interactive and progress.stage == self.last_stage:
            return
        click.echo(f"{_progress_mark(progress.stage)} {progress.message}")
        self.last_stage = progress.stage

    def _render_download(self, progress: InstallationProgress) -> None:
        total = progress.total or 0
        complete = progress.completed or 0
        if not self.interactive and complete not in {0, total}:
            return
        ratio = min(1.0, complete / total) if total else 0.0
        width = 24
        filled = round(width * ratio)
        bar = "█" * filled + "░" * (width - filled)
        line = (
            f"↓ {progress.message} [{bar}] {ratio:.0%} "
            f"{_format_bytes(complete)}/{_format_bytes(total)}"
        )
        line_complete = not self.interactive or complete >= total
        click.echo(f"\r{line}", nl=line_complete)
        self.activity_line_open = not line_complete
        self.last_stage = progress.stage

    def _render_activity(self, progress: InstallationProgress) -> None:
        message = progress.message
        if progress.stage == "download":
            message = f"{message} · {_format_bytes(progress.completed or 0)}"
        if self.interactive:
            click.echo(f"\r→ {message}", nl=False)
            self.activity_line_open = True
        elif progress.stage != self.last_stage:
            click.echo(f"→ {message}")
        self.last_stage = progress.stage

    def _finish_activity_line(self) -> None:
        if self.activity_line_open:
            click.echo()
            self.activity_line_open = False

    def finish(self) -> None:
        self._finish_activity_line()


def _progress_mark(stage: str) -> str:
    return "✓" if stage == "complete" else "→"


def _format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
        value /= 1024
    raise AssertionError("unreachable byte unit")


def _prepare_project_serpent_installation(
    project: Project,
    locked: bool,
    offline: bool,
    progress: InstallationProgressRenderer,
) -> None:
    if locked:
        lock = load_lock(project.directory / LOCK_NAME)
        ensure_lock_matches(project, lock)
        selectors = tuple(
            ToolchainSelector(item.provider, item.revision)
            for item in locked_toolchains_for_current_platform(lock)
        )
    else:
        selectors = project_toolchain_selectors(project)
    _prepare_serpent_installation(selectors, offline, progress)


def _prepare_serpent_installation(
    selectors: tuple[ToolchainSelector, ...],
    offline: bool,
    progress: InstallationProgressRenderer,
) -> None:
    if not any(selector.provider == "serpent" for selector in selectors):
        return
    _prepare_serpent_bison(offline, progress)
    _prepare_serpent_python(offline, progress)


def _prepare_serpent_bison(
    offline: bool,
    progress: InstallationProgressRenderer,
) -> None:
    if find_serpent_bison() is not None:
        return
    if offline:
        raise EvmError(
            "Serpent requires GNU Bison 3.7 or newer; --offline prevents installing it "
            "with Homebrew"
        )
    if not homebrew_can_install_bison():
        raise EvmError(serpent_bison_requirement_error())
    if not _input_is_interactive():
        raise EvmError(serpent_bison_requirement_error())
    if not click.confirm(
        "Serpent requires GNU Bison 3.7 or newer. Install it with Homebrew?",
        default=False,
    ):
        raise EvmError("GNU Bison was not installed; Serpent installation cancelled")
    install_bison_with_homebrew(progress)


def _prepare_serpent_python(
    offline: bool,
    progress: InstallationProgressRenderer,
) -> None:
    if find_serpent_python() is not None:
        return
    if offline:
        raise EvmError(
            "Serpent requires Python 3.13 or newer; --offline prevents installing it with uv"
        )
    if not uv_can_install_serpent_python():
        raise EvmError("Serpent requires Python 3.13 or newer; install Python 3.13 and retry")
    if not _input_is_interactive():
        raise EvmError(
            "Serpent requires Python 3.13 or newer; run interactively to let EVM install "
            "it with uv, or install Python 3.13 manually"
        )
    if not click.confirm(
        "Serpent requires Python 3.13 or newer. Install Python 3.13 with uv?",
        default=False,
    ):
        raise EvmError("Python 3.13 was not installed; Serpent installation cancelled")
    install_serpent_python_with_uv(progress)


def _input_is_interactive() -> bool:
    return sys.stdin.isatty()


def _verification_installations(
    selector: str | None,
    project_mode: bool,
) -> tuple[ToolchainInstallation, ...]:
    if selector is not None:
        return (select_installation(ToolchainSelector.parse(selector)),)
    if project_mode:
        project = _require_project(load_project_context())
        return tuple(select_installation(item) for item in project_toolchain_selectors(project))
    installations = list_installations()
    if not installations:
        raise EvmError("no EVM-managed or linked toolchains installed")
    return installations


def _selected_or_project_installation(selector: str | None) -> ToolchainInstallation:
    if selector is not None:
        return select_installation(ToolchainSelector.parse(selector))
    project = _optional_project()
    if project is not None and project.toolchain is not None:
        return select_installation(ToolchainSelector.parse(project.toolchain.default))
    installations = list_installations()
    if len(installations) == 1:
        return installations[0]
    raise EvmError("select a toolchain explicitly or configure toolchain.default")


def _optional_project() -> Project | None:
    try:
        return load_project_context().project
    except EvmError as error:
        if "Eiffel.toml not found" in str(error):
            return None
        raise


def _require_project(context: ProjectContext) -> Project:
    if context.project is not None:
        return context.project
    raise EvmError("this command requires a package; run it inside a workspace member")


def _installation_data(installation: ToolchainInstallation) -> dict[str, object]:
    return {
        "provider": installation.provider,
        "version": installation.version,
        "revision": installation.revision,
        "kind": installation.kind.value,
        "platform": installation.platform.identifier,
        "root": str(installation.root),
        "executable": str(installation.executable),
        "checksum": installation.checksum,
        "source": installation.source,
    }


def _artifact_data(artifact: ToolchainArtifact) -> dict[str, object]:
    return {
        "provider": artifact.provider,
        "version": artifact.version,
        "revision": artifact.revision,
        "channel": artifact.channel,
        "platform": artifact.platform.identifier,
        "url": artifact.url,
        "checksum": artifact.checksum,
    }
