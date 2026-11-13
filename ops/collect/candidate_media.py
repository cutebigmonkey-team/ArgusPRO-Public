"""Enrich a bounded sample of verified posts after sharded collection, before packaging."""
import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import sys
import tempfile
from urllib.parse import parse_qs, urlsplit

from candidate_feeds import PINS, tiktok_target
from cloud_social import process, require_cloud, tool_root
from run import url_ok


def video_id(item):
    url = item["post_url"]
    if not url_ok(url):
        return None
    u = urlsplit(url)
    platform = item["platform"]
    if u.scheme != "https":
        return None
    if platform == "tiktok":
        match = tiktok_target(url)
        identity = match[2] if match else None
    elif platform == "youtube" and u.netloc in ("www.youtube.com", "youtube.com") and u.path == "/watch":
        identity = parse_qs(u.query).get("v", [""])[0]
        if not re.fullmatch(r"[A-Za-z0-9_-]{11}", identity):
            return None
    elif platform == "facebook" and u.netloc in ("www.facebook.com", "facebook.com"):
        match = re.fullmatch(r"/(?:[A-Za-z0-9_.]+/videos|reel)/(\d+)/?", u.path)
        identity = match[1] if match else None
    elif platform == "x" and u.netloc in ("x.com", "twitter.com", "www.x.com"):
        match = re.fullmatch(r"/[A-Za-z0-9_]+/status/(\d+)/?", u.path)
        identity = match[1] if match else None
    else:
        return None
    return identity if identity and item["canonical_key"] == platform + ":" + identity else None


def select(items, registry):
    enabled = {row["platform"] for row in registry["collectors"] if row["enabled"]}
    if not registry["execution"]["media_fallback"]:
        return []
    candidates = sorted((p for p in items if p["platform"] in enabled and video_id(p)),
                        key=lambda p: (p.get("published_at") or "", p["canonical_key"]), reverse=True)
    clips = [p for p in candidates if p["platform"] == "tiktok"]
    missing = [p for p in candidates if p["platform"] != "tiktok" and not p.get("media_urls")]
    return clips[:1] + missing[:1]


async def worker(command, work, name, timeout):
    output = work / name
    code, status, _ = await process(command, work, output, timeout=timeout)
    if status or code or not output.is_file():
        return {"status": status or "worker_failed"}
    return json.loads(output.read_text())


async def enrich(item):
    root = tool_root()
    analyze = item["platform"] == "tiktok"
    with tempfile.TemporaryDirectory(prefix="argus-video-") as path:
        work = Path(path)
        data = await worker([str(root/"saver-python/bin/python"), str(Path(__file__).with_name("saver_media_worker.py")),
                             str(root), item["post_url"], video_id(item), path, "yes" if analyze else "no"],
                            work, "metadata.json", 75)
        outcome = {"canonical_key": item["canonical_key"], "saver_backend": data["status"]}
        if data["status"] == "ok":
            metadata = data["metadata"]
            item["saver_media"] = {**metadata, "upstream_commit": PINS["saver_backend"]["commit"]}
            thumbnail = metadata.get("thumbnail")
            if thumbnail and url_ok(thumbnail):
                item["media_urls"] = list(dict.fromkeys([*item.get("media_urls", []), thumbnail]))[:12]
        if analyze:
            outcome["tiktok-video-analyzer"] = data.get("clip_status", "no_video")
            if data.get("clip_status") == "ok":
                analysis = await worker([str(root/"video-python/bin/python"), str(Path(__file__).with_name("video_analysis_worker.py")),
                                         str(root), path], work, "analysis.json", 240)
                outcome["tiktok-video-analyzer"] = analysis["status"]
                if analysis["status"] == "ok":
                    item["video_analysis"] = {**analysis, "upstream_commit": PINS["tiktok-video-analyzer"]["commit"]}
                    sections = []
                    for label, rows in [("Мовлення", analysis["speech"]), ("Текст на екрані", analysis["screen"])]:
                        content = " ".join(dict.fromkeys(r["text"] for r in rows))
                        if content:
                            sections.append(label + ": " + content)
                    item["video_analysis_text"] = "\n\n".join(sections)[:20000]
                    item["text"] = (item["text"][:79998] + "\n\n" + item["video_analysis_text"]).strip()
        return outcome


async def main():
    require_cloud()
    p = argparse.ArgumentParser()
    p.add_argument("action", choices=["plan", "apply"])
    p.add_argument("--input", type=Path, required=True)
    p.add_argument("--registry", type=Path, required=True)
    args = p.parse_args()
    posts = args.input / "posts.jsonl"
    with posts.open(encoding="utf-8") as stream:
        items = [json.loads(line) for line in stream if line.strip()]
    selection = select(items, json.loads(args.registry.read_text()))
    if args.action == "plan":
        with open(os.environ["GITHUB_OUTPUT"], "a") as out:
            print("enabled=" + str(bool(selection)).lower(), file=out)
            print("analyze=" + str(any(p["platform"] == "tiktok" for p in selection)).lower(), file=out)
        return
    outcomes = [await enrich(item) for item in selection]
    report = args.input / "report.json"
    data = json.loads(report.read_text(encoding="utf-8"))
    data["media_enrichment"] = outcomes
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "local-storage"))
    from storage import pack
    posts.write_text("".join(json.dumps(p, ensure_ascii=False) + "\n" for p in items), encoding="utf-8")
    pack(posts, args.input/"media", args.input/"package.zip")
    report.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(outcomes))


if __name__ == "__main__":
    asyncio.run(main())
