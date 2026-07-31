# Toolchains

EVM is a workflow layer over native Eiffel compilers. It currently provides
adapters for ISE Eiffel and Gobo Eiffel.

| Adapter | Compiler | Standard library |
|---|---|---|
| `ise` | `ec` | EiffelBase |
| `gobo` | `gec` | FreeELKS |

## Diagnose installations

```console
$ evm doctor
$ evm doctor --json
```

ISE normally uses `ISE_EIFFEL` and `ISE_PLATFORM`; Gobo uses `GOBO`.
`doctor` reports compiler versions, discovery results, and missing environment
variables.

## Select a compiler

Select an adapter for one command:

```console
$ evm build --compiler ise
$ evm build --compiler gobo
```

Or select it through the environment:

```console
$ EVM_COMPILER=gobo evm build
```

Without an explicit choice, EVM deterministically prefers a compatible ISE
installation and then Gobo.

> [!TIP]
> Select the compiler explicitly in CI. Installing another compatible
> toolchain can otherwise change the automatic choice.

The selected adapter is shown in build output. `evm explain` also reports its
version, selection method, and reason:

```console
$ evm explain --compiler gobo
```

## Build modes and targets

Targets describe different Eiffel systems or roots:

```console
$ evm build --target server
```

Development and release describe how a target is compiled:

```console
$ evm build
$ evm build --release
```

They are build modes, not separate targets.

## Output isolation

Compiler state and artifacts are kept below:

```text
build/<compiler>/<target>/<mode>/
```

Examples:

```text
build/ise/default/dev/
build/ise/default/release/
build/gobo/server/dev/
```

Generated C, object files, executables, and `EIFGENs` are derived state rather
than portable project inputs.

## Capabilities

EVM validates the selected adapter and compiler version before building. It
does not silently claim equivalent behavior when a capability is unsupported
or only partially supported.

| Capability | ISE | Gobo |
|---|:---:|:---:|
| Discovery and version reporting | ✓ | ✓ |
| Configuration validation | ✓ | ✓ |
| Development and release builds | ✓ | ✓ |
| Application execution | ✓ | ✓ |
| Managed ECF generation | ✓ | ✓ |
| Conventional test target | ✓ | ✓ |
| `getest` integration | — | When available |

Use `evm doctor` for the installed environment and `evm explain` for the
effective project configuration.
