"""Comment eligibility targets; actual throughput is bounded by the shared worker."""
import math


def retry_seconds(value):
    try:
        seconds = float(value or 0)
        return max(0, seconds) if math.isfinite(seconds) else 0
    except (TypeError, ValueError):
        return 0


def interval(age, last_new, quiet_scans, now):
    if age < 3600:
        return 120
    if last_new and now - last_new < 3600:
        return 180
    if age < 86400:
        return 600
    if age < 604800:
        return 1800
    return min(86400, 21600 * 2 ** min(quiet_scans, 2))


def delay(age, activity, now, status, failures, continuation, retry_after):
    seconds = interval(age, activity['last_new'], activity['quiet_scans'], now)
    if continuation and status in ('success', 'partial'):
        seconds = 30
    if status in ('auth_required', 'forbidden', 'unsupported'):
        seconds = 86400
    if failures:
        seconds = min(86400, 60 * 2 ** min(failures, 10))
    # A continuation must NEVER override an upstream Retry-After.
    return max(seconds, retry_seconds(retry_after))
