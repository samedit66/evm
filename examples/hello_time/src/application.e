class
    APPLICATION

create
    make

feature {NONE} -- Initialization

    make
            -- Run the application.
        local
            clock: DT_SYSTEM_CLOCK
        do
            create clock.make
            print ("Hello from EVM!%N")
            print ("The current local time is ")
            print (clock.date_time_now.precise_out)
            print (".%N")
        end

end
