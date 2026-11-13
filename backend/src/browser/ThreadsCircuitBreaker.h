#pragma once

#include <algorithm>
#include <chrono>
#include <cstddef>
#include <string_view>

namespace argus::browser {

// A process-local guard for Threads' platform-wide soft throttling. It never
// changes scheduler/queue state: while open, the multiplex worker simply skips
// Threads and keeps polling the remaining platforms.
class ThreadsCircuitBreaker {
  public:
    using Clock = std::chrono::steady_clock;
    using TimePoint = Clock::time_point;

    ThreadsCircuitBreaker(std::size_t threshold, std::chrono::seconds cooldown)
        : threshold_(std::max<std::size_t>(1, threshold)),
          cooldown_(std::max(std::chrono::seconds(1), cooldown))
    {
    }

    bool isOpen(TimePoint now = Clock::now()) const noexcept { return now < openUntil_; }

    // Returns true only for the failure that opens the circuit.
    bool recordUnavailable(TimePoint now = Clock::now()) noexcept
    {
        if (isOpen(now)) return false;
        ++consecutiveUnavailable_;
        if (consecutiveUnavailable_ < threshold_) return false;
        consecutiveUnavailable_ = 0;
        openUntil_ = now + cooldown_;
        return true;
    }

    void recordSuccess() noexcept
    {
        consecutiveUnavailable_ = 0;
        openUntil_ = TimePoint{};
    }

    void resetStreak() noexcept { consecutiveUnavailable_ = 0; }

    std::size_t consecutiveUnavailable() const noexcept { return consecutiveUnavailable_; }
    std::size_t threshold() const noexcept { return threshold_; }
    std::chrono::seconds cooldown() const noexcept { return cooldown_; }

    static bool observes(std::string_view error) noexcept
    {
        return error == "empty_shell" || error == "selector_regression" ||
               error == "auth_required" || error == "challenge_required" ||
               error == "rate_limited";
    }

  private:
    std::size_t threshold_;
    std::chrono::seconds cooldown_;
    std::size_t consecutiveUnavailable_{0};
    TimePoint openUntil_{};
};

}  // namespace argus::browser
