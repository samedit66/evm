# Installation

EVM requires Python 3.11 or newer. Compiler-backed commands additionally need
at least one supported Eiffel toolchain, either installed through EVM or
provided by the environment:

- ISE Eiffel/EiffelStudio with `ec`; or
- Gobo Eiffel with `gec`.

Git is required for Git dependencies. The selected Eiffel compiler may also
require a native C toolchain.

## Install from the repository

Install the current development version directly from GitHub as an isolated
command with `uv`:

```console
$ uv tool install "git+https://github.com/samedit66/evm.git"
$ evm --version
```

Pin a known commit for a reproducible installation:

```console
$ uv tool install "git+https://github.com/samedit66/evm.git@<commit-sha>"
```

Upgrade to the current repository head explicitly:

```console
$ uv tool install --force "git+https://github.com/samedit66/evm.git"
```

To inspect the source before installation, clone the repository and install
from its root:

```console
$ git clone https://github.com/samedit66/evm.git
$ cd evm
$ uv tool install .
```

To run EVM from a development checkout:

```console
$ uv sync --all-groups
$ uv run evm --help
```

It can also be installed into a Python environment:

```console
$ python3.11 -m pip install .
$ evm --version
```

## Discover the environment

After installation, inspect compiler discovery and required environment
variables:

```console
$ evm discover
```

ISE Eiffel normally exposes:

- `ISE_EIFFEL`;
- `ISE_PLATFORM`.

Gobo Eiffel normally exposes:

- `GOBO`.

`discover` inventories Eiffel components and native C toolchains from the
environment, `PATH`, and platform-specific sources. An empty inventory is a
successful result; use `evm check` to validate a project against a compiler.

For machine-readable diagnostics:

```console
$ evm discover --json
```

See [Toolchains](toolchains.md) for compiler selection and capability handling.

## Install an Eiffel toolchain with EVM

List official distributions available for the current platform:

```console
$ evm toolchain list --available
```

Install and verify an exact release:

```console
$ evm toolchain install gobo@26.06
$ evm toolchain verify gobo@26.06
```

The verified distribution is stored once in EVM's user-level store. Override
that location with `EVM_TOOLCHAIN_HOME` when required by CI or system policy.

To register an existing installation without copying it:

```console
$ evm toolchain link /opt/EiffelStudio-25.12
$ evm toolchain list
```

For a project with an exact toolchain matrix, install the artifacts recorded in
its lock file:

```console
$ evm toolchain install --project --locked
$ evm toolchain verify --project
```
