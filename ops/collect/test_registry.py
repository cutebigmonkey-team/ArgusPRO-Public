import copy,io,json,unittest,urllib.error
from unittest.mock import patch
from registry import default_registry,validate,fetch_registry,NoRedirect

class RegistryTests(unittest.TestCase):
    def test_remote_registry_cannot_add_commands_secrets_or_activate_accounts(self):
        base=default_registry();self.assertEqual(len(validate(base)['collectors']),15)
        for mutate in (lambda r:r.update(command='curl attacker'),lambda r:r.update(cookies='secret'),
                       lambda r:r['execution'].update(max_posts=500),
                       lambda r:next(c for c in r['collectors'] if c['platform']=='x')['optional_adapter'].update(enabled=True)):
            value=copy.deepcopy(base);mutate(value)
            with self.assertRaises(ValueError):validate(value)

    def test_public_bin_and_duplicate_collectors_are_rejected(self):
        value=default_registry();value['collectors'].append(value['collectors'][0])
        with self.assertRaises(ValueError):validate(value)
        response=io.BytesIO(json.dumps({'metadata':{'private':False},'record':default_registry()}).encode())
        with patch('urllib.request.OpenerDirector.open',return_value=response):
            with self.assertRaisesRegex(ValueError,'private'):fetch_registry('a'*24,'not-real-key')

    def test_error_body_and_credentials_are_not_exposed(self):
        error=urllib.error.HTTPError('https://api.jsonbin.io',403,'Forbidden',{},io.BytesIO(b'secret-cookie'))
        with patch('urllib.request.OpenerDirector.open',side_effect=error):
            with self.assertRaisesRegex(RuntimeError,'^JSONBin HTTP 403$'):fetch_registry('a'*24,'not-real-key')
        with self.assertRaisesRegex(ValueError,'redirect'):NoRedirect().redirect_request(None,None,302,None,None,'https://elsewhere.test')
