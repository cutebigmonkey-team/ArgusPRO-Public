#!/usr/bin/env bash
# Isolated current-commit full-path checks. No production credentials or storage.
set -euo pipefail
cd "$(dirname "$0")/../.."
container_tool=${ARGUS_CONTAINER_TOOL:-podman}
case "$container_tool" in docker|podman) ;; *) exit 2 ;; esac
"$container_tool" info >/dev/null
git diff --quiet
git diff --cached --quiet
source_commit=$(git rev-parse HEAD)
# Created by validate_oom_container.sh. Bind to its immutable identity, not a
# moving tag. New source is exported from Git and mounted read-only below.
image_id=$("$container_tool" image inspect --format '{{.Id}}' argus-oom-validation:local)
evidence_root=$(mktemp -d "${TMPDIR:-/tmp}/argus-archive-scale-validation.XXXXXX")
mkdir "$evidence_root/source"
git archive "$source_commit" ops backend/src/platforms | tar -xf - -C "$evidence_root/source"
printf '%s\n' "$source_commit" > "$evidence_root/source-commit.txt"
printf '%s\n' "$image_id" > "$evidence_root/image-id.txt"
printf 'Archive-scale evidence directory: %s\n' "$evidence_root"
run_id="$(date -u +%Y%m%d%H%M%S)-$$"
for scenario in content publication; do
  container_name="argus-scale-${run_id}-${scenario}"
  text_repeats=5;article_repeats=400
  if [ "$scenario" = publication ]; then text_repeats=1;article_repeats=1100; fi
  printf '%s\n' "$container_name" >> "$evidence_root/container-names.txt"
  code=0
  "$container_tool" run --name "$container_name" --label argus.validation=archive-scale \
    --network=none --memory=768m --memory-swap=768m --cpus=0.25 \
    --security-opt label=disable --user root \
    --env "ARGUS_TEST_TEXT_REPEATS=$text_repeats" --env "ARGUS_TEST_ARTICLE_REPEATS=$article_repeats" \
    --volume "$evidence_root/source/ops:/app/ops:ro" \
    --volume "$evidence_root/source/backend:/app/backend:ro" \
    --volume "$evidence_root/source/ops/validation/archive-scale-recovery:/validation:ro" \
    --entrypoint sh "$image_id" -c 'set -eu; test "$(cat /sys/fs/cgroup/cpu.max)" = "25000 100000"; exec sh /validation/runner.sh' \
    2>&1 | tee "$evidence_root/$scenario.log" || code=$?
  "$container_tool" inspect --format '{{json .State}}' "$container_name" > "$evidence_root/$scenario-state.json"
  if [ "$code" -ne 0 ]; then
    "$container_tool" cp "$container_name:/tmp/argus-full-cycle/evidence.jsonl" "$evidence_root/$scenario-evidence.jsonl" || true
    exit "$code"
  fi
  "$container_tool" cp "$container_name:/tmp/argus-full-cycle/evidence.jsonl" "$evidence_root/$scenario-evidence.jsonl"
  "$container_tool" cp "$container_name:/tmp/argus-full-cycle/collector.log" "$evidence_root/$scenario-collector.log"
done
printf 'Both current-commit archive-scale cases passed. Retained evidence: %s\n' "$evidence_root"
# Containers are stopped and retained for evidence/scoped cleanup. No prune or
# deletion. PostgreSQL stops on runner exit; no local scheduler remains running.
