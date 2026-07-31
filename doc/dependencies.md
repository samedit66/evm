# Package management

EVM resolves direct and transitive Eiffel dependencies, records their exact
identities in `Eiffel.lock`, and materializes verified packages below `.evm/`.

```text
declare → resolve → lock → fetch → verify → materialize → generate ECF
```

Dependency commands keep `Eiffel.toml`, `Eiffel.lock`, installed package state,
and the managed ECF consistent.

## Supported sources

| Source | Typical use | Locked identity |
|---|---|---|
| `iron` | Published Eiffel package | Version and SHA-256 archive checksum |
| `git` | Package hosted in Git | Full commit and tree IDs |
| `path` | Local package under development | Manifest and normalized path |
| `ise` | Library distributed with ISE Eiffel | Distribution library identity |
| `gobo` | Library distributed with Gobo Eiffel | Distribution library identity |

EVM does not install packages into a global IRON directory and does not execute
package setup scripts.

## Add dependencies

IRON is the default source:

```console
$ evm add json@25.02
$ evm add json@25.02 --source iron
```

Add libraries distributed with a toolchain:

```console
$ evm add time --source ise
$ evm add gobo_xml --source gobo --library xml
```

Add a local project:

```console
$ evm add shared --path ../shared
```

Add a Git package:

```console
$ evm add json_git \
    --git https://github.com/eiffelhub/json.git \
    --branch master \
    --ecf library/json.ecf
```

A Git dependency requires exactly one revision selector:

```console
$ evm add example --git https://example.com/repo.git --tag v1.2.0
$ evm add example --git https://example.com/repo.git --branch main
$ evm add example --git https://example.com/repo.git --rev <commit>
```

Use `--subdir` for a package below the Git repository root and `--ecf` to select
its library ECF:

```console
$ evm add example \
    --git https://example.com/monorepo.git \
    --tag v1.2.0 \
    --subdir packages/example \
    --ecf library/example.ecf
```

Add a development-only dependency:

```console
$ evm add testing --source gobo --library test --dev
```

`--source`, `--git`, and `--path` are mutually exclusive.

## Manifest representation

The commands above produce entries such as:

```toml
[dependencies]
json = { source = "iron", version = "25.02" }
time = { source = "ise" }
gobo_xml = { source = "gobo", library = "xml" }
shared = { source = "path", path = "../shared" }
json_git = {
    source = "git",
    git = "https://github.com/eiffelhub/json.git",
    branch = "master",
    ecf = "library/json.ecf",
}

[dev-dependencies]
testing = { source = "gobo", library = "test" }
```

Prefer dependency commands over manual edits so every representation changes
as one operation.

## Remove and update

Remove a direct dependency and resolve the remaining graph:

```console
$ evm remove json
```

Update every permitted dependency:

```console
$ evm update
```

Update selected dependencies:

```console
$ evm update json parser
```

Pin one Git dependency to an exact commit:

```console
$ evm update json_git --precise <commit>
```

## Install from the lock file

Materialize the resolved graph:

```console
$ evm install
```

Require an existing lock file:

```console
$ evm install --locked
```

This is the normal restore command after cloning a project:

```console
$ git clone <project-url>
$ cd <project>
$ evm install --locked
```

## Inspect the graph

Display the resolved dependency tree:

```console
$ evm deps
```

Explain why a package is present:

```console
$ evm deps json
```

Display package relationships in a workspace:

```console
$ evm deps --workspace
```

## Offline operation

Commands that may access dependency sources support `--offline`:

```console
$ evm add json@25.02 --offline
$ evm remove json --offline
$ evm update --offline
$ evm install --locked --offline
$ evm build --offline
$ evm test --offline
```

Offline mode never accesses the network. Missing local content produces a
diagnostic containing the package identity and a recommendation to install it
with network access.

## Reproducibility and cleanup

Git packages are identified by full commit and tree IDs. IRON archives are
verified with SHA-256. Installed package content is checked before use.

Remove dependency state that is no longer reachable from the lock file:

```console
$ evm clean --unused
```

Remove every materialized dependency:

```console
$ evm clean --dependencies
```

Deleting `.evm/` is safe; `evm install --locked` reconstructs it.
