const number=value=>Number.isFinite(value)?value.toLocaleString('uk-UA',{maximumFractionDigits:2}):'—';
const date=value=>value&&Number.isFinite(Date.parse(value))?new Date(value).toLocaleString('uk-UA'):'—';
const states={delivering:'Нові публікації збережено',checked_no_new_posts:'Перевірено, нових постів немає',collection_errors:'Помилки збору',not_checked_recently:'За годину не перевірено'};
const reasons={login_required:'Потрібна авторизація',robots_denied:'Доступ заборонено robots',robots_challenge_required:'Перевірка доступу robots',challenge_required:'CAPTCHA / перевірка доступу',http_429:'Обмеження частоти',timeout:'Час очікування вичерпано',never_checked:'Ще не перевірено',partial_results:'Неповний результат',verified_post_identity_unavailable:'Немає підтверджених ідентичностей постів'};

export function monitorHeadline(report,now=Date.now()) {
  if(!report?.runtime && !report?.coverage)return null;
  const r=report.runtime,c=report.coverage;
  const times=[r?.at,c?.at].filter(t=>Number.isFinite(Date.parse(t)));
  const at=times.sort((a,b)=>Date.parse(b)-Date.parse(a))[0];
  const age=now-Date.parse(r?.at),fresh=Number.isFinite(age)&&age>=0&&age<=300000;
  return {at,stats:[['Worker Hostless',fresh?'Телеметрія актуальна':'Телеметрія застаріла або відсутня'],
    ['Успішно за 5 хв',number(c?.successful_sources_5m)],
    ['Остання доставка',date(r?.last_successful_delivery)],
    [r?.collection_sha256?'Код колекторів (SHA-256)':'Версія worker (заявлена)',(r?.collection_sha256||r?.deployment_sha)?.slice(0,7)||'—']]};
}

export function coverageView(report,now=Date.now()) {
  const c=report?.coverage,r=report?.runtime;
  if(!c)return null;
  const age=now-Date.parse(c.at),stale=!Number.isFinite(age)||age<0||age>300000;
  return {stale,title:'Покриття джерел',lines:[
    `Стан на ${date(c.at)}${stale?' · Дані застарілі; поточне покриття не підтверджене':''}`,
    `Зареєстровано: ${number(c.registered)} · Увімкнено: ${number(c.active)} · Без явного блокування доступу: ${number(c.eligible)}`,
    `Успішно перевірено за 5 / 15 хв: ${number(c.successful_sources_5m)} / ${number(c.successful_sources_15m)}`,
    `Coverage 5m від усіх джерел: ${number(c.coverage_5m_all_percent)}% · Серед доступних: ${number(c.coverage_5m_eligible_percent)}%`,
    `Спроб за 5 хв: ${number(c.attempted_sources_5m)} · Очікують перевірки: ${number(c.overdue)} · Обмеження частоти: ${number(c.rate_limited_sources)}`,
    `Джерел із помилками за 5 хв: ${number(c.failed_sources_5m)} · Нових записів за 5 хв / годину / добу: ${['5m','1h','24h'].map(k=>number(c.new_posts?.[k])).join(' / ')}`,
    `Нових публікацій за останню хвилину: ${number(c.new_posts_per_minute)}`,
    `Тривалість збору, p50 / p95 / p99: ${['p50','p95','p99'].map(k=>number(c.collection_seconds_1h?.[k])).join(' / ')} с`,
    `Виявлення → перевірений архів, середня / p50 / p95 / p99: ${['mean','p50','p95','p99'].map(k=>number(c.delivery_seconds_1h?.[k])).join(' / ')} с`,
    `Черга результатів: ${number(r?.pending_results)} · Найстаріший результат: ${number(r?.oldest_pending_seconds)} с · Помилки доставки результатів: ${number(r?.dead_letters)}`,
    `Зарезервовано: ${number(r?.leased)} · CPU: ${number(r?.cpu_percent)}% · RAM: ${number(r?.memory_bytes==null?null:r.memory_bytes/1048576)} МіБ`,
    `Остання підтверджена доставка: ${date(r?.last_successful_delivery)} · Синхронізація Drive: ${r?.drive_sync||'невідомо'} · Помилок запису в поточній сесії: ${number(r?.archive_write_failures_this_session)}`,
    `Остання дата публікації: ${date(c.last_publication_at)} · SHA-256 виконуваних колекторів: ${r?.collection_sha256||'не виміряно'} · Задекларований deployment: ${r?.deployment_sha||'невідомо'} · Старт процесу: ${date(r?.worker_started_at)}`,
    `Запис приросту: ${number(r?.archive_write_seconds)} с · Drive за цикл, отримано / передано: ${number(r?.drive_transfer?.download_bytes)} / ${number(r?.drive_transfer?.upload_bytes)} байтів`,
    'Успішна порожня відповідь враховується як перевірка. Авторизація, robots і непідтримувані джерела виключені лише зі знаменника «доступних»; вони залишаються в загальній статистиці.',
    'Затримка доставки закінчується перевіркою архіву; час появи на екрані користувача не виміряний.'
  ],platforms:Object.entries(c.platforms||{}).map(([name,p])=>{
    const q=r?.platform_queues?.[name];
    const blocked=Object.entries(p.latest_statuses||{}).filter(([s])=>!['ok','no_new_posts','no_public_posts'].includes(s)).map(([s,n])=>`${reasons[s]||s}: ${number(n)}`).join('; ');
    return `${name}: ${stale?'Поточний стан не підтверджено':states[p.collection_state]||'Стан не визначено'} · успішно за 5 хв ${number(p.successful_sources_5m)}/${number(p.registered)} · перевірено за годину ${number(p.checked_sources_1h)} · нових за годину ${number(p.new_posts_1h??0)} · помилок ${number(p.errors_1h)} (${p.error_rate_1h==null?'—':number(100*p.error_rate_1h)+'%'}) · затримка до архіву ${number(p.delivery_seconds_1h?.mean)} с · останній успіх ${date(p.last_success_at)} · очікують ${number(q?.due)}, у роботі ${number(q?.leased)}, результатів до запису ${number(q?.pending_results)}${blocked?' · Обмеження: '+blocked:''}`;
  }),
  sources:Object.entries(c.sources||{}).map(([id,s])=>`${id} · ${s.status} · успіх: ${date(s.last_success_at)} · остання спроба: ${date(s.last_attempt_at)} · наступна перевірка: ${date(s.next_check_at)}`)};
}
