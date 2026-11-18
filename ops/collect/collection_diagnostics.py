"""Allowlisted public diagnostics. Never serialize exception text or traceback paths."""
from pathlib import Path

STATUSES = frozenset('''ok no_new_posts no_public_posts partial_results publisher_only
 deferred_budget deferred_rate_limit disabled_by_registry not_due rate_limited
 gateway_rate_limited login_required challenge_required robots_denied
 robots_challenge_required robots_invalid_content robots_cross_origin robots_redirect_limit
 timeout parser_error collection_failed collector_failed unreported
 tls_certificate_error tls_error dns_error network_error connection_reset connection_refused
 runtime_type_error runtime_key_error runtime_name_error runtime_attribute_error
 runtime_unbound_local_error dependency_missing internal_import_error circular_import_error internal_module_missing browser_error source_deadline_exceeded
 invalid_source invalid_url unexpected_destination private_destination invalid_channel
 channel_id_unresolved channel_id_unavailable channel_id_unverified invalid_channel_id
 unverified_markup no_verified_page no_verified_posts stale_feed_only tool_unavailable
 unavailable not_exposed unsupported_platform response_too_large redirect_limit
 unexpected_article_redirect cross_origin_redirect not_modified missing_dependency
 incomplete_collection parser_failed extraction_failed gateway_unavailable
 partial_telegram_cards more_public_history public_history_exhausted empty_or_unreadable_feed
 no_verified_page no_verified_posts no_public_feed channel_owner_unresolved channel_id_unresolved
 feed_entry_owner_mismatch feed_owner_mismatch invalid_json_feed invalid_upload_cursor
 invalid_upload_page playlist_owner_mismatch pinned_channel_owner_changed pagination_stalled
 invalid_response unexpected_redirect unexpected_video_redirect unexpected_telegram_redirect
 feed_continuation_overflow unverified_markup crawler_worker_failed crawl_budget_reached
 source_access_restricted source_rate_limited robots_delay_exceeds_budget
 gateway_busy gateway_cooldown tool_missing dependency_unavailable unsupported not_configured
 unsupported_source_shape unsupported_target upstream_rejected upstream_revision_mismatch
 public_date_not_verified video_owner_mismatch invalid_video_metadata invalid_media_source
 invalid_crawl_bounds invalid_page_budget invalid_history_cursor
'''.split()) | frozenset(f'http_{n}' for n in range(100, 600)) | frozenset(f'robots_http_{n}' for n in range(100, 600))
TOOLS = frozenset('''anonymous_policy detail_enrichment enhanced_web gdelt_public_json
 instaloader playwright_article public_embed publisher_tiktok_embed rss_bridge rsshub
 source_deadline source_execution source_validation telegram_public_head telegram_public_history
 threads_public_json tinyfish_fetch yt-dlp sipher tiktok-cli feeds playwright gallery-dl
 granary fbgql skraper vkrss socialfetch public_feed_or_page public_feed_history
 publisher_news_sitemap publisher_headlines public_post_metrics youtube_public_history
 youtube_channel_rss pravaha facebook-scraper instagramprofiles rss-bridge gallery-dl-web
'''.split())
PHASES = frozenset(('extraction', 'enrichment', 'normalization', 'source_deadline', 'unknown'))
TYPES = frozenset('''HTTPError URLError TimeoutError SSLError SSLCertVerificationError
 gaierror ConnectionResetError ConnectionRefusedError ValueError TypeError KeyError
 NameError AttributeError UnboundLocalError ImportError ModuleNotFoundError Error
 RuntimeError OSError'''.split())
MODULES = frozenset(('run.py', 'feed_history.py', 'youtube_history.py', 'telegram_history.py',
                     'post_metrics.py', 'http_pool.py', 'crawl_common.py', 'browser_public.py'))


def public_status(value):
    return value if isinstance(value, str) and value in STATUSES else 'other_collector_error'


def public_tool(value):
    return value if isinstance(value, str) and value in TOOLS else 'other_reader'


# Only public, known module names may appear in diagnostics, never arbitrary
# ImportError.name values or exception messages.
INTERNAL_IMPORTS = frozenset(('source_adapters', 'cloud_social', 'post_metrics', 'run',
    'http_pool', 'feed_history', 'youtube_history', 'telegram_history', 'candidate_audit',
    'record_quality', 'url_identity', 'source_health', 'crawl_common', 'youtube_feed',
    'browser_public', 'remaining_http', 'collection_diagnostics', 'collector_import_smoke',
    'embed_social', 'remaining_social', 'rss_gateway', 'threads_public', 'public_embeds',
    'media_metadata', 'gdelt_public', 'runtime_policy', 'tinyfish_fetch', 'x_api', 'depth',
    'source_admission', 'platform_workers', 'browser_pool', 'storage'))
IMPORT_MODULES = INTERNAL_IMPORTS | frozenset(('requests', 'urllib3', 'bs4', 'feedparser',
    'playwright', 'trafilatura', 'yt_dlp', 'psycopg', 'gallery_dl', 'gallery_dl_web',
    'fbgql', 'granary', 'certifi', 'charset_normalizer', 'idna'))


def import_failure_code(error):
    name = getattr(error, 'name', None)
    root = name.split('.', 1)[0] if isinstance(name, str) else None
    if isinstance(error, ModuleNotFoundError):
        return 'internal_module_missing' if root in INTERNAL_IMPORTS else 'dependency_missing'
    # Inspect CPython's standard circular-import marker internally only; the
    # actual exception text never leaves this process.
    if 'partially initialized module' in str(error):
        return 'circular_import_error'
    return 'internal_import_error'


def diagnostic(error, code, phase='unknown'):
    row = {'code': public_status(code),
           'exception_type': type(error).__name__ if type(error).__name__ in TYPES else 'OtherException',
           'phase': phase if phase in PHASES else 'unknown'}
    if isinstance(error, ImportError):
        name = getattr(error, 'name', None)
        root = name.split('.', 1)[0] if isinstance(name, str) else None
        row['import_module'] = root if root in IMPORT_MODULES else 'NOT_VERIFIED'
    tb = error.__traceback__
    while tb:
        path = Path(tb.tb_frame.f_code.co_filename)
        if path.name in MODULES and path.parent.resolve() == Path(__file__).resolve().parent:
            row.update(module=path.name, line=tb.tb_lineno)
        tb = tb.tb_next
    return row
