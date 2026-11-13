"""Run one pinned public reader and produce source-bound ARGUS posts or page metadata."""
import argparse
import asyncio
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

from cloud_social import process, require_cloud, threads_rows, threads_target, tool_root
from remaining_social import normalize
from run import fetch, post, source_valid

HERE = Path(__file__).resolve().parent
PINS = json.loads((HERE / "cloud-adapter-pins.json").read_text())
PROFILE = r"/([A-Za-z0-9_.-]{1,80})/?"
SHAPES = {"sipher": PROFILE, "facebook-page-info": PROFILE, "marketpulse": r"/(?:s/)?([A-Za-z0-9_]{3,64})/?",
          "superbrain": r"/(?:p|reel)/([A-Za-z0-9_-]{1,40})/?", "vdite": r"/@([\w.]+)/post/([\w-]+)/?"}
THREADS_PROFILE = r"/@([A-Za-z0-9_.]{1,64})/?"


def target_source(tool, target):
    src = {"platform": PINS[tool]["platform"], "url": target}
    parsed = urlsplit(target)
    match = re.fullmatch(SHAPES.get(tool, THREADS_PROFILE), parsed.path, re.ASCII)
    if not (source_valid(src) and parsed.scheme == "https" and not parsed.query and not parsed.fragment and match):
        raise ValueError("unsupported_target")
    return src, match[1]


def ckelepel_rows(source, rows, limit):
    records = []
    for p in rows:
        author = (p.get("author") or {}).get("username")
        if author:
            stamp = p.get("taken_at")
            timestamp = datetime.fromtimestamp(stamp, timezone.utc).isoformat() if isinstance(stamp, int) and 0 < stamp < 2**35 else None
            records.append({**p, "timestamp": timestamp, "username": author, "permalink": p.get("url"), "text": p.get("caption"),
                            "media_urls": [m["url"] for m in p.get("media", []) if m.get("url")]})
    return threads_rows(source, records, limit, "ckelepel", PINS["ckelepel"]["commit"])


async def hosted_ckelepel(source, limit):
    owner, _ = threads_target(source)
    root = tool_root() / "ckelepel"
    cmd = ["node", str(root / "bin/ckelepel.js"), "posts", owner, "--limit", str(limit), "--json"]
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        code, status, stdout = await process(cmd, work, work / "unused", timeout=22)
        items = []
        if code and not status:
            err = (work / "stderr").read_text(errors="replace")
            failure = re.search(r"\bHTTP (401|403|404|429)\b", err)
            status = "http_" + failure[1] if failure else "worker_failed"
        if not status:
            items = ckelepel_rows(source, json.loads(stdout.read_text())["posts"], limit)
        return items, {"tool": "ckelepel", "upstream_commit": PINS["ckelepel"]["commit"],
                       "posts": len(items), "status": status or ("ok" if items else "no_verified_posts")}


async def hosted_sipher(source, limit):
    from browser_public import normalize_rows
    commit = PINS["sipher"]["commit"]
    attempt = {"tool": "sipher", "upstream_commit": commit, "posts": 0}
    try:
        target_source("sipher", source["url"])
    except ValueError:
        return [], {**attempt, "status": "unsupported_target"}
    with tempfile.TemporaryDirectory() as directory:
        work = Path(directory)
        output = work / "result.json"
        cmd = [sys.executable, str(HERE / "sipher_worker.py"), str(tool_root() / "sipher"), source["url"], str(output)]
        code, status, _ = await process(cmd, work, output, timeout=55)
        if status or code:
            return [], {**attempt, "status": status or "worker_failed"}
        result = json.loads(output.read_text())
        rows = [{"url": r.get("permalink"), "text": r.get("text"), "images": r.get("image_urls"),
                 "videos": r.get("video_urls")} for r in result["rows"]]
        items = normalize_rows(source, rows, limit)
        for item in items:
            item.update(capture_mode="sipher", collector_commit=commit)
        return items, {**attempt, "posts": len(items), "status": "ok" if items else result.get("status", "no_verified_posts")}


def add_marketpulse_metrics(items, body):
    cmd = [sys.executable, str(HERE / "marketpulse_page.py"), str(tool_root() / "marketpulse")]
    env = {k: os.environ[k] for k in ("LANG", "PATH", "LD_LIBRARY_PATH") if k in os.environ}
    try:
        raw = subprocess.check_output(cmd, input=body, env=env, timeout=8)
        rows = {r["post_url"]: r for r in json.loads(raw)}
    except (ValueError, KeyError, OSError, subprocess.SubprocessError):
        return
    for item in items:
        row = rows.get(item["post_url"], {})
        # The upstream supplies zero even when the public preview hides a metric.
        signals = {k: row[k] for k in ("views", "reactions") if isinstance(row.get(k), int) and row[k] > 0}
        if signals:
            item.setdefault("engagement", {}).update(signals)
            item["marketpulse_commit"] = PINS["marketpulse"]["commit"]


def marketpulse(source, root, owner, limit):
    from marketpulse_page import read_page
    body, _ = fetch("https://t.me/s/" + owner)
    rows = read_page(body.decode("utf-8"), root)
    items = {}
    for row in rows:
        url = row["post_url"]
        match = re.fullmatch(r"https://t\.me/" + re.escape(owner) + r"/(\d+)", url, re.I)
        if match and row["description"]:
            key = "telegram:" + owner.lower() + "/" + match[1]
            item = post(source, key, url, row["description"], row["posted_at"])
            item.update(views=row["views"], capture_mode="marketpulse", collector_commit=PINS["marketpulse"]["commit"])
            items[key] = item
    return sorted(items.values(), key=lambda p: int(p["canonical_key"].rsplit("/", 1)[1]), reverse=True)[:limit]


