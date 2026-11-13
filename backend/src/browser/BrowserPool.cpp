#include "BrowserPool.h"

#include <algorithm>
#include <stdexcept>
#include <utility>

namespace argus::browser {
namespace {

bool stopWasRequested(const StopPredicate &stopRequested) noexcept
{
    if (!stopRequested) return false;
    try
    {
        return stopRequested();
    }
    catch (...)
    {
        return true;
    }
}

}  // namespace

BrowserPool::Lease::Lease(BrowserPool *pool, std::unique_ptr<BrowserSession> session)
    : pool_(pool), session_(std::move(session))
{
}

BrowserPool::Lease::~Lease() { reset(); }

BrowserPool::Lease::Lease(Lease &&other) noexcept
    : pool_(std::exchange(other.pool_, nullptr)), session_(std::move(other.session_))
{
}

BrowserPool::Lease &BrowserPool::Lease::operator=(Lease &&other) noexcept
{
    if (this != &other)
    {
        reset();
        pool_ = std::exchange(other.pool_, nullptr);
        session_ = std::move(other.session_);
    }
    return *this;
}

void BrowserPool::Lease::reset()
{
    if (pool_ && session_) pool_->release(std::move(session_));
    pool_ = nullptr;
}

BrowserPool::BrowserPool(std::string endpoint, std::size_t maximumSessions)
    : endpoint_(std::move(endpoint)), maximumSessions_(std::max<std::size_t>(1, maximumSessions))
{
}

BrowserPool::Lease BrowserPool::acquire(std::chrono::milliseconds timeout,
                                        const StopPredicate &stopRequested)
{
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    std::unique_lock lock(mutex_);
    for (;;)
    {
        if (active_ < maximumSessions_)
        {
            lock.unlock();
            const bool cancelled = stopWasRequested(stopRequested);
            lock.lock();
            if (cancelled) throw std::runtime_error("cdp_cancelled");
            if (active_ < maximumSessions_)
            {
                ++active_;
                break;
            }
            continue;
        }
        lock.unlock();
        const bool cancelled = stopWasRequested(stopRequested);
        lock.lock();
        if (cancelled) throw std::runtime_error("cdp_cancelled");
        const auto now = std::chrono::steady_clock::now();
        if (now >= deadline) throw std::runtime_error("browser_unavailable");
        available_.wait_until(
            lock, std::min(deadline, now + std::chrono::milliseconds(50)));
    }
    lock.unlock();
    try
    {
        auto session = std::make_unique<BrowserSession>(endpoint_);
        session->open(timeout, stopRequested);
        return Lease(this, std::move(session));
    }
    catch (...)
    {
        std::lock_guard rollback(mutex_);
        --active_;
        available_.notify_one();
        throw;
    }
}

std::size_t BrowserPool::active() const
{
    std::lock_guard lock(mutex_);
    return active_;
}

void BrowserPool::release(std::unique_ptr<BrowserSession> session)
{
    if (session) session->close();
    std::lock_guard lock(mutex_);
    if (active_ > 0) --active_;
    available_.notify_one();
}

}  // namespace argus::browser
