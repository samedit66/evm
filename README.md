<div align="center">

# `evm`

### A project and dependency manager for Eiffel

**Run a file. Build a project. Reproduce its dependencies and toolchains.**

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Language: Eiffel](https://img.shields.io/badge/language-Eiffel-6f42c1)](https://www.eiffel.org/)
[![ISE Eiffel](https://img.shields.io/badge/toolchain-ISE%20Eiffel-17365D)](https://www.eiffel.com/)
[![Gobo Eiffel](https://img.shields.io/badge/toolchain-Gobo%20Eiffel-8B5A2B)](https://www.gobosoft.com/)
![Status: early development](https://img.shields.io/badge/status-early%20development-E3A008)

EVM brings source files, projects, dependencies, ECF, ISE Eiffel, and Gobo
Eiffel into one workflow without replacing the native Eiffel ecosystem.

[Quick start](#quick-start) ·
[Documentation](doc/) ·
[CLI reference](doc/cli-reference.md)

</div>

> [!IMPORTANT]
> EVM is a very young project under active development. The current version is
> `0.1.0`, and some interfaces may still evolve. Testing on real Eiffel
> projects is especially valuable at this stage; bug reports, use cases, and
> any other feedback are very welcome.

## Installation

EVM requires Python 3.11 or newer. Install the command directly from the GitHub
repository with
[`uv`](https://docs.astral.sh/uv/):

```console
uv tool install "git+https://github.com/samedit66/evm.git"
evm --version
```

For a reproducible installation, append a commit SHA or release tag to the Git
URL. See [Installation](doc/installation.md) for pinned and checkout-based
installation.

For development from a checkout:

```console
uv sync --all-groups
uv run evm --help
```

See [Installation](doc/installation.md) for other environment details.

## Quick start

### Run a single Eiffel file

Create `hello.e`:

```eiffel
class
    HELLO

create
    make

feature {NONE} -- Initialization

    make
        do
            print ("Hello, World!%N")
        end

end
```

Run it directly:

```console
evm run hello.e
```

No project manifest or handwritten ECF is required. EVM selects a compatible
installed toolchain, stages an internal ECF, builds the file, and runs it in an
isolated cache.

Choose a provider or exact version when needed:

```console
evm run --toolchain gobo hello.e
evm run --toolchain gobo@26.06 hello.e
```

### Install an Eiffel toolchain

EVM can use an existing ISE Eiffel or Gobo Eiffel installation. It can also
install a verified distribution in its shared user store:

```console
evm toolchain list --available
evm toolchain install gobo
evm toolchain verify gobo
```

Use an exact selector such as `gobo@26.06` when the version matters. See
[Toolchains](doc/toolchains.md) for linked installations, project matrices,
locked installation, offline use, and environment setup.

### Create a project

Turn the same workflow into a reproducible application project:

```console
evm new hello
cd hello
evm run
```

The generated project includes a human-readable `Eiffel.toml`, an exact
`Eiffel.lock`, a standard ECF, production sources, and a conventional test
directory. Common operations stay short:

```console
evm check
evm build --release
evm test
evm lint
evm doc
```

`evm lint` uses Gobo `gelint` or EiffelStudio Code Analyzer according to the
selected toolchain. `evm doc` generates HTML through Gobo `gedoc` or the native
EiffelStudio documentation filter. Both commands accept an explicit `--backend`
when a project needs to pin the implementation.

### Add and use a dependency

The following project uses the real
[Eiffel JSON](https://github.com/eiffelhub/json) library. Create it and add the
library from its `v0.11` Git tag:

```console
evm new json_demo
cd json_demo
evm add ejson \
    --git https://github.com/eiffelhub/json.git \
    --tag v0.11 \
    --ecf library/json.ecf
```

Replace `src/application.e` with this single file:

```eiffel
class
    APPLICATION

create
    make

feature {NONE} -- Initialization

    make
        local
            document: JSON_OBJECT
        do
            create document.make
            document.put_string ("evm", "project")
            document.put_string ("works", "status")
            print (document.representation)
            print ("%N")
        end

end
```

Run the application:

```console
evm run
```

It prints:

```json
{"project":"evm","status":"works"}
```

`evm add` records the declared source in `Eiffel.toml`, resolves the tag to
immutable Git identities in `Eiffel.lock`, materializes the dependency below
`.evm/`, and synchronizes the managed ECF. A fresh checkout can restore the
same state with:

```console
evm install --locked
```

See [Package management](doc/dependencies.md) for IRON, Git, local-path, ISE,
and Gobo dependencies, updates, offline operation, and graph inspection.

## Why EVM?

ECF is an expressive native description of an Eiffel system, but it is mainly
a compiler configuration format. By itself, it does not record where every
dependency comes from, which exact revisions produced a build, or how the full
project state can be restored on another machine.

EVM adds that project-management layer:

```text
Eiffel.toml          human-readable project intent
     +
Eiffel.lock          exact dependencies and locked toolchain artifacts
     ↓
standard ECF         EiffelStudio, ISE Eiffel, and Gobo Eiffel
```

The result is one CLI for everyday project operations, reproducible package
state, deterministic ECF generation, optional managed toolchains, and safe
adoption of existing Eiffel projects.

EVM does **not** implement an Eiffel compiler or require users to stop using
EiffelStudio, Gobo, ECF, `ec`, or `gec`. Native tools and formats remain
accessible and first-class.

## Project layout

```text
hello/
├── Eiffel.toml          # project intent and direct dependencies
├── Eiffel.lock          # exact resolved state
├── hello.ecf            # native Eiffel configuration
├── src/                 # production Eiffel sources
├── tests/               # test Eiffel sources
├── .evm/                # local dependencies and derived state
└── build/               # compiler output
```

Commit `Eiffel.toml`, `Eiffel.lock`, and the managed ECF. Ignore `.evm/` and
`build/`; EVM reconstructs them from committed state.

Already have an ECF project? Adopt EVM without rewriting it:

```console
evm import project.ecf
```

See [Migrating existing projects](doc/migrating-existing-projects.md) for the
safe legacy workflow.

## More capabilities

- ISE Eiffel and Gobo Eiffel adapters with deterministic selection;
- managed, linked, exact, and project-matrix toolchains;
- AutoTest, `getest`, conventional test targets, filters, and CI output;
- portable project tasks with explicit opt-in for shell steps;
- multi-package workspaces with dependency-order execution;
- offline dependency and toolchain restoration;
- deterministic `package.iron` import and export;
- stable JSON output for automation.

## Examples

- [`hello_time`](examples/hello_time/) uses a Gobo distribution library with
  both Gobo Eiffel and ISE EiffelStudio.
- [`json`](examples/json/) resolves the Eiffel JSON library from Git and calls
  it from a real application.
- [`calculator_autotest`](examples/calculator_autotest/) runs an
  `EQA_TEST_SET` suite through EVM's generated AutoTest console runner.

## Documentation

| Guide | Contents |
|---|---|
| [Installation](doc/installation.md) | Installation and environment diagnosis |
| [Projects and manifests](doc/projects.md) | Project layout, targets, build modes, and daily workflow |
| [Manifest reference](doc/manifest-reference.md) | `Eiffel.toml` sections, fields, and inferred defaults |
| [Compatibility contract](doc/compatibility.md) | Stability guarantees for CLI and project files |
| [Package management](doc/dependencies.md) | Sources, locking, updates, offline use, and graph inspection |
| [Toolchains](doc/toolchains.md) | Discovery, installation, selection, matrices, and capabilities |
| [Migrating existing projects](doc/migrating-existing-projects.md) | Adopt EVM without replacing an existing ECF |
| [Testing and tasks](doc/testing-and-tasks.md) | Test runners, filters, diagnostics, and workflows |
| [Workspaces and CI](doc/workspaces-and-ci.md) | Multi-package repositories and reproducible automation |
| [CLI reference](doc/cli-reference.md) | Every public command and option |

Every command also includes built-in help:

```console
evm --help
evm add --help
evm toolchain --help
```

## Project status

The current implementation covers managed and legacy ECF projects, ISE and
Gobo adapters, dependency resolution and locking, managed and linked
toolchains, project compilation matrices, testing, tasks, IRON interoperability,
and multi-package workspaces.

This is still a very young project. Testing against existing Eiffel codebases
is useful even when a workflow is not yet fully supported. I am glad to receive
bug reports, compatibility findings, workflow descriptions, and any other
feedback through the GitHub repository.

## License

EVM is licensed under the [Apache License 2.0](LICENSE).

---

<div align="center">

**EVM — modern project ergonomics for the native Eiffel ecosystem.**

</div>
