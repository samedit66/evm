# CLI reference

The public CLI follows user-visible project operations rather than exposing
internal stages such as resolution, fetching, or ECF generation.

```text
new      init
add      remove   update   install   deps
build    run      test     check     clean
doctor   explain  import   task
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
  --lib    Create a library project.
  --help
```

The command creates `Eiffel.toml`, `Eiffel.lock`, a managed ECF, `src/`, and
`tests/`. Applications also receive an initial root class.

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
  --target TEXT           Target name. [default: default]
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
  --target TEXT           Target name. [default: default]
  --offline               Forbid network access.
  --regenerate-ecf        Explicitly overwrite managed ECF.
  --package TEXT          Limit a workspace build to one package.
  --json                  Emit stable JSON for CI.
  --help
```

## `evm run`

Build and run an application. Arguments after `--` are passed to the program.

```text
evm run [OPTIONS] [-- ARGS...]

Options:
  --release               Build and run in release mode.
  --compiler TEXT         Compiler adapter ID: ise or gobo.
  --target TEXT           Target name. [default: default]
  --regenerate-ecf        Explicitly overwrite managed ECF.
  --help
```

Example:

```console
$ evm run --target server -- --port 8080
```

## `evm test`

Build and run the configured Eiffel test system.

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
  --help
```

## `evm doctor`

Diagnose installed Eiffel toolchains and their environments.

```text
evm doctor [OPTIONS]

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
  --target TEXT           Target name. [default: default]
  --release               Explain release mode.
  --compiler TEXT         Compiler adapter ID: ise or gobo.
  --json                  Emit stable JSON.
  --ecf-diff              Compare the ECF with the manifest.
  --help
```

`--json` and `--ecf-diff` select different output modes and should not be
combined.

## `evm import`

Create an initial manifest from an existing ECF without changing it.

```text
evm import [OPTIONS] ECF

Options:
  --destination DIRECTORY    Destination directory. [default: .]
  --help
```

The result is classified as `lossless`, `lossless-with-overlay`, `partial`, or
`unsupported`.

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
| `doctor` | The external Eiffel environment is diagnosed |
| `explain` | Effective configuration or an ECF difference is displayed |
| `import` | An initial project is created from an existing ECF |
| `task` | A manifest-defined workflow is executed |
