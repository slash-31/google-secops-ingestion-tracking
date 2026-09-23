"""
SSL / TLS utilities for Google SecOps Ingestion Intelligence.
Provides automated self-signed certificate generation, certificate inspection,
and SSL context creation for secure HTTPS operation.
"""
import os
import ssl
import stat
import logging
import ipaddress
import socket
from datetime import datetime, timedelta, timezone
from typing import Dict, Any, List, Optional, Tuple

from cryptography import x509
from cryptography.x509.oid import NameOID, ExtendedKeyUsageOID
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger(__name__)

DEFAULT_CERT_DIR = "certs"
DEFAULT_CERT_FILE = os.path.join(DEFAULT_CERT_DIR, "cert.pem")
DEFAULT_KEY_FILE = os.path.join(DEFAULT_CERT_DIR, "key.pem")


def generate_self_signed_cert(
    cert_path: str = DEFAULT_CERT_FILE,
    key_path: str = DEFAULT_KEY_FILE,
    hostname: str = "localhost",
    additional_alt_names: Optional[List[str]] = None,
    valid_days: int = 365,
    overwrite: bool = False,
) -> Tuple[str, str]:
    """
    Generate an RSA private key and self-signed X.509 v3 certificate.

    Args:
        cert_path: Path where certificate PEM will be saved.
        key_path: Path where private key PEM will be saved.
        hostname: Primary hostname / common name (e.g. 'localhost').
        additional_alt_names: Optional list of additional hostnames or IPs to include in SAN.
        valid_days: Number of days the certificate should remain valid.
        overwrite: If False and both files exist, skips regeneration and returns existing paths.

    Returns:
        Tuple of (cert_path, key_path).
    """
    if not overwrite and os.path.exists(cert_path) and os.path.exists(key_path):
        logger.info("Existing SSL certificate found at %s, skipping generation", cert_path)
        return cert_path, key_path

    # Ensure parent directories exist
    os.makedirs(os.path.dirname(os.path.abspath(cert_path)), exist_ok=True)
    os.makedirs(os.path.dirname(os.path.abspath(key_path)), exist_ok=True)

    logger.info("Generating 2048-bit RSA private key...")
    private_key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    # Subject & Issuer (self-signed)
    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
        x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "California"),
        x509.NameAttribute(NameOID.LOCALITY_NAME, "Mountain View"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Google SecOps Ingestion Intelligence"),
        x509.NameAttribute(NameOID.COMMON_NAME, hostname),
    ])

    # Build SAN entries
    san_entries: List[x509.GeneralName] = []

    def _add_name_or_ip(item: str):
        try:
            ip = ipaddress.ip_address(item)
            san_entries.append(x509.IPAddress(ip))
        except ValueError:
            san_entries.append(x509.DNSName(item))

    # Standard loopback identifiers
    _add_name_or_ip(hostname)
    if hostname != "localhost":
        _add_name_or_ip("localhost")
    _add_name_or_ip("127.0.0.1")
    _add_name_or_ip("::1")

    # Add system hostname if available
    try:
        sys_hostname = socket.gethostname()
        if sys_hostname:
            _add_name_or_ip(sys_hostname)
    except Exception:
        pass

    if additional_alt_names:
        for alt in additional_alt_names:
            _add_name_or_ip(alt)

    # Remove duplicates preserving order
    seen = set()
    unique_sans = []
    for entry in san_entries:
        key = (type(entry), str(entry.value))
        if key not in seen:
            seen.add(key)
            unique_sans.append(entry)

    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now)
        .not_valid_after(now + timedelta(days=valid_days))
        .add_extension(x509.SubjectAlternativeName(unique_sans), critical=False)
        .add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True,
        )
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(private_key, hashes.SHA256())
    )

    # Write private key with restrictive 0600 permissions
    key_pem = private_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.TraditionalOpenSSL,
        encryption_algorithm=serialization.NoEncryption(),
    )
    with open(key_path, "wb") as f:
        f.write(key_pem)
    try:
        os.chmod(key_path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass

    # Write certificate PEM
    cert_pem = cert.public_bytes(serialization.Encoding.PEM)
    with open(cert_path, "wb") as f:
        f.write(cert_pem)
    try:
        os.chmod(cert_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)
    except OSError:
        pass

    logger.info("Successfully generated SSL certificate at %s and key at %s", cert_path, key_path)
    return cert_path, key_path


def ensure_ssl_credentials(
    cert_path: Optional[str] = None,
    key_path: Optional[str] = None,
    hostname: str = "localhost",
) -> Tuple[str, str]:
    """
    Ensure valid SSL certificate and key files are present.
    If either file is missing, automatically generates a self-signed pair.

    Returns:
        Tuple of (cert_path, key_path).
    """
    target_cert = cert_path or DEFAULT_CERT_FILE
    target_key = key_path or DEFAULT_KEY_FILE

    if not (os.path.exists(target_cert) and os.path.exists(target_key)):
        generate_self_signed_cert(
            cert_path=target_cert,
            key_path=target_key,
            hostname=hostname,
            overwrite=True,
        )

    return target_cert, target_key


def create_ssl_context(cert_path: str, key_path: str) -> ssl.SSLContext:
    """
    Create a secure Python SSLContext configured with the given cert and key.

    Args:
        cert_path: Path to certificate file.
        key_path: Path to private key file.

    Returns:
        Configured ssl.SSLContext ready for Werkzeug or standard sockets.
    """
    if not os.path.exists(cert_path):
        raise FileNotFoundError(f"SSL certificate file not found: {cert_path}")
    if not os.path.exists(key_path):
        raise FileNotFoundError(f"SSL private key file not found: {key_path}")

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(certfile=cert_path, keyfile=key_path)
    return ctx


def get_certificate_info(cert_path: str) -> Dict[str, Any]:
    """
    Extract diagnostic metadata from a certificate PEM file.

    Returns dictionary with subject, issuer, validity window, days remaining, and SANs.
    """
    if not os.path.exists(cert_path):
        raise FileNotFoundError(f"SSL certificate file not found: {cert_path}")

    with open(cert_path, "rb") as f:
        cert_data = f.read()

    cert = x509.load_pem_x509_certificate(cert_data)

    san_list: List[str] = []
    try:
        san_ext = cert.extensions.get_extension_for_oid(x509.ExtensionOID.SUBJECT_ALTERNATIVE_NAME)
        for val in san_ext.value:
            san_list.append(str(val.value))
    except Exception:
        pass

    now = datetime.now(timezone.utc)
    not_after = cert.not_valid_after_utc if hasattr(cert, "not_valid_after_utc") else cert.not_valid_after.replace(tzinfo=timezone.utc)
    not_before = cert.not_valid_before_utc if hasattr(cert, "not_valid_before_utc") else cert.not_valid_before.replace(tzinfo=timezone.utc)
    days_remaining = max(0, (not_after - now).days)

    return {
        "cert_path": cert_path,
        "serial_number": hex(cert.serial_number),
        "subject_cn": cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME)[0].value if cert.subject.get_attributes_for_oid(NameOID.COMMON_NAME) else "unknown",
        "issuer": cert.issuer.rfc4514_string(),
        "valid_from": not_before.isoformat(),
        "valid_until": not_after.isoformat(),
        "days_remaining": days_remaining,
        "sans": san_list,
        "fingerprint_sha256": cert.fingerprint(hashes.SHA256()).hex(),
    }
