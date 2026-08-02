# EVM HTTP client

This workspace demonstrates a high-level Eiffel HTTP client built and resolved
entirely through EVM. It combines three dependency sources:

- workspace `path` dependencies for the client core and transport;
- EiffelStudio's contributed `http_client` library through the `ise` source;
- Eiffel JSON pinned to a Git tag and immutable commit in `Eiffel.lock`.

The core package provides request building, response classification, transport
errors, and bounded retries for idempotent requests. The ISE transport adapts
those abstractions to EiffelStudio's `DEFAULT_HTTP_CLIENT`. The application
performs a JSON GET request against `https://httpbin.org/get`.

```console
evm install --locked
evm test --package evm_http_core --toolchain ise
evm build --package app --toolchain ise
evm run --package app --toolchain ise
```

The first install requires network access for Eiffel JSON. Once `.evm/` is
populated, install and build can be repeated with `--offline`.
