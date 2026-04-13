local BASE = "/stress_test_data"

local SMALL_COUNT  = 2500000
local MEDIUM_COUNT = 250000
local LARGE_COUNT  = 250

local SMALL_PCT  = 0.70
local MEDIUM_PCT = 0.25

counter = 0

setup = function(thread)
    thread:set("id", counter)
    counter = counter + 1
end

init = function(args)
    math.randomseed(os.time() * (id + 1))
end

local function build_path(size_type, idx)
    local dir1 = math.floor(idx / 1000000)
    local dir2 = math.floor(idx / 1000) % 1000

    return string.format(
        "%s/%s/%03d/%03d/file_%09d.html",
        BASE, size_type, dir1, dir2, idx
    )
end

request = function()

    local r = math.random()

    if r < SMALL_PCT then
        local idx = math.random(0, SMALL_COUNT - 1)
        return wrk.format("GET", build_path("small", idx))

    elseif r < SMALL_PCT + MEDIUM_PCT then
        local idx = math.random(0, MEDIUM_COUNT - 1)
        return wrk.format("GET", build_path("medium", idx))

    else
        local idx = math.random(0, LARGE_COUNT - 1)
        return wrk.format("GET", build_path("large", idx))
    end
end

done = function(summary, latency, requests)
    print("\n=== Test Summary ===")
    print(string.format("Requests: %d", summary.requests))
    print(string.format("Bytes: %.2f GB", summary.bytes / (1024^3)))

    print("\nErrors:")
    print(string.format("connect: %d", summary.errors.connect))
    print(string.format("read: %d", summary.errors.read))
    print(string.format("write: %d", summary.errors.write))
    print(string.format("timeout: %d", summary.errors.timeout))
end
