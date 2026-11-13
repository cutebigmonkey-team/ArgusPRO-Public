import test from 'node:test';
import assert from 'node:assert/strict';
import {coverageView,monitorHeadline} from './coverage.mjs';
test('no fabricated coverage without measurements',()=>assert.equal(coverageView({}),null));
test('current Hostless headline does not reuse an older GitHub run',()=>{
  const r={updated_at:'2026-11-02T10:00:00Z',state:'passed',coverage:{at:'2026-11-05T12:00:00Z',successful_sources_5m:0},
    runtime:{at:'2026-11-05T12:00:03Z',deployment_sha:'17f8f60abcdef',last_successful_delivery:'2026-11-05T11:59:50Z'}};
  const h=monitorHeadline(r,Date.parse('2026-11-05T12:01:00Z'));
  assert.equal(h.at,r.runtime.at);assert.equal(h.stats[1][1],'0');assert.equal(h.stats[3][1],'17f8f60');
  assert.match(monitorHeadline(r,Date.parse('2026-11-05T12:06:00Z')).stats[0][1],/застаріла/);
  assert.equal(monitorHeadline({state:'passed'}),null);
});
test('stale data and unknown utilization remain explicit',()=>{
  const v=coverageView({coverage:{at:'2026-11-05T08:00:00Z',registered:1198,successful_sources_5m:4,
    coverage_5m_all_percent:100*4/1198,platforms:{x:{registered:31,successful_sources_5m:0,error_rate_1h:1}}}},Date.parse('2026-11-05T08:06:00Z'));
  assert.equal(v.stale,true);assert.match(v.lines.join('\n'),/Дані застарілі/);
  assert.match(v.lines.join('\n'),/CPU: —/);assert.match(v.platforms[0],/0\/31/);
});
test('empty successful checks never claim new delivery and queues remain visible',()=>{
  const view=coverageView({coverage:{at:'2026-11-06T08:00:00Z',platforms:{threads:{registered:9,active:9,
    successful_sources_5m:2,checked_sources_1h:3,collection_state:'checked_no_new_posts',new_posts_1h:0,
    latest_statuses:{timeout:1,login_required:2}}}},runtime:{platform_queues:{threads:{due:4,leased:1,pending_results:2}}}},Date.parse('2026-11-06T08:01:00Z'));
  assert.match(view.platforms[0],/Перевірено, нових постів немає/);
  assert.match(view.platforms[0],/очікують 4, у роботі 1, результатів до запису 2/);
  assert.match(view.platforms[0],/Потрібна авторизація: 2/);
});
