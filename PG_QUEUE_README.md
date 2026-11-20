# ARGUS PostgreSQL queue — isolated prototype

**Current scope:** a database adapter and manual SQL schema, with four
contract smoke tests. No database is created, no host is contacted, and no
Hostless production changes are made.

Requires a `psycopg` v3 connection object when used for real integration.
Do not use a GitHub Actions runner to supply archive/database credentials.

## Test

```bash
PYTHONPATH=ops/ingest python3 -m unittest discover -s ops/ingest -p 'test_*.py' -v
```

## Before any deployment

- Test with a disposable PostgreSQL instance: simultaneous claims, identical
  replay, contradictory replay, transaction rollback, stale lease fencing,
  crash/restart, bytes/quotas, invalid input, and DB outages.
- Set up **separate least-privilege roles** and a trusted stored procedure/API
  for ingestion. Do not expose the general-purpose `PostgresQueue` object to
  an untrusted HTTP handler with worker privileges.
- Persist replay / JWT `jti` prevention and implement quotas, cleanup and
  backup/retention; bound live row count and bytes.
- Ensure only the existing fenced Hostless writer may acknowledge after an
  independent archive read-back. No second writer.
- Verify that server resource availability and costs remain within the plan.
- Gate all endpoint/network behavior behind an explicitly reviewed feature
  flag, disabled by default. Schema is not applied automatically.
- No deployment or migration is included.
