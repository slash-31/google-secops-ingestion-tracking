"""
Unit tests for SSL certificate generation, validation, and context creation.
"""
import os
import ssl
import stat
import tempfile
import unittest
from datetime import datetime, timezone

from cryptography import x509
from cryptography.hazmat.primitives import hashes

from secops_ingestion.ssl_util import (
    generate_self_signed_cert,
    ensure_ssl_credentials,
    create_ssl_context,
    get_certificate_info,
)


class TestSSLUtil(unittest.TestCase):

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.cert_path = os.path.join(self.temp_dir.name, "cert.pem")
        self.key_path = os.path.join(self.temp_dir.name, "key.pem")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_generate_self_signed_cert(self):
        cert_p, key_p = generate_self_signed_cert(
            cert_path=self.cert_path,
            key_path=self.key_path,
            hostname="localhost",
            additional_alt_names=["test.secops.local", "192.168.1.50"],
            valid_days=90,
            overwrite=True,
        )
        self.assertTrue(os.path.exists(cert_p))
        self.assertTrue(os.path.exists(key_p))

        # Check key file permissions (0600)
        mode = stat.S_IMODE(os.stat(key_p).st_mode)
        self.assertEqual(mode, 0o600)

        # Inspect certificate
        with open(cert_p, "rb") as f:
            cert = x509.load_pem_x509_certificate(f.read())

        self.assertEqual(cert.subject.rfc4514_string(), cert.issuer.rfc4514_string())
        self.assertIn("Google SecOps Ingestion Intelligence", cert.subject.rfc4514_string())

        # Verify SAN extension
        san_ext = cert.extensions.get_extension_for_oid(x509.ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        san_names = [str(x.value) for x in san_ext.value]
        self.assertIn("localhost", san_names)
        self.assertIn("127.0.0.1", san_names)
        self.assertIn("::1", san_names)
        self.assertIn("test.secops.local", san_names)
        self.assertIn("192.168.1.50", san_names)

    def test_generate_preserves_existing_unless_overwrite(self):
        generate_self_signed_cert(
            cert_path=self.cert_path,
            key_path=self.key_path,
            hostname="first.local",
            overwrite=True,
        )
        with open(self.cert_path, "rb") as f:
            first_bytes = f.read()

        # Call again without overwrite
        generate_self_signed_cert(
            cert_path=self.cert_path,
            key_path=self.key_path,
            hostname="second.local",
            overwrite=False,
        )
        with open(self.cert_path, "rb") as f:
            second_bytes = f.read()

        self.assertEqual(first_bytes, second_bytes)

    def test_ensure_ssl_credentials_auto_generates(self):
        c_path, k_path = ensure_ssl_credentials(
            cert_path=self.cert_path,
            key_path=self.key_path,
        )
        self.assertTrue(os.path.exists(c_path))
        self.assertTrue(os.path.exists(k_path))

    def test_create_ssl_context(self):
        generate_self_signed_cert(
            cert_path=self.cert_path,
            key_path=self.key_path,
            overwrite=True,
        )
        ctx = create_ssl_context(self.cert_path, self.key_path)
        self.assertIsInstance(ctx, ssl.SSLContext)

    def test_create_ssl_context_missing_files_raises(self):
        with self.assertRaises(FileNotFoundError):
            create_ssl_context("nonexistent_cert.pem", "nonexistent_key.pem")

    def test_get_certificate_info(self):
        generate_self_signed_cert(
            cert_path=self.cert_path,
            key_path=self.key_path,
            hostname="secops.internal",
            valid_days=30,
            overwrite=True,
        )
        info = get_certificate_info(self.cert_path)
        self.assertEqual(info["cert_path"], self.cert_path)
        self.assertEqual(info["subject_cn"], "secops.internal")
        self.assertIn("secops.internal", info["sans"])
        self.assertTrue(info["days_remaining"] >= 29)
        self.assertIn("fingerprint_sha256", info)


if __name__ == "__main__":
    unittest.main()
