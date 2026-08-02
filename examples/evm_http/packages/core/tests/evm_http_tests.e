class
    EVM_HTTP_TESTS

inherit
    EQA_TEST_SET

feature -- Tests

    test_request_builder
        local
            request: EVM_HTTP_REQUEST
        do
            create request.make ("post", "/messages")
            request := request.with_query ("page", "2")
            request := request.with_bearer_token ("secret")
            request := request.with_json_body ("{%"message%": %"hello%"}")
            request := request.with_timeout (5)

            assert ("method normalized", request.method.same_string ("POST"))
            assert ("query added", request.query_parameters.count = 1)
            assert ("authorization added", request.headers.has ("Authorization"))
            assert ("content type added", request.headers.has ("Content-Type"))
            assert ("timeout changed", request.timeout_seconds = 5)
            assert ("post is not idempotent", not request.is_idempotent)
        end

    test_idempotent_request_retries_retryable_response
        local
            client: EVM_HTTP_CLIENT
            transport: EVM_HTTP_FAKE_TRANSPORT
            failed, succeeded, response: EVM_HTTP_RESPONSE
        do
            create transport.make
            create failed.make (503, "", "unavailable", Void)
            create succeeded.make (200, "", "ok", Void)
            transport.add_response (failed)
            transport.add_response (succeeded)
            create client.make ("https://example.test", transport)
            client.set_retry_count (1)

            response := client.execute (client.get ("/health"))

            assert ("eventually succeeds", response.is_success)
            assert ("retried once", transport.call_count = 2)
        end

    test_post_is_not_retried
        local
            client: EVM_HTTP_CLIENT
            transport: EVM_HTTP_FAKE_TRANSPORT
            failed, response: EVM_HTTP_RESPONSE
        do
            create transport.make
            create failed.make (503, "", "unavailable", Void)
            transport.add_response (failed)
            create client.make ("https://example.test", transport)
            client.set_retry_count (2)

            response := client.execute (client.post ("/messages"))

            assert ("single attempt", transport.call_count = 1)
            assert ("failure returned", response.status = 503)
        end

end
