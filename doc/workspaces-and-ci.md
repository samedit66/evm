# Workspaces and CI

## Workspaces

A workspace groups related Eiffel packages in one repository:

```toml
[workspace]
members = [
    "packages/core",
    "packages/parser",
    "apps/compiler",
]
```

Each member retains its own:

- `Eiffel.toml`;
- `Eiffel.lock`;
- ECF;
- build directory.

Packages share the workspace root `.evm/`. EVM processes them in dependency
order.

Run commands for the entire workspace:

```console
$ evm check
$ evm build
$ evm test
$ evm install
```

Limit an operation to one package and its workspace dependencies:

```console
$ evm check --package parser
$ evm build --package parser
$ evm test --package parser
$ evm install --package parser
```

Display the workspace package graph:

```console
$ evm deps --workspace
```

Packages remain independently usable through their own manifest and ECF. Their
generated ECF paths are valid within the workspace layout; copying a package
out of the workspace requires a new install and generation in the new root.

## JSON output

CI-oriented commands expose stable machine-readable output:

```console
$ evm discover --json
$ evm check --json
$ evm build --json
$ evm test --json
```

Errors are represented as structured diagnostics and commands retain meaningful
non-zero exit codes.

## Reproducible CI

A typical sequence is:

```console
$ evm install --locked
$ evm discover --json
$ evm check --toolchain ise --json
$ evm test --toolchain ise --json
$ evm build --toolchain ise --release --json
```

For deterministic CI:

- commit `Eiffel.toml`, `Eiffel.lock`, and the managed ECF;
- use `evm install --locked`;
- select the compiler explicitly with `--toolchain` or `EVM_TOOLCHAIN`;
- use `--offline` after all required sources have been populated;
- do not treat `.evm/`, `build/`, `EIFGENs`, or generated C as portable state.

Example environment selection:

```console
$ EVM_TOOLCHAIN=gobo evm test --json
```

When the project commits an exact `[toolchain]` matrix and its locked
artifacts, CI can reproduce the compiler environment before restoring package
dependencies:

```console
$ evm toolchain install --project --locked
$ evm toolchain verify --project --json
$ evm install --locked
$ evm check --toolchain all --json
$ evm test --toolchain all --json
$ evm build --toolchain all --release --json
```

The toolchain store may be shared by projects or cached by CI. Package
dependencies remain confined to the project or workspace `.evm/` directory.

An offline verification step can ensure that a populated build does not access
the network:

```console
$ evm install --locked --offline
$ evm test --offline --json
$ evm build --offline --release --json
```
