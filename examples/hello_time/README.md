# Hello time

This example proves that EVM can resolve a library shipped with an installed
toolchain and use the same project, generated ECF, and Eiffel source with both
supported compiler adapters.

The application uses the Gobo Time Library to print a greeting and the current
local time. EVM resolves that distribution library without downloading or
vendoring it. Both Gobo Eiffel and ISE EiffelStudio compile the same source.

## Run with Gobo Eiffel

```console
$ evm install --locked
$ evm run --compiler gobo
```

## Run with ISE EiffelStudio

```console
$ evm install --locked
$ evm run --compiler ise
```

The output has this form:

```text
Hello from EVM!
The current local time is 2026/07/31 18:00:05.398.
```

The timestamp naturally differs on each run.
