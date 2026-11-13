#include "CdpClient.h"

#include <curl/curl.h>
#include <drogon/HttpRequest.h>
#include <drogon/WebSocketClient.h>
#include <trantor/net/EventLoopThread.h>

#include <arpa/inet.h>
#include <netdb.h>

#include <algorithm>
#include <condition_variable>
#include <cstdint>
#include <iostream>
#include <stdexcept>
#include <thread>
#include <utility>

namespace argus::browser {
namespace {

struct HttpResult {
    long status{0};
    std::string body;
};

bool stopWasRequested(const StopPredicate &stopRequested) noexcept
{
    if (!stopRequested) return false;
    try
    {
        return stopRequested();
    }
    catch (...)
    {
        // A cancellation callback must never unwind through libcurl or a CDP
        // event-loop wait. Treat a broken callback as a cancellation request.
        return true;
    }
}

void throwIfStopRequested(const StopPredicate &stopRequested)
{
    if (stopWasRequested(stopRequested)) throw std::runtime_error("cdp_cancelled");
}

int abortHttpWhenStopping(void *opaque, curl_off_t, curl_off_t, curl_off_t, curl_off_t) noexcept
{
    const auto *stopRequested = static_cast<const StopPredicate *>(opaque);
    return stopRequested && stopWasRequested(*stopRequested) ? 1 : 0;
}

std::size_t appendBody(char *data, std::size_t size, std::size_t count, void *opaque)
{
    auto &body = *static_cast<std::string *>(opaque);
    body.append(data, size * count);
    return size * count;
}

HttpResult request(const std::string &url, const char *method, std::chrono::milliseconds timeout,
                   const StopPredicate &stopRequested = {})
{
    throwIfStopRequested(stopRequested);
    CURL *curl = curl_easy_init();
    if (!curl) throw std::runtime_error("browser_unavailable");
    HttpResult result;
    curl_easy_setopt(curl, CURLOPT_URL, url.c_str());
    curl_easy_setopt(curl, CURLOPT_CUSTOMREQUEST, method);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, appendBody);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &result.body);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT_MS, static_cast<long>(timeout.count()));
    curl_easy_setopt(curl, CURLOPT_TIMEOUT_MS, static_cast<long>(timeout.count()));
    curl_easy_setopt(curl, CURLOPT_NOSIGNAL, 1L);
    curl_easy_setopt(curl, CURLOPT_PROTOCOLS_STR, "http,https");
    curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, abortHttpWhenStopping);
    curl_easy_setopt(curl, CURLOPT_XFERINFODATA, &stopRequested);
    // Chromium rejects non-loopback Host headers on the DevTools HTTP server
    // as DNS-rebinding protection. The TCP peer remains the internal Compose
    // hostname; only the HTTP authority is normalized to loopback.
    curl_slist *headers = curl_slist_append(nullptr, "Host: 127.0.0.1");
    curl_easy_setopt(curl, CURLOPT_HTTPHEADER, headers);
    const CURLcode code = curl_easy_perform(curl);
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &result.status);
    curl_slist_free_all(headers);
    curl_easy_cleanup(curl);
    if (code != CURLE_OK)
    {
        if (code == CURLE_ABORTED_BY_CALLBACK && stopWasRequested(stopRequested))
            throw std::runtime_error("cdp_cancelled");
        throw std::runtime_error("browser_unavailable");
    }
    return result;
}

std::string rewriteWebSocketEndpoint(const std::string &webSocketUrl,
                                     const std::string &httpEndpoint)
{
    CURLU *webSocket = curl_url();
    CURLU *endpoint = curl_url();
    if (!webSocket || !endpoint)
    {
        if (webSocket) curl_url_cleanup(webSocket);
        if (endpoint) curl_url_cleanup(endpoint);
        throw std::runtime_error("browser_unavailable");
    }
    auto cleanup = [&] {
        curl_url_cleanup(webSocket);
        curl_url_cleanup(endpoint);
    };
    if (curl_url_set(webSocket, CURLUPART_URL, webSocketUrl.c_str(), 0) != CURLUE_OK ||
        curl_url_set(endpoint, CURLUPART_URL, httpEndpoint.c_str(), 0) != CURLUE_OK)
    {
        cleanup();
        throw std::runtime_error("invalid_payload");
    }
    char *host = nullptr;
    char *port = nullptr;
    if (curl_url_get(endpoint, CURLUPART_HOST, &host, 0) != CURLUE_OK || !host)
    {
        cleanup();
        throw std::runtime_error("invalid_payload");
    }
    (void)curl_url_get(endpoint, CURLUPART_PORT, &port, 0);
    const CURLUcode hostResult = curl_url_set(webSocket, CURLUPART_HOST, host, 0);
    const CURLUcode portResult = curl_url_set(webSocket, CURLUPART_PORT,
                                              port ? port : "80", 0);
    curl_free(host);
    curl_free(port);
    char *rewritten = nullptr;
    const CURLUcode urlResult = curl_url_get(webSocket, CURLUPART_URL, &rewritten, 0);
    const std::string result = rewritten ? rewritten : "";
    curl_free(rewritten);
    cleanup();
    if (hostResult != CURLUE_OK || portResult != CURLUE_OK || urlResult != CURLUE_OK || result.empty())
        throw std::runtime_error("invalid_payload");
    return result;
}

