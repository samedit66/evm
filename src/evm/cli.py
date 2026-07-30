"""Command-line interface for EVM."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import wraps
from pathlib import Path

import click

from evm.ecf import semantic_diff
from evm.errors import EvmError
from evm.manifest import find_manifest, load_manifest
from evm.model import BuildRequest
from evm.project import (
    compile_project,
    create_project,
    effective_root,
    effective_sources,
    explain_data,
    import_ecf,
    prepare_project,
    run_project,
)
from evm.toolchains import doctor_lines


def command_errors[**P, R](function: Callable[P, R]) -> Callable[P, R]:
    """Render domain errors consistently without Python tracebacks."""

    @wraps(function)
    def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
        try:
            return function(*args, **kwargs)
        except EvmError as error:
            raise click.ClickException(str(error)) from error

    return wrapped


@click.group()
@click.version_option(package_name="evm")
def main() -> None:
    """Manage Eiffel projects and their toolchains."""


@main.command("new")
@click.argument("path", type=click.Path(path_type=Path))
@click.option("--lib", "library", is_flag=True, help="Create a library project.")
@command_errors
def new_command(path: Path, library: bool) -> None:
    """Create a new EVM project in PATH."""
    project = create_project(path, library=library)
    click.echo(f"Created {project.kind} project {project.name!r}")
    for name in ("Eiffel.toml", "Eiffel.lock", project.ecf_path.name, "src", "tests"):
        click.echo(f"  {project.directory / name}")


@main.command("init")
@click.option("--lib", "library", is_flag=True, help="Initialize a library project.")
@command_errors
def init_command(library: bool) -> None:
    """Initialize the current directory without overwriting files."""
    project = create_project(Path.cwd(), library=library, initialize=True)
    click.echo(f"Initialized {project.kind} project {project.name!r}")


@main.command("check")
@click.option("--configuration-only", is_flag=True, help="Do not invoke an Eiffel compiler.")
@click.option("--release", is_flag=True, help="Check the release mode.")
@click.option("--compiler", type=str, help="Compiler adapter ID: ise or gobo.")
@click.option("--target", default="default", show_default=True)
@click.option("--regenerate-ecf", is_flag=True, help="Explicitly overwrite managed ECF.")
@command_errors
def check_command(
    configuration_only: bool,
    release: bool,
    compiler: str | None,
    target: str,
    regenerate_ecf: bool,
) -> None:
    """Validate project configuration and reachable Eiffel classes."""
    project = load_manifest(find_manifest())
    if configuration_only:
        changed = prepare_project(project, regenerate=regenerate_ecf)
        if changed:
            click.echo(f"Generated {project.ecf_path}")
        click.echo("Configuration is valid.")
        return
    compile_project(
        project,
        BuildRequest(
            compiler=compiler,
            target=target,
            release=release,
            regenerate_ecf=regenerate_ecf,
        ),
        check_only=True,
    )
    click.echo("Project check completed.")


@main.command("build")
@click.option("--release", is_flag=True, help="Build in release mode.")
@click.option("--compiler", type=str, help="Compiler adapter ID: ise or gobo.")
@click.option("--target", default="default", show_default=True)
@click.option("--offline", is_flag=True, help="Forbid network access.")
@click.option("--regenerate-ecf", is_flag=True, help="Explicitly overwrite managed ECF.")
@command_errors
def build_command(
    release: bool,
    compiler: str | None,
    target: str,
    offline: bool,
    regenerate_ecf: bool,
) -> None:
    """Build an Eiffel application or library."""
    project = load_manifest(find_manifest())
    compile_project(
        project,
        BuildRequest(
            compiler=compiler,
            target=target,
            release=release,
            regenerate_ecf=regenerate_ecf,
        ),
    )
    click.echo("Build completed.")


@main.command(
    "run",
    context_settings={"ignore_unknown_options": True, "allow_extra_args": True},
)
@click.option("--release", is_flag=True, help="Build and run in release mode.")
@click.option("--compiler", type=str, help="Compiler adapter ID: ise or gobo.")
@click.option("--target", default="default", show_default=True)
@click.option("--regenerate-ecf", is_flag=True, help="Explicitly overwrite managed ECF.")
@click.argument("arguments", nargs=-1, type=click.UNPROCESSED)
@command_errors
def run_command(
    release: bool,
    compiler: str | None,
    target: str,
    regenerate_ecf: bool,
    arguments: tuple[str, ...],
) -> None:
    """Build and run an application; arguments after -- go to the program."""
    project = load_manifest(find_manifest())
    exit_code = run_project(
        project,
        BuildRequest(
            compiler=compiler,
            target=target,
            release=release,
            regenerate_ecf=regenerate_ecf,
        ),
        arguments,
    )
    if exit_code:
        raise click.exceptions.Exit(exit_code)


@main.command("doctor")
@command_errors
def doctor_command() -> None:
    """Diagnose installed Eiffel toolchains and their environments."""
    lines, healthy = doctor_lines()
    click.echo("\n".join(lines))
    if not healthy:
        raise click.exceptions.Exit(1)


@main.command("explain")
@click.option("--targets", "show_targets", is_flag=True, help="List effective targets.")
@click.option("--target", default="default", show_default=True)
@click.option("--release", is_flag=True, help="Explain release mode.")
@click.option("--compiler", type=str, help="Compiler adapter ID: ise or gobo.")
@click.option("--json", "output_mode", flag_value="json", help="Emit stable JSON.")
@click.option(
    "--ecf-diff",
    "output_mode",
    flag_value="ecf-diff",
    help="Compare the ECF with the manifest.",
)
@command_errors
def explain_command(
    show_targets: bool,
    target: str,
    release: bool,
    compiler: str | None,
    output_mode: str | None,
) -> None:
    """Show the effective project configuration."""
    project = load_manifest(find_manifest())
    if output_mode == "ecf-diff":
        click.echo(semantic_diff(project), nl=False)
        return
    if show_targets:
        if output_mode == "json":
            click.echo(
                json.dumps(
                    [
                        {
                            "name": item.name,
                            "root": (
                                None
                                if effective_root(project, item.name) is None
                                else (
                                    f"{effective_root(project, item.name).class_name}."
                                    f"{effective_root(project, item.name).feature}"
                                )
                            ),
                            "sources": list(effective_sources(project, item.name)),
                        }
                        for item in project.targets
                    ],
                    indent=2,
                )
            )
            return
        click.echo("TARGET\tROOT\tSOURCES")
        for item in project.targets:
            root = effective_root(project, item.name)
            root_text = "-" if root is None else f"{root.class_name}.{root.feature}"
            sources = ", ".join(effective_sources(project, item.name))
            click.echo(f"{item.name}\t{root_text}\t{sources}")
        return
    data = explain_data(project, target=target, release=release, compiler=compiler)
    if output_mode == "json":
        click.echo(json.dumps(data, indent=2, sort_keys=True))
        return
    click.echo(f"Target: {data['target']}")
    click.echo(f"Mode: {data['mode']}")
    click.echo(f"Platform: {data['platform']}")
    toolchain = data["toolchain"]
    if isinstance(toolchain, dict):
        click.echo(f"Toolchain: {toolchain['adapter']} {toolchain['version']}")
        click.echo(f"Selection: {toolchain['selection']}")
        click.echo(f"Reason: {toolchain['reason']}")
    else:
        click.echo("Toolchain: unavailable")
    click.echo(f"Root: {data['root'] or 'all classes'}")
    click.echo("Clusters:")
    for source in data["sources"]:
        click.echo(f"  {source}")
    if data["conditions"]:
        click.echo("Conditions:")
        for condition in data["conditions"]:
            status = "matched" if condition["matched"] else "excluded"
            click.echo(f"  {condition['when']}: {status}")


@main.command("import")
@click.argument("ecf", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--destination",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
)
@command_errors
def import_command(ecf: Path, destination: Path) -> None:
    """Create an initial manifest from an existing ECF without changing it."""
    project, level, warnings = import_ecf(ecf, destination)
    click.echo(f"Import level: {level}")
    click.echo(f"Created: {project.manifest_path}")
    if warnings:
        click.echo("Warnings:")
        for warning in warnings:
            click.echo(f"  {warning}")
