# Testing and project tasks

## Run tests

Build and execute the configured Eiffel test system:

```console
$ evm test
$ evm test --release
$ evm test --compiler ise
$ evm test --compiler gobo
```

Test target selection follows this order:

1. the target named by `[test].target`;
2. a declared target named `test`;
3. the conventional `TEST_APPLICATION.make` target when `tests/` contains
   Eiffel sources.

Gobo can use `getest` when it is available.

## Test filters

Request a supported test class or feature:

```console
$ evm test --class STRING_TESTS
$ evm test --feature test_append
```

Filters are adapter capabilities. If the selected runner cannot honor one,
EVM fails explicitly rather than silently executing a broader test suite.

Other test options include:

```console
$ evm test --offline
$ evm test --regenerate-ecf
$ evm test --package parser
$ evm test --json
```

## One-command tasks

Expose common EVM operations in `Eiffel.toml`:

```toml
[scripts]
serve = "run --target server"
verify = "check --configuration-only"
```

Run a task:

```console
$ evm task serve
```

The shorthand contains exactly one EVM command. Shell operators, pipelines,
redirects, substitutions, and environment expansion are rejected.

## Portable structured tasks

Combine built-in commands into a cross-platform workflow:

```toml
[scripts.ci]
steps = [
    { command = "check", configuration-only = true },
    { command = "test" },
    { command = "build", release = true },
]
```

```console
$ evm task ci
```

Structured steps have the same interpretation on Windows and Unix-like
systems. EVM detects recursive task invocation.

## Explicit shell steps

When a native EVM command cannot express a step, declare shell execution
explicitly:

```toml
[scripts.generate]
steps = [
    { shell = "./tools/generate.sh" },
]
```

Shell execution is non-portable and opt-in:

```console
$ evm task generate --allow-build-scripts
```

Without `--allow-build-scripts`, EVM refuses to execute the shell step.
