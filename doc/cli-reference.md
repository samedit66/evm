# CLI reference

The public CLI follows user-visible project operations rather than exposing
internal stages such as resolution, fetching, or ECF generation.

```text
new      init
add      remove   update   install   deps
build    run      test     check     clean
discover explain  import   task
```

Use `evm COMMAND --help` for the help bundled with the installed version.

## Global interface

```text
evm [OPTIONS] COMMAND [ARGS]...

Options:
  --version
  --help
```

## `evm new`

Create a new EVM project in `PATH`.

```text
evm new [OPTIONS] PATH

Options:
  --lib      Create a library project.
  --scoop    Enable SCOOP concurrency.
  --help
```

The command creates `Eiffel.toml`, `Eiffel.lock`, a managed ECF, `src/`, and
`tests/`. Applications also receive an initial root class. Generated targets
support non-SCOOP execution by default. `--scoop` records
`[requires].concurrency = "scoop"` and enables SCOOP in the ECF.

## `evm init`

Initialize the current directory without overwriting existing files.

```text
evm init [OPTIONS]

Options:
  --lib    Initialize a library project.
  --help
```

## `evm check`

Validate project configuration and reachable Eiffel classes.

```text
evm check [OPTIONS]

Options:
  --configuration-only    Do not invoke an Eiffel compiler.
  --release               Check the release mode.
  --compiler TEXT         Compiler adapter ID: ise or gobo.
  --target TEXT           Target name; defaults to project.default-target.
  --regenerate-ecf        Explicitly overwrite managed ECF.
  --package TEXT          Limit a workspace command to one package.
  --json                  Emit stable JSON for CI.
  --help
```

## `evm build`

Build an Eiffel application or library.

```text
evm build [OPTIONS]

Options:
  --release               Build in release mode.
  --compiler TEXT         Compiler adapter ID: ise or gobo.
  --target TEXT           Target name; defaults to project.default-target.
  --offline               Forbid network access.
  --regenerate-ecf        Explicitly overwrite managed ECF.
  --package TEXT          Limit a workspace build to one package.
  --json                  Emit stable JSON for CI.
  --help
```

## `evm run`

Build and run a project application or an explicit set of Eiffel files.
Arguments after `--` are passed to the program.

```text
evm run [OPTIONS] [FILES...] [-- ARGS...]

Options:
  --release               Build and run in release mode.
  --compiler TEXT         Compiler adapter ID: ise or gobo.
  --target TEXT           Target name; defaults to project.default-target.
  --offline               Forbid network access.
  --regenerate-ecf        Explicitly overwrite managed ECF.
  --class TEXT            Root class for file mode.
  --feature TEXT          Root creation feature for file mode.
  --manifest FILE         Use an explicit Eiffel.toml for file mode.
  --standalone            Ignore project context in file mode.
  --help
```

Project example:

```console
$ evm run --target server -- --port 8080
```

File examples:

```console
$ evm run hello.e
$ evm run hello.e helper.e -- input.txt --verbose
$ evm run --standalone --compiler gobo hello.e
```

The first file supplies the root class and the remaining files supply
additional classes. EVM stages exactly those files and keeps the generated ECF
and build output in `.evm/scripts/` when one project contains all files, or in
the user cache in standalone mode. It does not create `Eiffel.toml`,
`Eiffel.lock`, an ECF, or `build/` beside standalone source files.

When all files belong to one EVM package, its manifest and lock file provide
dependencies and compiler configuration. `--standalone` disables this lookup;
`--manifest` selects the context explicitly. `--target` and `--regenerate-ecf`
apply only to project mode.

## `evm test`

Build and run the configured Eiffel test system.

The `[test].runner` manifest field accepts `auto`, `target`, `getest`, or
`autotest`. The AutoTest runner supports exact `--class` and `--feature`
filtering and reports Tests, Passed, Failed, and Unresolved counters.
By default, AutoTest failures are correlated with project sources and shown as
`file:line`, feature, source assertion, and assertion tag. `--trace` expands the
normalized exception metadata and Eiffel stack trace. `--raw` suppresses EVM's
summary and passes through the selected runner's output. The three machine or
diagnostic output modes `--json`, `--trace`, and `--raw` are mutually exclusive.

```text
evm test [OPTIONS]

Options:
  --release               Build tests in release mode.
  --compiler TEXT         Compiler adapter ID: ise or gobo.
  --class TEXT            Run one supported test class.
  --feature TEXT          Run one supported test feature.
  --offline               Forbid network access.
  --regenerate-ecf        Explicitly overwrite managed ECF.
  --package TEXT          Limit a workspace test to one package.
  --json                  Emit stable JSON for CI.
  --trace                 Show complete normalized failure diagnostics.
  --raw                   Pass through the test runner's native output.
  --help
```

## `evm discover`

