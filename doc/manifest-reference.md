# Manifest reference

`Eiffel.toml` records portable project intent. `Eiffel.lock` records the exact
resolved dependency graph and, when configured, platform-specific toolchain
artifacts. Generated or discovered machine state does not belong in the
manifest.

This page is the user-facing contract for the manifest format. Every accepted
top-level section and field is listed here. Unknown sections and fields are
errors: EVM does not silently ignore misspelled or future configuration.

## Contract conventions

`Eiffel.toml` is UTF-8 TOML. Paths are relative to the directory containing the
manifest unless a field says otherwise. Managed project paths must stay inside
the project directory; dependency-local `ecf` and `subdir` paths must stay
inside that dependency.

The only sections required in every manifest are `[project]` and `[sources]`.
Applications additionally require `[root]`. All other sections are optional.

| Top-level section | Required | Purpose |
|---|---:|---|
| `[project]` | Yes | Project identity, type, ECF ownership, and default target |
| `[root]` | Applications only | Root class and creation feature of the default target |
| `[sources]` | Yes | Source clusters of the default target |
| `[compatibility]` | No | Permitted toolchain adapters and version constraints |
| `[toolchain]` | No | Exact default toolchain and compilation matrix |
| `[requires]` | No | Required portable language/compiler capabilities |
| `[targets.<name>]` | No | Additional named targets and inheritance |
| `[[conditions]]` | No | Conditional sources and external objects |
| `[compiler.<adapter>]` | No | Explicit compiler-specific arguments |
| `[dependencies]` | No | Runtime dependency declarations |
| `[dev-dependencies]` | No | Development and test dependencies |
| `[patch.<name>]` | No | Local replacement for a declared dependency |
| `[ecf]` | No | Managed ECF overlay files |
| `[test]` | No | Test target and runner selection |
| `[scripts]` / `[scripts.<name>]` | No | Short and structured project tasks |
| `[workspace]` | No | Member packages of a workspace root |
| `[package]` | No | IRON publication metadata |

Names used for projects, dependencies, targets, and tasks start with a letter
and contain only letters, digits, `_`, or `-`.

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

| Field | Required | Type / default | Meaning |
|---|---:|---|---|
| `name` | Yes | Name | Package name, unique within a workspace |
| `version` | Yes | Semantic version | Project version, for example `0.1.0` or `1.0.0-beta.1` |
| `type` | Yes | `application` or `library` | Determines whether an application root is required |
| `uuid` | Yes | UUID string | Stable ECF system UUID; generation and import preserve it |
| `ecf` | No | `<name>.ecf` | ECF path relative to the manifest |
| `ecf-managed` | No | Boolean, `true` | Whether EVM owns and generates the ECF |
| `default-target` | No | Name, `default` | Target selected when `--target` is omitted |

Renaming a project or regenerating its ECF does not change the UUID. Imported
projects normally set `ecf-managed = false` and retain the source ECF as
authoritative. A managed ECF path cannot escape the project directory.

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

| Section and field | Required | Type | Meaning |
|---|---:|---|---|
| `root.class` | Applications only | Non-empty string | Eiffel root class name |
| `root.feature` | Applications only | Non-empty string | Root creation procedure |
| `sources.clusters` | Yes | Non-empty string array | Source directories for the default target |

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
The experimental revision-based `serpent` and `liberty` adapters may be listed
without a constraint.

`compatibility.compilers` is the only field in the section and is required when
the section is present. Each adapter may appear once. Omitting the section lets
EVM select any compatible built-in adapter.

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
| `standard` | Optional: `ecma`, `ise` |
| `void-safety` | Optional: `none`, `conformance`, `initialization`, `all` |
| `concurrency` | Optional: `none`, `thread`, `scoop` |
| `ise-semantics` | Optional numeric ISE semantics version |

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

ISE and Gobo selectors must contain exact numeric versions. Experimental
Serpent and Liberty selectors must contain exact Git commits. `default` must
occur in `matrix`, and duplicates are rejected. Use `evm toolchain use` to
resolve channels and update the manifest and lock file transactionally.

| Field | Required | Type / default | Meaning |
|---|---:|---|---|
| `default` | Yes | Exact selector | Toolchain used when no higher-priority selector is supplied |
| `matrix` | No | String array, `[default]` | Finite set used by matrix operations and locked installation |

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

A string value is shorthand for an IRON version:

```toml
[dependencies]
json = "25.02"
```

Dependency inline tables accept the following fields:

| Field | Required | Applies to | Meaning |
|---|---:|---|---|
| `source` | No | All | `ise`, `gobo`, `iron`, `git`, or `path`; inferred from `git` or `path`, otherwise `iron` |
| `version` | IRON only | IRON and distribution sources | Requested package or library version |
| `git` | Git only | Git | Repository URL |
| `tag` | Exactly one revision selector | Git | Tag resolved to immutable Git identities |
| `branch` | Exactly one revision selector | Git | Branch resolved to immutable Git identities |
| `rev` | Exactly one revision selector | Git | Revision resolved to a full commit |
| `path` | Path only | Path | Relative path from `Eiffel.toml`; absolute paths are rejected |
| `library` | Gobo only | ISE/Gobo | Distribution library identifier; required for Gobo |
| `ecf` | No | Git/path/distribution | ECF path inside the dependency |
| `subdir` | No | Git | Package directory inside a repository checkout |

`[dev-dependencies]` accepts the same representation. A dependency name cannot
appear in both tables. EVM-provided implicit runtime libraries cannot be
declared as ordinary dependencies.

### `[patch.<name>]`

