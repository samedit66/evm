class
    EVM_HTTP_CLIENT

create
    make

feature {NONE} -- Initialization

    make (a_base_url: READABLE_STRING_8; a_transport: EVM_HTTP_TRANSPORT)
        require
            base_url_not_empty: not a_base_url.is_empty
        do
            base_url := a_base_url.to_string_8
            transport := a_transport
        end

feature -- Access

    base_url: STRING_8

    transport: EVM_HTTP_TRANSPORT

    retry_count: INTEGER

feature -- Requests

    get (a_path: READABLE_STRING_8): EVM_HTTP_REQUEST
        do
            create Result.make ("GET", a_path)
        end

    post (a_path: READABLE_STRING_8): EVM_HTTP_REQUEST
        do
            create Result.make ("POST", a_path)
        end

    put (a_path: READABLE_STRING_8): EVM_HTTP_REQUEST
        do
            create Result.make ("PUT", a_path)
        end

    patch (a_path: READABLE_STRING_8): EVM_HTTP_REQUEST
        do
            create Result.make ("PATCH", a_path)
        end

    delete (a_path: READABLE_STRING_8): EVM_HTTP_REQUEST
        do
            create Result.make ("DELETE", a_path)
        end

    execute (a_request: EVM_HTTP_REQUEST): EVM_HTTP_RESPONSE
        local
            retries: INTEGER
        do
            Result := transport.execute (base_url, a_request)
            from
            until
                retries >= retry_count or else not a_request.is_idempotent or else
                    not Result.is_retryable
            loop
                retries := retries + 1
                Result := transport.execute (base_url, a_request)
            end
        end

feature -- Configuration

    set_retry_count (a_count: INTEGER)
        require
            non_negative: a_count >= 0
        do
            retry_count := a_count
        ensure
            retry_count_set: retry_count = a_count
        end

invariant
    base_url_not_empty: not base_url.is_empty
    retry_count_non_negative: retry_count >= 0

end
