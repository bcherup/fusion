-- Runs on the selected answered phone leg, without replacing other answer hooks.
if session and session:ready() then
    for index, direction in ipairs({"read", "write"}) do
        local level = tonumber(argv[index])
        if not level or level ~= math.floor(level) or level < -4 or level > 4 then return end
    end
    session:execute("set_audio_level", "read " .. argv[1])
    session:execute("set_audio_level", "write " .. argv[2])
end
