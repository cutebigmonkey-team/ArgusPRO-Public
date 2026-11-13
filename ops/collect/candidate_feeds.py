"""Anonymous feed adapters and source-bound records for the scheduled collector."""
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import urlsplit

from cloud_social import process, require_cloud, threads_rows, threads_target, tool_root
from remaining_social import normalize

PINS = json.loads(Path(__file__).with_name("candidate-upstreams.json").read_text())
TIKTOK_PATH = re.compile(r"/@([A-Za-z0-9_.-]{1,64})(?:/video/(\d{8,22}))?/?")


def tiktok_target(url):
    u = urlsplit(url)
    if u.scheme != "https" or u.netloc not in ("www.tiktok.com", "tiktok.com"):
        return None
    return TIKTOK_PATH.fullmatch(u.path)


def tiktok_rows(source, rows, limit):
    target = tiktok_target(source["url"])
    accepted = []
    for row in rows:
        link = tiktok_target(row.get("url") or "")
        if not link or not link[2] or link[2] != row.get("id"):
            continue
        author = str(row.get("author") or "").lstrip("@").lower()
        if link[1].lower() != author or author != target[1].lower() or target[2] not in (None, link[2]):
            continue
        from engagement_metrics import retain_counters
        accepted.append(retain_counters({"url": row["url"], "source_owner": author, "text": row.get("desc"),
                         "published": row.get("create_time"), "media": [row.get("cover"), row.get("play_addr")]},row))
    return normalize(source, accepted, limit, "tiktok-cli", PINS["tiktok-cli"]["commit"])


async def feed(source, limit):
    from run import source_valid
    require_cloud()
    platform = source["platform"]
    tool = {"threads": "threads-scraper", "tiktok": "tiktok-cli"}[platform]
    commit = PINS[tool]["commit"]
    attempt = {"tool": tool, "upstream_commit": commit, "posts": 0}
    if not source_valid(source) or not 1 <= limit <= 100:
        return [], {**attempt, "status": "invalid_source"}
    root = tool_root()
    with tempfile.TemporaryDirectory(prefix="argus-feed-") as temp:
        work = Path(temp)
        output = work / "result.json"
        if platform == "tiktok":
            target = tiktok_target(source["url"])
            if not target:
                return [], {**attempt, "status": "unsupported_source_shape"}
            args = ["video", "https://www.tiktok.com" + target[0]] if target[2] else ["posts", "@" + target[1]]
            command = [str(root / "tt"), *args, "-o", "jsonl", "-n", str(limit), "--retries", "0",
                       "--timeout", "12s", "--quiet"]
        else:
            try:
                owner, code = threads_target(source)
            except ValueError:
                return [], {**attempt, "status": "unsupported_source_shape"}
            command = [str(root / "threads-python/bin/python"), str(Path(__file__).with_name("threads_scraper_worker.py")),
                       str(root / tool), owner, code or "", str(output)]
        code, status, stdout = await process(command, work, output, timeout=35)
        if not status and code:
            status = ({3: "no_public_posts", 4: "challenge_required", 6: "unavailable"}.get(code, "extraction_failed")
                      if platform == "tiktok" else "worker_failed")
        if status:
            return [], {**attempt, "status": status}
        try:
            if platform == "tiktok":
                rows = [json.loads(line) for line in stdout.read_text().splitlines() if line.strip()]
                items = tiktok_rows(source, rows, limit)
                status = "unverified_posts" if rows else "no_public_posts"
            else:
                data = json.loads(output.read_text())
                items = threads_rows(source, data["rows"], limit, tool, commit=commit)
                status = data["status"]
        except (ValueError, OSError, KeyError, AttributeError, TypeError):
            return [], {**attempt, "status": "invalid_output"}
        return items, {**attempt, "posts": len(items), "status": "ok" if items else status}
