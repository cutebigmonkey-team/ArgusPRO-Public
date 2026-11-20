"""Offline import contract for the exact interpreter used by a public lane.

Import only: no sources, browser launch, credentials, Drive access or delivery.
"""
import argparse
import importlib
import json
import sys
from collection_diagnostics import diagnostic, import_failure_code

CORE = ('requests', 'urllib3', 'run', 'source_adapters', 'remaining_http',
        'http_pool', 'cloud_social', 'post_metrics', 'feed_history',
        'telegram_history', 'youtube_history')
LANES = {
    'fast': CORE + ('threads_public', 'public_embeds', 'media_metadata', 'gdelt_public'),
    'extended': CORE + ('embed_social', 'remaining_social', 'browser_public', 'rss_gateway'),
}
ENVIRONMENTS = {
    'embed': ('gallery_dl', 'gallery_dl_web.gallerydl.config_builder'),
    'remaining': ('fbgql', 'granary.facebook', 'granary.instagram'),
}


def smoke(lane, environment='main', reverse=False, importer=importlib.import_module):
    if lane not in LANES or environment not in ('main', *ENVIRONMENTS):
        raise ValueError('invalid_smoke_configuration')
    if environment != 'main' and lane != 'extended':
        raise ValueError('invalid_smoke_configuration')
    modules = LANES[lane] if environment == 'main' else ENVIRONMENTS[environment]
    rows = []
    for module in reversed(modules) if reverse else modules:
        try:
            importer(module)
            rows.append({'module': module, 'status': 'ok'})
        except ImportError as error:
            info = diagnostic(error, import_failure_code(error))
            rows.append({'module': module, 'status': info['code'],
                         'exception_type': info['exception_type'],
                         'import_module': info.get('import_module', 'NOT_VERIFIED')})
        except Exception:
            rows.append({'module': module, 'status': 'import_smoke_failed'})
    return {'lane': lane, 'environment': environment, 'reverse': reverse,
            'passed': all(row['status'] == 'ok' for row in rows), 'imports': rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lane', choices=LANES, required=True)
    parser.add_argument('--environment', choices=('main', *ENVIRONMENTS), default='main')
    parser.add_argument('--reverse', action='store_true')
    args = parser.parse_args()
    # Smoke tests must never become a hidden live collection entry point.
    def offline(event, arguments):
        if event in ('socket.connect', 'socket.getaddrinfo'):
            raise RuntimeError('import_smoke_network_forbidden')
    sys.addaudithook(offline)
    try:
        result = smoke(args.lane, args.environment, args.reverse)
    except Exception:
        print('IMPORT_SMOKE_CONFIGURATION_FAILED')
        return 1
    print(json.dumps(result))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
