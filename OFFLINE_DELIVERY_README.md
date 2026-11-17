# ARGUS — isolated collector → envelope → queue → readback test

This patch adds only `ops/ingest/offline_delivery.py` and `ops/ingest/test_offline_delivery.py`.

It relies on existing `envelope.py` and `durable_queue.py` in PR #2.

Run:

```bash
PYTHONPATH=ops/ingest python3 -m unittest discover -s ops/ingest -p 'test_*.py' -v
```

The test exercises a synthetic collector report and JSONL, canonical candidate envelope,
HMAC-signed queue submission, a disposable fake archive, readback-check prior to ack,
idempotent reprocessing after readback failure, persisted queue restart, input mismatches,
and fail-closed behavior when the 250-record envelope cap is exceeded.

**This is not a production archive adapter or a network transport.** `FakeIndependentArchive`
is an in-memory fixture, and archive verification remains entirely mock-based.
Do not use `drain_verified_once` with a caller-controlled fake readback. Real Hostless
integration must enforce writer session fencing, source ownership and identity,
private durable storage, trusted non-PR workflow identity, receipt bound to real
readback, replay and quota controls. Never export real source data into GitHub artifacts.

Important: Current envelope v1 permits at most 250 records, and the adapter
rejects larger collections instead of silently truncating. A production system
needs separately reviewed batch partitioning and IDs before scaling up.
