# Markdown documentation generator roadmap

EVM currently delegates HTML documentation to Gobo `gedoc` or EiffelStudio.
A native Markdown backend is planned, but is not part of the current `evm doc`
compatibility guarantee.

## Intended architecture

The generator should parse the effective ECF and Eiffel sources into a semantic
documentation model before rendering any files:

```text
ECF and Eiffel sources
        ↓
classes, features, contracts, notes, and inheritance
        ↓
stable documentation model
        ↓
Markdown renderer
        ↓
index.md, classes/*.md, and relations/*.md
```

The model must not be derived from `gedoc` or EiffelStudio HTML. Keeping source
analysis separate from rendering makes links, ordering, and future output formats
deterministic.

## Planned behavior

- Generate stable pages for classes and public features.
- Include contracts, selected `note` clauses, and inheritance relationships.
- Use canonical relative links and detect broken links.
- Preserve source locations so repositories can add source links.
- Support application, library, target, and workspace scopes.
- Separate public API documentation from optional internal documentation.
- Avoid rewriting unchanged files and remove stale files only inside an
  EVM-owned output directory.
- Produce deterministic output suitable for review and CI snapshots.

## Proposed interface

```shell
evm doc --format markdown
evm doc --format markdown --target library
evm doc --format markdown --output build/doc
```

Before implementation, the Eiffel parser boundary, public-feature visibility
rules, filename encoding, and source-link configuration need to be specified.
Snapshot tests should cover inheritance, overloaded feature names, contracts,
Unicode identifiers, libraries, and workspace links.
