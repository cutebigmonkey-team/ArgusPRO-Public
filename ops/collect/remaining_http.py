"""Anonymous, no-retry HTTP shared by extra collectors."""
import time
import re
from urllib.parse import urlsplit, urljoin
import requests


class StopCollection(BaseException):
    # Upstream broad `except Exception` blocks must not retry access restrictions.
    def __init__(self, status):
        self.status = status


class PublicHTTP:
    def __init__(self, platform, budget=5, check_html_login=True):
        from run import HOSTS
        self.hosts = set(HOSTS[platform]) | ({'i.instagram.com'} if platform == 'instagram' else set())
        self.budget = budget
        self.platform = platform
        self.last = 0
        self.check_html_login = check_html_login

    def request(self, url, method='GET', headers=None, data=None):
        from run import safe_url
        for _ in range(4):
            if self.budget <= 0:
                raise StopCollection('request_budget')
            self.budget -= 1
            parsed = urlsplit(url)
            if parsed.scheme != 'https' or parsed.hostname not in self.hosts:
                raise StopCollection('unexpected_destination')
            if any(x in parsed.path.lower() for x in ('/login', '/checkpoint', '/challenge', '/accounts/login')):
                raise StopCollection('login_required')
            safe_url(url)
            time.sleep(max(0, 1 - (time.monotonic() - self.last)))
            self.last = time.monotonic()
            clean = {k:v for k,v in (headers or {}).items()
                     if k.lower() not in ('cookie', 'authorization', 'proxy-authorization', 'host')}
            clean.setdefault('User-Agent', 'Mozilla/5.0 ArgusPublicCollector/1.0')
            with requests.Session() as session:
                session.trust_env = False
                with session.request(method, url, headers=clean, data=data, timeout=(8, 12),
                                     allow_redirects=False, stream=True) as response:
                    if response.status_code in (401, 403, 429):
                        raise StopCollection('http_' + str(response.status_code))
                    if response.status_code in (301, 302, 303, 307, 308):
                        url = urljoin(url, response.headers.get('Location', ''))
                        if response.status_code in (301, 302, 303):
                            method, data = 'GET', None
                        continue
                    if response.status_code != 200:
                        raise StopCollection('http_' + str(response.status_code))
                    chunks, size = [], 0
                    for chunk in response.iter_content(65536):
                        size += len(chunk)
                        if size > 4*1024**2:
                            raise StopCollection('response_too_large')
                        chunks.append(chunk)
                    raw = b''.join(chunks)
                    charset = re.search(r'charset=["\s]*([A-Za-z0-9_-]+)', response.headers.get('Content-Type',''), re.I)
                    encoding = charset[1] if charset else 'utf-8'
                    try:
                        body = raw.decode(encoding)
                    except (UnicodeDecodeError, LookupError):
                        body = raw.decode('cp1251' if self.platform == 'vk' else 'utf-8', errors='replace')
                    low = body.lower()
                    if any(x in low for x in ('"login_required"', '"challenge_required"')):
                        raise StopCollection('login_required')
                    if self.check_html_login and any(x in low for x in ('name="login"', 'id="login_form"')):
                        raise StopCollection('login_required')
                    return body
        raise StopCollection('redirect_limit')
