#!/bin/sh
# Local/disposable validation only. Never pushes or deploys an image, passes
# production credentials, or starts a GitHub Actions collector.
set -eu
container_tool=${ARGUS_CONTAINER_TOOL:-docker}
case "$container_tool" in docker|podman) ;; *) echo 'Use docker or podman' >&2; exit 2 ;; esac
cd "$(dirname "$0")/../.."
# This existing target installs Python 3.12, Chromium, psycopg and disposable
# PostgreSQL, and runs backend checks. The upstream social-probe stages are not
# dependencies of this target. No registry export/push occurs.
"$container_tool" build -f ops/hostless/Dockerfile --target comments-backend-check -t argus-oom-validation:local .
"$container_tool" run --rm --network=none --memory=768m --memory-swap=768m --cpus=0.25 \
  --user root --entrypoint sh argus-oom-validation:local -c '
    set -eu
    test "$(cat /sys/fs/cgroup/memory.max)" = 805306368
    test "$(cat /sys/fs/cgroup/memory.swap.max)" = 0
    sh /app/ops/hostless/check_comments_build.sh
    export PYTHONPATH=/app/ops/hostless:/app/ops/drive:/app/ops/collect:/app/ops/local-storage
    python -m unittest test_schedule_store test_feed_journal test_analytics_snapshot test_publish
  '
"$container_tool" run --rm -i --network=none --memory=768m --memory-swap=768m --cpus=0.25 \
  --user collector --entrypoint python argus-oom-validation:local -u - <<'PY'
import asyncio,json,subprocess,sys
from pathlib import Path
from playwright.async_api import async_playwright

async def main():
    root=Path('/sys/fs/cgroup')
    assert int((root/'memory.max').read_text())==768*1024**2
    async with async_playwright() as driver:
        browser=await driver.chromium.launch(headless=True)
        try:
            # Exercise context cleanup, then retain one real browser/context
            # while the archive-sized restart workload runs in a child.
            for _ in range(3):
                context=await browser.new_context()
                try:
                    page=await context.new_page()
                    await page.set_content('<article>ARGUS offline validation</article>')
                    assert await page.locator('article').inner_text()=='ARGUS offline validation'
                finally:await context.close()
            context=await browser.new_context()
            try:
                page=await context.new_page();await page.set_content('<article>Memory workload</article>')
                subprocess.run([sys.executable,'/app/ops/hostless/profile_comments_memory.py'],check=True)
            finally:await context.close()
        finally:await browser.close()
    peak=int((root/'memory.peak').read_text())
    headroom=768*1024**2-peak
    print(json.dumps(dict(container_peak_bytes=peak,container_limit_bytes=768*1024**2,
                         headroom_bytes=headroom,synthetic=True)))
    # A pass requires at least 96 MiB total-container headroom. This includes
    # page cache; it is intentionally conservative. It does not replace a full
    # worker-cycle test with representative archive and pending-result data.
    assert headroom>=96*1024**2,'insufficient_container_headroom'

asyncio.run(main())
PY
