#!/usr/bin/env bash
# Hostless-only disposable validation. No cloud credentials/push/deployment.
set -euo pipefail
cd "$(dirname "$0")/../.."
ARGUS_CONTAINER_TOOL=${ARGUS_CONTAINER_TOOL:-podman}
case "$ARGUS_CONTAINER_TOOL" in docker|podman) ;; *) exit 2 ;; esac
export ARGUS_CONTAINER_TOOL
"$ARGUS_CONTAINER_TOOL" info >/dev/null
git diff --quiet
git diff --cached --quiet
evidence_root=$(mktemp -d "${TMPDIR:-/tmp}/argus-hostless-minimal-validation.XXXXXX")
printf 'Hostless-only evidence: %s\n' "$evidence_root"
git rev-parse HEAD > "$evidence_root/source-commit.txt"
printf '%s\n' '{"complete":false,"production_modified":false}' > "$evidence_root/status.json"
for tag in argus-oom-validation:local argus-hostless-minimal:local; do
  "$ARGUS_CONTAINER_TOOL" image inspect --format '{{.Id}}' "$tag" 2>/dev/null || true
done > "$evidence_root/prior-image-ids.txt"
inventory() {
  for tag in argus-oom-validation:local argus-hostless-minimal:local; do
    if image_id=$("$ARGUS_CONTAINER_TOOL" image inspect --format '{{.Id}}' "$tag" 2>/dev/null); then
      printf '%s %s\n' "$tag" "$image_id"
    fi
  done > "$evidence_root/result-image-ids.txt"
}
trap inventory EXIT
sh ops/hostless/validate_oom_container.sh 2>&1 | tee "$evidence_root/component-768m.log"
bash ops/hostless/validate_archive_scale.sh 2>&1 | tee "$evidence_root/archive-scale.log"
# Build the exact final target locally, including all required UI/backend gates.
# No registry push and no activation of this image are performed.
"$ARGUS_CONTAINER_TOOL" build -f ops/hostless/Dockerfile --target production \
  -t argus-hostless-minimal:local . 2>&1 | tee "$evidence_root/final-image-build.log"
printf '%s\n' '{"complete":true,"production_modified":false,"scope":"local fixtures and final-image build; live acceptance still required"}' > "$evidence_root/status.json"
printf 'Hostless-only gates completed: %s\n' "$evidence_root"
