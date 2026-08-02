# Toolchains

EVM can use existing Eiffel installations or install verified distributions in
a shared user store. Projects declare compatibility and requirements
independently from an optional exact compilation matrix.

| Provider ID | Adapter | Main executable | Standard library |
|---|---|---|---|
| `ise` | ISE Eiffel | `ec` | EiffelBase |
| `gobo` | Gobo Eiffel | `gec` | FreeELKS |
| `serpent` | Serpent Eiffel (experimental) | managed Python worker | Serpent stdlib |
| `liberty` | Liberty Eiffel (experimental) | `se` | Liberty core |

`ise`, `gobo`, and exact values such as `gobo@26.06` are toolchain selectors.
Executable names, filesystem paths, and constraints such as `gobo >=26.06` are
not selectors.

Serpent and Liberty are explicit opt-in adapters. They support application
`build` and `run` only and never participate in the built-in automatic
fallback. Their exact selectors use Git commits rather than release versions.

## Discover the environment

```console
$ evm discover
$ evm discover --json
```

`discover` inventories known Eiffel and native C components from the
environment, `PATH`, the EVM store, and platform-specific discovery. It reports
active and shadowed installations without changing the system. An empty result
is successful.

Use `evm toolchain list` for the narrower inventory of installations registered
with EVM:

```console
$ evm toolchain list
$ evm toolchain list gobo --json
```

There is no separate `evm toolchain discover` command.

## Install a distribution

List official releases available for the current platform and install an exact
one:

```console
$ evm toolchain list --available
$ evm toolchain list gobo --available
$ evm toolchain install gobo@26.06
$ evm toolchain verify gobo@26.06
```

Managed distributions are downloaded, checksum-verified, and atomically
materialized. Repeating the installation reuses the verified copy.

The user store follows platform conventions and can be overridden with
`EVM_TOOLCHAIN_HOME`. It is outside the project: Eiffel package dependencies
remain project-local below `.evm/`, while one compiler installation can serve
multiple projects.

Offline installation uses only the verified download cache:

```console
$ evm toolchain install gobo@26.06 --offline
```

Install an experimental adapter from an immutable revision with:

```console
$ evm toolchain install serpent@c95ab517a5914ebc9e8d2ccf767a6d2e6caf49f2
$ evm toolchain install liberty@21b081378ec12798080128e7f39878d5d2097cb7
```

Serpent needs Python 3.13 or newer, `make`, GCC, Flex, Bison, and a JDK. EVM
installs it into an isolated virtual environment and compiles to JVM class
files. Liberty needs Git, Bash, GCC, and G++; its bootstrap remains inside the
managed checkout. Liberty cannot be newly installed offline.

## Link an existing installation

Register an existing EiffelStudio or Gobo installation without copying it:

```console
$ evm toolchain link /opt/EiffelStudio-25.12
$ evm toolchain list
```

EVM probes its provider and exact version before registration. Removing a
linked registration never deletes the external directory:

```console
$ evm toolchain remove ise@25.12
```

## Select a toolchain

Use the canonical option for one command:

```console
$ evm build --toolchain ise
$ evm build --toolchain gobo@26.06
$ evm build --toolchain serpent@c95ab517
$ evm run --toolchain liberty@21b0813
```

`--compiler` is a compatibility alias for `--toolchain`. `EVM_TOOLCHAIN`
accepts the same selector:

```console
$ EVM_TOOLCHAIN=gobo@26.06 evm build
```

EVM selects a compatible installation in this order:

1. `--toolchain`;
2. `EVM_TOOLCHAIN`;
3. `[toolchain].default`;
4. the first compatible entry in `[compatibility].compilers`;
5. the built-in provider order: ISE, then Gobo.

Executable order in `PATH` does not change provider priority. `evm explain`
reports the selected adapter, version, selection method, and reason.

## Declare compatibility and requirements

Projects can restrict compatible providers and versions without pinning a
specific local installation:

```toml
[compatibility]
compilers = ["ise >=25.12,<26", "gobo =26.06.30"]

[requires]
standard = "ecma"
void-safety = "all"
concurrency = "none"
```

The order of `compatibility.compilers` is the preference order. Constraints use
numeric comparisons with `=`, `>=`, `>`, `<=`, and `<`; SemVer operators such
as `^`, `~`, and wildcards are not supported.

`[requires]` contains positive language and capability requirements. The
initial catalog is `standard`, `void-safety`, `concurrency`, and
`ise-semantics`. Unknown keys are errors. A required unsupported capability
stops the command before compilation.

See [Manifest reference](manifest-reference.md) for allowed values.

## Fix a project matrix

Configure an exact default and a finite compilation matrix:

```console
$ evm toolchain use gobo@26.06 ise@25.12
```

This transactionally writes exact versions to `Eiffel.toml` and their
platform-specific artifacts to `Eiffel.lock`:

```toml
[toolchain]
default = "gobo@26.06"
matrix = ["gobo@26.06", "ise@25.12"]
```

The first selector is the default. Channels such as `latest`, `beta`, or
`nightly` may be supplied to `toolchain use`, but EVM resolves them before
writing the project files:

```console
$ evm toolchain use gobo@latest --install
```

Install the project matrix later with:

```console
$ evm toolchain install --project
$ evm toolchain install --project --locked
```

The locked form uses only exact artifacts for the current platform recorded in
`Eiffel.lock`. Add `--offline` to forbid network access.

## Run a compilation matrix

`check`, `build`, and `test` accept repeated selectors:

```console
$ evm check --toolchain gobo@26.06 --toolchain ise@25.12
$ evm build --toolchain all
$ evm test --toolchain all
```

`all` uses `[toolchain].matrix`. Without a project matrix, it expands to the
declared compatible or installed supported toolchains. Every selected item is
run, results are reported separately, and the command fails if any item fails.

`run` and `explain` select one toolchain rather than a matrix.

## Export the environment

EVM computes `GOBO`, `ISE_EIFFEL`, `ISE_LIBRARY`, `ISE_PLATFORM`,
`EVM_TOOLCHAIN`, and `PATH` for child processes. To use the same environment in
an interactive shell or another tool, print it in a suitable format:

```console
$ evm toolchain env gobo@26.06 --shell zsh
$ evm toolchain env ise@25.12 --shell dotenv
```

The command prints changes only. It does not modify the current shell or create
a project `.env` file.

## Verify and remove installations

Verify one installation or the complete project matrix:

```console
$ evm toolchain verify gobo@26.06
$ evm toolchain verify --project --json
```

Verification checks the expected executable and its reported version.

Remove an unused managed installation or linked registration:

```console
$ evm toolchain remove gobo@26.06
```

If the current project selects it, removal requires explicit confirmation:

```console
$ evm toolchain remove gobo@26.06 --force
```

Managed removal is confined to the EVM user store.

## Build output and capabilities

Compiler state and artifacts are isolated by provider, target, and mode:

```text
build/<toolchain>/<target>/<mode>/
```

Examples include `build/ise/default/dev/` and
`build/gobo/server/release/`. Generated C, object files, executables, and
`EIFGENs` are derived state.

EVM validates the selected adapter, version, project requirements, dependency
compatibility, and its versioned capability matrix before building. A
capability can be `supported`, `partial`, `unsupported`, or `unknown`; EVM does
not present compiler-specific behavior as portable behavior.
