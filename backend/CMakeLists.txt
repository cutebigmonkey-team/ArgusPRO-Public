cmake_minimum_required(VERSION 3.21)
project(ArgusProBackend CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)

find_package(Drogon CONFIG REQUIRED)
find_package(CURL REQUIRED)

add_executable(argus_backend
    src/Database.cpp
    src/main.cpp
    src/TelevisionController.cpp
)

target_link_libraries(argus_backend PRIVATE Drogon::Drogon CURL::libcurl)

target_compile_definitions(argus_backend PRIVATE
    ARGUS_WEB_ROOT="${CMAKE_CURRENT_SOURCE_DIR}/../frontend/web"
    ARGUS_FIXTURES_ROOT="${CMAKE_CURRENT_SOURCE_DIR}/../fixtures"
)

add_library(argus_pipeline_common STATIC src/PipelineDb.cpp)
target_link_libraries(argus_pipeline_common PUBLIC Drogon::Drogon)
target_include_directories(argus_pipeline_common PUBLIC src)

add_executable(argus_scheduler src/Scheduler.cpp)
target_link_libraries(argus_scheduler PRIVATE argus_pipeline_common)

add_executable(argus_normalizer src/Normalizer.cpp)
target_link_libraries(argus_normalizer PRIVATE argus_pipeline_common)

add_library(argus_rss_normalize STATIC src/RssNormalize.cpp)
target_link_libraries(argus_rss_normalize PUBLIC CURL::libcurl)
target_include_directories(argus_rss_normalize PUBLIC src)

add_executable(argus_collector_rss src/RssCollector.cpp)
target_link_libraries(argus_collector_rss PRIVATE argus_pipeline_common argus_rss_normalize CURL::libcurl)

add_library(argus_platform_adapters STATIC src/PlatformAdapters.cpp)
target_link_libraries(argus_platform_adapters PUBLIC
    argus_pipeline_common
    argus_rss_normalize
    CURL::libcurl
)
target_include_directories(argus_platform_adapters PUBLIC src)

# One hardened C++20 runtime is shared by the native HTTP/API platforms. The
# Compose platform filter gives each platform its own isolated process/service.
add_executable(argus_collector_native src/NativeCollector.cpp)
target_link_libraries(argus_collector_native PRIVATE argus_platform_adapters)

function(add_native_collector target platform)
    add_executable(${target} src/NativeCollector.cpp)
    target_compile_definitions(${target} PRIVATE ARGUS_COMPILED_PLATFORM="${platform}")
    target_link_libraries(${target} PRIVATE argus_platform_adapters)
endfunction()

add_native_collector(argus_collector_telegram telegram)
add_native_collector(argus_collector_youtube youtube)
add_native_collector(argus_collector_reddit reddit)
add_native_collector(argus_collector_gdelt gdelt)
add_native_collector(argus_collector_dsns_http dsns-http)

add_library(argus_cdp STATIC
    src/browser/CdpClient.cpp
    src/browser/BrowserSession.cpp
    src/browser/BrowserPool.cpp
)
target_include_directories(argus_cdp PUBLIC src src/browser)
target_link_libraries(argus_cdp PUBLIC Drogon::Drogon CURL::libcurl)

add_library(argus_browser_platforms STATIC
    src/platforms/PlatformAdapter.cpp
    src/platforms/FacebookAdapter.cpp
    src/platforms/InstagramAdapter.cpp
    src/platforms/XAdapter.cpp
    src/platforms/ThreadsAdapter.cpp
    src/platforms/TikTokAdapter.cpp
    src/platforms/VkAdapter.cpp
    src/platforms/OkAdapter.cpp
    src/platforms/MaxAdapter.cpp
    src/platforms/DsnsBrowserAdapter.cpp
)
target_include_directories(argus_browser_platforms PUBLIC src src/platforms)
target_link_libraries(argus_browser_platforms PUBLIC Drogon::Drogon CURL::libcurl)

add_executable(argus_collector_browser src/BrowserCollector.cpp)
target_link_libraries(argus_collector_browser PRIVATE
    argus_pipeline_common
    argus_cdp
    argus_browser_platforms
)

# Read-only diagnostic exercising RSS canonicalization edge cases
# (docs/rss-shadow-validation.md, "Canonical URL"). Not part of the runtime
# collector; never linked into argus_collector_rss.
add_executable(argus_rss_normalize_tests src/RssNormalizeTests.cpp)
target_link_libraries(argus_rss_normalize_tests PRIVATE argus_rss_normalize)

add_executable(argus_platform_adapter_tests src/PlatformAdaptersTests.cpp)
target_link_libraries(argus_platform_adapter_tests PRIVATE argus_platform_adapters)

add_executable(argus_cdp_tests src/browser/CdpClientTests.cpp)
target_link_libraries(argus_cdp_tests PRIVATE argus_cdp)

add_executable(argus_threads_circuit_breaker_tests src/browser/ThreadsCircuitBreakerTests.cpp)
target_include_directories(argus_threads_circuit_breaker_tests PRIVATE src/browser)

# Optional live smoke test. It is deliberately not registered with ctest
# because it needs the internal argus-chromium service.
add_executable(argus_cdp_live_smoke src/browser/CdpLiveSmoke.cpp)
target_link_libraries(argus_cdp_live_smoke PRIVATE argus_cdp argus_browser_platforms)

add_executable(argus_browser_platform_tests src/platforms/PlatformAdapterTests.cpp)
target_link_libraries(argus_browser_platform_tests PRIVATE argus_browser_platforms)

add_executable(argus_pipeline_shutdown_tests src/PipelineShutdownTests.cpp)
target_link_libraries(argus_pipeline_shutdown_tests PRIVATE argus_pipeline_common)

enable_testing()
add_test(NAME rss_normalize COMMAND argus_rss_normalize_tests)
add_test(NAME platform_adapters COMMAND argus_platform_adapter_tests)
add_test(NAME cdp_client COMMAND argus_cdp_tests)
add_test(NAME threads_circuit_breaker COMMAND argus_threads_circuit_breaker_tests)
add_test(NAME browser_platforms COMMAND argus_browser_platform_tests)
add_test(NAME pipeline_shutdown COMMAND argus_pipeline_shutdown_tests)
