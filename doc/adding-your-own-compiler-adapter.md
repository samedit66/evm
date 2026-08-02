# Adding your own compiler adapter

Compiler adapters translate EVM's compiler-independent project model into a
native build and run lifecycle. An adapter is part of EVM itself: the current
registry is intentionally explicit and does not load arbitrary project code or
third-party Python entry points.

## Define the supported contract first

Choose a stable lowercase provider ID and document the smallest useful scope.
An adapter does not need to implement every EVM operation. It must reject an
unsupported project kind, capability, or operation before invoking the
compiler.

The experimental `serpent` and `liberty` adapters are examples of narrow
contracts. They support application `build` and `run`, but not libraries,
configuration-only checks, tests, or implicit automatic selection.

Decide these points before adding code:

- how installations are identified and versioned;
- whether the compiler consumes ECF, generated configuration, source roots, or
  an API request;
- which artifact a successful build produces;
- how that artifact is run;
- which language capabilities are supported;
- whether EVM can install the compiler reproducibly.

## Register identity and discovery metadata

Add the adapter implementation and `CompilerAdapterMetadata` entry in
`evm.toolchain.compilers`. Registry order is user-visible in diagnostics and
`toolchain list`.

Metadata contains:

- `name`: the stable selector ID;
- `display_name`: the label printed in build output;
- `executable`: the executable used for environment discovery;
- `version_arguments`: arguments used to probe a numeric version;
- `automatic`: whether an unconfigured project may select the adapter.

Keep experimental adapters out of automatic selection. Users can still select
them explicitly with `--toolchain`, `EVM_TOOLCHAIN`, `[compatibility]`, or
`[toolchain]`.

## Implement the compiler lifecycle

Implement the `CompilerAdapter` protocol in `evm.toolchain.compilers`:

- `prepare_build` writes only derived input below the isolated build directory;
- `compiler_command` returns the build command without running it;
- `artifact_candidates` describes the native executable, JVM classpath, or
  other output expected after a successful build;
- `run_command` returns the command that executes that artifact and preserves
  application arguments;
- `legacy_ecf_variables` supplies variables needed when adapting a legacy ECF;
- `compatibility_error` explains the first unsupported project requirement.

Do not write generated compiler configuration beside user sources. Build state
belongs below:

```text
build/<adapter>/<version-or-revision>/<target>/<mode>/
```

Adapters that consume source roots directly should use the effective target
chain and installed dependency roots. They must not scan the project build
directory or silently include neighboring source files.

ISE, Gobo, and Liberty produce native executables. Serpent instead produces a
JVM classpath directory. Its adapter runs a worker with the managed Python
interpreter; the worker imports the Serpent API and starts Java with Serpent's
required settings. This keeps Python 3.13 dependencies out of EVM's Python
3.11 process.

Use a structured request or serialized worker request when an external API has
a long or unstable call signature. Do not add provider checks to the project
workflow when the behavior belongs to the adapter.

## Add managed installation support

Managed installations live in the shared toolchain store and must have an
immutable identity. Extend `evm.toolchain.installation` and
`evm.toolchain.store` with:

1. selector resolution;
2. acquisition and integrity or revision verification;
3. installation under a staging or provider-specific managed directory;
4. executable and prerequisite verification;
5. environment construction;
6. idempotent reuse and safe removal;
7. locked and offline behavior.

Release providers normally use numeric versions and downloadable artifacts.
Revision providers resolve `latest` to a full Git commit and store that commit
in `Eiffel.toml` and `Eiffel.lock`. Never leave a mutable branch name in the
project policy.

If installation requires a different runtime, isolate it. Serpent, for
example, is installed into its own Python 3.13-or-newer virtual environment.
Do not install compiler packages into EVM's environment. Provider-specific
configuration must also remain isolated: Liberty bootstrap uses a managed
`HOME` so it does not replace the user's Liberty configuration.

## Extend validation

The manifest parser gets known adapter IDs from the registry. If the provider
uses Git revisions rather than numeric versions, extend exact-selector and
compatibility validation deliberately. A revision-based provider should reject
numeric constraints until it has a documented mapping from revisions to
releases.

Implement conservative capability checks. Report unsupported behavior before
the build; do not assume that a compiler implements ECMA, ISE semantics, void
safety, or concurrency merely because it accepts the source.

## Test the adapter

Every adapter change requires deterministic tests for:

- registration and automatic-selection policy;
- selector parsing, exact identity, and unknown-provider diagnostics;
- build directory isolation and generated configuration;
- root class, creation procedure, and source/dependency mapping;
- dev and release behavior;
- artifact lookup and run argument forwarding;
- unsupported project kinds and capabilities;
- missing prerequisites and compiler failures;
- managed installation, reuse, locked identity, and offline behavior.

Use fake executables, in-memory HTTP responses, and mocked subprocess
boundaries for unit tests. Mark tests requiring a real compiler with the
`toolchain` marker. Finish with:

```console
make ci
```

When the adapter changes product behavior, update `SPEC.md` first or in the
same logical change. User documentation should state experimental limitations
without duplicating the complete specification in code docstrings.
