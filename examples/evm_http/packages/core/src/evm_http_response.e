class
    EVM_HTTP_RESPONSE

create
    make

feature {NONE} -- Initialization

    make (
        a_status: INTEGER;
        a_raw_headers: READABLE_STRING_8;
        a_body, a_error: detachable READABLE_STRING_8
    )
        do
            status := a_status
            raw_headers := a_raw_headers.to_string_8
            if a_body /= Void then
                body := a_body.to_string_8
            else
                create body.make_empty
            end
            if a_error /= Void then
                error_message := a_error.to_string_8
            end
        end

feature -- Access

    status: INTEGER

    raw_headers: STRING_8

    body: STRING_8

    error_message: detachable STRING_8

    is_success: BOOLEAN
        do
            Result := not has_transport_error and status >= 200 and status < 300
        end

    has_transport_error: BOOLEAN
        do
            Result := attached error_message
        end

    is_retryable: BOOLEAN
        do
            Result := has_transport_error or status = 408 or status = 429 or status >= 500
        end

end
