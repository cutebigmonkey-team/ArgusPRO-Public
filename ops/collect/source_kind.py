"""URL scope is separate from successful extraction of an individual post."""
import re
from urllib.parse import urlsplit


def kind(source):
    url=urlsplit(source['url']);platform=source.get('platform');path=url.path
    if (platform=='youtube' and (path in ('/watch',) or path.startswith(('/shorts/','/embed/')))) or (
        platform=='tiktok' and re.search(r'/video/\d+',path)) or (
        platform=='rutube' and '/video/' in path) or (
        platform=='mailru' and '/article/' in path) or (
        platform=='telegram' and re.fullmatch(r'/[^/]+/\d+/?',path)):
        return 'single_publication'
    if platform=='tiktok' and url.hostname in {'vt.tiktok.com','vm.tiktok.com'}:return 'unresolved_short_link'
    return 'publisher'


def video_scope(source):
    """Descriptive registry audit only; never rewrites URLs or resets cursors."""
    url=urlsplit(source['url']);path=url.path;platform=source.get('platform')
    if platform=='rutube' and path.startswith('/live/video/'):return 'livestream'
    if platform=='youtube':
        if url.hostname=='youtu.be' or path=='/watch' or path.startswith(('/shorts/','/embed/','/live/')):return 'video'
        if path=='/playlist':return 'playlist'
        if path=='/feeds/videos.xml':return 'publication_feed'
        if path.startswith(('/channel/','/c/','/user/','/@')):return 'channel'
    if platform=='rutube':
        if '/video/' in path:return 'video'
        if path.startswith('/plst/'):return 'playlist'
        if path.startswith(('/channel/','/u/')):return 'channel'
    if platform=='tiktok':
        if url.hostname in {'vt.tiktok.com','vm.tiktok.com'}:return 'unresolved_short_link'
        if '/video/' in path:return 'video'
        if re.fullmatch(r'/@[^/]+/?',path):return 'channel'
    return 'not_classified'
