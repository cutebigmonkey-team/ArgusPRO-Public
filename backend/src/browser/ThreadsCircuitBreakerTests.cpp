#include "ThreadsCircuitBreaker.h"

#include <chrono>
#include <iostream>

namespace {
int failures = 0;

void expect(bool condition, const char *message)
{
    if (!condition)
    {
        std::cerr << "FAIL: " << message << '\n';
        ++failures;
    }
}
}  // namespace

int main()
{
    using namespace std::chrono_literals;
    using argus::browser::ThreadsCircuitBreaker;

    const ThreadsCircuitBreaker::TimePoint start{};
    ThreadsCircuitBreaker breaker(3, 10min);
    expect(!breaker.recordUnavailable(start), "first empty response keeps circuit closed");
    expect(!breaker.recordUnavailable(start + 1s), "second empty response keeps circuit closed");
    expect(breaker.consecutiveUnavailable() == 2, "unavailable streak is bounded and visible");
    expect(breaker.recordUnavailable(start + 2s), "third empty response opens circuit");
    expect(breaker.isOpen(start + 9min), "circuit remains open during cooldown");
    expect(!breaker.isOpen(start + 10min + 2s), "circuit closes at bounded deadline");
    expect(breaker.consecutiveUnavailable() == 0, "opening circuit clears the probe streak");

    expect(!breaker.recordUnavailable(start + 11min), "new probe starts a new streak");
    breaker.recordSuccess();
    expect(!breaker.isOpen(start + 11min), "success closes circuit");
    expect(breaker.consecutiveUnavailable() == 0, "success clears unavailable streak");

    breaker.recordUnavailable(start + 12min);
    breaker.resetStreak();
    expect(breaker.consecutiveUnavailable() == 0, "unrelated failure breaks consecutive streak");

    expect(ThreadsCircuitBreaker::observes("empty_shell"), "empty shell is observed");
    expect(ThreadsCircuitBreaker::observes("selector_regression"), "selector regression is observed");
    expect(ThreadsCircuitBreaker::observes("auth_required"), "login wall is observed");
    expect(ThreadsCircuitBreaker::observes("challenge_required"), "challenge is observed");
    expect(ThreadsCircuitBreaker::observes("rate_limited"), "rate limit is observed");
    expect(!ThreadsCircuitBreaker::observes("navigation_timeout"),
           "transport failures do not masquerade as consecutive empty pages");

    if (failures == 0) std::cout << "threads circuit breaker tests passed\n";
    return failures == 0 ? 0 : 1;
}
