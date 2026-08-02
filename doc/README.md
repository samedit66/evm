# EVM documentation

This directory is the user guide for the current EVM release.

## Guides

| Guide | Contents |
|---|---|
| [Installation](installation.md) | Requirements, installation, and environment diagnosis |
| [Projects and manifests](projects.md) | Project layout, manifests, targets, build modes, and daily workflow |
| [Manifest reference](manifest-reference.md) | `Eiffel.toml` sections, fields, constraints, and inferred defaults |
| [Compatibility contract](compatibility.md) | Stability guarantees for the CLI, project files, and generated state |
| [Package management](dependencies.md) | Sources, locking, installation, updates, graph inspection, and offline mode |
| [Toolchains](toolchains.md) | Discovery, managed installations, selection, matrices, capabilities, and output |
| [Adding your own compiler adapter](adding-your-own-compiler-adapter.md) | Adapter registry, lifecycle, installation, capabilities, and tests |
| [ECF interoperability](ecf.md) | Managed ECF, overlays, legacy mode, and project import |
| [Migrating existing projects](migrating-existing-projects.md) | Adopt EVM without replacing an existing ECF |
| [Testing and tasks](testing-and-tasks.md) | Test target selection, filters, and workflows |
| [Workspaces and CI](workspaces-and-ci.md) | Multi-package repositories and reproducible automation |
| [CLI reference](cli-reference.md) | Every public command and option |
| [Development](development.md) | Repository setup, checks, tests, and conventions |

The guides describe the supported user-facing behavior. Contributors should
also consult the repository's internal specification when changing product
requirements.
