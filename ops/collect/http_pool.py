"""Bounded keep-alive transport; redirects remain the caller's policy."""
import io
import ssl
import urllib.error
import urllib3
import time
from contextvars import ContextVar

POOL=urllib3.PoolManager(num_pools=16,maxsize=2,block=True,cert_reqs='CERT_REQUIRED')
MAX_BODY=8*1024**2
request_metrics=ContextVar('pooled_http_metrics',default=None)


def request(url,headers,timeout,*,response_headers=None):
    response=None
    started=time.monotonic();metrics=request_metrics.get()
    if metrics is not None:metrics['requests']+=1
    try:
        response=POOL.request('GET',url,headers=headers,redirect=False,retries=False,
                              timeout=urllib3.Timeout(connect=min(5,timeout),read=timeout,total=timeout),
                              pool_timeout=timeout,preload_content=False)
        body=response.read(MAX_BODY+1,decode_content=True)
        if metrics is not None:metrics['bytes']+=len(body)
        if response_headers is not None:
            response_headers.update({k.lower():v for k,v in response.headers.items() if k.lower() in ('etag','last-modified')})
        if len(body)>MAX_BODY:raise ValueError('response_too_large')
        if response.status>=300:
            raise urllib.error.HTTPError(url,response.status,'HTTP response',response.headers,io.BytesIO(body[:65536]))
        return body,url
    except urllib3.exceptions.SSLError as error:
        raise ssl.SSLError('certificate_validation_failed') from error
    except urllib3.exceptions.TimeoutError as error:
        raise TimeoutError('http_deadline') from error
    except urllib3.exceptions.HTTPError as error:
        raise urllib.error.URLError('connection_failed') from error
    finally:
        if metrics is not None:metrics['seconds']+=time.monotonic()-started
        if response is not None:
            # Unread oversized/failed responses must not poison pooled sockets.
            if not response.isclosed():response.close()
            response.release_conn()
