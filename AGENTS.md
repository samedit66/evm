# EVM Development Guidelines

## Project

EVM is a project and dependency manager for Eiffel. It provides a
human-oriented manifest and reproducible local dependency management while
remaining compatible with native Eiffel toolchains, including EiffelStudio
and Gobo Eiffel.

`SPEC.md` is the source of truth for product requirements and behavior. Do not
duplicate the specification in code documentation.

## Code

- Use Python 3.11 and add type annotations to all new production code.
- Prefer the simplest solution that satisfies current requirements.
- Do not introduce frameworks, extension points, abstraction layers, or
  generalized infrastructure solely for possible future use.
- Use classes when identity, mutable state, invariants, protocols, or
  interchangeable implementations are required.
- Use functions for stateless transformations, validation, orchestration, and
  actions that do not require an object lifecycle.
- Keep side effects at explicit boundaries. Prefer pure functions for parsing,
  normalization, comparison, and dependency-graph transformations.
- Follow DRY: keep each rule and piece of domain knowledge in one authoritative
  place. Do not remove small, incidental repetition by introducing an
  abstraction that is harder to understand than the repeated code.
- Keep modules and public interfaces focused. Split code only when distinct
  responsibilities have emerged.
- Use the standard library where it provides a clear and reliable solution;
  use established dependencies when implementing the same behavior correctly
  would add substantial complexity.
- Update or add tests for every behavior change.
- Run `make ci` before considering a change complete.

## Commits

- Use the Conventional Commits format.
- Write commit types, optional scopes, and descriptions in English.
- Use the form `<type>(<optional-scope>): <description>`.
- Keep the description concise, imperative, and lowercase unless it contains a
  proper name.
- Keep each commit limited to one logical change.

Examples:

```text
feat(deps): add Git dependency resolution
fix(ecf): preserve target inheritance order
docs: clarify toolchain selection
test(lockfile): cover checksum mismatch
chore: update development dependencies
```
