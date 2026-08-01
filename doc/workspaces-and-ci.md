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
$ evm check --compiler ise --json
$ evm test --compiler ise --json
$ evm build --compiler ise --release --json
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

An offline verification step can ensure that a populated build does not access
the network:

```console
$ evm install --locked --offline
$ evm test --offline --json
$ evm build --offline --release --json
```
