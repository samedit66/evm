# Manifest reference

`Eiffel.toml` records portable project intent. `Eiffel.lock` records the exact
resolved dependency graph and, when configured, platform-specific toolchain
artifacts. Generated or discovered machine state does not belong in the
manifest.

This page is a user-oriented reference. [`SPEC.md`](../SPEC.md) defines the
normative format and behavior.

## Minimal application

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

EVM infers the managed ECF path, default target, development and release modes,
test target, toolchain selection, and local dependency layout when they are not
declared.

## `[project]`

| Field | Meaning |
|---|---|
| `name` | Package name, unique within a workspace |
| `version` | Project semantic version |
| `type` | `application` or `library` |
| `uuid` | Stable ECF system UUID |
| `ecf` | ECF path relative to the manifest |
| `ecf-managed` | Whether EVM generates the ECF |
| `default-target` | Target used when `--target` is omitted |

Managed projects require `name`, `version`, `type`, and `uuid`. Renaming a
project or regenerating its ECF does not change the UUID. Imported projects
normally set `ecf-managed = false` and retain the source ECF as authoritative.

## `[root]` and `[sources]`

An application root identifies its Eiffel class and creation feature:

```toml
[root]
class = "APPLICATION"
feature = "make"

[sources]
clusters = ["src", "generated"]
```

Paths are relative to the package directory and use portable separators.
Library projects do not require an application root.

## `[compatibility]`

Compatibility limits acceptable providers and versions without selecting an
installation:

```toml
[compatibility]
compilers = ["ise >=25.12,<26", "gobo =26.06.30"]
```

Entries are ordered by preference. Constraints compare sequences of numeric
components and support `=`, `>=`, `>`, `<=`, and `<`. Operators `^`, `~`,
wildcards, and prerelease identifiers are not supported by the initial format.

## `[requires]`

Requirements state positive language and compiler capabilities:

```toml
[requires]
standard = "ecma"
void-safety = "all"
concurrency = "none"
ise-semantics = "25.12"
```

| Field | Allowed values |
|---|---|
| `standard` | `ecma`, `ise` |
| `void-safety` | `none`, `conformance`, `initialization`, `all` |
| `concurrency` | `none`, `thread`, `scoop` |
| `ise-semantics` | Numeric ISE semantics version |

Unknown keys are errors. Omission means that the project does not require the
capability; it does not assert that a feature is disabled. Managed targets
still default to `concurrency = "none"` unless SCOOP is requested explicitly.

## `[toolchain]`

An exact project policy contains a default and finite compilation matrix:

```toml
[toolchain]
default = "gobo@26.06"
matrix = ["gobo@26.06", "ise@25.12"]
```

Project selectors must contain exact numeric versions. `default` must occur in
`matrix`, and duplicates are rejected. Use `evm toolchain use` to resolve
channels and update the manifest and lock file transactionally.

`[compatibility]`, `[requires]`, and `[toolchain]` have different roles:

- compatibility says what may be used;
- requirements say what behavior is needed;
- toolchain fixes what this project normally uses and tests.

## Dependencies

Runtime and development-only dependencies use separate tables:

```toml
[dependencies]
json = { source = "iron", version = "25.02" }
time = { source = "ise" }
xml = { source = "gobo", library = "xml" }
shared = { source = "path", path = "../shared" }
http = { source = "git", git = "https://example.com/http.git", tag = "v1.4.2", ecf = "library/http.ecf" }

[dev-dependencies]
testing = { source = "gobo", library = "test" }
```

Git dependencies use exactly one of `tag`, `branch`, or `rev`. Optional
`subdir` selects a package inside a repository, and `ecf` selects its library
configuration. Prefer `evm add`, `remove`, and `update` over manual edits so the
manifest, lock file, materialized packages, and managed ECF remain consistent.

See [Package management](dependencies.md) for source behavior and locking.

## Targets and modes

Targets represent distinct Eiffel systems, roots, or source sets. Development
and release are build modes rather than separate targets. A standard project
does not need to declare either explicitly.

Use `evm explain --targets` to inspect the inferred and declared target model,
and `evm explain --target NAME` to inspect inheritance and effective settings.

## Conditions

Portable conditions use structured array entries:

```toml
[[conditions]]
when = { os = "windows" }
sources = ["src/windows"]

[[conditions]]
when = { os = "unix", compiler = "gobo" }
sources = ["src/unix"]
```

Separate entries are alternatives; multiple fields inside one `when` table all
need to match. `evm explain` shows applied and excluded elements and their
reasons.

The initial predicate fields are `os`, `architecture`, `compiler`, and `mode`.

## Named targets and compiler escape hatch

Additional systems use named targets:

```toml
[targets.server]
root = "SERVER.make"
sources = ["src", "server"]
```

Rare provider-specific arguments have one explicit escape hatch:

```toml
[compiler.ise]
arguments = ["-some-option"]

[compiler.gobo]
arguments = ["--gc=boehm"]
```

These arguments apply only to the named adapter and remain visible to `check`,
`explain`, and ECF comparison.

## `[test]`

```toml
[test]
target = "test"
runner = "autotest"
```

`runner` accepts `auto`, `target`, `getest`, or `autotest`. EVM infers a
conventional test target when possible. See [Testing and project tasks](testing-and-tasks.md).

## Scripts

A short task contains one EVM command and is never interpreted by a shell:

```toml
[scripts]
verify = "check --configuration-only"
```

A structured task combines portable built-in operations:

```toml
[scripts.ci]
steps = [
    { command = "check", configuration-only = true },
    { command = "test" },
    { command = "build", release = true },
]
```

Explicit `{ shell = "..." }` steps are non-portable and require
`evm task --allow-build-scripts`.

## `[workspace]`

```toml
[workspace]
members = ["packages/core", "packages/parser", "apps/compiler"]
```

Each member has its own manifest, lock file, ECF, and build output. Members
share the workspace root `.evm/` and are processed in dependency order.

## `[ecf]`

Managed projects may include a validated ECF overlay for native constructs that
the high-level manifest cannot represent:

```toml
[ecf]
include = ["project.overlay.ecf"]
```

An overlay is an escape hatch, not a second project manifest. See
[ECF interoperability](ecf.md).

## Publication metadata

IRON publication metadata lives in `[package]`, `[package.links]`, and
`[package.iron]`. It does not become a dependency source. Use
`evm iron export` to generate `package.iron`; see
[Projects and manifests](projects.md#iron-package-metadata).
