# Installation

EVM requires Python 3.14 or newer. Compiler-backed commands additionally need
at least one supported Eiffel toolchain:

- ISE Eiffel/EiffelStudio with `ec`; or
- Gobo Eiffel with `gec`.

Git is required for Git dependencies. The selected Eiffel compiler may also
require a native C toolchain.

## Install from the repository

EVM is not yet presented as a stable published package. From the repository
root, install it as an isolated command with `uv`:

```console
$ uv tool install .
$ evm --version
```

To run EVM from a development checkout:

```console
$ uv sync --all-groups
$ uv run evm --help
```

It can also be installed into a Python environment:

```console
$ python3.14 -m pip install .
$ evm --version
```

## Diagnose the environment

After installation, inspect compiler discovery and required environment
variables:

```console
$ evm doctor
```

ISE Eiffel normally exposes:

- `ISE_EIFFEL`;
- `ISE_PLATFORM`.

Gobo Eiffel normally exposes:

- `GOBO`.

`doctor` reports detected compiler versions and returns a non-zero exit status
when no healthy supported toolchain is available.

For machine-readable diagnostics:

```console
$ evm doctor --json
```

See [Toolchains](toolchains.md) for compiler selection and capability handling.
