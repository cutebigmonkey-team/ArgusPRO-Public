#include "TelevisionController.h"
#include "Database.h"

#include <drogon/drogon.h>
#include <drogon/HttpAppFramework.h>
#include <curl/curl.h>
#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <string>

using namespace drogon;

namespace {

// ARGUS_WEB_ROOT / ARGUS_FIXTURES_ROOT are injected by CMake as absolute paths
// to frontend/web and fixtures/ so the server works regardless of the current
// working directory it happens to be launched from.
constexpr const char *kWebRoot = ARGUS_WEB_ROOT;
constexpr const char *kFixturesRoot = ARGUS_FIXTURES_ROOT;

std::string readFile(const std::filesystem::path &path)
{
    std::ifstream file(path, std::ios::binary);
    std::ostringstream contents;
    contents << file.rdbuf();
    return contents.str();
}

// ARGUS_BIND_ADDRESS / ARGUS_PORT let the container override the listen
// address/port (0.0.0.0:8090 inside Docker) without touching the native
// default: with neither set, this still resolves to 127.0.0.1:8090 exactly
// as before.
std::string envOr(const char *name, std::string fallback)
{
    const char *value = std::getenv(name);
    return (value != nullptr && *value != '\0') ? std::string(value) : fallback;
}

}  // namespace

int main()
{
    // libcurl is used synchronously by TelevisionController for outbound
    // HTTPS GETs to YouTube's public per-channel feed — global init must
    // happen once, before any thread (Drogon's IO loops) touches curl.
    curl_global_init(CURL_GLOBAL_DEFAULT);

    // /health — fixed shape required by the ArgusPRO Phase-1 spec.
    app().registerHandler(
        "/health",
        [](const HttpRequestPtr &, std::function<void(const HttpResponsePtr &)> &&callback) {
            Json::Value json;
            json["status"] = "ok";
            json["application"] = "ArgusPro";
            callback(HttpResponse::newHttpJsonResponse(json));
        },
        {Get});

    // Local JSON fixtures — served verbatim, no query/filter logic. The
    // frontend fetches e.g. /fixtures/posts.json directly.
    app().registerHandler(
        "/fixtures/{filename}",
        [](const HttpRequestPtr &, std::function<void(const HttpResponsePtr &)> &&callback,
           const std::string &filename) {
            const std::filesystem::path requested = std::filesystem::path(kFixturesRoot) / filename;
            std::error_code ec;
            const auto canonicalFixtures = std::filesystem::weakly_canonical(kFixturesRoot, ec);
            const auto canonicalRequested = std::filesystem::weakly_canonical(requested, ec);
            const bool withinFixturesDir =
                !ec && canonicalRequested.native().starts_with(canonicalFixtures.native());

            if (!withinFixturesDir || !std::filesystem::is_regular_file(canonicalRequested))
            {
                callback(HttpResponse::newNotFoundResponse());
                return;
            }
            auto resp = HttpResponse::newHttpResponse();
            resp->setBody(readFile(canonicalRequested));
            resp->setContentTypeCode(CT_APPLICATION_JSON);
            callback(resp);
        },
        {Get});

    // /api/tv/channels + /api/tv/videos — real backend contract for the
    // Television tab (see backend/src/TelevisionController.cpp for why:
    // proxies YouTube's own public per-channel feed instead of a fixture).
    argus::backend::television::registerRoutes();
    argus::backend::database::configure();

    // Static assets (styles.css, the emcc-built app.js/app.wasm, icons,
    // vendor JS) served directly from the document root by Drogon. Drogon's
    // static handler only serves extensions on an allowlist (defaults don't
    // include "wasm" — an unlisted extension silently falls through to the
    // SPA-fallback 404 page instead of an error, which looks like a working
    // 200 response with the wrong body), so it must be set explicitly here.
    app().setFileTypes({"html", "css", "js", "mjs", "wasm", "svg", "png", "jpg", "jpeg", "gif",
                         "ico", "json", "map", "woff", "woff2"});
    app().setDocumentRoot(kWebRoot);
    app().setStaticFilesCacheTime(0);  // local dev — always reflect the latest build
    app().setHomePage("index.html");   // "/" serves frontend/web/index.html

    // SPA fallback: any GET that isn't a real static file or a registered
    // route above (e.g. a client-side path like /dsns typed directly into
    // the address bar, or reloaded) falls through here and gets the same
    // index.html body (status 200, not a real 404) so the C++ router takes
    // over from there.
    const auto indexPath = std::filesystem::path(kWebRoot) / "index.html";
    if (std::filesystem::is_regular_file(indexPath))
    {
        auto spaFallback = HttpResponse::newHttpResponse();
        spaFallback->setBody(readFile(indexPath));
        spaFallback->setContentTypeCode(CT_TEXT_HTML);
        spaFallback->setStatusCode(k200OK);
        app().setCustom404Page(spaFallback, /*set404=*/false);
    }
    else
    {
        LOG_WARN << "frontend/web/index.html not found yet — build the frontend first. "
                    "SPA fallback will return a plain 404 until then.";
    }

    // NOTE: port 8080 is already bound by the reference "Argus Dashboard" project's
    // own local stack (Docker) on this machine, so ArgusPRO uses 8090 instead to
    // avoid any interference with it. Native runs stay localhost-only by default;
    // the container overrides both via env vars (see docs/docker.md).
    const std::string bindAddress = envOr("ARGUS_BIND_ADDRESS", "127.0.0.1");
    const std::string portStr = envOr("ARGUS_PORT", "8090");
    unsigned short port = 8090;
    try
    {
        const int parsed = std::stoi(portStr);
        if (parsed < 1 || parsed > 65535) throw std::out_of_range("port out of range");
        port = static_cast<unsigned short>(parsed);
    }
    catch (const std::exception &e)
    {
        LOG_WARN << "Invalid ARGUS_PORT=\"" << portStr << "\" (" << e.what() << ") — falling back to 8090";
        port = 8090;
    }

    LOG_INFO << "ArgusPRO backend starting on http://" << bindAddress << ":" << port << " (mode: local-ui)";
    LOG_INFO << "Serving static files from: " << kWebRoot;
    LOG_INFO << "Serving fixtures from: " << kFixturesRoot;

    app().addListener(bindAddress, port);
    app().setThreadNum(2);
    app().run();
    curl_global_cleanup();
    return 0;
}
