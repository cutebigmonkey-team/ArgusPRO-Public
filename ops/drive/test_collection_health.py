from datetime import datetime,timezone
import unittest
from pathlib import Path
import sys
sys.path.append(str(Path(__file__).resolve().parents[1]/'collect'))
from collection_health import summarize
from collection_health import percentiles


class LatencyTests(unittest.TestCase):
    def test_wall_metrics_do_not_equate_a_clean_exit_with_archived_content(self):
        from datetime import datetime,timezone
        now=datetime(2026,11,3,12,0,tzinfo=timezone.utc)
        url='https://ok.ru/group/7'
        catalog={'sources':[{'id':'g','platform':'ok','url':url}]}
        state={'incremental_polling':{'g':{'url':url,'last_checked_at':now.isoformat(),
            'recent_checks':[{'at':now.isoformat(),'status':'no_public_posts','verified':False},
                             {'at':'2026-11-03T11:35:00Z','status':'ok','verified':True},
                             {'at':'2026-11-03T09:00:00Z','status':'ok','verified':True}]}}}
        post={'id':'ok:7','source_id':'g','platform':'ok','first_archived_at':'2026-11-03T11:36:00Z',
              'published_at':'2026-11-03T11:32:00Z','discovered_at':'2026-11-03T11:34:00Z'}
        data=summarize(catalog,state,[post],now)['platforms_last_hour']['ok']
        self.assertEqual((data['measured_checks'],data['verified_content_checks'],data['fresh_dated_ids']),(2,1,1))
        self.assertEqual(data['detection_seconds']['p50'],120)

    def test_nearest_rank_reports_sample_size_and_empty_population(self):
        self.assertEqual(percentiles([]),{'n':0,'p50':None,'p90':None,'p99':None})
        self.assertEqual(percentiles(range(1,101)),{'n':100,'p50':50,'p90':90,'p99':99})


class HealthTests(unittest.TestCase):
    def test_overdue_success_remains_distinct_from_failure_and_counts_first_insert(self):
        when=datetime(2026,11,3,9,tzinfo=timezone.utc)
        catalog={'sources':[{'id':'a','platform':'rss','url':'https://example.org'}]}
        row={'url':'https://example.org','collector_status':'no_new_posts','last_success_at':'2026-11-03T08:00:00Z',
             'last_checked_at':'2026-11-03T08:00:00Z','next_check_at':'2026-11-03T08:05:00Z'}
        post={'source_id':'a','first_archived_at':'2026-11-03T08:30:00Z','published_at':'2026-11-03T08:20:00Z'}
        report=summarize(catalog,{'incremental_polling':{'a':row}},[post],when)
        self.assertEqual(report['counts'],{'DELAYED':1})
        self.assertEqual((report['sources']['a']['new_1h'],report['sources']['a']['new_24h']),(1,1))

    def test_missing_attempt_and_partial_progress_are_distinct(self):
        when = datetime(2026,11,3,9,tzinfo=timezone.utc)
        sources = [{'id':s,'platform':'rss','url':'https://example.org/'+s} for s in ('a','b')]
        row = {'url':sources[1]['url'],'collector_status':'ok','source_status':'Partial Results',
               'last_checked_at':when.isoformat(),'pipeline':{'unique_keys':4},'remaining_ranges':2}
        report = summarize({'sources':sources},{'incremental_polling':{'b':row}},[],when)
        self.assertEqual(report['counts'],{'NEVER_CHECKED':1,'PARTIAL':1})
        self.assertEqual(report['sources']['b']['last_pipeline']['unique_keys'],4)
