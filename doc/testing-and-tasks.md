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

Select a runner explicitly when the project uses a framework:

```toml
[test]
target = "test"
runner = "autotest"
```

Supported runner values are `auto`, `target`, `getest`, and `autotest`.
`auto` uses `getest` when both the executable and a matching configuration are
present, otherwise it runs the compiled test target.

EVM uses `getest` when the executable is available and the project root contains
a matching configuration file. It prefers `getest.ge` for Gobo and `getest.ise`
for ISE, then falls back to `getest.cfg`. Without a `getest` configuration EVM
runs the compiled test target directly.

## EiffelStudio AutoTest

The `autotest` runner requires ISE EiffelStudio and discovers effective
descendants of `EQA_TEST_SET` through compiler views. A test is an immediate
public procedure without arguments, matching AutoTest's own definition.

EVM adds the ISE `testing` library to the configured test target, generates a
console runner and ECF under `.evm/autotest/`, and executes every selected test
in a separate process. Project Eiffel sources and the managed ECF are not
modified by runner generation. Existing manual, extracted, and synthesized EQA
test sets use the same execution path.

AutoTest reports `passed`, `failed`, or `unresolved`. Failed and unresolved
tests both make `evm test` return a nonzero exit code, while the summary retains
separate counters for the two outcomes.

## Test filters

Request a supported test class or feature:

```console
$ evm test --class STRING_TESTS
$ evm test --feature test_append
```

With AutoTest, class and feature options are exact case-insensitive names. With
`getest`, class and feature names are passed as anchored, escaped regular
expressions, so each option selects exactly the requested name. The options can
be combined to run one feature from one test class. A plain compiled test target
has no portable filtering protocol, so EVM reports `filter unsupported` rather
than silently executing a broader test suite.

See `examples/calculator_autotest` for a complete ISE project.

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
