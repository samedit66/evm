# JSON

This example proves that EVM can resolve a real library hosted on GitHub, pin
the selected tag to immutable Git commit and tree identities, materialize it
inside the project, generate the native ECF reference, and compile code that
actually calls the dependency.

The application creates a `JSON_OBJECT` with
[Eiffel JSON](https://github.com/eiffelhub/json) and prints its serialized
representation. The dependency is requested at tag `v0.11`; `Eiffel.lock`
records the exact commit and tree selected by EVM.

## Run

```console
$ evm install --locked
$ evm run --compiler ise
```

The output is a JSON object containing these fields:

```json
{"project":"evm","status":"works"}
```

After the initial installation, the locked dependency and build can also be
verified without network access:

```console
$ evm install --locked --offline
$ evm build --compiler ise --offline
```
