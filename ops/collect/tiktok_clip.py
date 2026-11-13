"""Read a verified public TikTok post and its bounded CDN video in one anonymous session."""
from http.cookiejar import CookieJar, DefaultCookiePolicy
import http.client
import json
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit

CAP = 40 * 1024 * 1024
PAGE_CAP = 5 * 1024 * 1024
CDNS = ("tiktok.com", "tiktokcdn.com", "tiktokcdn-us.com", "tiktokcdn-eu.com", "tiktokv.com",
        "tiktokv.us", "byteoversea.com", "ibytedtos.com", "muscdn.com")
USER_AGENT = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
ORIGIN = "https://www.tiktok.com/"


def cdn_url(url):
    try:
        u = urlsplit(url)
        return (u.scheme == "https" and not u.username and not u.password and u.port in (443, None)
                and any(u.hostname == domain or (u.hostname or "").endswith("." + domain) for domain in CDNS))
    except ValueError:
        return False


class CDNRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not cdn_url(newurl):
            raise ValueError("unexpected_destination")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def public_session():
    # The page sets domain-scoped cookies for its signed playback link. Keep
    # them only for this clip and let CookieJar scope them on every redirect.
    jar = CookieJar(DefaultCookiePolicy(strict_ns_domain=DefaultCookiePolicy.DomainStrictNonDomain))
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), CDNRedirect(),
                                       urllib.request.HTTPCookieProcessor(jar))


def download(session, url, target):
    if not url or not cdn_url(url):
        return "no_public_stream"
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": ORIGIN,
                                               "Accept": "*/*", "Range": "bytes=0-"})
    try:
        with session.open(req, timeout=12) as resp:
            if resp.headers.get_content_type() not in ("video/mp4", "application/octet-stream", "video/webm"):
                return "unexpected_media_type"
            length = int(resp.headers.get("Content-Length") or 0)
            if resp.status == 206:
                span = re.fullmatch(r"bytes 0-(\d+)/(\d+)", resp.headers.get("Content-Range", ""))
                if not span or int(span[1]) + 1 != int(span[2]):
                    return "partial_stream"
                length = int(span[2])
            if length > CAP:
                return "outside_video_bounds"
            with target.open("wb") as out:
                while chunk := resp.read(131072):
                    if out.tell() + len(chunk) > CAP:
                        return "outside_video_bounds"
                    out.write(chunk)
                if length and out.tell() != length:
                    return "partial_stream"
        return "ok" if target.stat().st_size else "empty_stream"
    except urllib.error.HTTPError as exc:
        return "http_" + str(exc.code)
    except (ValueError, OSError, http.client.HTTPException):
        return "stream_unavailable"


def extract(url, expected, work, dto_type):
    owner = urlsplit(url).path.split("/")[1]
    url = ORIGIN + owner + "/video/" + expected
    session = public_session()
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Referer": ORIGIN, "Accept": "text/html"})
    try:
        with session.open(req, timeout=12) as response:
            page = response.read(PAGE_CAP + 1)
        if len(page) > PAGE_CAP:
            return {"status": "response_too_large"}
        script = re.search(rb'<script\b[^>]*\bid=["\']__UNIVERSAL_DATA_FOR_REHYDRATION__["\'][^>]*>(.*?)</script>',
                           page, re.S)
        if not script:
            return {"status": "no_public_video_data"}
        detail = json.loads(script[1])["__DEFAULT_SCOPE__"]["webapp.video-detail"]
        row = (detail.get("itemInfo") or {}).get("itemStruct") or {}
        author = (row.get("author") or {}).get("uniqueId", "")
        if row.get("id") != expected or "@" + author.lower() != owner.lower():
            return {"status": "unverified_video"}
        video = row.get("video") or {}
        dto = dto_type(source_id=expected, url=url, title=row.get("desc"), duration=video.get("duration"),
                       width=video.get("width"), height=video.get("height"), thumbnail_url=video.get("cover"))
    except urllib.error.HTTPError as exc:
        return {"status": "http_" + str(exc.code)}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {"status": "invalid_public_video_data"}
    except (OSError, http.client.HTTPException):
        return {"status": "network_error"}
    fields = {"title": dto.title, "width": dto.width, "height": dto.height, "thumbnail": dto.thumbnail_url,
              "duration": dto.duration, "provider": "tiktok-public-page"}
    status = "outside_video_bounds"
    if dto.duration and 0 < dto.duration <= 120:
        status = download(session, video.get("playAddr") or video.get("downloadAddr"), work/"video.mp4")
    return {"status": "ok", "metadata": {k: v for k, v in fields.items() if v is not None}, "clip_status": status}
