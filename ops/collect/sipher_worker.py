"""Run the upstream Facebook timeline reader in a fresh anonymous browser context."""
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, sys.argv[1])
os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(Path.home() / ".cache/ms-playwright"))
from adapters.facebook.scrape import scrape_profile
from playwright.sync_api import sync_playwright

with sync_playwright() as pw:
    browser = pw.chromium.launch()
    try:
        rows = scrape_profile(browser.new_context(), sys.argv[2], max_scrolls=2)
        result = {"rows": rows}
    except RuntimeError as error:
        if "main 컨테이너" not in str(error):
            raise
        result = {"status": "page_unavailable", "rows": []}
    finally:
        browser.close()
Path(sys.argv[3]).write_text(json.dumps(result))
