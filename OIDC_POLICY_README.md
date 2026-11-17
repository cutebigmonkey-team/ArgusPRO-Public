# ARGUS ingestion trust policy — offline code review only

`ops/ingest/oidc_policy.py` defines an **authorization policy on verified GitHub OIDC claims**. Its caller must FIRST verify GitHub's JWT signature against the official `https://token.actions.githubusercontent.com/.well-known/jwks` keys, issuer, expected audience, expiration, not-before and algorithm using a maintained JWT library. This patch intentionally contains **no JWT verification, HTTP listener, database or production delivery**.

Policy only authorizes GitHub-hosted `schedule` or `workflow_dispatch` on `cutebigmonkey-team/ArgusPRO-Public` `refs/heads/main` via `argus-public-collection.yml`. PR jobs, other repositories, other branches and self-hosted runners are rejected. Token verification must also bind envelope `run_id` to the verified `run_id:run_attempt:lane:shard` and ensure its `sha` is the approved deployment commit. Never grant `id-token: write` or ingestion credentials to existing PR jobs.

To prepare deployment, independently verify Hostless free plan available app/database allocation; tenant limits and writer startup status. Do not create a new App, change environment variables, deploy or resume a paused writer as part of this patch. A functioning inbound HTTPS service and persistent shared queue remain separate future tasks.

Test with `PYTHONPATH=ops/ingest python3 -m unittest discover -s ops/ingest -p 'test_*.py' -v`.