Json::Value parseJson(const std::string &body)
{
    Json::CharReaderBuilder builder;
    Json::Value value;
    std::string errors;
    const std::unique_ptr<Json::CharReader> reader(builder.newCharReader());
    if (!reader->parse(body.data(), body.data() + body.size(), &value, &errors))
        throw std::runtime_error("invalid_payload");
    return value;
}

std::string jsonText(const Json::Value &value)
{
    Json::StreamWriterBuilder builder;
    builder["indentation"] = "";
    return Json::writeString(builder, value);
}

std::string escapedUrl(const std::string &value)
{
    CURL *curl = curl_easy_init();
    if (!curl) throw std::runtime_error("browser_unavailable");
    char *raw = curl_easy_escape(curl, value.c_str(), static_cast<int>(value.size()));
    if (!raw)
    {
        curl_easy_cleanup(curl);
        throw std::runtime_error("invalid_payload");
    }
    std::string encoded(raw);
    curl_free(raw);
    curl_easy_cleanup(curl);
    return encoded;
}

struct WebSocketParts {
    std::string host;
    std::string path;
    uint16_t port{0};
    bool secure{false};
};

std::string resolveHost(const std::string &host)
{
    addrinfo hints{};
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    addrinfo *addresses = nullptr;
    if (getaddrinfo(host.c_str(), nullptr, &hints, &addresses) != 0 || !addresses)
        throw std::runtime_error("browser_unavailable");
    std::string resolved;
    for (addrinfo *current = addresses; current && resolved.empty(); current = current->ai_next)
    {
        char buffer[INET6_ADDRSTRLEN]{};
        const void *raw = nullptr;
        if (current->ai_family == AF_INET)
            raw = &reinterpret_cast<const sockaddr_in *>(current->ai_addr)->sin_addr;
        else if (current->ai_family == AF_INET6)
            raw = &reinterpret_cast<const sockaddr_in6 *>(current->ai_addr)->sin6_addr;
        if (raw && inet_ntop(current->ai_family, raw, buffer, sizeof(buffer))) resolved = buffer;
    }
    freeaddrinfo(addresses);
    if (resolved.empty()) throw std::runtime_error("browser_unavailable");
    return resolved;
}

WebSocketParts splitWebSocketUrl(const std::string &url)
{
    CURLU *parsed = curl_url();
    if (!parsed) throw std::runtime_error("browser_unavailable");
    auto cleanup = [&] { curl_url_cleanup(parsed); };
    if (curl_url_set(parsed, CURLUPART_URL, url.c_str(), 0) != CURLUE_OK)
    {
        cleanup();
        throw std::runtime_error("invalid_payload");
    }
    auto part = [&](CURLUPart key) {
        char *raw = nullptr;
        std::string output;
        if (curl_url_get(parsed, key, &raw, 0) == CURLUE_OK && raw)
        {
            output = raw;
            curl_free(raw);
        }
        return output;
    };
    const std::string scheme = part(CURLUPART_SCHEME);
    const std::string host = part(CURLUPART_HOST);
    const std::string port = part(CURLUPART_PORT);
    std::string path = part(CURLUPART_PATH);
    const std::string query = part(CURLUPART_QUERY);
    cleanup();
    if ((scheme != "ws" && scheme != "wss") || host.empty())
        throw std::runtime_error("invalid_payload");
    if (path.empty()) path = "/";
    if (!query.empty()) path += "?" + query;
    int numericPort = scheme == "wss" ? 443 : 80;
    if (!port.empty())
    {
        try { numericPort = std::stoi(port); }
        catch (...) { throw std::runtime_error("invalid_payload"); }
    }
    if (numericPort < 1 || numericPort > 65535) throw std::runtime_error("invalid_payload");
    return {host, std::move(path), static_cast<uint16_t>(numericPort), scheme == "wss"};
}

