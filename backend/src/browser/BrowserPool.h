#pragma once

#include "BrowserSession.h"

#include <chrono>
#include <condition_variable>
#include <cstddef>
#include <memory>
#include <mutex>
#include <string>

namespace argus::browser {

class BrowserPool {
  public:
    class Lease {
      public:
        Lease() = default;
        Lease(BrowserPool *pool, std::unique_ptr<BrowserSession> session);
        ~Lease();
        Lease(Lease &&other) noexcept;
        Lease &operator=(Lease &&other) noexcept;
        Lease(const Lease &) = delete;
        Lease &operator=(const Lease &) = delete;
        BrowserSession *operator->() const { return session_.get(); }
        BrowserSession &operator*() const { return *session_; }
        explicit operator bool() const { return session_ != nullptr; }

      private:
        void reset();
        BrowserPool *pool_{nullptr};
        std::unique_ptr<BrowserSession> session_;
    };

    explicit BrowserPool(std::string endpoint, std::size_t maximumSessions = 1);
    Lease acquire(std::chrono::milliseconds timeout = std::chrono::seconds(10),
                  const StopPredicate &stopRequested = {});
    std::size_t active() const;

  private:
    friend class Lease;
    void release(std::unique_ptr<BrowserSession> session);
    std::string endpoint_;
    std::size_t maximumSessions_;
    mutable std::mutex mutex_;
    std::condition_variable available_;
    std::size_t active_{0};
};

}  // namespace argus::browser
