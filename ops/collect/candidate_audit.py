"""Per-source counts at parser inputs. No payloads or identifiers are logged."""
from collections import Counter
import re
from contextvars import ContextVar

current = ContextVar('candidate_counts', default=None)
rejections = ContextVar('candidate_rejections', default=None)
publication_window = ContextVar('publication_window', default=None)


def windowed(rows):
    from datetime import datetime, timezone
    since=publication_window.get()
    if since is None:return rows
    until=datetime.now(timezone.utc)
    kept=[]
    for row in rows:
        stamp=row.get('published_at')
        if stamp:
            instant=datetime.fromisoformat(stamp.replace('Z','+00:00'))
            if instant < since or instant > until:
                reject('before_publication_window' if instant < since else 'future_publication',row.get('canonical_key'))
                continue
        kept.append(row)
    return kept


def publisher_candidate(platform,url,published):
    from hashlib import sha256
    from url_identity import clean_permalink
    return {'key':platform+':'+sha256(clean_permalink(url).encode()).hexdigest(), 'published_at':published}


def sample_candidates(candidates,limit=20):
    # Stable per-response sample chosen before content parsing or archive lookup.
    return sorted(candidates,key=lambda row:row['key'])[:limit]


def reject(reason, identity=None):
    count(reason)
    log = rejections.get()
    if log is not None:
        if identity and not (isinstance(identity,str) and len(identity)<=180 and
                re.fullmatch(r'[a-z]{1,12}:[A-Za-z0-9_/:-]+',identity)):
            from hashlib import sha256
            identity = 'ref:' + sha256(str(identity).encode()).hexdigest()
        log.append({'reason':reason,'candidate':identity})


def start():
    counts = Counter()
    current.set(counts)
    return counts


def count(reason, amount=1):
    counts = current.get()
    if counts is not None and amount:
        counts[reason] += amount
