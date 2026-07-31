class
    CALCULATOR

feature -- Arithmetic

    add (left, right: INTEGER): INTEGER
        do
            Result := left + right
        end

    subtract (left, right: INTEGER): INTEGER
        do
            Result := left - right
        end

    multiply (left, right: INTEGER): INTEGER
        do
            Result := left * right
        end

    divide (left, right: REAL_64): REAL_64
        require
            divisor_not_zero: right /= 0.0
        do
            Result := left / right
        end

end
