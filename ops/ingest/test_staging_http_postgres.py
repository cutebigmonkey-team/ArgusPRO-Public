"""Real loopback HTTP -> isolated PostgreSQL tests. Never use production."""
import hashlib
import json
import tempfile
import threading
import unittest
import urllib.request
import urllib.error
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import Mock
from wsgiref.simple_server import make_server

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa

from envelope import canonical
from partition import partition_source
from private_gateway import REUSABLE
from staging_http import (
    QuietHandler, build_gateway, connect_staging,
    validate_staging_config,
)
from test_oidc_verifier import valid_claims
from test_pg_queue_integration import connection_string, psycopg


class HTTPPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dsn = connection_string()
        cls.db = psycopg.connect(cls.dsn, autocommit=True)
        cls.db.execute("DROP SCHEMA IF EXISTS argus_ingest CASCADE")
        for name in ("pg_queue_schema.sql", "pg_phase2_schema.sql"):
            cls.db.execute(
                Path(__file__).with_name(name).read_text()
            )
        cls.key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048
        )

    @classmethod
    def tearDownClass(cls):
        cls.db.close()

    def setUp(self):
        self.db.execute(
            "TRUNCATE argus_ingest.batches, "
            "argus_ingest.source_cursors, "
            "argus_ingest.checkpoint_groups, "
            "argus_ingest.oidc_replays CASCADE"
        )
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        policy = Path(tmp.name) / "sources.json"
        policy.write_text(json.dumps({
            "sources": [{"id": "rss_fixture", "platform": "rss"}]
        }))

        env = {
            "ARGUS_STAGING_GATEWAY_ENABLED": "1",
            "ARGUS_STAGING_PG_DSN": self.dsn,
            "ARGUS_STAGING_SOURCE_POLICY": str(policy),
            "ARGUS_STAGING_REPOSITORY_ID": "111",
            "ARGUS_STAGING_OWNER_ID": "222",
        }
        validate_staging_config(env)
        conn = connect_staging(env)
        self.addCleanup(conn.close)

        self.gateway = build_gateway(env, conn)
        self.gateway.key_client = Mock(
            get_signing_key_from_jwt=Mock(
                return_value=Mock(key=self.key.public_key())
            )
        )

        server = make_server(
            "127.0.0.1", 0, self.gateway.http,
            handler_class=QuietHandler
        )
        self.addCleanup(server.server_close)
        thread = threading.Thread(
            target=server.serve_forever, daemon=True
        )
        thread.start()
        self.addCleanup(lambda: (server.shutdown(), thread.join(3)))
        self.url = (
            f"http://127.0.0.1:{server.server_port}/v2/ingest"
        )

    def packet(self, changed=False):
        now = datetime.now(timezone.utc)
        stamp = now.isoformat()
        sid = "rss_fixture"
        shard = int(hashlib.sha256(sid.encode()).hexdigest(), 16) % 4
        post = {
            "canonical_key": "rss:fixture/1",
            "source_id": sid,
            "platform": "rss",
            "text": "Changed" if changed else "Synthetic post",
            "published_at": stamp,
            "observed_at": stamp,
            "post_url": "https://example.org/posts/1",
            "metadata": {"ordinal": 1},
        }
        part = partition_source(
            {"run_id": 44058882718, "attempt": 1,
             "lane": "fast", "shard": shard, "sha": "a" * 40},
            {"source_id": sid, "platform": "rss", "status": "ok"},
            [post],
            {"base_revision": 0, "proposal": {"head_id": "1"}},
            stamp, now=now,
        )[0]
        claims = valid_claims()
        claims.update({
            "iat": int(now.timestamp()) - 20,
            "nbf": int(now.timestamp()) - 20,
            "exp": int(now.timestamp()) + 180,
            "jti": "synthetic-jti",
            "repository_id": "111",
            "repository_owner_id": "222",
            "job_workflow_ref": REUSABLE,
            "job_workflow_sha": "a" * 40,
            "workflow_sha": "a" * 40,
        })
        token = jwt.encode(
            claims, self.key, algorithm="RS256",
            headers={"kid": "test-key"}
        )
        return canonical(part), token

    def send(self, body, token):
        request = urllib.request.Request(
            self.url, body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer " + token,
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status
        except urllib.error.HTTPError as error:
            return error.code

    def count(self):
        return self.db.execute(
            "SELECT count(*) FROM argus_ingest.batches"
        ).fetchone()[0]

    def test_http_to_postgres_and_fresh_connection(self):
        body, token = self.packet()
        self.assertEqual(self.send(body, token), 202)
        with psycopg.connect(self.dsn, autocommit=True) as db:
            row = db.execute(
                "SELECT body,state FROM argus_ingest.batches"
            ).fetchone()
            self.assertEqual(bytes(row[0]), body)
            self.assertEqual(row[1], "pending")
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM argus_ingest.source_cursors"
                ).fetchone()[0], 0
            )

    def test_retry_does_not_duplicate(self):
        body, token = self.packet()
        self.assertEqual(self.send(body, token), 202)
        self.assertEqual(self.send(body, token), 202)
        self.assertEqual(self.count(), 1)

    def test_invalid_signature_is_rejected(self):
        body, token = self.packet()
        head, payload, signature = token.split(".")
        bad = head + "." + payload + "." + (
            ("A" if signature[0] != "A" else "B") + signature[1:]
        )
        self.assertEqual(self.send(body, bad), 403)
        self.assertEqual(self.count(), 0)

    def test_conflicting_replay_is_rejected(self):
        body, token = self.packet()
        self.assertEqual(self.send(body, token), 202)
        changed, _ = self.packet(changed=True)
        self.assertEqual(self.send(changed, token), 403)
        self.assertEqual(self.count(), 1)

    def test_disabled_gateway_rejects_requests(self):
        self.gateway.enabled = False
        body, token = self.packet()
        self.assertEqual(self.send(body, token), 503)
        self.assertEqual(self.count(), 0)


if __name__ == "__main__":
    unittest.main()