def run_adapter(tool, source, owner, work, limit=5):
    root = tool_root() / tool
    url = source["url"]
    commit = PINS[tool]["commit"]
    sys.path.insert(0, str(root))
    details = {}
    if tool == "marketpulse":
        items = marketpulse(source, root, owner, limit)
    elif tool == "ckelepel":
        items, attempt = asyncio.run(hosted_ckelepel(source, limit))
        if attempt["status"] == "worker_failed":
            raise RuntimeError("ckelepel failed to run")
        return items, {"status": attempt["status"]}
    elif tool == "facebook-page-info":
        from facebook_page_info_scraper.client import scrape_page
        page = scrape_page(url, want_about=False, timeout=15)
        if not page["ok"]:
            return [], {"status": "page_unavailable", "reason": str(page.get("error", ""))[:180]}
        if not page.get("page_id") and urlsplit(page.get("canonical") or "").path.strip("/").lower() != owner.lower():
            return [], {"status": "unverified_page"}
        details["page_metadata"] = {k: page[k] for k in ("page_name", "page_id", "bio", "final_url") if page.get(k)}
        return [], {**details, "status": "metadata_only"}
    elif tool == "sipher":
        items, attempt = asyncio.run(hosted_sipher(source, limit))
        if attempt["status"] == "worker_failed":
            raise RuntimeError("sipher failed to run")
        return items, {"status": attempt["status"]}
    elif tool == "vdite":
        from threads_scraper import scrape_threads_recursive
        rows = asyncio.run(scrape_threads_recursive(url, max_pages=2))
        records = [{**r, "permalink": "https://www.threads.com/@%s/post/%s" % (r["author"], r["code"])}
                   for r in rows if r.get("author") and r.get("code")]
        items = threads_rows(source, records, limit, tool, commit)
    elif tool == "threads-actor":
        storage = work / "storage"
        inputs = storage / "key_value_stores/default"
        inputs.mkdir(parents=True)
        (inputs / "INPUT.json").write_text(json.dumps({"usernames": [owner], "maxPosts": limit,
                                                      "proxyConfiguration": {"useApifyProxy": False}}))
        env = {**os.environ, "CRAWLEE_STORAGE_DIR": str(storage), "APIFY_LOCAL_STORAGE_DIR": str(storage),
               "CRAWLEE_PURGE_ON_START": "0", "PUPPETEER_EXECUTABLE_PATH": "/usr/bin/google-chrome"}
        subprocess.run(["node", str(root / "src/main.js")], cwd=work, env=env, check=True)
        rows = [json.loads(p.read_text()) for p in (storage / "datasets/default").glob("*.json")]
        records = [{"username": r.get("username"), "text": r.get("postText"), "permalink": r.get("postUrl"),
                    "timestamp": r.get("postedAt"), "media_urls": r.get("mediaUrls", [])}
                   for r in rows if r.get("type") == "post"]
        items = threads_rows(source, records, limit, tool, commit)
    elif tool == "superbrain":
        sys.path.insert(0, str(root / "backend"))
        from instagram import instagram_downloader as reader
        reader.TEMP_DIR = work
        try:
            output = reader.download_instagram_content(url)
        except reader.RetryableDownloadError:
            return [], {"status": "login_or_rate_limited"}
        if not output:
            return [], {"status": "post_unavailable"}
        info = (Path(output) / "info.txt").read_text()
        header, sep, caption = info.partition("Caption:\n" + "-" * 50 + "\n")
        caption = caption.split("\n" + "-" * 50)[0].strip()
        if not sep or f"URL: {url}\n" not in header or caption == "No caption":
            return [], {"status": "no_verified_text"}
        stamp = re.search(r"^Date: (.+)$", header, re.M)
        items = normalize(source, [{"url": url, "text": caption, "published": stamp[1] + "+00:00" if stamp else None}],
                          limit, tool, commit)
    else:
        if not os.environ.get("SOCIALCRAWL_API_KEY"):
            return [], {"status": "requires_api_key"}
        result = json.loads(subprocess.check_output(["node", str(HERE / "socialcrawl_read.mjs"), str(root), owner]))
        if result["status"] != 200:
            return [], {"status": "http_" + str(result["status"])}
        records = []
        for row in result["items"]:
            p = row["post"]
            content = p.get("content") or {}
            records.append({"username": (p.get("author") or {}).get("username"), "permalink": p.get("url"),
                            "text": content.get("text"), "media_urls": content.get("media_urls", []),
                            "timestamp": p.get("published_at")})
        items = threads_rows(source, records, limit, tool, commit)
    return items, {"status": "ok" if items else "no_verified_posts", **details}


def main():
    require_cloud()
    parser = argparse.ArgumentParser()
    parser.add_argument("tool", choices=PINS)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    tool = args.tool
    target = os.environ.get("TARGET") or PINS[tool]["target"]
    source, owner = target_source(tool, target)
    with tempfile.TemporaryDirectory() as directory:
        items, details = run_adapter(tool, source, owner, Path(directory))
    report = {"tool": tool, "target": target, "commit": PINS[tool]["commit"], "posts": len(items), **details, "items": items}
    args.output.write_text(json.dumps(report, ensure_ascii=False))
    print(tool, details["status"], len(items))


if __name__ == "__main__":
    main()
