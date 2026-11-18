"""Mandatory isolated PostgreSQL gate: missing service or skipped safety tests fail."""
import os
from pathlib import Path
import sys
import unittest

if __name__=='__main__':
    if not os.environ.get('ARGUS_PG_INTEGRATION_DSN'):
        raise SystemExit('isolated_postgres_required')
    root=str(Path(__file__).resolve().parent)
    loader=unittest.TestLoader();suite=unittest.TestSuite()
    for pattern in ('test_pg_queue_integration.py','test_phase2_postgres.py'):
        suite.addTests(loader.discover(root,pattern=pattern))
    result=unittest.TextTestRunner(verbosity=2).run(suite)
    if result.testsRun<19 or result.skipped or not result.wasSuccessful():
        raise SystemExit('postgres_gate_failed_or_incomplete')
    raise SystemExit(0)
