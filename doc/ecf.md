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

Create an initial EVM project from an existing ECF without changing the source
file:

```console
$ evm import legacy.ecf --destination imported
$ cd imported
$ evm explain --ecf-diff
$ evm check --configuration-only
```

EVM reports the import fidelity:

| Level | Meaning |
|---|---|
| `lossless` | The manifest represents the ECF directly |
| `lossless-with-overlay` | Safe unsupported constructs are retained in an overlay |
| `partial` | The project is imported with explicit limitations |
| `unsupported` | The ECF cannot be represented safely |

## Legacy mode

Imported projects default to:

```toml
[project]
ecf-managed = false
ecf = "legacy.ecf"
```

In legacy mode EVM reads and uses the existing ECF but does not automatically
rewrite it. Unidentified dependencies and unsupported constructs are exposed
through diagnostics rather than guessed.

This allows gradual adoption: an existing project can use EVM for inspection,
dependency management, and supported build workflows while retaining its
original compiler configuration.