Discover installed Eiffel components and native C toolchains.

```text
evm discover [OPTIONS]

Options:
  --json    Emit stable JSON for CI.
  --help
```

## `evm explain`

Show the effective project configuration or compare a managed ECF.

```text
evm explain [OPTIONS]

Options:
  --targets               List effective targets.
  --target TEXT           Target name; defaults to project.default-target.
  --release               Explain release mode.
  --compiler TEXT         Compiler adapter ID: ise or gobo.
  --json                  Emit stable JSON.
  --ecf-diff              Compare the ECF with the manifest.
  --help
```

`--json` and `--ecf-diff` select different output modes and should not be
combined.

## `evm import`

Create an initial manifest from an existing ECF or `package.iron` without
changing the source.

```text
evm import [OPTIONS] SOURCE

Options:
  --project TEXT             Select an ECF from package.iron.
  --destination DIRECTORY    Destination directory. [default: .]
  --help
```

The command atomically creates only `Eiffel.toml` and `Eiffel.lock`. The
original ECF remains authoritative and is neither changed nor copied.
Importing an IRON package with multiple ECF entries requires `--project`.
IRON `setup` declarations are reported but never executed.

## `evm iron export`

Create a deterministic `package.iron` from the selected EVM package.

```text
evm iron export [OPTIONS]

Options:
  --output FILE       Write to a different path.
  --check             Check without writing the file.
  --force             Overwrite a different existing file.
  --package TEXT      Select a workspace package.
  --help
```

`--check` and `--force` are mutually exclusive. Dependencies remain
authoritative in `Eiffel.toml` and are not copied into `package.iron`.

## `evm add`

Add, resolve, lock, and install a dependency.

```text
evm add [OPTIONS] PACKAGE

Options:
  --dev                    Add a development dependency.
  --source [ise|gobo|iron]
  --git TEXT               Git repository URL.
  --path TEXT              Local dependency path.
  --tag TEXT
  --branch TEXT
  --rev TEXT
  --library TEXT           Distribution library name.
  --ecf TEXT               ECF path inside the dependency.
  --subdir TEXT            Package subdirectory in a Git repository.
  --offline                Forbid network access.
  --help
```

`PACKAGE` accepts `name` or `name@version`. `--source`, `--git`, and `--path`
are mutually exclusive. A Git dependency requires exactly one of `--tag`,
`--branch`, or `--rev`.

## `evm remove`

Remove a direct dependency and update the resolved graph.

```text
evm remove [OPTIONS] PACKAGE

Options:
  --offline    Forbid network access.
  --help
```

## `evm update`

Resolve and install newer permitted dependency identities.

```text
evm update [OPTIONS] [PACKAGES]...

Options:
  --precise TEXT    Pin one Git dependency to a commit.
  --offline         Forbid network access.
  --help
```

With no package arguments, all permitted dependencies are updated.

## `evm install`

Materialize dependencies from `Eiffel.lock`.

```text
evm install [OPTIONS]

Options:
  --locked          Require an existing lock file.
  --offline         Forbid network access.
  --package TEXT    Limit a workspace install to one package.
  --help
```

## `evm deps`

Show the resolved dependency graph, explain why one package is present, or
display workspace package relationships.

```text
evm deps [OPTIONS] [PACKAGE]

Options:
  --workspace    Show workspace package dependencies.
  --help
```

`PACKAGE` and `--workspace` cannot be combined.

## `evm task`

Run a workflow declared in `Eiffel.toml`.

```text
evm task [OPTIONS] NAME

Options:
  --allow-build-scripts    Allow explicitly declared shell steps.
  --help
```

## `evm clean`

Remove selected generated project state.

```text
evm clean [OPTIONS]

Options:
  --dependencies    Remove all materialized dependencies.
  --unused          Remove only state unused by Eiffel.lock.
  --help
```

With no option, `clean` removes build output. `--dependencies` and `--unused`
are mutually exclusive.

## Command outcomes

| Command | Guaranteed user-visible result |
|---|---|
| `new` | A new EVM project is created |
| `init` | The current directory is initialized |
| `add` | A dependency is declared, locked, and installed |
| `remove` | A direct dependency is removed from the resolved graph |
| `update` | New permitted identities are locked and installed |
| `install` | `.evm/deps/` matches the lock file |
| `deps` | A dependency or workspace graph is displayed |
| `check` | Project configuration and optionally Eiffel classes are validated |
| `build` | An up-to-date application or library artifact is produced |
| `run` | An up-to-date application is executed |
| `test` | The configured tests are built and executed |
| `clean` | Explicitly selected derived state is removed |
| `discover` | Local Eiffel and C components are inventoried |
| `explain` | Effective configuration or an ECF difference is displayed |
| `import` | An initial project is created from an existing ECF |
| `task` | A manifest-defined workflow is executed |
