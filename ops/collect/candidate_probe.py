"""Small cloud acceptance run using public catalog URLs, separate from the archive."""
import argparse
import asyncio
import json
import os
from pathlib import Path

from candidate_feeds import feed
from candidate_media import enrich

PROFILE = "https://www.tiktok.com/@fixture1107"
VIDEO = PROFILE + "/video/7702050405622058261"
NARRATION = "https://www.tiktok.com/@fixture1111/video/7698242285544197397"
THREADS = "https://www.threads.com/@fixture1096"


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("mode", choices=["feeds", "media"])
    mode = p.parse_args().mode
    catalog = json.loads(Path(__file__).with_name("sources.json").read_text(encoding="utf-8"))
    sources = {s["url"]: s for s in catalog["sources"] if s.get("enabled", True)}
    video = {**sources[PROFILE], "url": VIDEO}
    if mode == "feeds":
        outcomes = []
        for source in [sources[THREADS], video, sources[PROFILE]]:
            if any(row["url"].startswith(PROFILE) and row["status"] == "http_429" for row in outcomes):
                continue
            items, attempt = await feed(source, 3)
            outcomes.append({**attempt, "url": source["url"], "keys": [p["canonical_key"] for p in items]})
    else:
        outcomes = []
        for url in (VIDEO, NARRATION):
            if url.rsplit("/video/", 1)[0] not in sources:
                raise RuntimeError("Acceptance source absent from the enabled catalog")
            item = {"platform": "tiktok", "canonical_key": "tiktok:" + url.rsplit("/", 1)[-1],
                    "post_url": url, "text": "", "media_urls": []}
            outcome = await enrich(item)
            analysis = item.get("video_analysis") or {}
            outcomes.append({**outcome, "url": url, "language": analysis.get("language"),
                             "speech_sample": analysis.get("speech", [])[:8], "screen_sample": analysis.get("screen", [])[:6],
                             "speech_segments": len(analysis.get("speech", [])), "screen_frames": len(analysis.get("screen", []))})
    print(json.dumps(outcomes), flush=True)
    with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
        out.write("```json\n" + json.dumps(outcomes, indent=2) + "\n```\n")
    Path(mode+"-outcomes.json").write_text(json.dumps(outcomes, indent=2))
    if mode == "media":
        if not outcomes[0]["screen_frames"]:
            raise RuntimeError("No frame text from the DSNS safety clip")
        if not outcomes[1]["speech_segments"] or outcomes[1]["language"] != "uk":
            raise RuntimeError("No Ukrainian speech verified in the DSNS safety briefing")
    if any(r.get(k) in {"invalid_output", "worker_failed", "invalid_source", "unsupported_source_shape"}
           for r in outcomes for k in ("status", "saver_backend", "tiktok-video-analyzer")):
        raise RuntimeError("Adapter failed its cloud contract")


if __name__ == "__main__":
    asyncio.run(main())
