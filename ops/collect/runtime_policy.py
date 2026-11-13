"""Explicit cloud runtime selection; do not impersonate GitHub Actions."""
import os
import sys


def require_cloud():
    if os.getenv('GITHUB_ACTIONS') == 'true' and os.getenv('RUNNER_ENVIRONMENT') == 'github-hosted':
        return
    if sys.platform == 'linux' and os.getenv('ARGUS_COLLECTOR_RUNTIME') == 'hostless':
        return
    raise RuntimeError('Collection requires a GitHub-hosted runner or explicit Hostless Linux runtime')
