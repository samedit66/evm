<div align="center">

# `evm`

### A project and dependency manager for Eiffel

**Human-readable projects. Reproducible dependencies. Native Eiffel toolchains.**

[![Python 3.14+](https://img.shields.io/badge/Python-3.14%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Language: Eiffel](https://img.shields.io/badge/language-Eiffel-6f42c1)](https://www.eiffel.org/)
[![ISE Eiffel](https://img.shields.io/badge/toolchain-ISE%20Eiffel-17365D)](https://www.eiffel.com/)
[![Gobo Eiffel](https://img.shields.io/badge/toolchain-Gobo%20Eiffel-8B5A2B)](https://www.gobosoft.com/)
![Status: early development](https://img.shields.io/badge/status-early%20development-E3A008)

EVM provides one workflow for creating, building, testing, and sharing Eiffel
projects while keeping ECF, EiffelStudio, `ec`, and `gec` first-class citizens.

[Quick start](#quick-start) ·
[Installation](doc/installation.md) ·
[Package management](doc/dependencies.md) ·
[CLI reference](doc/cli-reference.md) ·
[`SPEC.md`](SPEC.md)

</div>

> [!IMPORTANT]
> EVM is under active development. The current package version is `0.1.0`;
> the CLI and manifest format may evolve. [`SPEC.md`](SPEC.md) is the source of
> truth for product requirements and behavior.

## Why EVM?

ECF is an expressive description of an Eiffel system, but it is primarily a
compiler configuration format. By itself, it does not record where every
dependency comes from, which exact revisions produced a build, or how the full
project state can be restored on another machine.

EVM adds that project-management layer:

```text
Eiffel.toml          human-readable project intent
     +
Eiffel.lock          exact, reproducible dependency graph
     ↓
standard ECF         EiffelStudio, ISE Eiffel, and Gobo Eiffel
```

EVM offers:

- one CLI for creating, checking, building, running, and testing projects;
- reproducible IRON, Git, local-path, ISE, and Gobo dependencies;
- deterministic ECF generation without replacing the native format;
- project-local dependency state below `.evm/`;
- safe adoption of existing ECF projects;
- portable project tasks and multi-package workspaces;
- stable JSON output for CI.

EVM does **not** implement an Eiffel compiler or require abandoning
EiffelStudio, Gobo, or existing ECF-based tooling.

## Quick start

### Create and run an application

```console
$ evm new hello
$ cd hello
$ evm check --configuration-only
$ evm build
$ evm run
```

Arguments after `--` are passed directly to the application:

```console
$ evm run -- --example-argument
```

### Create a library

```console
$ evm new shared --lib
$ cd shared
$ evm build
```

To initialize the current directory without overwriting existing files:

```console
$ evm init
```

### Add a dependency

IRON is the default package source:

```console
$ evm add json@25.02
```

EVM also supports libraries distributed with Eiffel toolchains, Git
repositories, and local projects:

```console
$ evm add time --source ise
$ evm add gobo_xml --source gobo --library xml
$ evm add shared --path ../shared
$ evm add json_git \
    --git https://github.com/eiffelhub/json.git \
    --branch master \
    --ecf library/json.ecf
```

`evm add` updates `Eiffel.toml`, resolves the complete graph, records exact
identities in `Eiffel.lock`, installs packages below `.evm/`, and synchronizes
the managed ECF.

Restore an existing project from its lock file:

```console
$ evm install --locked
```

See [Package management](doc/dependencies.md) for sources, updates, offline
operation, graph inspection, and reproducibility guarantees.

## Project layout

A new project keeps human-authored configuration separate from reproducible
and generated state:

```text
hello/
├── Eiffel.toml          # project intent and direct dependencies
├── Eiffel.lock          # exact resolved dependency graph
├── hello.ecf            # native Eiffel configuration
├── src/                 # production Eiffel sources
├── tests/               # test Eiffel sources
├── .evm/                # local dependencies and derived state
└── build/               # compiler output
```

Commit `Eiffel.toml`, `Eiffel.lock`, and the managed ECF. Ignore `.evm/` and
`build/`; EVM can reconstruct them.

## Documentation

Until a separate documentation website is available, the complete user guide
lives in [`doc/`](doc/).

| Guide | Contents |
|---|---|
| [Installation](doc/installation.md) | Requirements, installation, and environment diagnosis |
| [Projects and manifests](doc/projects.md) | Project layout, `Eiffel.toml`, targets, build modes, and daily workflow |
| [Package management](doc/dependencies.md) | Dependency sources, locking, installation, updates, graph inspection, and offline mode |
| [Toolchains](doc/toolchains.md) | ISE and Gobo discovery, selection, capabilities, and build output |
| [ECF interoperability](doc/ecf.md) | Managed ECF, overlays, legacy mode, and importing existing projects |
| [Testing and tasks](doc/testing-and-tasks.md) | Test target selection, filters, and manifest-defined workflows |
| [Workspaces and CI](doc/workspaces-and-ci.md) | Multi-package repositories, package selection, JSON output, and reproducible CI |
| [CLI reference](doc/cli-reference.md) | Every public command and option |
| [Development](doc/development.md) | Local setup, checks, tests, and contribution conventions |

Every command also provides built-in help:

```console
$ evm --help
$ evm add --help
$ evm build --help
```

## Project status

The current implementation covers normalized manifests, managed ECF
generation, ISE and Gobo compiler adapters, reproducible dependency locking,
project-local dependency materialization, legacy ECF compatibility, tests,
project workflows, and multi-package workspaces.

For normative behavior, file formats, design boundaries, and acceptance
criteria, see [`SPEC.md`](SPEC.md).

---

<div align="center">

**EVM — modern project ergonomics for the native Eiffel ecosystem.**

</div>