trantor::EventLoop *cdpEventLoop()
{
    // Drogon WebSocketClient schedules stop/close work on its EventLoop. A
    // per-CdpClient EventLoopThread can therefore be destroyed while those
    // callbacks are still queued. Keep one bounded CDP loop for the process
    // lifetime; individual clients detach through weak callback state.
    static trantor::EventLoopThread *thread = [] {
        auto *created = new trantor::EventLoopThread("argus-cdp");
        created->run();
        return created;
    }();
    return thread->getLoop();
}

void stopAndReleaseOnLoop(drogon::WebSocketClientPtr client)
{
    if (!client) return;
    cdpEventLoop()->queueInLoop([client = std::move(client)]() mutable {
        client->setMessageHandler(
            [](std::string &&, const drogon::WebSocketClientPtr &,
               const drogon::WebSocketMessageType &) {});
        client->setConnectionClosedHandler([](const drogon::WebSocketClientPtr &) {});
        client->stop();
        // Release the final caller-owned reference on the same I/O loop, after
        // stop() has had an opportunity to enqueue its close operations.
        cdpEventLoop()->queueInLoop([client = std::move(client)]() mutable { client.reset(); });
    });
}

}  // namespace

struct CdpMessageRouter::Pending {
    std::condition_variable ready;
    bool complete{false};
    Json::Value response;
    std::string failure;
};

int CdpMessageRouter::beginRequest()
{
    std::lock_guard lock(mutex_);
    const int id = nextId_++;
    pending_.emplace(id, std::make_shared<Pending>());
    return id;
}

Json::Value CdpMessageRouter::waitForResponse(int id, std::chrono::milliseconds timeout,
                                              const StopPredicate &stopRequested)
{
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    std::unique_lock lock(mutex_);
    const auto it = pending_.find(id);
    if (it == pending_.end()) throw std::runtime_error("cdp_request_unknown");
    const auto pending = it->second;
    while (!pending->complete)
    {
        lock.unlock();
        const bool cancelled = stopWasRequested(stopRequested);
        lock.lock();
        if (cancelled)
        {
            pending_.erase(id);
            throw std::runtime_error("cdp_cancelled");
        }
        const auto now = std::chrono::steady_clock::now();
        if (now >= deadline)
        {
            pending_.erase(id);
            throw std::runtime_error("cdp_timeout");
        }
        pending->ready.wait_until(
            lock, std::min(deadline, now + std::chrono::milliseconds(50)),
            [&] { return pending->complete; });
    }
    pending_.erase(id);
    if (!pending->failure.empty()) throw std::runtime_error(pending->failure);
    if (pending->response.isMember("error")) throw std::runtime_error("cdp_command_failed");
    return pending->response.get("result", Json::Value(Json::objectValue));
}

bool CdpMessageRouter::ingest(std::string_view message)
{
    Json::CharReaderBuilder builder;
    Json::Value root;
    std::string errors;
    const std::unique_ptr<Json::CharReader> reader(builder.newCharReader());
    if (!reader->parse(message.data(), message.data() + message.size(), &root, &errors) || !root.isObject())
    {
        std::lock_guard lock(mutex_);
        ++malformed_;
        return false;
    }
    std::lock_guard lock(mutex_);
    if (root["id"].isInt())
    {
        const auto it = pending_.find(root["id"].asInt());
        if (it == pending_.end()) return true;
        it->second->response = std::move(root);
        it->second->complete = true;
        it->second->ready.notify_all();
        return true;
    }
    if (root["method"].isString())
    {
        if (events_.size() >= maximumEvents_) events_.erase(events_.begin());
        events_.push_back(std::move(root));
    }
    return true;
}

