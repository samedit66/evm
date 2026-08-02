class
    EVM_HTTP_ISE_TRANSPORT

inherit
    EVM_HTTP_TRANSPORT

feature -- Execution

    execute (a_base_url: READABLE_STRING_8; a_request: EVM_HTTP_REQUEST): EVM_HTTP_RESPONSE
        local
            http_client: DEFAULT_HTTP_CLIENT
            session: HTTP_CLIENT_SESSION
            context: HTTP_CLIENT_REQUEST_CONTEXT
            raw_response: HTTP_CLIENT_RESPONSE
        do
            create http_client
            session := http_client.new_session (a_base_url)
            session.set_timeout (a_request.timeout_seconds)
            session.set_connect_timeout (a_request.timeout_seconds)
            session.set_max_redirects (a_request.max_redirects)
            create context.make
            from
                a_request.headers.start
            until
                a_request.headers.after
            loop
                context.add_header (
                    a_request.headers.key_for_iteration,
                    a_request.headers.item_for_iteration
                )
                a_request.headers.forth
            end
            across a_request.query_parameters as parameter loop
                context.add_query_parameter (parameter.name, parameter.value)
            end
            raw_response := execute_request (session, context, a_request)
            create Result.make (
                raw_response.status,
                raw_response.raw_header,
                raw_response.body,
                raw_response.error_message
            )
            session.close
        end

feature {NONE} -- Implementation

    execute_request (
        a_session: HTTP_CLIENT_SESSION;
        a_context: HTTP_CLIENT_REQUEST_CONTEXT;
        a_request: EVM_HTTP_REQUEST
    ): HTTP_CLIENT_RESPONSE
        do
            if a_request.method.same_string ("GET") then
                Result := a_session.get (a_request.path, a_context)
            elseif a_request.method.same_string ("POST") then
                Result := a_session.post (a_request.path, a_context, a_request.body)
            elseif a_request.method.same_string ("PUT") then
                Result := a_session.put (a_request.path, a_context, a_request.body)
            elseif a_request.method.same_string ("PATCH") then
                Result := a_session.patch (a_request.path, a_context, a_request.body)
            elseif a_request.method.same_string ("DELETE") then
                Result := a_session.delete (a_request.path, a_context)
            else
                Result := a_session.custom (a_request.method, a_request.path, a_context)
            end
        end

end
