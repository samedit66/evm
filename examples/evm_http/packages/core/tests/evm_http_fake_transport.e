class
    EVM_HTTP_FAKE_TRANSPORT

inherit
    EVM_HTTP_TRANSPORT

create
    make

feature {NONE} -- Initialization

    make
        do
            create responses.make (2)
        end

feature -- Access

    call_count: INTEGER

    last_request: detachable EVM_HTTP_REQUEST

feature -- Configuration

    add_response (a_response: EVM_HTTP_RESPONSE)
        do
            responses.extend (a_response)
        end

feature -- Execution

    execute (a_base_url: READABLE_STRING_8; a_request: EVM_HTTP_REQUEST): EVM_HTTP_RESPONSE
        do
            call_count := call_count + 1
            last_request := a_request
            if responses.is_empty then
                create Result.make (599, "", Void, "no fake response configured")
            else
                Result := responses.item
                responses.remove
            end
        end

feature {NONE} -- Implementation

    responses: ARRAYED_QUEUE [EVM_HTTP_RESPONSE]

end