Json::Value CdpMessageRouter::waitForEvent(const std::string &method,
                                           std::chrono::milliseconds timeout,
                                           const StopPredicate &stopRequested)
{
    const auto deadline = std::chrono::steady_clock::now() + timeout;
    std::unique_lock lock(mutex_);
    for (;;)
    {
        lock.unlock();
        const bool cancelled = stopWasRequested(stopRequested);
        lock.lock();
        if (cancelled) throw std::runtime_error("cdp_cancelled");
        const auto it = std::find_if(events_.begin(), events_.end(), [&](const Json::Value &event) {
            return event.get("method", "").asString() == method;
        });
        if (it != events_.end())
        {
            Json::Value event = std::move(*it);
            events_.erase(it);
            return event.get("params", Json::Value(Json::objectValue));
        }
        if (std::chrono::steady_clock::now() >= deadline) throw std::runtime_error("cdp_timeout");
        lock.unlock();
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
        lock.lock();
    }
}

std::vector<Json::Value> CdpMessageRouter::takeEvents(const std::string &method,
                                                       std::size_t maximum)
{
    std::vector<Json::Value> output;
    std::lock_guard lock(mutex_);
    for (auto it = events_.begin(); it != events_.end() && output.size() < maximum;)
    {
        if (it->get("method", "").asString() == method)
        {
            output.push_back(it->get("params", Json::Value(Json::objectValue)));
            it = events_.erase(it);
        }
        else ++it;
    }
    return output;
}

void CdpMessageRouter::failRequest(int id, const std::string &reason)
{
    std::lock_guard lock(mutex_);
    const auto it = pending_.find(id);
    if (it == pending_.end()) return;
    it->second->failure = reason;
    it->second->complete = true;
    it->second->ready.notify_all();
}

void CdpMessageRouter::failAll(const std::string &reason)
{
    std::lock_guard lock(mutex_);
    for (auto &[id, pending] : pending_)
    {
        (void)id;
        pending->failure = reason;
        pending->complete = true;
        pending->ready.notify_all();
    }
}

std::size_t CdpMessageRouter::malformedMessages() const
{
    std::lock_guard lock(mutex_);
    return malformed_;
}

class CdpClient::Impl {
  public:
    struct CallbackState {
        explicit CallbackState(std::shared_ptr<CdpMessageRouter> messageRouter)
            : router(std::move(messageRouter))
        {
        }

        std::shared_ptr<CdpMessageRouter> router;
        std::mutex mutex;
        std::condition_variable changed;
        std::uint64_t generation{0};
        bool connecting{false};
        bool connected{false};
        bool shuttingDown{false};
    };

    explicit Impl(std::shared_ptr<CdpMessageRouter> router)
        : state_(std::make_shared<CallbackState>(std::move(router)))
    {
    }
    ~Impl() { shutdown(); }

