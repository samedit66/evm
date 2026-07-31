# Migrating an existing Eiffel project

Use `evm import`, not `evm init`, when a project already has an ECF. The
initial migration keeps that ECF as the native source of truth and adds EVM's
manifest and lock file alongside it.

## Import in place

From the project root, select the ECF that represents the project:

```console
$ evm import project.ecf
```

The command creates exactly two files:

```text
Eiffel.toml
Eiffel.lock
```

It does not modify or copy the source ECF, source directories, or existing
toolchain metadata. If either output file already exists, import stops without
writing a partial result.

`evm init` is for scaffolding a new project. When it finds an existing ECF, it
prints the corresponding `evm import` command instead of overwriting the
project.

## Verify the migration

First validate the imported configuration without invoking a compiler:

```console
$ evm check --configuration-only
```

Then inspect the targets and build the default target:

```console
$ evm explain --targets
$ evm build
```

If the ECF has no target named `default`, EVM records the selected native name
as `project.default-target`. Commands without `--target` use that name. Other
targets remain available explicitly:

```console
$ evm build --target benchmark
```

## Migrate the test suite

Import recognizes an unambiguous target named `test` or `tests`. It also
recognizes a target that directly or indirectly includes the ISE `testing`
library. The resulting manifest declares the existing target explicitly:

```toml
[test]
target = "tests"
runner = "autotest"
```

A named test target without the `testing` library uses `runner = "target"`.
When several candidates exist, import reports them and leaves `[test]`
unconfigured instead of guessing.

Run the migrated suite with:

```console
$ evm test --compiler ise
```

The default result correlates failed AutoTest cases with the migrated source
clusters and prints `file:line`, `CLASS.feature`, the source assertion, and its
tag whenever that mapping is unambiguous. Use `evm test --trace` for normalized
exception metadata and the full Eiffel stack trace, or `evm test --raw` for
backend-oriented output without EVM's summary. JSON CI output retains all
structured diagnostics.

AutoTest discovery and generated runners use disposable files below `.evm/`;
the existing test sources and legacy ECF are not changed.

For a library ECF, `system@library_target` determines both the project type and
the default target. Application targets for tests, examples, or benchmarks do
not turn the whole package into an application.

## Legacy mode

The imported manifest starts in legacy mode:

```toml
[project]
ecf-managed = false
ecf = "project.ecf"
default-target = "project"
```

In this mode the original ECF retains options, capabilities, libraries,
conditions, and compiler-specific settings that do not have high-level
manifest fields. EVM reads and validates the file but never rewrites it.

ECF namespaces `configuration-1-18-0` and `configuration-1-23-0` are accepted
for legacy imports. Relative cluster paths may use either `/` or `\`; EVM
normalizes them to portable manifest paths. Unsupported namespaces and
absolute non-portable paths are reported explicitly.

When a legacy ECF uses platform-specific separators, build commands stage a
normalized derivative below `.evm/tmp/`. This is disposable build state: the
source ECF remains unchanged and import still creates only the manifest and
lock file. If an old `*-safe.ecf` runtime name is absent but its modern `.ecf`
counterpart is installed, the staged configuration uses the available name.

## Import from package.iron

An existing IRON package can be imported through its project declaration:

```console
$ evm import package.iron
```

When `package.iron` declares multiple projects, select one:

```console
$ evm import package.iron --project json
```

Known publication metadata is copied into `[package]`. IRON `setup`
declarations are reported and are never executed.

## Import to another directory

`--destination` can place the two EVM files elsewhere while continuing to
reference the original ECF:

```console
$ evm import ../legacy/project.ecf --destination imported
```

In-place import is preferred for adopting EVM in an existing repository
because its source paths stay project-relative.
