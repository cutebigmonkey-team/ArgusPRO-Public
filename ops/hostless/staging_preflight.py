"""Check staging bindings and optionally read aggregate catalog metadata."""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import sys


CATALOG_BINDINGS = (
    'ARGUS_DRIVE_OAUTH', 'ARGUS_DRIVE_FOLDER_ID',
    'ARGUS_JSONBIN_BIN_ID', 'ARGUS_JSONBIN_KEY',
)
REQUIRED = (*CATALOG_BINDINGS, 'DATABASE_URL', 'ARGUS_MIN_PRIVATE_SOURCES')
OPTIONAL = (
    'ARGUS_DATABASE_SESSION_URL', 'ARGUS_PRIVATE_SOURCE_FILE',
    'ARGUS_AUDIT_DATABASE_URL', 'ARGUS_JSONBIN_KEY_TYPE',
    'ARGUS_COLLECTOR_RUNTIME', 'ARGUS_RUNTIME_MODE', 'ARGUS_ARCHIVE_WRITER_OWNER',
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--read-catalog', action='store_true',
                        help='Read already authorized, isolated staging Drive and JSONBin bindings')
    args = parser.parse_args()
    present = {key: bool(os.getenv(key)) for key in (*REQUIRED, *OPTIONAL)}
    minimum = os.getenv('ARGUS_MIN_PRIVATE_SOURCES', '')
    valid_minimum = minimum.isascii() and minimum.isdecimal() and bool(minimum.lstrip('0'))
    result = {
        'bindings_present': present,
        'catalog_route': 'mounted_file' if present['ARGUS_PRIVATE_SOURCE_FILE'] else 'drive_sources_json',
        'minimum_valid': valid_minimum,
    }
    if not args.read_catalog or not all(present[key] for key in CATALOG_BINDINGS):
        print(json.dumps(result))
        return int(args.read_catalog)

    ops = Path(__file__).resolve().parents[1]
    sys.path[:0] = [str(ops / name) for name in ('collect', 'drive', 'local-storage')]
    phase = 'imports'
    try:
        from drive_store import from_environment
        from registry import ENGINES, EXTENSIONS, fetch_registry
        from runtime_catalog import load_private_catalog
        from site_sources import with_submissions
        from source_admission import source_enabled

        phase = 'archive_access'
        store = from_environment()
        store.assert_private()
        phase = 'source_catalog'
        catalog = load_private_catalog(store, with_submissions, inventory_only=True)
        phase = 'engine_registry'
        registry = fetch_registry(os.environ['ARGUS_JSONBIN_BIN_ID'], os.environ['ARGUS_JSONBIN_KEY'],
                                  os.getenv('ARGUS_JSONBIN_KEY_TYPE', 'access'))
        engines = {row['platform'] for row in registry['collectors'] if row['enabled']}
        phase = 'source_summary'
        counts = {}
        for source in catalog['sources']:
            platform = source['platform']
            label = platform if platform in ENGINES or platform in EXTENSIONS else 'unknown'
            row = counts.setdefault(label, Counter(configured=0, source_enabled=0, engine_eligible=0))
            row['configured'] += 1
            row['source_enabled'] += source_enabled(source)
            row['engine_eligible'] += source_enabled(source) and platform in engines
        result.update(configured_sources=len(catalog['sources']), platforms=counts,
                      runtime_schema='argus_drive_' + hashlib.sha256(store.root.encode()).hexdigest()[:24])
    except Exception:
        result['failed_phase'] = phase
        print(json.dumps(result))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
