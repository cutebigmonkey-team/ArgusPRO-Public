#!/bin/sh
set -eu
# Build-only, isolated Unix socket; no network listener or production credentials.
pg_bin="$(pg_config --bindir)"
pg_test_root="$(mktemp -d /tmp/argus-comments-check.XXXXXX)"
chown postgres:postgres "$pg_test_root"
trap 'runuser -u postgres -- "$pg_bin/pg_ctl" -D "$pg_test_root/data" -m immediate stop >/dev/null 2>&1 || true' EXIT
runuser -u postgres -- "$pg_bin/initdb" -D "$pg_test_root/data" -A trust --no-locale --encoding=UTF8 >/dev/null
runuser -u postgres -- "$pg_bin/pg_ctl" -D "$pg_test_root/data" -o "-k $pg_test_root -h '' -F" -w start >/dev/null
runuser -u postgres -- "$pg_bin/createdb" -h "$pg_test_root" argus_comments_test
export ARGUS_COMMENTS_TEST_DSN="postgresql:///argus_comments_test?user=postgres&host=$pg_test_root"
export PYTHONPATH=/app/ops/collect:/app/ops/drive:/app/ops/local-storage:/app/ops/hostless:/app/ops/ai
cd /app
python ops/hostless/release_tests.py
if [ "${ARGUS_RUN_ARCHIVE_BENCHMARK:-0}" = 1 ]; then
  python ops/hostless/benchmark_continuous.py --browser --output /evidence/archive-benchmark.json
fi
python -c 'import json,datetime; print(json.dumps({"status":"passed","postgresql":"disposable Unix-socket database","at":datetime.datetime.now(datetime.timezone.utc).isoformat()}))' > /comments-backend-verified.json
echo 'ARGUS COMMENTS BACKEND BUILD CHECKS PASSED'
