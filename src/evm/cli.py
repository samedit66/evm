"""Command-line interface for EVM."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import wraps
from pathlib import Path
from typing import Any

import click

from evm.dependencies import clean_unused, dependency_tree_lines
from evm.dependency_commands import (
    add_dependency,
    install_project,
    remove_dependency,
    update_dependencies,
)
from evm.discovery import discover_environment, discovery_lines
from evm.ecf import semantic_diff
from evm.errors import EvmError
from evm.lockfile import LOCK_NAME, load_lock
from evm.model import BuildRequest, Dependency, Project
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
from evm.tasks import run_task
from evm.testing import TestRequest, test_project
from evm.workspace import ProjectContext, load_project_context, workspace_tree_lines


@dataclass(frozen=True)
class _AddCommandOptions:
    development: bool
    source: str | None
    git_url: str | None
    dependency_path: str | None
    tag: str | None
    branch: str | None
    revision: str | None
    library: str | None
    ecf: str | None
    subdir: str | None
    offline: bool


@dataclass(frozen=True)
class _TestCommandOptions:
    release: bool
    compiler: str | None
    class_name: str | None
    feature: str | None
    offline: bool
    regenerate_ecf: bool
    package: str | None
    output_json: bool


@dataclass(frozen=True)
class _CheckCommandOptions:
    configuration_only: bool
    release: bool
    compiler: str | None
    target: str
    regenerate_ecf: bool
    package: str | None
    output_json: bool


@dataclass(frozen=True)
class _BuildCommandOptions:
    release: bool
    compiler: str | None
    target: str
    offline: bool
    regenerate_ecf: bool
    package: str | None
    output_json: bool


def command_errors[**P, R](function: Callable[P, R]) -> Callable[P, R]:
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
@click.option("--package", type=str, help="Limit a workspace command to one package.")
@click.option("--json", "output_json", is_flag=True, help="Emit stable JSON for CI.")
@command_errors
def check_command(
    **raw_options: Any,
) -> None:
    """Validate project configuration and reachable Eiffel classes."""
    options = _check_command_options(raw_options)
    projects = _command_projects(load_project_context(), options.package)
    results: list[dict[str, str]] = []
    for project in projects:
        if options.configuration_only:
            changed = prepare_project(
                project,
                regenerate=options.regenerate_ecf,
                release=options.release,
            )
            if changed and not options.output_json:
                click.echo(f"Generated {project.ecf_path}")
        else:
            compile_project(
                project,
                BuildRequest(
                    compiler=options.compiler,
                    target=options.target,
                    release=options.release,
                    regenerate_ecf=options.regenerate_ecf,
                ),
                check_only=True,
                announce=not options.output_json,
            )
        results.append({"package": project.name, "status": "passed"})
        if not options.output_json:
            click.echo(f"{project.name}: project check completed.")
    if options.output_json:
        _echo_json_result("passed", results)


@main.command("build")
@click.option("--release", is_flag=True, help="Build in release mode.")
@click.option("--compiler", type=str, help="Compiler adapter ID: ise or gobo.")
@click.option("--target", default="default", show_default=True)
@click.option("--offline", is_flag=True, help="Forbid network access.")
@click.option("--regenerate-ecf", is_flag=True, help="Explicitly overwrite managed ECF.")
@click.option("--package", type=str, help="Limit a workspace build to one package.")
@click.option("--json", "output_json", is_flag=True, help="Emit stable JSON for CI.")
@command_errors
def build_command(
    **raw_options: Any,
) -> None:
    """Build an Eiffel application or library."""
    options = _build_command_options(raw_options)
    projects = _command_projects(load_project_context(), options.package)
    results: list[dict[str, str]] = []
    for project in projects:
        compile_project(
            project,
            BuildRequest(
                compiler=options.compiler,
                target=options.target,
                release=options.release,
                regenerate_ecf=options.regenerate_ecf,
                offline=options.offline,
            ),
            announce=not options.output_json,
        )
        results.append({"package": project.name, "status": "passed"})
        if not options.output_json:
            click.echo(f"{project.name}: build completed.")
    if options.output_json:
        _echo_json_result("passed", results)


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
    project = _require_project(load_project_context())
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


@main.command("discover")
@click.option("--json", "output_json", is_flag=True, help="Emit stable JSON for CI.")
@command_errors
def discover_command(output_json: bool) -> None:
    """Discover installed Eiffel components and native C toolchains."""
    result = discover_environment()
    if output_json:
        click.echo(json.dumps(result.to_dict(), sort_keys=True))
    else:
        click.echo("\n".join(discovery_lines(result)))


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
    project = _require_project(load_project_context())
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


@main.command("add")
@click.argument("package")
@click.option("--dev", is_flag=True, help="Add a development dependency.")
@click.option("--source", type=click.Choice(["ise", "gobo", "iron"]))
@click.option("--git", "git_url", type=str, help="Git repository URL.")
@click.option("--path", "dependency_path", type=str, help="Local dependency path.")
@click.option("--tag", type=str)
@click.option("--branch", type=str)
@click.option("--rev", type=str)
@click.option("--library", type=str, help="Distribution library name.")
@click.option("--ecf", type=str, help="ECF path inside the dependency.")
@click.option("--subdir", type=str, help="Package subdirectory in a Git repository.")
@click.option("--offline", is_flag=True, help="Forbid network access.")
@command_errors
def add_command(
    package: str,
    **raw_options: Any,
) -> None:
    """Add, resolve, lock, and install a dependency."""
    options = _add_command_options(raw_options)
    name, version = _package_spec(package)
    source_count = sum(
        item is not None for item in (options.source, options.git_url, options.dependency_path)
    )
    if source_count > 1:
        raise EvmError("--source, --git, and --path are mutually exclusive")
    inferred_source = (
        "git"
        if options.git_url
        else "path"
        if options.dependency_path
        else options.source or "iron"
    )
    revision = [
        (kind, value)
        for kind, value in (
            ("tag", options.tag),
            ("branch", options.branch),
            ("rev", options.revision),
        )
        if value
    ]
    if inferred_source == "git" and len(revision) != 1:
        raise EvmError("a Git dependency requires exactly one of --tag, --branch, or --rev")
    if inferred_source != "git" and revision:
        raise EvmError("--tag, --branch, and --rev require --git")
    dependency = Dependency(
        name=name,
        source=inferred_source,
        version=version,
        git=options.git_url,
        requested_kind=revision[0][0] if revision else None,
        requested_value=revision[0][1] if revision else None,
        path=options.dependency_path,
        library=options.library,
        ecf=options.ecf,
        subdir=options.subdir,
        development=options.development,
    )
    project = _require_project(load_project_context())
    lock = add_dependency(project, dependency, offline=options.offline)
    selected = lock.package(name)
    click.echo(f"Added {selected.name} {selected.version} [{selected.source}]")


@main.command("remove")
@click.argument("package")
@click.option("--offline", is_flag=True, help="Forbid network access.")
@command_errors
def remove_command(package: str, offline: bool) -> None:
    """Remove a direct dependency and update the resolved graph."""
    project = _require_project(load_project_context())
    remove_dependency(project, package, offline=offline)
    click.echo(f"Removed {package}")


@main.command("update")
@click.argument("packages", nargs=-1)
@click.option("--precise", type=str, help="Pin one Git dependency to a commit.")
@click.option("--offline", is_flag=True, help="Forbid network access.")
@command_errors
def update_command(packages: tuple[str, ...], precise: str | None, offline: bool) -> None:
    """Resolve newer permitted dependency identities and install them."""
    project = _require_project(load_project_context())
    lock = update_dependencies(
        project,
        names=set(packages) if packages else None,
        precise=precise,
        offline=offline,
    )
    click.echo(f"Updated {len(lock.packages)} locked package(s)")


@main.command("install")
@click.option("--locked", is_flag=True, help="Require the existing lock file.")
@click.option("--offline", is_flag=True, help="Forbid network access.")
@click.option("--package", type=str, help="Limit a workspace install to one package.")
@command_errors
def install_command(locked: bool, offline: bool, package: str | None) -> None:
    """Materialize dependencies from Eiffel.lock."""
    projects = _command_projects(load_project_context(), package)
    for project in projects:
        if locked and not (project.directory / LOCK_NAME).is_file():
            raise EvmError(f"Eiffel.lock is required by --locked for package {project.name}")
        lock = install_project(project, offline=offline)
        click.echo(f"{project.name}: installed {len(lock.packages)} package(s)")


@main.command("deps")
@click.argument("package", required=False)
@click.option("--workspace", "show_workspace", is_flag=True, help="Show package dependencies.")
@command_errors
def deps_command(package: str | None, show_workspace: bool) -> None:
    """Show the resolved dependency graph or why a package is present."""
    context = load_project_context()
    if show_workspace:
        if package is not None:
            raise EvmError("a dependency name cannot be combined with --workspace")
        if context.workspace is None:
            raise EvmError("--workspace requires a workspace manifest")
        click.echo("\n".join(workspace_tree_lines(context.workspace)))
        return
    project = _require_project(context)
    lock = load_lock(project.directory / LOCK_NAME)
    click.echo("\n".join(dependency_tree_lines(project, lock, focus=package)))


@main.command("test")
@click.option("--release", is_flag=True, help="Build tests in release mode.")
@click.option("--compiler", type=str, help="Compiler adapter ID: ise or gobo.")
@click.option("--class", "class_name", type=str, help="Run one supported test class.")
@click.option("--feature", type=str, help="Run one supported test feature.")
@click.option("--offline", is_flag=True, help="Forbid network access.")
@click.option("--regenerate-ecf", is_flag=True, help="Explicitly overwrite managed ECF.")
@click.option("--package", type=str, help="Limit a workspace test to one package.")
@click.option("--json", "output_json", is_flag=True, help="Emit stable JSON for CI.")
@command_errors
def test_command(
    **raw_options: Any,
) -> None:
    """Build and run a configured Eiffel test system."""
    options = _test_command_options(raw_options)
    projects = _command_projects(load_project_context(), options.package)
    results: list[dict[str, object]] = []
    failed_code = 0
    for project in projects:
        result = test_project(
            project,
            TestRequest(
                compiler=options.compiler,
                release=options.release,
                class_name=options.class_name,
                feature=options.feature,
                offline=options.offline,
                regenerate_ecf=options.regenerate_ecf,
                capture_output=options.output_json,
            ),
        )
        data = {"package": project.name, **result.as_dict()}
        results.append(data)
        failed_code = failed_code or result.exit_code
        if not options.output_json:
            click.echo(f"Package: {project.name}")
            click.echo(f"Status: {result.status}")
            click.echo(f"Exit code: {result.exit_code}")
            click.echo(f"Time: {result.elapsed_seconds:.2f}s")
            click.echo(f"Compiler: {result.compiler}")
            click.echo(f"Runner: {result.runner}")
    if options.output_json:
        _echo_json_result("failed" if failed_code else "passed", results)
    if failed_code:
        raise click.exceptions.Exit(failed_code)


@main.command("task")
@click.argument("name")
@click.option(
    "--allow-build-scripts",
    is_flag=True,
    help="Allow explicitly declared non-portable shell steps.",
)
@command_errors
def task_command(name: str, allow_build_scripts: bool) -> None:
    """Run a manifest-defined project workflow."""
    context = load_project_context()
    project = _require_project(context)
    previous_directory = Path.cwd()
    try:
        os.chdir(project.directory)
        run_task(
            project,
            name,
            _run_nested_command,
            allow_build_scripts=allow_build_scripts,
        )
    finally:
        os.chdir(previous_directory)
    click.echo(f"Task {name!r} completed.")


@main.command("clean")
@click.option("--dependencies", is_flag=True, help="Remove all materialized dependencies.")
@click.option("--unused", is_flag=True, help="Remove only state unused by Eiffel.lock.")
@command_errors
def clean_command(dependencies: bool, unused: bool) -> None:
    """Remove selected generated project state."""
    if dependencies and unused:
        raise EvmError("--dependencies and --unused are mutually exclusive")
    project = _require_project(load_project_context())
    if unused:
        lock = load_lock(project.directory / LOCK_NAME)
        removed_deps, removed_sources = clean_unused(project, lock)
        click.echo(
            f"Removed {removed_deps} unused dependency directory(s) "
            f"and {removed_sources} unused source(s)"
        )
        return
    target = project.state_directory / "deps" if dependencies else project.directory / "build"
    if target.is_dir():
        shutil.rmtree(target)
    click.echo(f"Removed {target}")


def _package_spec(value: str) -> tuple[str, str | None]:
    if "@" not in value:
        return value, None
    name, version = value.rsplit("@", 1)
    if not name or not version:
        raise EvmError("package must have the form name or name@version")
    return name, version


def _add_command_options(values: Mapping[str, Any]) -> _AddCommandOptions:
    return _AddCommandOptions(
        development=values["dev"],
        source=values["source"],
        git_url=values["git_url"],
        dependency_path=values["dependency_path"],
        tag=values["tag"],
        branch=values["branch"],
        revision=values["rev"],
        library=values["library"],
        ecf=values["ecf"],
        subdir=values["subdir"],
        offline=values["offline"],
    )


def _test_command_options(values: Mapping[str, Any]) -> _TestCommandOptions:
    return _TestCommandOptions(
        release=values["release"],
        compiler=values["compiler"],
        class_name=values["class_name"],
        feature=values["feature"],
        offline=values["offline"],
        regenerate_ecf=values["regenerate_ecf"],
        package=values["package"],
        output_json=values["output_json"],
    )


def _check_command_options(values: Mapping[str, Any]) -> _CheckCommandOptions:
    return _CheckCommandOptions(
        configuration_only=values["configuration_only"],
        release=values["release"],
        compiler=values["compiler"],
        target=values["target"],
        regenerate_ecf=values["regenerate_ecf"],
        package=values["package"],
        output_json=values["output_json"],
    )


def _build_command_options(values: Mapping[str, Any]) -> _BuildCommandOptions:
    return _BuildCommandOptions(
        release=values["release"],
        compiler=values["compiler"],
        target=values["target"],
        offline=values["offline"],
        regenerate_ecf=values["regenerate_ecf"],
        package=values["package"],
        output_json=values["output_json"],
    )


def _require_project(context: ProjectContext) -> Project:
    if context.project is not None:
        return context.project
    raise EvmError("this command requires a package; run it inside a workspace member")


def _command_projects(
    context: ProjectContext,
    package: str | None,
) -> tuple[Project, ...]:
    if context.workspace is not None:
        return context.workspace.ordered_packages(package)
    if package is not None:
        raise EvmError("--package requires a workspace")
    return (_require_project(context),)


def _echo_json_result(status: str, packages: list[dict[str, object]]) -> None:
    click.echo(json.dumps({"status": status, "packages": packages}, sort_keys=True))


def _run_nested_command(arguments: tuple[str, ...]) -> int:
    try:
        main.main(args=list(arguments), standalone_mode=False)
    except click.exceptions.Exit as error:
        return error.exit_code
    except click.ClickException as error:
        error.show()
        return error.exit_code
    return 0
