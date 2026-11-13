"""On-demand public collectors. Reports separate provider outcomes from runner failures."""
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from tempfile import TemporaryDirectory
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import urlopen

OUT = Path("additional-output")
HERE = Path(__file__).resolve().parent
DEFAULT = {
    "instareelrag": "https://www.instagram.com/p/DevnTScCq1F/",
    "parth-dl": "https://www.instagram.com/p/DevnTScCq1F/",
    "granary": "https://www.instagram.com/fixture595",
    "grab-site": "https://www.iana.org/help/example-domains",
    "bluesky": "ДСНС",
}


def bluesky(target):
    feed = target.startswith("@")
    endpoint = "getAuthorFeed" if feed else "searchPosts"
    params = {"actor": target[1:]} if feed else {"q": target}
    params["limit"] = 10
    host = "public.api.bsky.app" if feed else "api.bsky.app"
    url = "https://" + host + "/xrpc/app.bsky.feed." + endpoint + "?" + urlencode(params)
    try:
        with urlopen(url, timeout=25) as response:
            data = json.load(response)
    except HTTPError as error:
        return {"status": "http_" + str(error.code), "method": endpoint}
    posts = [item["post"] for item in data["feed"]] if feed else data["posts"]
    rows = []
    for post in posts:
        did = post["author"]["did"]
        prefix = "at://" + did + "/app.bsky.feed.post/"
        if not post["uri"].startswith(prefix):
            raise ValueError("Invalid AT post identity")
        rows.append({"url": "https://bsky.app/profile/" + did + "/post/" + post["uri"][len(prefix):],
                     "uri": post["uri"], "author": post["author"]["handle"], "text": post["record"]["text"],
                     "published": post["record"]["createdAt"], "embed_type": (post.get("embed") or {}).get("$type"),
                     "metrics": {k: post.get(k) for k in ("likeCount", "replyCount", "repostCount", "quoteCount")}})
    return {"status": "ok" if rows else "no_public_posts", "method": endpoint, "rows": rows}


def instagram_media(target, video_only=False):
    from parth_dl import DownloadError, InstagramDownloader, NetworkError, RateLimitError
    match = re.fullmatch(r"https://(?:www\.)?instagram\.com/(?:p|reel)/([\w-]+)/?", target, re.ASCII)
    if not match:
        raise ValueError("Expected an Instagram post or Reel permalink")
    downloader = InstagramDownloader(quiet=True)
    try:
        info = downloader.get_info(target)
    except (DownloadError, NetworkError, RateLimitError) as error:
        return {"status": "media_unavailable", "reason": type(error).__name__}
    if info["id"] != match[1] or not any(e["formats"] for e in info["entries"]):
        raise ValueError("Missing or mismatched Instagram media")
    report = {"status": "ok", "url": target, **{k: info[k] for k in ("id", "title", "uploader", "type")},
              "media": [e["kind"] for e in info["entries"]]}
    entries = info["entries"][:10]
    if video_only:
        entries = [e for e in entries if e["kind"] == "video"][:1]
        if not entries:
            return {**report, "status": "no_video"}
    with TemporaryDirectory() as stage:
        try:
            files = downloader.download(target, stage, quality="worst", output_mode="directory",
                                        info={**info, "entries": entries})
        except (DownloadError, NetworkError, RateLimitError) as error:
            return {**report, "status": "download_unavailable", "reason": type(error).__name__}
        paths = [Path(p) for p in (files if isinstance(files, list) else [files])]
        if len(paths) != len(entries) or not all(p.stat().st_size for p in paths):
            raise ValueError("Incomplete media download")
        if video_only:
            report["rows"] = [speech_index(paths[0], info["title"], target, OUT)]
        else:
            for idx, path in enumerate(paths, 1):
                path.replace(OUT / (f"{match[1]}-{idx:02}" + path.suffix))
    return report