    void connect(const std::string &url, std::chrono::milliseconds timeout,
                 const StopPredicate &stopRequested)
    {
        throwIfStopRequested(stopRequested);
        stop();
        const WebSocketParts parts = splitWebSocketUrl(url);
        const auto state = state_;
        if (!state) throw std::runtime_error("browser_unavailable");
        std::uint64_t generation = 0;
        {
            std::lock_guard lock(state->mutex);
            if (state->shuttingDown) throw std::runtime_error("browser_unavailable");
            generation = ++state->generation;
            state->connecting = true;
            state->connected = false;
        }
        // Resolve the internal service name and connect using its network IP.
        // Chromium's DevTools server permits IP Host headers but rejects DNS
        // names as a rebinding defense. The address is still confined to the
        // Compose bridge and CDP has no published host port.
        const std::string resolvedHost = resolveHost(parts.host);
        if (stopWasRequested(stopRequested))
        {
            stop();
            throw std::runtime_error("cdp_cancelled");
        }
        auto client = drogon::WebSocketClient::newWebSocketClient(
            resolvedHost, parts.port, parts.secure, cdpEventLoop());
        const std::weak_ptr<CallbackState> weakState = state;
        client->setMessageHandler([weakState, generation](std::string &&message,
                                                          const drogon::WebSocketClientPtr &,
                                                          const drogon::WebSocketMessageType &type) {
            const auto locked = weakState.lock();
            if (!locked || type != drogon::WebSocketMessageType::Text) return;
            std::shared_ptr<CdpMessageRouter> router;
            {
                std::lock_guard guard(locked->mutex);
                if (locked->shuttingDown || locked->generation != generation) return;
                router = locked->router;
            }
            router->ingest(message);
        });
        client->setConnectionClosedHandler([weakState, generation](const drogon::WebSocketClientPtr &) {
            const auto locked = weakState.lock();
            if (!locked) return;
            std::shared_ptr<CdpMessageRouter> router;
            {
                std::lock_guard guard(locked->mutex);
                if (locked->shuttingDown || locked->generation != generation) return;
                locked->connected = false;
                locked->connecting = false;
                router = locked->router;
            }
            router->failAll("cdp_disconnected");
            locked->changed.notify_all();
        });
        auto req = drogon::HttpRequest::newHttpRequest();
        req->setPath(parts.path);
        req->addHeader("Origin", "http://argus-chromium");
        {
            std::lock_guard lock(state->mutex);
            if (state->shuttingDown || state->generation != generation)
            {
                stopAndReleaseOnLoop(std::move(client));
                throw std::runtime_error("browser_unavailable");
            }
            client_ = client;
        }
        client->connectToServer(req, [weakState, generation](drogon::ReqResult result,
                                                             const drogon::HttpResponsePtr &response,
                                                             const drogon::WebSocketClientPtr &connectedClient) {
            const auto connection = connectedClient ? connectedClient->getConnection() : nullptr;
            const bool established = result == drogon::ReqResult::Ok && connection &&
                                     connection->connected();
            const auto locked = weakState.lock();
            if (!locked) return;
            bool active = false;
            {
                std::lock_guard guard(locked->mutex);
                if (!locked->shuttingDown && locked->generation == generation)
                {
                    locked->connecting = false;
                    locked->connected = established;
                    active = true;
                }
            }
            if (active && !established)
                std::cerr << "{\"component\":\"cdp\",\"event\":\"connect_failed\",\"result\":"
                          << static_cast<int>(result) << ",\"http_status\":"
                          << (response ? static_cast<int>(response->statusCode()) : 0) << "}\n";
            if (active) locked->changed.notify_all();
        });
        const auto deadline = std::chrono::steady_clock::now() + timeout;
        std::unique_lock lock(state->mutex);
        while (state->generation == generation && state->connecting)
        {
            lock.unlock();
            const bool cancelled = stopWasRequested(stopRequested);
            lock.lock();
            if (cancelled)
            {
                lock.unlock();
                stop();
                throw std::runtime_error("cdp_cancelled");
            }
            const auto now = std::chrono::steady_clock::now();
            if (now >= deadline) break;
            state->changed.wait_until(
                lock, std::min(deadline, now + std::chrono::milliseconds(50)));
        }
        if (state->generation != generation || !state->connected)
        {
            lock.unlock();
            const bool cancelled = stopWasRequested(stopRequested);
            stop();
            throw std::runtime_error(cancelled ? "cdp_cancelled" : "browser_unavailable");
        }
        lock.unlock();
        if (stopWasRequested(stopRequested))
        {
            stop();
            throw std::runtime_error("cdp_cancelled");
        }
    }

    bool connected() const
    {
        const auto state = state_;
        if (!state) return false;
        std::lock_guard lock(state->mutex);
        const auto connection = client_ ? client_->getConnection() : nullptr;
        return !state->shuttingDown && state->connected && connection && connection->connected();
    }

    void send(const std::string &message)
    {
        const auto state = state_;
        if (!state) throw std::runtime_error("browser_unavailable");
        std::lock_guard lock(state->mutex);
        const auto connection = client_ ? client_->getConnection() : nullptr;
        if (state->shuttingDown || !state->connected || !connection)
            throw std::runtime_error("browser_unavailable");
        connection->send(message);
    }

    void stop()
    {
        stopInternal(false);
    }

    void shutdown()
    {
        stopInternal(true);
    }

  private:
    void stopInternal(bool shuttingDown)
    {
        const auto state = state_;
        if (!state) return;
        drogon::WebSocketClientPtr client;
        {
            std::lock_guard lock(state->mutex);
            if (shuttingDown) state->shuttingDown = true;
            ++state->generation;
            client = std::move(client_);
            state->connected = false;
            state->connecting = false;
        }
        stopAndReleaseOnLoop(std::move(client));
        state->router->failAll("cdp_disconnected");
        state->changed.notify_all();
        if (shuttingDown) state_.reset();
    }

    std::shared_ptr<CallbackState> state_;
    drogon::WebSocketClientPtr client_;
};

CdpClient::CdpClient(std::string httpEndpoint)
    : httpEndpoint_(std::move(httpEndpoint)),
      router_(std::make_shared<CdpMessageRouter>()),
      impl_(std::make_unique<Impl>(router_))
{
    while (!httpEndpoint_.empty() && httpEndpoint_.back() == '/') httpEndpoint_.pop_back();
}

