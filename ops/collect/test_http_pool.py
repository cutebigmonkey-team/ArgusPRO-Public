import io
import unittest
import urllib.error
from unittest.mock import Mock,patch
import http_pool
import run


class TransportTests(unittest.TestCase):
    def response(self,status=200,body=b'ok'):
        value=Mock(status=status,headers={'Retry-After':'120'})
        value.read.return_value=body;value.isclosed.return_value=True
        return value

    def test_rate_limit_metadata_preserved_no_automatic_retry(self):
        response=self.response(429)
        with patch.object(http_pool.POOL,'request',return_value=response) as request:
            with self.assertRaises(urllib.error.HTTPError) as error:http_pool.request('https://example.com',{},4)
        self.assertEqual(error.exception.headers['Retry-After'],'120')
        self.assertFalse(request.call_args.kwargs['retries'])
        self.assertFalse(request.call_args.kwargs['redirect'])
        response.release_conn.assert_called_once()

    def test_redirect_checked_before_second_request(self):
        redirect=urllib.error.HTTPError('https://example.com',302,'redirect',{'Location':'http://127.0.0.1/'},io.BytesIO())
        with patch('http_pool.request',side_effect=redirect) as request,patch.object(run,'safe_url',side_effect=[None,ValueError('private_destination')]):
            with self.assertRaisesRegex(ValueError,'private_destination'):run.fetch('https://example.com')
        request.assert_called_once()

    def test_oversized_response_closed(self):
        response=self.response(body=b'a'*10);response.isclosed.return_value=False
        with patch.object(http_pool,'MAX_BODY',8),patch.object(http_pool.POOL,'request',return_value=response):
            with self.assertRaisesRegex(ValueError,'response_too_large'):http_pool.request('https://example.com',{},4)
        response.close.assert_called_once()