def normalize_profile(target):
    from remaining_http import PublicHTTP, StopCollection
    from remaining_social import normalize
    from remaining_worker import granary

    sources = json.loads((HERE / "sources.json").read_text(encoding="utf-8"))["sources"]
    source = next((s for s in sources if s.get("enabled", True) and s["url"].rstrip("/") == target.rstrip("/")
                   and s["platform"] in ("facebook", "instagram")), None)
    if not source:
        raise ValueError("Expected a Facebook or Instagram URL from the ARGUS catalog")
    try:
        rows = normalize(source, granary(source, PublicHTTP(source["platform"])), 5, "granary")
    except StopCollection as error:
        return {"status": error.status}
    return {"status": "ok" if rows else "no_public_posts", "rows": rows}


def archive_page(target):
    from warcio.archiveiterator import ArchiveIterator

    sources = json.loads((HERE / "sources.json").read_text(encoding="utf-8"))["sources"]
    allowed = {urlsplit(s["url"]).netloc for s in sources if s["platform"] == "rss" and s.get("enabled", True)}
    url = urlsplit(target)
    if target != DEFAULT["grab-site"] and (url.scheme != "https" or url.netloc not in allowed):
        raise ValueError("Expected an HTTPS page on an ARGUS catalog website")
    subprocess.run(["grab-site", "--1", "--no-sitemaps", "--no-offsite-links", "--no-video",
                    "--page-requisites-level=0", "--max-content-length=8388608",
                    "--wpull-args=--tries=1 --read-timeout=15",
                    "--dir=capture", target], timeout=110, check=True)
    records = []
    for warc in Path("capture").glob("*.warc.gz"):
        keep = False
        with warc.open("rb") as stream:
            for record in ArchiveIterator(stream):
                if record.rec_type == "response":
                    uri = record.rec_headers.get_header("WARC-Target-URI")
                    status = int(record.http_headers.get_statuscode())
                    records.append({"url": uri, "http": status})
                    if uri == target and 200 <= status < 300 and record.content_stream().read(1):
                        keep = True
        if keep:
            warc.rename(OUT / warc.name)
    return {"status": "ok" if any(OUT.glob("*.warc.gz")) else "no_target_capture", "responses": records}


def speech_index(video, caption, url, output):
    from config import ECOSYSTEM
    from faster_whisper import WhisperModel
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    # Load only the upstream keyword index on CPU, without importing its GPU/vector stack.
    import bm25_index

    audio = output / "speech.mp3"
    transcript = ""
    if ECOSYSTEM.execute_ffmpeg_audio_extraction(str(video), str(audio)):
        segments, _ = WhisperModel("tiny", device="cpu", compute_type="int8", cpu_threads=2).transcribe(str(audio))
        transcript = " ".join(segment.text.strip() for segment in segments).strip()
        audio.unlink()
    content = (caption + " " + transcript).strip()
    if content:
        with Session(create_engine("sqlite:///" + str(output / "reels.sqlite"))) as session:
            bm25_index.BM25Index(session).add_documents([content], [url])
    return {"url": url, "caption": caption, "transcript": transcript, "indexed": bool(content)}


def reels_profile(target):
    import instaloader.exceptions as ig
    from scraper.ig_scraper import IGScraper
    if target.startswith("https://"):
        return instagram_media(target, video_only=True)
    if not re.fullmatch(r"[A-Za-z0-9_.]{1,30}", target):
        raise ValueError("Expected an Instagram username")
    scraper = IGScraper()
    scraper.L.context.max_connection_attempts = 1
    try:
        items = scraper.scrape_profile(target, max_posts=1)
    except ig.InstaloaderException as error:
        return {"status": "instagram_unavailable", "reason": type(error).__name__}
    rows = [speech_index(item["video_path"], item["caption"], item["url"], OUT) for item in items]
    return {"status": "ok" if rows else "no_public_reels", "rows": rows}


if __name__ == "__main__":
    tool = sys.argv[1]
    target = os.environ.get("TARGET", "").strip() or DEFAULT[tool]
    if len(target) > 1024 or any(ord(c) < 32 for c in target):
        raise ValueError("Invalid target")
    OUT.mkdir(exist_ok=True)
    handlers = {"bluesky": bluesky, "parth-dl": instagram_media, "granary": normalize_profile,
                "grab-site": archive_page, "instareelrag": reels_profile}
    result = handlers[tool](target)
    report = {"tool": tool, "target": target, **result}
    (OUT / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a", encoding="utf-8") as summary:
        summary.write(f"**{tool}** · {result['status']}\n")
