note
    testing: "type/manual"

class
    CALCULATOR_TESTS

inherit
    EQA_TEST_SET

feature -- Tests

    test_add
        local
            calculator: CALCULATOR
        do
            create calculator
            assert ("sum", calculator.add (2, 3) = 5)
        end

    test_subtract
        local
            calculator: CALCULATOR
        do
            create calculator
            assert ("difference", calculator.subtract (7, 4) = 3)
        end

    test_multiply
        local
            calculator: CALCULATOR
        do
            create calculator
            assert ("product", calculator.multiply (6, 7) = 42)
        end

    test_divide
        local
            calculator: CALCULATOR
        do
            create calculator
            assert ("quotient", (calculator.divide (7.5, 2.5) - 3.0).abs < 0.000_001)
        end

end
