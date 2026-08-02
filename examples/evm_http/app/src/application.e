class
    APPLICATION

create
    make

feature {NONE} -- Initialization

    make
            -- Fetch and parse a JSON document.
        local
            client: EVM_HTTP_CLIENT
            transport: EVM_HTTP_ISE_TRANSPORT
            request: EVM_HTTP_REQUEST
            response: EVM_HTTP_RESPONSE
            parser: JSON_PARSER
        do
            create transport
            create client.make ("https://httpbin.org", transport)
            client.set_retry_count (2)
            request := client.get ("/get")
            request := request.with_query ("client", "evm_http")
            request := request.with_header ("Accept", "application/json")
            response := client.execute (request)
            if response.is_success then
                create parser.make_with_string (response.body)
                if parser.is_parsed and then attached parser.parsed_json_value as value then
                    print (value.representation)
                    print ("%N")
                else
                    print ("Server returned invalid JSON.%N")
                end
            elseif attached response.error_message as error then
                print ("Request failed: " + error + "%N")
            else
                print ("HTTP status: " + response.status.out + "%N")
            end
        end

end
