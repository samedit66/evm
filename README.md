# EVM

EVM is an Eiffel project manager. This repository currently implements stage 1
from [`SPEC.md`](SPEC.md): the normalized project model, managed ECF generation,
ISE/Gobo compiler adapters, built-in development and release modes, conditions,
numeric toolchain constraints, and the `new`, `init`, `check`, `build`, `run`,
`doctor`, `explain`, and `import` commands.

Create and build a project:

```shell
evm new hello
cd hello
evm check --configuration-only
evm build
evm run -- --example-argument
```

Choose a compiler explicitly with `--compiler ise` or `--compiler gobo`, or set
`EVM_COMPILER`. Without either, EVM deterministically prefers a compatible ISE
installation and then Gobo. Build output is isolated below
`build/<compiler>/<target>/<mode>/`.

`evm doctor` reports detected compiler versions and missing environment
variables. ISE normally exposes `ISE_EIFFEL` and `ISE_PLATFORM`; Gobo uses
`GOBO`. Generated ECF files select EiffelBase for ISE and FreeELKS for Gobo.

The dependency manager, materialized `.evm/` state, workspaces, full legacy
round-tripping, and test adapters belong to later implementation stages.