A patch redirects an already declared dependency to a live local directory:

```toml
[patch.json]
path = "../json-working-copy"
```

`path` is the only field and is required. It must be relative to the manifest.
The patch name must match a dependency from `[dependencies]` or
`[dev-dependencies]`. Active patches are visible in diagnostics and are not
portable locked-release inputs.

See [Package management](dependencies.md) for source behavior and locking.

## Targets and modes

Targets represent distinct Eiffel systems, roots, or source sets. Development
and release are build modes rather than separate targets. A standard project
does not need to declare either explicitly.

Use `evm explain --targets` to inspect the inferred and declared target model,
and `evm explain --target NAME` to inspect inheritance and effective settings.

```toml
[targets.server]
root = "SERVER.make"
sources = ["server"]
extends = "default"
```

| Field | Required | Type | Meaning |
|---|---:|---|---|
| `root` | Applications: `root` or `extends` | `CLASS.feature` | Root override for the target |
| `sources` | No | Non-empty string array | Additional or standalone source clusters |
| `extends` | No | Target name | Parent target whose effective settings are inherited |

A target cannot reuse `project.default-target`. Parent targets must exist, and
inheritance cycles are rejected.

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

| Field | Required | Values / default | Meaning |
|---|---:|---|---|
| `target` | No | Default target | Target receiving the conditional values |
| `when` | Yes | Non-empty inline table | Predicate; all contained fields must match |
| `sources` | One output required | String array | Conditional source clusters |
| `external-objects` | One output required | String array | Conditional native objects or link inputs |

`when.os` accepts `windows`, `unix`, `macos`, or `darwin`;
`when.compiler` accepts `ise` or `gobo`; `when.mode` accepts `dev` or `release`.
`when.architecture` is a non-empty platform architecture identifier. A
condition must provide `sources`, `external-objects`, or both.

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

Only `compiler.ise` and `compiler.gobo` are accepted. `arguments` is the only
field in either adapter table and, when present, is a non-empty string array.

## `[test]`

```toml
[test]
target = "test"
runner = "autotest"
```

`runner` accepts `auto`, `target`, `getest`, or `autotest`. EVM infers a
conventional test target when possible. See [Testing and project tasks](testing-and-tasks.md).

| Field | Required | Type / default | Meaning |
|---|---:|---|---|
| `target` | Yes when `[test]` exists | Existing target name | Target compiled and executed for tests |
| `runner` | No | `auto` | `auto`, `target`, `getest`, or `autotest` |

When `[test]` is absent, EVM uses a declared target named `test`. For a managed
project with Eiffel files below `tests/`, it can infer a conventional test
target and `auto` runner.

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

Task names follow the normal manifest name grammar. A short task must contain
one non-empty EVM command. Shell operators, redirects, substitutions, and
pipes are rejected.

Each structured task requires one non-empty `steps` array. Every step defines
exactly one of:

- `command` with optional `release`, `compiler`, `target`, `offline`,
  `configuration-only`, and `package` options;
- `shell`, with no other fields.

Boolean command options are emitted only when true. String options must be
non-empty. A nested `task` command must name another declared task, and runtime
recursion is rejected.

## `[workspace]`

```toml
[workspace]
members = ["packages/core", "packages/parser", "apps/compiler"]
```

Each member has its own manifest, lock file, ECF, and build output. Members
share the workspace root `.evm/` and are processed in dependency order.

`members` is the only field and is required when `[workspace]` is present. It
is a non-empty array of distinct relative directories. Every member directory
must contain `Eiffel.toml`; duplicate members, package-name conflicts, invalid
boundaries, and dependency cycles are rejected.

## `[ecf]`

Managed projects may include a validated ECF overlay for native constructs that
the high-level manifest cannot represent:

```toml
[ecf]
include = ["project.overlay.ecf"]
```

An overlay is an escape hatch, not a second project manifest. See
[ECF interoperability](ecf.md).

`include` is the only field and is required when `[ecf]` is present. It is a
non-empty array of overlay paths that stay inside the project directory.

## Publication metadata

IRON publication metadata lives in `[package]`, `[package.links]`, and
`[package.iron]`. It does not become a dependency source. Use
`evm iron export` to generate `package.iron`; see
[Projects and manifests](projects.md#iron-package-metadata).

| Field | Required | Type | Meaning |
|---|---:|---|---|
| `package.title` | No | Non-empty string | Human-readable package title |
| `package.description` | No | Non-empty string | Package summary |
| `package.license` | No | Non-empty string | License identifier or description |
| `package.copyright` | No | Non-empty string | Copyright statement |
| `package.tags` | No | Non-empty string array | Search and classification tags |
| `package.links.<category>` | No | URL string or table | Categorized external link |
| `package.links.<category>.url` | Yes for table form | Non-empty string | Link destination |
| `package.links.<category>.title` | No | Non-empty string | Display title |
| `package.iron.maps` | No | Non-empty string array | IRON map declarations |

Link category names must be non-empty. Package metadata affects IRON import and
export only; it does not alter dependency resolution.

## Validation and forward compatibility

EVM rejects:

- unknown top-level sections and unknown fields inside known sections;
- empty values where a non-empty string or array is required;
- absolute or escaping paths in portable project-owned fields;
- duplicate dependency names, toolchain selectors, or workspace members;
- target inheritance and workspace dependency cycles;
- incompatible toolchain policies and unsupported capability values.

This strictness is part of the contract: a typo must fail with a diagnostic
instead of producing a plausible but incorrect build. Before adopting a future
manifest field, use an EVM version whose documentation lists that field.
