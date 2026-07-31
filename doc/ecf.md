# ECF interoperability

ECF remains EVM's native integration format with EiffelStudio, ISE Eiffel, and
Gobo Eiffel. Projects operate in either managed or legacy mode.

## Managed ECF

```toml
[project]
ecf-managed = true
ecf = "hello.ecf"
```

In managed mode, `Eiffel.toml` and `Eiffel.lock` are the source of truth. EVM
generates a deterministic ECF from the resolved project model.

Commit the generated ECF so the project remains directly usable with
EiffelStudio and native compiler commands. After cloning, materialize its
library paths first:

```console
$ evm install --locked
```

EVM detects changes made outside EVM and does not silently overwrite them.
Inspect the semantic difference:

```console
$ evm explain --ecf-diff
```

Regenerate explicitly when replacement is intentional:

```console
$ evm check --configuration-only --regenerate-ecf
$ evm build --regenerate-ecf
```

## ECF overlays

Low-level safe ECF constructs not represented by the high-level manifest can
be retained in a validated overlay:

```toml
[ecf]
include = ["project.overlay.ecf"]
```

The overlay is an escape hatch for exceptional native target configuration,
not a second project manifest.

## Import an existing project

Create an initial EVM project in place without changing or copying the source
ECF:

```console
$ evm import legacy.ecf
$ evm check --configuration-only
```

The command creates only `Eiffel.toml` and `Eiffel.lock`. Native constructs
that have no high-level manifest representation remain in the original legacy
ECF. See [Migrating an existing Eiffel project](migrating-existing-projects.md)
for target selection, old ECF versions, path handling, and verification.

## Legacy mode

Imported projects default to:

```toml
[project]
ecf-managed = false
ecf = "legacy.ecf"
default-target = "legacy"
```

In legacy mode EVM reads and uses the existing ECF but does not automatically
rewrite it. Unidentified dependencies and unsupported constructs are exposed
through diagnostics rather than guessed.

This allows gradual adoption: an existing project can use EVM for inspection,
dependency management, and supported build workflows while retaining its
original compiler configuration.
