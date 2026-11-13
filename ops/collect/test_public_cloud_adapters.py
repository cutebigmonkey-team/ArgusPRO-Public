import json
import unittest
from unittest.mock import patch

from public_cloud_adapters import PINS, add_marketpulse_metrics, ckelepel_rows, target_source


class SocialAdaptersTest(unittest.TestCase):
    def test_all_defaults_and_host_boundaries(self):
        for tool, pin in PINS.items():
            with self.subTest(tool=tool):
                source, _ = target_source(tool, pin["target"])
                self.assertEqual(source["platform"], pin["platform"])
                for target in ("https://example.org/@dsns", pin["target"] + "?target=elsewhere"):
                    with self.assertRaises(ValueError):
                        target_source(tool, target)

    def test_threads_identity_deduplication_and_missing_date(self):
        source, owner = target_source("ckelepel", PINS["ckelepel"]["target"])
        row = {"author": {"username": owner}, "url": source["url"] + "/post/abc",
               "caption": "Verified", "media": [{"url": "https://example.org/image"}]}
        rows = [row, {**row, "author": {"username": "other"}}, row, {**row, "url": "https://example.org/post/abc"}]
        items = ckelepel_rows(source, rows, 3)
        self.assertEqual([p["canonical_key"] for p in items], ["threads:abc"])
        self.assertIsNone(items[0]["published_at"])
        self.assertEqual(ckelepel_rows(source, [{**row, "taken_at": 86400}], 1)[0]["published_at"], "1970-01-02T00:00:00+00:00")

    def test_telegram_metrics_keep_scope_and_existing_fields(self):
        url = "https://t.me/fixture110/2"
        items = [{"post_url": url, "engagement": {"likes": 4}}, {"post_url": url + "7"}]
        raw = json.dumps([{"post_url": url, "views": 13, "reactions": 0}]).encode()
        with patch("subprocess.check_output", return_value=raw):
            add_marketpulse_metrics(items, b"html")
        self.assertEqual(items[0]["engagement"], {"likes": 4, "views": 13})
        self.assertNotIn("engagement", items[1])
