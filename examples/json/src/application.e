class
    APPLICATION

create
    make

feature {NONE} -- Initialization

    make
            -- Run the application.
        local
            document: JSON_OBJECT
        do
            create document.make
            document.put_string ("evm", "project")
            document.put_string ("works", "status")
            print (document.representation)
            print ("%N")
        end

end
