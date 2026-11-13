import unittest
from public_surfaces import wall_surface
from browser_public import normalize_rows


class PublicSurfacesTests(unittest.TestCase):
    def test_reject_foreign_hosts_query_redirects_and_disallowed_widgets(self):
        for platform,url in (('vk','https://vk.ru/club123'),('ok','https://evil.test/group/123'),
                    ('ok','https://ok.ru/group/123?next=other'),('ok','https://user@ok.ru/group/123')):
            self.assertIsNone(wall_surface({'platform':platform,'url':url}))

    def test_ok_topics_does_not_change_catalogue_identity(self):
        from unittest.mock import patch
        import public_surfaces
        source={'id':'fixture','platform':'ok','url':'https://ok.ru/group/123456'}
        with patch.dict(public_surfaces.OK_ALIASES,{'123456':'fixture-group'},clear=True):
            url,scope=wall_surface(source)
            self.assertEqual(url,'https://ok.ru/fixture-group/topics')
            self.assertEqual(scope,source)
            rows=[{'url':'https://ok.ru/fixture-group/topic/111','groupId':'123456','text':'Public fixture'},
                  {'url':'https://ok.ru/other/topic/222','groupId':'999','text':'Foreign'},
                  {'url':'https://ok.ru/fixture-group/topic/333','groupId':'123456','privateCard':True,'text':'Private'}]
            self.assertEqual(len(normalize_rows(scope,rows,30)),1)

    def test_unverified_group_keeps_its_numeric_path(self):
        source={'platform':'ok','url':'https://ok.ru/group/123'}
        self.assertEqual(wall_surface(source),(source['url']+'/topics',source))
