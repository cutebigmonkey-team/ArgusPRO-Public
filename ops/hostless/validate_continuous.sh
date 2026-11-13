#!/usr/bin/env bash
# Local disposable validation only. No push, deploy, cloud variables or archive mounts.
set -euo pipefail
cd "$(dirname "$0")/../.."
tool=${ARGUS_CONTAINER_TOOL:-podman}
case "$tool" in docker|podman) ;; *) exit 2 ;; esac
evidence=$(mktemp -d "${TMPDIR:-/tmp}/argus-continuous-validation.XXXXXX")
printf '%s\n' '{"complete":false,"production_modified":false}' > "$evidence/status.json"
git diff --quiet
git diff --cached --quiet
test -z "$(git ls-files --others --exclude-standard)"
revision=$(git rev-parse HEAD)
printf '%s\n' "$revision" > "$evidence/source-sha.txt"
"$tool" info > "$evidence/container-info.txt"
frontend=${ARGUS_FRONTEND_WORKTREE:?Set the reviewed clean frontend worktree path}
git -C "$frontend" diff --quiet
git -C "$frontend" diff --cached --quiet
test -z "$(git -C "$frontend" ls-files --others --exclude-standard)"
git -C "$frontend" rev-parse HEAD > "$evidence/frontend-sha.txt"
for test_file in "$frontend"/frontend/cloud/*.test.mjs; do node "$test_file"; done > "$evidence/frontend-tests.log" 2>&1
command -v emcmake >/dev/null
emcmake cmake -S "$frontend/frontend" -B "$evidence/frontend-build" -DCMAKE_BUILD_TYPE=Release -DARGUS_CLOUD_APP=ON
cmake --build "$evidence/frontend-build" --parallel 2 > "$evidence/frontend-build.log" 2>&1
"$tool" build --build-arg "ARGUS_SOURCE_SHA=$revision" -f ops/hostless/Dockerfile \
  --target comments-backend-check -t argus-continuous-check:local . 2>&1 | tee "$evidence/build.log"
"$tool" run --rm --network none --memory 768m --memory-swap 768m --cpus .25 \
  -e ARGUS_RUN_ARCHIVE_BENCHMARK=1 -v "$evidence:/evidence:Z" \
  --entrypoint sh argus-continuous-check:local /app/ops/hostless/check_comments_build.sh \
  2>&1 | tee "$evidence/runtime.log"
"$tool" build --build-arg "ARGUS_SOURCE_SHA=$revision" -f ops/hostless/Dockerfile \
  --target production -t argus-continuous:local . 2>&1 | tee "$evidence/production-build.log"
"$tool" image inspect argus-continuous:local > "$evidence/image.json"
printf '%s\n' '{"complete":true,"production_modified":false,"scope":"local 768 MiB/0.25 CPU fixtures; actual Hostless limits and live acceptance still required"}' > "$evidence/status.json"
printf 'Evidence: %s\n' "$evidence"
