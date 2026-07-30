# EVM

EVM is an Eiffel project and dependency manager. The current implementation
covers stages 1–3 from [`SPEC.md`](SPEC.md): normalized manifests, managed ECF
generation, ISE/Gobo compiler adapters, reproducible dependency locking,
project-local dependency materialization, and legacy ECF compatibility.

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

Add dependencies from supported sources:

```shell
evm add time --source ise
evm add gobo_xml --source gobo --library xml
evm add json@25.02
evm add shared --path ../shared
evm add json_git --git https://github.com/eiffelhub/json.git \
  --branch master --ecf library/json.ecf
```

`evm add`, `remove`, and `update` keep `Eiffel.toml`, `Eiffel.lock`, and the
managed ECF consistent. `evm install --locked` reconstructs `.evm/deps` from
the lock file, while `--offline` forbids network access. Git packages are
identified by full commit and tree IDs. IRON archives are kept project-local
and verified with SHA-256; EVM does not install them into the user's global
IRON package directory or execute package setup scripts.

Use `evm deps` to inspect the graph, `evm deps <package>` to explain a path,
and `evm clean --unused` to remove source and package state no longer reachable
from the current lock file.

Import an existing ECF without modifying it:

```shell
evm import legacy.ecf --destination imported
cd imported
evm explain --ecf-diff
evm check --configuration-only
```

Imports report `lossless`, `lossless-with-overlay`, `partial`, or `unsupported`.
Legacy projects keep `ecf-managed = false`; unknown but safe ECF constructs are
retained in a validated `[ecf].include` overlay. Managed projects can use the
same escape hatch with an `ecf-overlay` fragment for low-level target settings.

Workspaces and test adapters belong to the next implementation stage.
