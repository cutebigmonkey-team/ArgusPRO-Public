"""Contract for an invocation INSIDE the existing fenced Hostless writer only.

No Drive client, credentials, scheduler, listener, new worker or writer implementation.
ArchivePort must commit idempotently by canonical identity and batch ID, then read
actual durable journal content independently; latest-feed cache is not readback.
"""
import hashlib
from envelope import canonical
from partition import decode


def verify_readback(body, receipt):
    # Parsing old durable work does not expire it after 24h: original creation
    # time was verified at ingress; retries may take days during a Drive outage.
    import json
    from envelope import _iso
    raw=json.loads(body);part=decode(body,now=_iso(raw['created_at']))
    expected={'batch_id':f"{part['group_id']}:{part['index']}",
              'sha256':hashlib.sha256(body).hexdigest(),'posts':part['posts']}
    if not isinstance(receipt,dict) or set(receipt)!=set(expected) or canonical(receipt)!=canonical(expected):
        raise ValueError('archive_readback_mismatch')
    return expected['sha256']


class TrustedHostlessConsumer:
    def __init__(self, queue, archive_port, owner_guard, *, enabled=False):
        if not callable(owner_guard):raise ValueError('existing_writer_fence_required')
        self.queue=queue;self.archive=archive_port;self.owner_guard=owner_guard;self.enabled=enabled

    def consume_once(self, *, now=None):
        if not self.enabled:raise ValueError('production_consumer_disabled')
        if self.owner_guard() is not True:raise ValueError('writer_unavailable')
        lease=self.queue.claim_part(now=now)
        if lease is None:return None
        try:
            self.queue.check_part_lease(lease['run_id'],lease['lease_token'],now=now)
            if self.owner_guard() is not True:raise ValueError('writer_unavailable')
            import json
            from envelope import _iso
            part=decode(lease['body'],now=_iso(json.loads(lease['body'])['created_at']))
            if hashlib.sha256(lease['body']).hexdigest()!=lease['sha256'] or lease['run_id']!=f"{part['group_id']}:{part['index']}":
                raise ValueError('archive_readback_mismatch')
            # Port performs no URL fetching; producer media links remain metadata.
            self.archive.import_candidate(lease['body'],batch_id=lease['run_id'])
            receipt=self.archive.readback(lease['run_id'])
            digest=verify_readback(lease['body'],receipt)
            for sibling in self.queue.verification_siblings(lease['run_id'],lease['lease_token'],now=now):
                if self.owner_guard() is not True:raise ValueError('writer_unavailable')
                if hashlib.sha256(sibling['body']).hexdigest()!=sibling['sha256']:
                    raise ValueError('archive_readback_mismatch')
                sibling_digest=verify_readback(sibling['body'],self.archive.readback(sibling['run_id']))
                if sibling_digest!=sibling['sha256']:raise ValueError('archive_readback_mismatch')
            if self.owner_guard() is not True:raise ValueError('writer_unavailable')
            return self.queue.acknowledge_part(lease['run_id'],lease['lease_token'],verified_body_sha=digest,now=now)
        except Exception as error:
            code=str(error) if isinstance(error,ValueError) and str(error) in ('archive_readback_mismatch','writer_unavailable') else 'consumer_failed'
            try:self.queue.retry_part(lease['run_id'],lease['lease_token'],error_code=code,now=now)
            except ValueError:pass  # Expired lease is fenced; new owner retries.
            raise ValueError(code) from None