CdpClient::~CdpClient()
{
    if (impl_) impl_->shutdown();
}

Json::Value CdpClient::version(std::chrono::milliseconds timeout,
                               const StopPredicate &stopRequested) const
{
    const HttpResult response = request(
        httpEndpoint_ + "/json/version", "GET", timeout, stopRequested);
    if (response.status != 200) throw std::runtime_error("browser_unavailable");
    return parseJson(response.body);
}

Json::Value CdpClient::targets(std::chrono::milliseconds timeout,
                               const StopPredicate &stopRequested) const
{
    const HttpResult response = request(
        httpEndpoint_ + "/json/list", "GET", timeout, stopRequested);
    if (response.status != 200) throw std::runtime_error("browser_unavailable");
    return parseJson(response.body);
}

Json::Value CdpClient::createTargetViaHttp(const std::string &url,
                                           std::chrono::milliseconds timeout) const
{
    const HttpResult response = request(httpEndpoint_ + "/json/new?" + escapedUrl(url), "PUT", timeout);
    if (response.status < 200 || response.status >= 300) throw std::runtime_error("browser_unavailable");
    return parseJson(response.body);
}

bool CdpClient::closeTargetViaHttp(const std::string &targetId,
                                   std::chrono::milliseconds timeout) const
{
    const HttpResult response = request(httpEndpoint_ + "/json/close/" + escapedUrl(targetId), "GET", timeout);
    return response.status >= 200 && response.status < 300;
}

void CdpClient::connectBrowser(std::chrono::milliseconds timeout,
                               const StopPredicate &stopRequested)
{
    const std::string url = version(timeout, stopRequested)
                                .get("webSocketDebuggerUrl", "").asString();
    if (url.empty()) throw std::runtime_error("browser_unavailable");
    throwIfStopRequested(stopRequested);
    connectWebSocket(rewriteWebSocketEndpoint(url, httpEndpoint_), timeout, stopRequested);
}

void CdpClient::connectTarget(const std::string &targetId, std::chrono::milliseconds timeout,
                              const StopPredicate &stopRequested)
{
    const Json::Value list = targets(timeout, stopRequested);
    for (const auto &target : list)
    {
        throwIfStopRequested(stopRequested);
        if (target.get("id", "").asString() != targetId) continue;
        const std::string url = target.get("webSocketDebuggerUrl", "").asString();
        if (url.empty()) break;
        connectWebSocket(rewriteWebSocketEndpoint(url, httpEndpoint_), timeout, stopRequested);
        return;
    }
    throw std::runtime_error("browser_unavailable");
}

void CdpClient::connectWebSocket(const std::string &webSocketUrl,
                                 std::chrono::milliseconds timeout,
                                 const StopPredicate &stopRequested)
{
    webSocketUrl_ = webSocketUrl;
    impl_->connect(webSocketUrl_, timeout, stopRequested);
}

void CdpClient::reconnect(std::chrono::milliseconds timeout,
                          const StopPredicate &stopRequested)
{
    if (webSocketUrl_.empty()) throw std::runtime_error("browser_unavailable");
    impl_->connect(webSocketUrl_, timeout, stopRequested);
}

bool CdpClient::connected() const { return impl_->connected(); }

void CdpClient::stop()
{
    if (impl_) impl_->stop();
}

Json::Value CdpClient::command(const std::string &method, const Json::Value &params,
                               std::chrono::milliseconds timeout,
                               const StopPredicate &stopRequested)
{
    throwIfStopRequested(stopRequested);
    if (!connected())
        reconnect(std::min(timeout, std::chrono::milliseconds(5000)), stopRequested);
    throwIfStopRequested(stopRequested);
    const int id = router_->beginRequest();
    Json::Value message;
    message["id"] = id;
    message["method"] = method;
    message["params"] = params;
    try
    {
        impl_->send(jsonText(message));
    }
    catch (...)
    {
        router_->failRequest(id, "browser_unavailable");
        throw;
    }
    return router_->waitForResponse(id, timeout, stopRequested);
}

Json::Value CdpClient::waitForEvent(const std::string &method,
                                    std::chrono::milliseconds timeout,
                                    const StopPredicate &stopRequested)
{
    return router_->waitForEvent(method, timeout, stopRequested);
}

std::vector<Json::Value> CdpClient::takeEvents(const std::string &method, std::size_t maximum)
{
    return router_->takeEvents(method, maximum);
}

}  // namespace argus::browser
