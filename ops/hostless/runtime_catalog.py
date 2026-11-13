"""Read private source configuration without a shipped production catalog."""
import json
import os
from pathlib import Path

MAX_BYTES=8*1024**2


class PrivateCatalogError(ValueError):
    """Only fixed safe codes cross the private-configuration boundary."""


def safe_read(reader):
    try:return reader()
    except (json.JSONDecodeError,UnicodeError):
        raise PrivateCatalogError('private_catalog_invalid_json') from None
    except Exception as error:
        status=getattr(error,'code',None) or getattr(getattr(error,'resp',None),'status',None)
        if isinstance(error,PermissionError) or status in (401,403) or type(error).__name__ in {'RefreshError','DefaultCredentialsError','AccessTokenRefreshError'}:
            code='private_catalog_access_denied'
        elif status==429:code='private_catalog_rate_limited'
        else:code='private_catalog_unavailable'
        raise PrivateCatalogError(code) from None



def load_private_catalog(store,with_submissions):
    configured=os.environ.get('ARGUS_PRIVATE_SOURCE_FILE')
    if configured:
        path=Path(configured)
        project=Path(__file__).resolve().parents[2]
        if (not path.is_absolute() or path.resolve().is_relative_to(project)
                or not path.is_file() or path.stat().st_size>MAX_BYTES):
            raise ValueError('invalid_private_source_file')
        with path.open('rb') as stream:raw=stream.read(MAX_BYTES+1)
        if len(raw)>MAX_BYTES:raise ValueError('private_catalog_too_large')
        catalog=safe_read(lambda:json.loads(raw))
    else:
        catalog=safe_read(lambda:store.read_json('sources.json'))
    if not isinstance(catalog,dict) or not isinstance(catalog.get('sources'),list):
        raise ValueError('private_source_registry_missing')
    if not catalog['sources']:raise PrivateCatalogError('private_source_registry_empty')
    catalog=safe_read(lambda:with_submissions(store,catalog))
    if not isinstance(catalog,dict):raise PrivateCatalogError('invalid_private_source_registry')
    rows=catalog.get('sources')
    if not isinstance(rows,list) or not rows:raise ValueError('private_source_registry_empty')
    ids=set()
    for row in rows:
        if (not isinstance(row,dict) or any(not isinstance(row.get(k),str) or not row[k] for k in ('id','url','platform'))
                or row['id'] in ids):raise ValueError('invalid_private_source_registry')
        ids.add(row['id'])
    minimum=os.environ.get('ARGUS_MIN_PRIVATE_SOURCES','')
    if os.environ.get('ARGUS_RUNTIME_MODE','preflight')=='collect' and (not minimum.isdigit() or int(minimum)<1):
        raise ValueError('private_source_completeness_confirmation_required')
    if minimum and (not minimum.isdigit() or len(rows)<int(minimum)):
        raise ValueError('private_source_registry_incomplete')
    return catalog
