# Testing and project tasks

## Run tests

Build and execute the configured Eiffel test system:

```console
$ evm test
$ evm test --release
$ evm test --toolchain ise
$ evm test --toolchain gobo
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

The default report is optimized for locating a failure in source code. EVM
matches the test class to an effective source cluster and, when the assertion
tag is a unique string literal, reports the corresponding assertion line:

```text
FAILED GAUSSIAN_ELIMINIATION_TEST.test_solve_equation
tests/gaussian_eliminiation_test.e:50
    assert ("value2", l_algo.output[2,1] = -1.0)
    Assertion failed: value2

1 failed, 149 passed, 150 total in 11.73s
```

Source correlation is deliberately approximate. If a class, feature, or
assertion tag cannot be resolved unambiguously, EVM reports the nearest
reliable location and never presents a breakpoint slot as a source line.

Use `--trace` for exception class, feature, code and tag, invalid-test and
trace-validity flags, captured output, and the complete Eiffel stack trace.

With `--json`, the same names are returned in the `failed_tests` and
`unresolved_tests` arrays. `test_details` contains every diagnostic supplied by
AutoTest: assertion tag, exception class and feature, exception code and tag,
breakpoint slot, invalid-test and trace-validity classifications, captured
output, process standard error, and the full stack trace. Fields unavailable
from the compiler or framework are omitted. Successful source correlation adds
`source_path`, `source_line`, and `source_text`.

For `getest` and a compiled test target, EVM cannot reliably normalize every
framework-specific diagnostic. It therefore preserves the runner's complete
standard output and standard error in the terminal report and in the JSON
`stdout` and `stderr` fields. Assertion locations and stack traces printed by
those runners remain available for debugging instead of being reduced to an
exit code.

Use `--raw` when native runner output is more useful than normalization. For
`getest` and compiled test targets, stdout and stderr are inherited directly.
For AutoTest, which has no stable standalone console CLI, EVM's generated
`EQA_TEST_EVALUATOR` runner prints the underlying EQA status, tag, output, and
trace without EVM's summary or source correlation. `--json`, `--trace`, and
`--raw` are mutually exclusive.

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

Run the exact project toolchain matrix or an explicit subset with:

```console
$ evm test --toolchain all
$ evm test --toolchain gobo@26.06 --toolchain ise@25.12
```

Results are reported for every selected toolchain, and the overall command
fails when any matrix entry fails. `--compiler` remains an alias for
`--toolchain` for compatibility.

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
