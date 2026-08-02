# Projects and manifests

## Creating a project

Create an application:

```console
$ evm new hello
```

Create a library:

```console
$ evm new shared --lib
```

New targets explicitly declare non-SCOOP concurrency support. Enable SCOOP
when creating a project that requires it:

```console
$ evm new concurrent_service --scoop
```

This records `concurrency = "scoop"` in `[requires]`; omitting the option keeps
the target compatible with libraries that support only non-SCOOP execution.

Initialize an existing directory without overwriting its files:

```console
$ evm init
$ evm init --lib
```

A generated application contains:

```text
hello/
├── Eiffel.toml
├── Eiffel.lock
├── hello.ecf
├── src/
├── tests/
├── .evm/
└── build/
```

## State ownership

| Path | Responsibility | Commit? |
|---|---|:---:|
| `Eiffel.toml` | Project intent, targets, direct dependencies, workflows | Yes |
| `Eiffel.lock` | Exact dependency graph and locked toolchain artifacts | Yes |
| `<project>.ecf` | Native Eiffel configuration | Yes |
| `.evm/` | Installed dependencies, source cache, locks, temporary state | No |
| `build/` | Compiler products | No |

Recommended `.gitignore`:

```gitignore
.evm/
build/
```

Deleting `.evm/` is safe. `evm install --locked` reconstructs it from the
manifest and lock file.

## Minimal manifest

```toml
[project]
name = "hello"
version = "0.1.0"
type = "application"
uuid = "2e17c4af-2d3f-4ca5-95d0-e69be3cd02f1"

[root]
class = "APPLICATION"
feature = "make"

[sources]
clusters = ["src"]
```

The manifest describes intent rather than machine-specific state. EVM infers
the default target, development and release modes, compiler selection, local
package layout, and a conventional test target when explicit configuration is
unnecessary.

Add `project.ecf`, `project.ecf-managed`, or `project.default-target` only when
the inferred managed configuration is not sufficient. See the
[Manifest reference](manifest-reference.md) for every supported section.

## Toolchain policy

A portable project can omit all toolchain configuration. EVM then selects a
compatible installed provider deterministically. Projects that need explicit
compatibility and capabilities can declare them separately:

```toml
[compatibility]
compilers = ["ise >=25.12,<26", "gobo =26.06.30"]

[requires]
void-safety = "all"
concurrency = "none"
```

Fix the default and compilation matrix through the CLI:

```console
$ evm toolchain use gobo@26.06 ise@25.12
$ evm toolchain install --project --locked
$ evm check --toolchain all
```

Exact platform artifacts are recorded in `Eiffel.lock`; compiler distributions
are stored once in the EVM user store, not below the project `.evm/`. See
[Toolchains](toolchains.md) for selection order, managed installations, linked
installations, and matrices.

## IRON package metadata

Projects intended for IRON publication can keep metadata in `Eiffel.toml`:

```toml
[package]
title = "Example library"
description = "Reusable Eiffel components."
license = "MIT"
tags = ["example", "library"]

[package.links]
source = { title = "Source", url = "https://example.com/source" }

[package.iron]
maps = ["/example/library"]
```

Generate or verify the IRON interoperability file explicitly:

```console
$ evm iron export
$ evm iron export --check
```

`Eiffel.toml` remains authoritative. EVM validates an existing `package.iron`
during configuration checks but does not read dependencies from it.

## Check and explain

Validate configuration without invoking an Eiffel compiler:

```console
$ evm check --configuration-only
```

Run compiler-backed validation:

```console
$ evm check
$ evm check --release
$ evm check --target server
```

Inspect the effective configuration:

```console
$ evm explain
$ evm explain --target server
$ evm explain --release --toolchain gobo
$ evm explain --json
```

List effective targets:

```console
$ evm explain --targets
```

Targets are declared in `Eiffel.toml`. Development and release are build modes,
not separate targets.

## Build and run

```console
$ evm build
$ evm build --release
$ evm build --target server
$ evm run
$ evm run --target server -- --port 8080
```

Arguments following `--` are passed directly to the application.

Build output is isolated by compiler, target, and mode:

```text
build/<compiler>/<target>/<mode>/
```

Before compiling, EVM restores locked dependencies and verifies that the ECF
matches the resolved project.

## Clean generated state

Remove build output:

```console
$ evm clean
```

Remove materialized dependencies:

```console
$ evm clean --dependencies
```

Remove only state no longer reachable from the current lock file:

```console
$ evm clean --unused
```

See [ECF interoperability](ecf.md) for managed and legacy configuration.
