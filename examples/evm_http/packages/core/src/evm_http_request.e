class
    EVM_HTTP_REQUEST

create
    make

feature {NONE} -- Initialization

    make (a_method, a_path: READABLE_STRING_8)
        require
            method_not_empty: not a_method.is_empty
            path_not_empty: not a_path.is_empty
        do
            method := a_method.as_upper.to_string_8
            path := a_path.to_string_8
            create headers.make (4)
            create query_parameters.make (2)
            timeout_seconds := 30
            max_redirects := 5
        end

feature -- Access

    method: STRING_8

    path: STRING_8

    headers: HASH_TABLE [STRING_8, STRING_8]

    query_parameters: ARRAYED_LIST [TUPLE [name: STRING_8; value: STRING_8]]

    body: detachable STRING_8

    timeout_seconds: INTEGER

    max_redirects: INTEGER

    is_idempotent: BOOLEAN
        do
            Result := method.same_string ("GET") or else method.same_string ("PUT") or else
                method.same_string ("DELETE")
        end

feature -- Building

    with_header (a_name, a_value: READABLE_STRING_8): EVM_HTTP_REQUEST
        require
            name_not_empty: not a_name.is_empty
        do
            headers.force (a_value.to_string_8, a_name.to_string_8)
            Result := Current
        ensure
            header_set: headers.has (a_name.to_string_8)
        end

    with_query (a_name, a_value: READABLE_STRING_GENERAL): EVM_HTTP_REQUEST
        require
            name_not_empty: not a_name.is_empty
        do
            query_parameters.extend ([a_name.to_string_8, a_value.to_string_8])
            Result := Current
        ensure
            parameter_added: query_parameters.count = old query_parameters.count + 1
        end

    with_bearer_token (a_token: READABLE_STRING_8): EVM_HTTP_REQUEST
        require
            token_not_empty: not a_token.is_empty
        do
            Result := with_header ("Authorization", "Bearer " + a_token)
        end

    with_json_body (a_body: READABLE_STRING_8): EVM_HTTP_REQUEST
        do
            body := a_body.to_string_8
            Result := with_header ("Content-Type", "application/json")
        ensure
            body_set: attached body as l_body and then l_body.same_string (a_body)
        end

    with_timeout (a_seconds: INTEGER): EVM_HTTP_REQUEST
        require
            positive: a_seconds > 0
        do
            timeout_seconds := a_seconds
            Result := Current
        ensure
            timeout_set: timeout_seconds = a_seconds
        end

    with_max_redirects (a_count: INTEGER): EVM_HTTP_REQUEST
        require
            valid_count: a_count >= -1
        do
            max_redirects := a_count
            Result := Current
        ensure
            redirects_set: max_redirects = a_count
        end

invariant
    method_not_empty: not method.is_empty
    path_not_empty: not path.is_empty
    positive_timeout: timeout_seconds > 0
    valid_max_redirects: max_redirects >= -1

end
