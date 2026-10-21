"""
Tests for rate-limit client identification (spoof resistance).

Author: Joshua Koch
Created: 2026-10-21
"""
import os
import unittest
from unittest import mock

from flask import Flask

from secops_ingestion.security import _client_key, RateLimiter, rate_limit

_app = Flask(__name__)


def key_for(headers, env=None, remote="10.0.0.9"):
    with mock.patch.dict(os.environ, env or {}, clear=False):
        for k in ("TRUSTED_PROXY_HOPS", "TRUST_CLOUDFLARE"):
            if not env or k not in env:
                os.environ.pop(k, None)
        with _app.test_request_context("/", headers=headers, environ_base={"REMOTE_ADDR": remote}):
            return _client_key()


class TestClientKey(unittest.TestCase):

    def test_default_uses_rightmost_hop_not_spoofed_leftmost(self):
        # Cloud Run appends the real client after anything the caller supplied.
        self.assertEqual(key_for({"X-Forwarded-For": "1.1.1.1, 203.0.113.7"}), "203.0.113.7")

    def test_google_alb_two_hops(self):
        hdr = {"X-Forwarded-For": "6.6.6.6, 203.0.113.7, 34.120.0.1"}
        self.assertEqual(key_for(hdr, {"TRUSTED_PROXY_HOPS": "2"}), "203.0.113.7")

    def test_rotating_spoofed_header_does_not_change_key(self):
        env = {"TRUSTED_PROXY_HOPS": "2"}
        a = key_for({"X-Forwarded-For": "1.2.3.4, 203.0.113.7, 34.120.0.1"}, env)
        b = key_for({"X-Forwarded-For": "9.9.9.9, 203.0.113.7, 34.120.0.1"}, env)
        self.assertEqual(a, b)

    def test_cf_connecting_ip_ignored_unless_trusted(self):
        hdr = {"CF-Connecting-IP": "6.6.6.6", "X-Forwarded-For": "203.0.113.7"}
        self.assertEqual(key_for(hdr), "203.0.113.7")
        self.assertEqual(key_for(hdr, {"TRUST_CLOUDFLARE": "true"}), "6.6.6.6")

    def test_short_chain_and_zero_hops_fall_back_to_peer(self):
        self.assertEqual(key_for({"X-Forwarded-For": "203.0.113.7"}, {"TRUSTED_PROXY_HOPS": "2"}), "10.0.0.9")
        self.assertEqual(key_for({"X-Forwarded-For": "203.0.113.7"}, {"TRUSTED_PROXY_HOPS": "0"}), "10.0.0.9")
        self.assertEqual(key_for({}), "10.0.0.9")

    def test_invalid_hops_defaults_to_one(self):
        self.assertEqual(key_for({"X-Forwarded-For": "1.1.1.1, 203.0.113.7"}, {"TRUSTED_PROXY_HOPS": "x"}), "203.0.113.7")

    def test_spoofing_cannot_bypass_rate_limit(self):
        app = Flask("rl")
        limiter = RateLimiter(limit=2, window_seconds=60)

        @app.route("/x")
        @rate_limit(limiter)
        def x():
            return "ok"

        client = app.test_client()
        os.environ.pop("TRUSTED_PROXY_HOPS", None)
        codes = [
            client.get("/x", headers={"X-Forwarded-For": f"{i}.{i}.{i}.{i}, 203.0.113.7"}).status_code
            for i in range(1, 5)
        ]
        self.assertEqual(codes, [200, 200, 429, 429])


if __name__ == "__main__":
    unittest.main()
