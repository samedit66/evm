# Calculator AutoTest example

This project demonstrates EVM's ISE AutoTest runner with a stateless
`CALCULATOR` class.

```console
evm test
evm test --class CALCULATOR_TESTS
evm test --class CALCULATOR_TESTS --feature test_divide
```

The `[test]` section selects `runner = "autotest"`. EVM discovers effective
descendants of `EQA_TEST_SET`, generates a console runner under `.evm/`, and
executes each selected test in a separate process.
