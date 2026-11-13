import io,json,unittest
from stream_json import load
from drive_store import encoded

class StreamingJSONTests(unittest.TestCase):
    def test_boundaries_unicode_large_values_and_shared_text(self):
        value={'items':[{'id':str(i),'text':('Публікація 🔥 " \\ '+str(i))*100,'content':('Публікація 🔥 " \\ '+str(i))*100} for i in range(7)],'packages':['p'],'number':123456789,'negative':-123.45e10,'flag':True,'nothing':None}
        for chunk in (1,7,65536):
            decoded=load(io.StringIO(encoded(value).decode()),chunk=chunk)
            self.assertEqual(decoded,value)
            self.assertIs(decoded['items'][0]['text'],decoded['items'][0]['content'])
    def test_consumer_verifies_without_retaining_array(self):
        seen=[];value={'items':[{'id':'a'},{'id':'b'}],'packages':[]}
        result=load(io.StringIO(json.dumps(value)),item_consumer=seen.append,chunk=2)
        self.assertEqual(seen,value['items']);self.assertEqual(result,{'items':[],'packages':[]})
    def test_malformed_duplicate_and_trailing_data_rejected(self):
        for value in ('{"items":[{},]}','{"items":[],}','{"items":[{"id":"a","id":"b"}]}','{"items":[],"items":[]}','{"items":[]} {}','{"items":[{"id":'):
            with self.subTest(value=value),self.assertRaises(ValueError):load(io.StringIO(value),chunk=3)
