#include "CdpClient.h"
#include "BrowserPool.h"

#include <chrono>
#include <future>
#include <iostream>
#include <stdexcept>
#include <string>
#include <thread>

namespace {
int failures = 0;
void expect(bool condition, const char *message)
{
    if (!condition) { std::cerr << "FAIL: " << message << '\n'; ++failures; }
}
}

int main(int argc, char **argv)
{
    using namespace argus::browser;
    using namespace std::chrono_literals;
    CdpMessageRouter router;
    const int first = router.beginRequest();
    const int second = router.beginRequest();
    router.ingest("{\"id\":2,\"result\":{\"value\":\"second\"}}");
    router.ingest("{\"id\":1,\"result\":{\"value\":\"first\"}}");
    expect(router.waitForResponse(second, 20ms).get("value", "").asString() == "second",
           "out-of-order response correlation");
    expect(router.waitForResponse(first, 20ms).get("value", "").asString() == "first",
           "first response correlation");

    const std::size_t malformedBefore = router.malformedMessages();
    expect(!router.ingest("not-json"), "malformed response rejected");
    expect(router.malformedMessages() == malformedBefore + 1, "malformed response counted");

    const int timeoutId = router.beginRequest();
    bool timedOut = false;
    try { (void)router.waitForResponse(timeoutId, 5ms); }
    catch (const std::runtime_error &error) { timedOut = std::string(error.what()) == "cdp_timeout"; }
    expect(timedOut, "bounded command timeout");

    const int cancelledId = router.beginRequest();
    bool responseCancelled = false;
    try
    {
        (void)router.waitForResponse(cancelledId, 2s, [&router] {
            // Cancellation callbacks run without the router mutex held.
            (void)router.malformedMessages();
            return true;
        });
    }
    catch (const std::runtime_error &error)
    {
        responseCancelled = std::string(error.what()) == "cdp_cancelled";
    }
    expect(responseCancelled, "pending command cancellation");

    bool eventCancelled = false;
    try
    {
        (void)router.waitForEvent("Page.neverArrives", 2s, [] { return true; });
    }
    catch (const std::runtime_error &error)
    {
        eventCancelled = std::string(error.what()) == "cdp_cancelled";
    }
    expect(eventCancelled, "event wait cancellation");

    BrowserPool pool("http://127.0.0.1:9", 1);
    bool acquireCancelled = false;
    try { (void)pool.acquire(2s, [] { return true; }); }
    catch (const std::runtime_error &error)
    {
        acquireCancelled = std::string(error.what()) == "cdp_cancelled";
    }
    expect(acquireCancelled && pool.active() == 0,
           "pool cancellation does not consume a session slot");

    expect(router.ingest("{\"method\":\"Page.loadEventFired\",\"params\":{\"timestamp\":1}}"),
           "event ingested");
    expect(router.waitForEvent("Page.loadEventFired", 20ms).get("timestamp", 0).asInt() == 1,
           "event separated from command responses");

    for (int index = 0; index < 2100; ++index)
        router.ingest("{\"method\":\"Runtime.consoleAPICalled\",\"params\":{\"index\":" +
                      std::to_string(index) + "}}");
    const auto bounded = router.takeEvents("Runtime.consoleAPICalled", 3000);
    expect(bounded.size() == 2048, "CDP event queue is bounded");
    expect(bounded.front().get("index", -1).asInt() == 52 &&
           bounded.back().get("index", -1).asInt() == 2099,
           "bounded queue retains newest events");

    const int pending = router.beginRequest();
    auto waiting = std::async(std::launch::async, [&] {
        try { (void)router.waitForResponse(pending, 2s); }
        catch (const std::runtime_error &error) { return std::string(error.what()); }
        return std::string();
    });
    std::this_thread::sleep_for(5ms);
    router.failAll("cdp_disconnected");
    expect(waiting.get() == "cdp_disconnected", "disconnect releases pending command");

    if (argc == 2 && std::string(argv[1]) == "--router-only")
    {
        if (failures == 0) std::cout << "CDP router tests passed\n";
        return failures == 0 ? 0 : 1;
    }

    bool unavailable = false;
    try
    {
        CdpClient missing("http://127.0.0.1:9");
        (void)missing.version(50ms);
    }
    catch (const std::runtime_error &error)
    {
        unavailable = std::string(error.what()) == "browser_unavailable";
    }
    expect(unavailable, "Chromium unavailable classified");

    // Exercise the asynchronous connect callback + immediate teardown path
    // repeatedly. This used to leave raw-this callbacks on a per-client event
    // loop and could terminate the process after enough BrowserSession churn.
    bool teardownChurnSafe = true;
    for (int iteration = 0; iteration < 200; ++iteration)
    {
        try
        {
            CdpClient churn("http://127.0.0.1:9");
            try
            {
                churn.connectWebSocket("ws://127.0.0.1:9/devtools/browser/churn", 20ms);
                teardownChurnSafe = false;
            }
            catch (const std::runtime_error &error)
            {
                if (std::string(error.what()) != "browser_unavailable") teardownChurnSafe = false;
            }
        }
        catch (...)
        {
            teardownChurnSafe = false;
        }
    }
    std::this_thread::sleep_for(100ms);  // Drain late weak callbacks on the shared CDP loop.
    expect(teardownChurnSafe, "repeated asynchronous CdpClient teardown is safe");

    if (failures == 0) std::cout << "CDP client tests passed\n";
    return failures == 0 ? 0 : 1;
}
