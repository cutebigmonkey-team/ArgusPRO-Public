"""Strict full regression gate in the existing disposable Hostless build stage."""
import os
import json
import subprocess
from pathlib import Path
import sys
import unittest

OPS=Path(__file__).resolve().parents[1]
SUITES=('hostless','drive','collect')


# Exact hosted-only tests. These must pass separately in hosted-adapters.yml.
HOSTED_ONLY = frozenset("""
collect.test_cloud_social.SocialTests.test_real_radar_parser_when_installed
collect.test_cloud_social.SocialTests.test_freshrss_parser_when_installed
collect.test_cloud_social.SocialTests.test_browser_launches_inside_sanitized_worker_environment
collect.test_discovery.DiscoveryTests.test_actual_changedetection_baseline_change_and_replay
collect.test_embed.PinnedParserTests.test_actual_instagramprofiles_parser
collect.test_embed.PinnedParserTests.test_actual_rss_bridge_embedded_timeline_parser
collect.test_embed.PinnedParserTests.test_actual_gallery_and_web_modules_no_media_download
collect.test_embed.PinnedParserTests.test_actual_gallery_web_anonymous_settings
collect.test_facebook_public.FacebookPublicTests.test_real_browser_markup
collect.test_facebook_x.PublicReaders.test_numeric_post_returned_with_opaque_permalink
collect.test_facebook_x.PublicReaders.test_article_requires_matching_author_and_status
collect.test_remaining.UpstreamTests.test_actual_skraper_parsers_four_platforms
collect.test_remaining.UpstreamTests.test_actual_socialfetch_feed_parser
collect.test_remaining.UpstreamTests.test_actual_fbgql_discovery_and_feed_parser
collect.test_remaining.UpstreamTests.test_actual_granary_instagram_converter
collect.test_remaining.UpstreamTests.test_actual_granary_facebook_permalink_and_attachments
collect.test_remaining.UpstreamTests.test_actual_vkrss_generator
""".split())


def verified_suite_result(name, result):
    if not result.wasSuccessful():
        return False
    skipped = [test.id() for test, _ in result.skipped]
    if len(skipped) != len(set(skipped)):
        return False
    if name != 'collect':
        return not skipped
    return set(skipped) == HOSTED_ONLY


def suite_path(name):
    if name not in SUITES:raise ValueError('invalid_release_suite')
    selected=str(OPS/name)
    # The selected test directory MUST precede helper paths. Discover() does not
    # promote a directory already present in sys.path, so broad PYTHONPATH can
    # otherwise import another directory's test_comments/test_registry module.
    # Parent packages anchor test imports even when legacy modules alter sys.path.
    helpers=[str(OPS/p) for p in ('hostless','drive','collect','local-storage','ai') if p!=name]
    sys.path[:]=[str(OPS),selected,*helpers,*[p for p in sys.path if p not in [str(OPS),selected,*helpers]]]
    return selected


def run_suites(run=subprocess.run):
    failure=0
    for name in SUITES:
        try:
            result=run([sys.executable,str(Path(__file__).resolve()),'--suite',name],
                       cwd=OPS.parent,timeout=900)
            code=result.returncode if result.returncode>=0 else 128-result.returncode
        except subprocess.TimeoutExpired:code=124
        print(json.dumps({'event':'release_suite_completed','suite':name,'exit_code':code}),flush=True)
        if not failure and code:failure=code
    return failure

if __name__=='__main__':
    # Prevent a green image with silently skipped PostgreSQL safety tests.
    if not os.getenv('ARGUS_COMMENTS_TEST_DSN'):raise SystemExit('disposable_postgres_required')
    import psycopg
    try:
        with psycopg.connect(os.environ['ARGUS_COMMENTS_TEST_DSN']) as db:
            if db.execute('SELECT current_database()').fetchone()[0]!='argus_comments_test':
                raise SystemExit('disposable_postgres_required')
    except psycopg.Error:
        raise SystemExit('disposable_postgres_unavailable') from None
    if len(sys.argv)==3 and sys.argv[1]=='--suite':
        name=sys.argv[2]
        directory=suite_path(name)
        suite=unittest.TestLoader().discover(directory,pattern='test_*.py',top_level_dir=str(OPS))
        result=unittest.TextTestRunner(verbosity=2).run(suite)
        raise SystemExit(0 if verified_suite_result(name, result) else 1)
    raise SystemExit(run_suites())
