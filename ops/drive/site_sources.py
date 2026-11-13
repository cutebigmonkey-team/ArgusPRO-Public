"""Read individual operator submissions without replacing another operator's work."""
import json
import re
from urllib.parse import parse_qsl, urlsplit

from registry import ENGINES
from source_origin import catalog_url

FILENAME = 'argus-site-source-v1.json'


def source_row(file):
    try:
        data = json.loads(file['description'])
        url = data['url']
        identity = catalog_url(url)
        if not identity or not re.fullmatch(r'[\w-]{10,200}', file['id']):
            return None
        platform, canonical = identity
        host = urlsplit(url).hostname
        if (platform not in ENGINES or ENGINES[platform] == 'unsupported'
                or not re.fullmatch(r'(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,63}', host)
                or host.endswith(('.localhost', '.local', '.internal', '.test', '.invalid'))
                or len(url) > 2048 or any(ord(c) <= 32 for c in url)
                or any(re.search(r'token|secret|password|auth|session|email|api.?key', key, re.I)
                       for key, _ in parse_qsl(urlsplit(url).query, keep_blank_values=True))
                or not isinstance(data['name'], str) or not 1 <= len(data['name'].strip()) <= 200
                or not isinstance(data['region'], str) or len(data['region']) > 120
                or data['origin'] not in ('general', 'dsns')):
            return None
        if platform == 'telegram' and not re.fullmatch(r'/[a-z0-9_]{3,64}', urlsplit(canonical).path):
            return None
        return {'id':'web-'+file['id'], 'platform':platform, 'name':data['name'].strip(),
                'url':url if platform in ('rss','mailru','youtube') else canonical,
                'origin':data['origin'], 'region':data['region'], 'enabled':True,
                'references':[{'kind':'site_submission'}]}
    except (KeyError, TypeError, ValueError, AttributeError, UnicodeError):
        return None


def with_submissions(store, catalog):
    from source_discovery import add_registered
    rows = []
    page = None
    while True:
        result = store.service.files().list(
            q=f"'{store.root}' in parents and trashed=false and name='{FILENAME}' and mimeType='application/json'",
            orderBy='createdTime', fields='nextPageToken,files(id,description,createdTime)',
            pageSize=1000, pageToken=page).execute(num_retries=3)
        rows.extend(result.get('files', []))
        page = result.get('nextPageToken')
        if not page:
            break
    rows.sort(key=lambda f:(f['createdTime'], f['id']))
    return add_registered(catalog, {'sources':[row for file in rows if (row := source_row(file))]})
