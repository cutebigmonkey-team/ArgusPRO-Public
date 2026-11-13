"""Native anonymous feed surfaces; source identity remains the catalogue URL."""
import re
from urllib.parse import urlsplit

# Public group aliases verified against each page's canonical numeric group.
from private_assets import private_asset_path
import json
OK_ALIASES = json.loads(private_asset_path('ok-aliases.json').read_text())


def wall_surface(source):
    platform=source.get('platform'); p=urlsplit(source.get('url',''))
    if p.scheme!='https' or p.username or p.password or p.query or p.fragment:
        return None
    path=p.path.strip('/')
    # VK's readable widget is excluded: its robots policy disallows /widget_*.
    if platform=='ok' and p.hostname in {'ok.ru','www.ok.ru'} and re.fullmatch(r'group/\d+',path):
        public_path=OK_ALIASES.get(path.split('/')[1],path)
        return ('https://ok.ru/'+public_path+'/topics',source)
    return None
