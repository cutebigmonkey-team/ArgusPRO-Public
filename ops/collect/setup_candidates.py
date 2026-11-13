"""Prepare just the upstreams needed by the current disposable job."""
import json
import os
from pathlib import Path
import sys

from cloud_social import require_cloud, tool_root
from setup_social import checkout

require_cloud()
root = tool_root()
root.mkdir(exist_ok=True)
pins = json.loads(Path(__file__).with_name("candidate-upstreams.json").read_text())
names = {"feeds": ["threads-scraper"], "tt": ["tiktok-cli"], "facebook-x": ["facebook-cli", "x2md"],
         "media": ["saver_backend", "tiktok-video-analyzer"]}[sys.argv[1]]
checkout(root, {name: pins[name] for name in names})
with open(os.environ["GITHUB_ENV"], "a") as out:
    out.write(f"ARGUS_SOCIAL_TOOLS={root}\n")
