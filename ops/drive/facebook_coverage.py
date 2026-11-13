"""Produce a source-by-source report from the independently verified run package."""
STOPS = {
    'no_further_visible_posts':'після прокручування нових дописів не з’явилося',
    'no_visible_posts':'придатних публічних дописів не виявлено',
    'login_required':'перехід на сторінку входу',
    'login_prompt':'показано запрошення увійти, дописів не виявлено',
    'server_end':'Facebook повідомив про кінець цієї стрічки',
    'http_429':'Facebook обмежив кількість запитів (HTTP 429)',
    'http_403':'Facebook відмовив у доступі (HTTP 403)',
    'http_401':'потрібна авторизація (HTTP 401)',
    'challenge_required':'Facebook показав перевірку відвідувача',
    'more_advertised_without_progress':'Facebook позначив наявність продовження, але не завантажив його',
    'time_budget':'вичерпано час перевірки джерела',
    'timeout':'перевищено час очікування відповіді',
    'deferred_rate_limit':'запит відкладено після обмеження частоти',
}
SURFACES = {'timeline':'Стрічка','page_plugin':'Віджет','permalink':'Сторінка допису'}


def qualified(post):
    return bool(post.get('text') and post.get('published_at') and not post.get('text_may_be_truncated',True)
                and post.get('facebook_text_source') in ('story_message','expanded_card'))


def render(run_id, sources, posts):
    multi = [s for s in sources if len(set(s['canonical_keys']))>1]
    totals = {'sources':len(sources),'sources_with_posts':sum(bool(s['posts']) for s in sources),
              'unique_posts':len(posts),'with_date':sum(bool(p.get('published_at')) for p in posts.values()),
              'full_text_and_date':sum(qualified(p) for p in posts.values()),'multiple_sources':len(multi),
              'media_only':sum(not p['text'] and bool(p.get('media_urls')) for p in posts.values())}
    lines = ['ARGUS · Facebook · публічний збір без акаунта', 'Запуск '+run_id, '',
             f"Джерел перевірено — {len(sources)}. Джерел із дописами — {totals['sources_with_posts']}.",
             f"Унікальних дописів — {len(posts)}. Із повним текстом і датою — {totals['full_text_and_date']}.",
             f"Джерел із двома або більше дописами — {len(multi)}.",'']
    if multi:
        lines.extend(f"{s['url']} — {len(set(s['canonical_keys']))} дописів" for s in multi)
    lines.extend(['','Перевірка кожного джерела',''])
    for i,s in enumerate(sorted(sources,key=lambda s:s['url']),1):
        keys=set(s['canonical_keys'])
        count=sum(qualified(posts[k]) for k in keys)
        lines.extend([f"{i}. {s['name']}",s['url'],f"Дописів — {len(keys)}. З повним текстом і датою — {count}."])
        for attempt in s.get('attempts',[]):
            for surface in attempt.get('surfaces',[]):
                reason=surface['stop']
                lines.append(f"{SURFACES.get(surface['surface'],surface['surface'])} — {STOPS.get(reason,reason)}.")
        if not s.get('attempts'):
            lines.append(STOPS.get(s['status'],s['status']))
        for key in sorted(keys):
            post=posts[key]
            label='повний текст і дата' if qualified(post) else 'лише медіа' if not post['text'] else 'неповні дані'
            lines.append(f"  {label} · {post.get('published_at') or 'дата невідома'} · {post['post_url']}")
        lines.append('')
    return totals,'\n'.join(lines).encode('utf-8')
