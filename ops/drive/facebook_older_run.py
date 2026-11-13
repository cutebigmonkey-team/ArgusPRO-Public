"""Freeze the Facebook pilot cohort, then publish and verify strictly earlier posts."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path
import sys
import tempfile

from drive_store import DriveStore,from_environment
from publish import load_snapshot,publish
from platform_views import render_posts
from verify_collection import verify_posts
from facebook_coverage import qualified

COLLECT=Path(__file__).resolve().parents[1]/'collect'
sys.path.insert(0,str(COLLECT))
from browser_public import facebook_owned
from facebook_older import candidate,strictly_older
from cloud_social import require_cloud
from registry import validate
from storage import pack

BASELINE='41412547663'

def continuation_plan(store,run_id,sources):
    from facebook_continue import legacy_tail
    if not run_id.isdigit():raise ValueError('prior_full_run_required')
    parent=json.loads(Path('prior-report/report.json').read_text())
    original=json.loads(Path('prior-input/plan.json').read_text())
    gate=store.read_json('facebook-older-gate-'+run_id+'.json')
    if (not gate or gate!=parent['summary'] or gate['run']!=run_id or gate['phase']!='all' or
            gate['baseline']!=BASELINE or original['phase']!='all' or original['baseline']!=BASELINE):
        raise ValueError('unverified_prior_run')
    catalog={s['id']:s for s in sources}
    if {s['id'] for s in original['sources']}!=catalog.keys() or parent['sources'].keys()!=catalog.keys():
        raise ValueError('prior_catalog_mismatch')
    remaining={};anchors={};excluded={}
    for sid,record in parent['sources'].items():
        if record['stages'].get('detail')!='candidate_budget':
            continue
        if record['id']!=sid or record['url']!=catalog[sid]['url']:raise ValueError('source_identity_mismatch')
        remaining[sid]=legacy_tail(record,original)
        excluded[sid]=[r['candidate'] for r in record['reads']]
        if record.get('anchor'):anchors[sid]=record['anchor']
    if not remaining or (run_id=='41417079087' and len(remaining)!=23):
        raise ValueError('unexpected_budget_cohort')
    snapshot=load_snapshot(store)
    fb=[p for p in snapshot['items'] if p['platform']=='facebook' and not p.get('sample')]
    known={p['id'] for p in fb}
    known.update(f['canonical_key'] for p in fb if (f:=candidate(p['url'])))
    native_ids={p['facebook_story_id'] for p in fb if p.get('facebook_story_id')}
    native_ids.update(k[9:] for k in known if k.startswith('facebook:') and k[9:].isdigit())
    return {'phase':'resume','baseline':BASELINE,'prior_run':run_id,'sources':[s for s in sources if s['id'] in remaining],
            'anchors':anchors,'remaining':remaining,'excluded':excluded,'known':sorted(known),
            'known_native':sorted(native_ids),
            'qualified_before':sorted(p['id'] for p in fb if qualified(p)),
            'archive_before':{'facebook_posts':len(fb),'snapshot_time':snapshot.get('updated_at')}}


def prepare(store,phase,pilot):
    registry=validate(json.loads(Path('registry/collectors.json').read_text()))
    if not any(c['platform']=='facebook' and c['enabled'] for c in registry['collectors']):
        raise ValueError('facebook_disabled_in_registry')
    catalog=json.loads((COLLECT/'sources.json').read_text())['sources']
    sources=[s for s in catalog if s['platform']=='facebook' and s.get('enabled',True)]
    report=store.read_json('collection-report-'+BASELINE+'.json')
    by_id={s['source_id']:s for s in report['sources']}
    # Replay the original verified scope even after more Facebook publishers are added.
    sources=[s for s in sources if s['id'] in by_id]
    if len(by_id)!=225 or {s['id'] for s in sources}!=set(by_id):
        raise ValueError('catalog_does_not_match_verified_baseline')
    if phase=='resume':
        plan=continuation_plan(store,pilot,sources)
        Path('input').mkdir(exist_ok=True)
        Path('input/plan.json').write_text(json.dumps(plan,ensure_ascii=False),encoding='utf-8')
        print(json.dumps({'resume_sources':len(plan['sources']),'pending_pairs':sum(map(len,plan['remaining'].values())),
                          'archive_before':plan['archive_before']}))
        return
    if phase=='all':
        if not pilot.isdigit():
            raise ValueError('pilot_run_required')
        gate=store.read_json('facebook-older-gate-'+pilot+'.json')
        if not gate or gate['phase']!='pilot' or gate['baseline']!=BASELINE or gate['sources']!=107 or not gate['passed']:
            raise ValueError('pilot_did_not_verify_older_posts')
        plan=store.read_json('facebook-older-input-'+pilot+'.json')
        if not plan or plan['baseline']!=BASELINE or len(plan['anchors'])!=107:
            raise ValueError('missing_frozen_pilot_plan')
    else:
        saved=load_snapshot(store)['items']
        fb={p['id']:p for p in saved if p['platform']=='facebook' and not p.get('sample')}
        anchors={}
        for sid,s in by_id.items():
            if s['posts']==1:
                key,=s['canonical_keys']
                if not s['latest_published_at'] or key not in fb:
                    raise ValueError('missing_anchor')
                anchors[sid]={'key':key,'date':s['latest_published_at'],'story_id':fb[key].get('facebook_story_id'),'basis':'verified_run_'+BASELINE}
        if len(anchors)!=107:
            raise ValueError('pilot_cohort_changed')
        seeds={};cross={}
        linked={u for p in saved for u in p.get('outbound_social_links',[]) if candidate(u)}
        for s in sources:
            seeds[s['id']]=list(dict.fromkeys(p['url'] for p in fb.values() if candidate(p['url']) and
                (facebook_owned(s,{},p['url']) or p.get('source_name')==s['name'])))
            cross[s['id']]=sorted(u for u in linked if facebook_owned(s,{},u))
        plan={'baseline':BASELINE,'known':sorted(fb),'known_native':sorted({p['facebook_story_id'] for p in fb.values() if p.get('facebook_story_id')}),
              'qualified_before':sorted(k for k,p in fb.items() if qualified(p)), 'seeds':seeds,'crosslinks':cross,'anchors':anchors}
    citations=json.loads(Path('public-citations.json').read_text())
    plan['citations']={s['id']:[u for u in citations if facebook_owned(s,{},u)] for s in sources}
    plan['citation_pages']=citations
    plan.update(phase=phase,sources=[s for s in sources if phase=='all' or s['id'] in plan['anchors']])
    Path('input').mkdir(exist_ok=True)
    Path('input/plan.json').write_text(json.dumps(plan,ensure_ascii=False),encoding='utf-8')
    print(json.dumps({'phase':phase,'sources':len(plan['sources']),'anchors':len(plan['anchors'])}))


def finish(store,run):
    plan=json.loads(Path('input/plan.json').read_text())
    expected={s['id']:s for s in plan['sources']}
    records={};posts={};members={}
    for path in sorted(Path('parts').glob('*/part.json')):
        part=json.loads(path.read_text())
        if part['phase']!=plan['phase'] or part['baseline']!=BASELINE:
            raise ValueError('wrong_part')
        rows={p['canonical_key']:p for p in part['posts']}
        reported=set()
        for s in part['sources']:
            sid=s['id']
            if sid not in expected or sid in records or expected[sid]['url']!=s['url']:
                raise ValueError('unexpected_source')
            if sid in plan['anchors'] and s['anchor']!=plan['anchors'][sid]:
                raise ValueError('changed_comparator')
            if plan['phase']=='resume':
                queue=plan['remaining'][sid]
                read=[v['candidate'] for v in s['reads']]
                if (read!=[v['key'] for v in queue[:len(read)]] or set(read).intersection(plan['excluded'][sid]) or
                        s['remaining_queue']!=queue[len(read):]):
                    raise ValueError('continuation_order_or_overlap_mismatch')
            records[sid]=s
            members[sid]=set()
            for key in s['accepted']:
                row=rows[key]
                if not strictly_older(row,s['anchor']) or row['platform']!='facebook' or candidate(row['post_url'])['canonical_key']!=key:
                    raise ValueError('unverified_older_record')
                native=row.get('facebook_story_id') or key
                if plan['phase']=='resume' and row['facebook_known_before']!=(key in plan['known'] or native in plan['known_native']):
                    raise ValueError('archive_baseline_mismatch')
                members[sid].add(native)
                reported.add(key)
                prev=posts.get(native)
                if prev is None or (qualified(row),row['observed_at'])>(qualified(prev),prev['observed_at']):
                    posts[native]=row
        if set(rows)!=reported:
            raise ValueError('unexpected_posts_in_part')
    if records.keys()!=expected.keys():
        raise ValueError('incomplete_source_coverage')
    by_key={p['canonical_key']:p for p in posts.values()}
    if len(by_key)!=len(posts):
        raise ValueError('story_identity_conflict')
    if by_key:
        with tempfile.TemporaryDirectory() as tmp:
            tmp=Path(tmp);(tmp/'media').mkdir()
            (tmp/'posts.jsonl').write_text(''.join(json.dumps(p,ensure_ascii=False)+'\n' for p in by_key.values()),encoding='utf-8')
            pack(tmp/'posts.jsonl',tmp/'media',tmp/'posts.zip')
            publish(store,tmp/'posts.zip')
            if publish(store,tmp/'posts.zip')['status']!='already_imported':
                raise ValueError('package_replay_failed')
    snapshot=load_snapshot(store)
    checked=verify_posts(by_key,snapshot)
    # Keep the normal Facebook posts view current without replacing the daily feed status.
    mapping=json.loads((COLLECT/'platform-folders.json').read_text())
    if mapping['root']!=store.root:raise ValueError('wrong_archive_root')
    folder=DriveStore(store.service,mapping['platforms']['facebook']['folder_id']);folder.assert_private()
    if by_key:
        items=sorted((p for p in snapshot['items'] if p['platform']=='facebook' and not p.get('sample')),
                     key=lambda p:(p.get('published_at') or p['observed_at'],p['id']),reverse=True)
        folder.json('posts.json',{'posts':items},mutable=True)
        folder.put('posts.txt',render_posts(items),mutable=True)
        if folder.read_json('posts.json')['posts']!=items:raise ValueError('facebook_view_readback_failed')
    good=[p for p in posts.values() if qualified(p)]
    independent=[p for p in good if p['facebook_older_method']!='archive_permalink']
    enriched=[p for p in good if p['canonical_key'] not in plan['qualified_before']]
    gate={'baseline':BASELINE,'run':run,'phase':plan['phase'],'sources':len(records),'older_posts':len(posts),
          'sources_with_older':sum(bool(v) for v in members.values()),'full_text_with_date':len(good),
          'sources_with_full':sum(any(qualified(posts[k]) for k in v) for v in members.values()),
          'new_to_archive_full':sum(not p['facebook_known_before'] for p in good),
          'newly_qualified_full':len(enriched),'independently_discovered_full':len(independent),
          'methods_full':dict(Counter(p['facebook_older_method'] for p in good)),
          'passed':bool(independent or enriched),'readback':checked}
    if plan['phase']=='resume':
        read=[r['candidate'] for s in records.values() for r in s['reads']]
        gate.update(prior_run=plan['prior_run'],skipped_prior_pairs=sum(map(len,plan['excluded'].values())),
                    pending_at_start=sum(map(len,plan['remaining'].values())),checked_pairs=len(read),
                    checked_distinct_urls=len(set(read)),still_pending=sum(len(s['remaining_queue']) for s in records.values()),
                    reasons=dict(Counter(r['status'] for s in records.values() for r in s['reads'])),
                    archive_before=plan['archive_before'])
    lines=[f"ARGUS · Старіші дописи Facebook · запуск {run}",f"Контрольний запуск {BASELINE}",
           f"Перевірено джерел — {len(records)}. Старіших дописів — {len(posts)}. З повним текстом і датою — {len(good)}.",
           f"Нових для архіву серед повних — {gate['new_to_archive_full']}.",'']
    if plan['phase']=='resume':
        lines.extend([f"Продовження запуску {plan['prior_run']} з позиції 21 для кожного джерела.",
                      f"Перевірено пар джерело–посилання: {gate['checked_pairs']} із {gate['pending_at_start']}. Унікальних URL: {gate['checked_distinct_urls']}.",''])
    for sid,s in sorted(records.items(),key=lambda x:x[1]['url']):
        group=[posts[k] for k in members[sid]]
        lines.extend([s['name'],s['url'],f"Старіших — {len(group)}. Повних із датою — {sum(qualified(p) for p in group)}.",
                      'Контрольний допис — '+(s['anchor']['key']+' · '+s['anchor']['date'] if s['anchor'] else 'дату не підтверджено'),
                      'Етапи — '+json.dumps(s['stages'],ensure_ascii=False),
                      'Прямі перевірки — '+json.dumps(dict(Counter(i['status'] for i in s['reads'])),ensure_ascii=False)])
        for p in sorted(group,key=lambda p:p['published_at'],reverse=True):
            lines.append(f"{p['published_at']} · {p['post_url']} · {p['facebook_older_method']} · "+('повний текст' if qualified(p) else 'неповний запис'))
        lines.append('')
    gate['report_file_id']=folder.put('facebook-older-'+run+'.txt','\n'.join(lines).encode('utf-8'))
    if plan['phase']=='pilot':
        store.json('facebook-older-input-'+run+'.json',plan)
    store.json('facebook-older-gate-'+run+'.json',gate)
    if store.read_json('facebook-older-gate-'+run+'.json')!=gate:raise ValueError('gate_readback_failed')
    Path('verified').mkdir(exist_ok=True)
    Path('verified/report.json').write_text(json.dumps({'summary':gate,'sources':records},ensure_ascii=False),encoding='utf-8')
    print(json.dumps(gate))


if __name__=='__main__':
    require_cloud();p=argparse.ArgumentParser();p.add_argument('action',choices=['prepare','finish']);p.add_argument('--phase',choices=['pilot','all','resume'],default='pilot');p.add_argument('--pilot',default='')
    a=p.parse_args();store=from_environment();store.assert_private()
    if a.action=='prepare':prepare(store,a.phase,a.pilot)
    else:finish(store,os.environ['GITHUB_RUN_ID'])
