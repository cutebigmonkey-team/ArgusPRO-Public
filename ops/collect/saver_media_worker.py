"""Extract one public video's media metadata and optionally stage a bounded TikTok clip."""
import json
from pathlib import Path
import resource
import sys


def extract(root, url, expected, work, analyze):
    sys.path.insert(0, str(root / "saver_backend"))
    from saver_backend.services.downloaders.schema import VideoDTO
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
    from media_metadata import failure

    cap = 40 * 1024 * 1024
    resource.setrlimit(resource.RLIMIT_FSIZE, (cap, cap))
    if analyze:
        from tiktok_clip import extract as extract_clip
        return extract_clip(url, expected, work, VideoDTO)
    options = {"quiet": True, "noplaylist": True, "cachedir": False, "proxy": "", "socket_timeout": 12,
               "retries": 0, "extractor_retries": 0, "fragment_retries": 0, "max_filesize": cap,
               "format": "best",
               "allowed_extractors": ["TikTok", "Youtube", "Facebook", "Twitter", "Instagram"],
               "ignore_no_formats_error": True}
    try:
        with YoutubeDL(options) as ydl:
            info = ydl.extract_info(url, download=False)
            if not isinstance(info, dict) or info.get("_type", "video") != "video":
                return {"status": "unverified_video"}
            if str(info.get("id")) != expected:
                return {"status": "unverified_video"}
            dto = VideoDTO.from_yt_dlp(info)
            fields = {"title": dto.title, "duration": dto.duration, "width": dto.width, "height": dto.height,
                      "thumbnail": dto.thumbnail_url, "format_count": len(dto.formats)}
            result = {"status": "ok", "metadata": {k: v for k, v in fields.items() if v is not None}}
            return result
    except DownloadError as exc:
        return {"status": failure(str(exc))}


if __name__ == "__main__":
    root, url, expected, directory, analyze = sys.argv[1:]
    work = Path(directory)
    result = extract(Path(root), url, expected, work, analyze == "yes")
    (work / "metadata.json").write_text(json.dumps(result))
