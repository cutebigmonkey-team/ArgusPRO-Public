"""Fair, bounded admission with an explicit (not guaranteed) five-minute target."""
from collections import defaultdict
from catalog_scope import due_batch
from source_health import instant

TARGET_SECONDS = 300


def plan(catalog, state, enabled, now, budget=4):
    polling = state.get('incremental_polling', {})
    # Never shorten a stored deadline: legacy receipts may include Retry-After
    # in next_check_at without retaining its separate origin.
    eligible = due_batch(catalog, state,
                         enabled, now, budget=max(1, len(catalog['sources'])))
    groups = defaultdict(list)
    platform_last = defaultdict(str)
    for source in catalog['sources']:
        row = polling.get(source['id'], {})
        if row.get('url') == source['url']:
            platform_last[source['platform']] = max(platform_last[source['platform']], row.get('last_checked_at') or '')
    for source in eligible['sources']:
        groups[source['platform']].append(source)
    def checked(source):
        row = polling.get(source['id'], {})
        return row.get('last_checked_at') or '' if row.get('url') == source['url'] else ''
    for rows in groups.values():
        rows.sort(key=lambda source: (checked(source), source['id']))
    order = sorted(groups, key=lambda platform: (platform_last[platform], platform))
    selected = []
    while order and len(selected) < budget:
        for platform in list(order):
            if len(selected) == budget:
                break
            selected.append(groups[platform].pop(0))
            if not groups[platform]:
                order.remove(platform)
    overdue = [source for source in eligible['sources'] if not checked(source)
               or (now - instant(checked(source))).total_seconds() > TARGET_SECONDS]
    return {**eligible, 'sources': selected, 'live_lane': 'all',
            'live_order': [source['id'] for source in selected], 'news_signals': {},
            'poll_target_seconds': TARGET_SECONDS, 'eligible_sources': len(eligible['sources']),
            'overdue_sources': len(overdue),
            'platforms_waiting': sorted({source['platform'] for source in eligible['sources']})}
