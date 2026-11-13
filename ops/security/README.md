# Final snapshot validation

`publication_audit.py` is a redacted heuristic control, never the sole basis for
publication approval. Its reviewed findings remain visible. The historical
Compose fixture review matches an immutable whole-file SHA256, Git blob, path,
rule and line; it does not exempt arbitrary credentials or future file changes.
New disposable Compose configurations use fresh random credentials in a mode
0600 file. There is no production credential in that fixture.

The independent offline gate is:

```sh
python ops/security/independent_scan.py --root .
```

It requires a genuine Gitleaks executable on PATH. When the tool is unavailable,
every scope is NOT_EXECUTED and the command fails closed. It never substitutes
this project's scanner for Gitleaks. Reports stay under `.git/publication-validation`
and must never be uploaded as public artifacts. Docker scope is a conservative
source superset; it is not an engine-generated context.

```sh
python -m unittest discover -s ops/security -p 'test_*.py' -v
PYTHONPATH=ops/hostless python -m unittest discover -s ops/hostless -p test_runtime_catalog.py -v
```

The registry tests use controlled stores and private temporary files only.
Transient failure recovery is tested by a subsequent explicit invocation; the
loader does not add an automatic retry loop or shorten Retry-After. Missing,
empty, incomplete or inaccessible configuration never enables shipped fixtures.

Public CI uses read-only contents permissions, standard GitHub-hosted runners,
normal pull_request events, no production secrets and no deployment. Exact
Docker, real-engine, Chromium, PostgreSQL and runtime-catalog steps remain
mandatory. Static workflow validation does not establish a successful image.
