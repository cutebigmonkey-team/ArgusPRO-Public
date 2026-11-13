# syntax=docker/dockerfile:1
# Backend and collectors only. The protected local archive uses ops/local-storage.
FROM debian:trixie-slim AS backend-builder

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      cmake ninja-build g++ pkg-config \
      libcurl4-openssl-dev libdrogon-dev libhiredis-dev libjsoncpp-dev \
      libmariadb-dev libpq-dev libsqlite3-dev libyaml-cpp-dev uuid-dev \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app/backend
COPY backend/CMakeLists.txt ./CMakeLists.txt
COPY backend/src ./src

# CMAKE_CURRENT_SOURCE_DIR here is /app/backend, so the ARGUS_WEB_ROOT /
# ARGUS_FIXTURES_ROOT compile definitions bake in as
# "/app/backend/../frontend/web" and "/app/backend/../fixtures" — resolved
# against whatever the runtime stage places at /app/frontend/web and
# /app/fixtures (see below). No fixtures/frontend files are needed at build
# time — those macros are just path strings until the server actually opens
# a file at runtime.
RUN cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release \
 && cmake --build build

########################################################################
# Stage 3 — runtime (no compiler, no Emscripten, no source)
########################################################################
FROM debian:trixie-slim AS runtime

# libdrogon1t64 is the runtime-only shared library package (pulls in its own
# transitive runtime deps — jsoncpp, uuid, zlib, etc. — via apt, no -dev
# packages). curl is only for the HEALTHCHECK below.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libdrogon1t64 curl ca-certificates \
 && rm -rf /var/lib/apt/lists/* \
 && groupadd --gid 1000 argus \
 && useradd --uid 1000 --gid argus --home-dir /app --shell /usr/sbin/nologin argus

WORKDIR /app

# Mirrors the repo's own top-level layout (backend/, frontend/web/,
# fixtures/) exactly, so the path baked into the binary at build time
# ("/app/backend/../frontend/web", "/app/backend/../fixtures") resolves.
COPY --from=backend-builder  /app/backend/build/argus_backend  /app/backend/argus_backend
COPY --from=backend-builder  /app/backend/build/argus_scheduler /app/backend/argus_scheduler
COPY --from=backend-builder  /app/backend/build/argus_normalizer /app/backend/argus_normalizer
COPY --from=backend-builder  /app/backend/build/argus_collector_rss /app/backend/argus_collector_rss
COPY --from=backend-builder  /app/backend/build/argus_collector_native /app/backend/argus_collector_native
COPY --from=backend-builder  /app/backend/build/argus_collector_telegram /app/backend/argus_collector_telegram
COPY --from=backend-builder  /app/backend/build/argus_collector_youtube /app/backend/argus_collector_youtube
COPY --from=backend-builder  /app/backend/build/argus_collector_reddit /app/backend/argus_collector_reddit
COPY --from=backend-builder  /app/backend/build/argus_collector_gdelt /app/backend/argus_collector_gdelt
COPY --from=backend-builder  /app/backend/build/argus_collector_dsns_http /app/backend/argus_collector_dsns_http
COPY --from=backend-builder  /app/backend/build/argus_collector_browser /app/backend/argus_collector_browser
# Mount a separately built frontend at /app/frontend/web if using the legacy UI server.
RUN mkdir -p /app/frontend/web /app/fixtures

RUN chown -R argus:argus /app
USER argus

# Container-only defaults — the native binary (no env vars set) still
# defaults to 127.0.0.1:8090 in code, per the task's requirement #6.
ENV ARGUS_BIND_ADDRESS=0.0.0.0 \
    ARGUS_PORT=8090

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${ARGUS_PORT}/health" || exit 1

ENTRYPOINT ["/app/backend/argus_backend"]
