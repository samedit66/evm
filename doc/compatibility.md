# Compatibility contract

This page defines the user-facing compatibility promises for the EVM beta
series. It distinguishes stable project data from output that may continue to
evolve while EVM is young.

## Compatibility classes

| Interface | Beta guarantee |
|---|---|
| `Eiffel.toml` | Documented fields retain their meaning. Removal or incompatible reinterpretation requires an explicit migration path. |
| `Eiffel.lock` | Format versions 1 and 2 remain readable. An unsupported format is rejected before project state is changed. |
| Managed ECF | UUIDs and generated content are deterministic for unchanged inputs. External edits require explicit regeneration. |
| CLI commands and options | Public commands are not silently removed or renamed. Incompatible changes require a deprecation period. |
| Exit status | `0` means success. Domain and configuration failures are nonzero; compiler and runner failures preserve their meaningful status where supported. |
| JSON output | Documented keys and value types remain compatible within the beta series. New optional keys may be added. |
| Human-readable output | Wording and layout may change without notice; automation should use `--json`. |
| Python modules below `evm.*` | Internal implementation, not a public library API during the beta series. |

## Project-file guarantees

`Eiffel.toml`, `Eiffel.lock`, and a managed ECF are reproducible project inputs
and should be committed. `.evm/` and `build/` are derived state and may be
removed.

EVM follows these rules when reading or updating project files:

- unknown manifest fields are rejected rather than ignored;
- invalid and unsupported input is rejected before files are rewritten;
- manifest, lock-file, and managed-ECF updates use transactional writes;
- a managed ECF changed outside EVM is not overwritten without
  `--regenerate-ecf`;
- a lock file with an unknown format version is rejected;
- deleting `.evm/` does not remove irreplaceable project state;
- `evm install --locked` reconstructs local dependency state from committed
  project files.

The complete `Eiffel.toml` field contract is documented in
[Manifest reference](manifest-reference.md). Locking and restoration behavior
is documented in [Package management](dependencies.md).

## CLI contract

The public top-level command set is:

```text
add       build     check     clean     deps      discover
doc       explain   import    init      install   iron
lint      new       remove    run       task      test
toolchain update
```

The `toolchain` and `iron` entries are command groups. Their subcommands and
all command options are listed in the [CLI reference](cli-reference.md).

The semantic toolchain identifiers are `ise` and `gobo`. Executable names such
as `ec` and `gec` are not interchangeable adapter IDs. For commands that accept
a toolchain selector, an explicit CLI option has priority over
`EVM_TOOLCHAIN`, project policy, and automatic selection.

## Machine-readable output

Commands documented with `--json` emit one complete JSON value. Successful
workspace operations use a status plus per-package results. A domain error is
represented as:

```json
{
  "diagnostics": [
    {
      "level": "error",
      "message": "description of the failure"
    }
  ],
  "status": "error"
}
```

Consumers must ignore additional object keys they do not understand. They may
rely on documented keys retaining their meaning and JSON type throughout the
beta series.

## Changing a frozen contract

A change to a frozen interface must include:

1. a compatibility assessment;
2. tests for the old and new representations;
3. a migration or deprecation path when existing projects are affected;
4. updated user documentation;
5. an explicit note in the GitHub release description.

Changes that could make project state unreadable or non-reproducible must not
be shipped as an implicit migration.
