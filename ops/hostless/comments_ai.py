"""Reuse TypeSafe's request ledger, daily budget, privacy review and validation.

Only text with an existing explicit policy approval is eligible. Author profiles
and discussion metadata never enter the AI request. No new API or budget.
"""
import asyncio
import hashlib
import json
import os
import time
from drive_store import encoded
from typesafe_schedule import worker_class


def item(row):
    return {'id':'comment:'+row['id'],'text':row['text'],'platform':row['platform'],
            'published_at':row.get('published_at')}


class CommentAnalysis:
    def __init__(self):self.next_run=0
    def run_due(self,comments,store,journal,emit):
        if time.monotonic()<self.next_run:return
        self.next_run=time.monotonic()+900
        if os.getenv('ARGUS_TYPESAFE_ENABLED')!='true' or not os.getenv('TYPESAFE_API_KEY'):return
        journal.check_session()
        policy=store.read_json('typesafe-policy.json') or {}
        approved=policy.get('publications',{})
        ids=[k.removeprefix('comment:') for k in approved if k.startswith('comment:') and approved[k].get('approved') is True][:500]
        rows=[]
        for identity in ids:
            found=comments.sql('SELECT payload FROM collected_comments WHERE id=?',(identity,)).fetchone()
            if found:rows.append(json.loads(found['payload']))
        if not rows:return
        config=json.loads(os.getenv('ARGUS_TYPESAFE_CONFIG','{}'))
        config.update(max_posts=1,concurrency=1,public_archive_consent=False,public_dsns_consent=False)
        Worker,Client=worker_class()
        snapshot={'items':[item(row) for row in rows]}
        report=asyncio.run(Worker(store,Client(),config,guard=journal.check_session,pointer='comments-typesafe-catalog.json').run(snapshot,policy,max_seconds=35))
        pointer=store.read_json('comments-typesafe-catalog.json')
        raw=store.read_bytes('typesafe-'+pointer['sha256']+'.json',20*1024**2)
        if not raw or hashlib.sha256(raw).hexdigest()!=pointer['sha256']:raise ValueError('comment_analysis_checksum')
        assessments=json.loads(raw)['assessments']
        with comments.transaction():
            for row in rows:
                value=assessments.get('comment:'+row['id'],{})
                if value.get('status')!='completed' or value.get('content_sha256')!=hashlib.sha256(encoded(item(row))).hexdigest():continue
                row['analysis']=value
                sentiment=(value.get('assessments') or {}).get('sentiment',{})
                choice=sentiment.get('value') if isinstance(sentiment,dict) else sentiment
                if choice in ('positive','neutral','negative'):row['sentiment']=choice
                comments.sql('UPDATE collected_comments SET payload=? WHERE id=?',(encoded(row).decode(),row['id']))
        emit('comments_analysis_published',**report)
