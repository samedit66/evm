# Development

EVM targets Python 3.14. New production code must include type annotations, and
every behavior change requires corresponding tests.

## Set up the repository

```console
$ uv sync --all-groups
```

Install pre-commit hooks:

```console
$ make install-hooks
```

## Local checks

Format source and tests:

```console
$ make format
```

Check formatting and lint rules:

```console
$ make lint
```

Run tests that do not require network access or a real Eiffel toolchain:

```console
$ make test
```

Run the complete required check:

```console
$ make ci
```

`make ci` installs the locked development environment, checks formatting and
lint rules, and runs the default test suite.

## Optional integration tests

The test suite defines markers for:

- `integration` — subprocess and filesystem integration;
- `network` — real network access;
- `toolchain` — a real Eiffel compiler.

These tests require the corresponding external environment and are excluded
from the normal `make test` and `make ci` selection where appropriate.

## Design guidelines

- Keep side effects at explicit boundaries.
- Prefer pure functions for parsing, normalization, comparison, and dependency
  graph transformations.
- Use classes for identity, mutable state, invariants, protocols, or
  interchangeable implementations.
- Use functions for stateless transformations, validation, and orchestration.
- Prefer the simplest implementation that satisfies current requirements.
- Keep each rule and piece of domain knowledge in one authoritative place.
- Use [`SPEC.md`](../SPEC.md) as the source of truth; do not duplicate the
  specification in code documentation.

## Commits

Use Conventional Commits and keep each commit limited to one logical change:

```text
feat(deps): add Git dependency resolution
fix(ecf): preserve target inheritance order
docs: clarify toolchain selection
test(lockfile): cover checksum mismatch
chore: update development dependencies
```
