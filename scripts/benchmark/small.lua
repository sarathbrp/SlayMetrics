local BASE = "/stress_test_data"
local COUNT = 2500000

counter = 0

setup = function(thread)
    thread:set("id", counter)
    counter = counter + 1
end

init = function(args)
    math.randomseed(os.time() * (id + 1))
end

local function build_path(idx)
    local dir1 = math.floor(idx / 1000000)
    local dir2 = math.floor(idx / 1000) % 1000

    return string.format(
        "%s/small/%03d/%03d/file_%09d.html",
        BASE, dir1, dir2, idx
    )
end

request = function()
    local idx = math.random(0, COUNT - 1)
    return wrk.format("GET", build_path(idx))
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
