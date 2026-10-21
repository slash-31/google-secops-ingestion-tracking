"""
Security primitives for the SecOps Ingestion Dashboard.

Author: Joshua Koch
Created: 2026-09-23
Last Updated: 2026-09-23
Version: 1.0.0
Description: Authentication gates, response hardening headers, input validation,
             and output sanitization shared across the Flask app and API client.
"""
import os
import re
import hmac
import time
import logging
from functools import wraps
from threading import Lock
from typing import Optional, Dict, List, Tuple

from flask import request, jsonify

logger = logging.getLogger("secops_ingestion.security")

# GCP project IDs: 6-30 chars, lowercase letter start, letters/digits/hyphens.
# Also permit a trailing ":domain" for legacy domain-scoped projects.
_PROJECT_ID_RE = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9](:[a-z0-9.-]{1,60})?$")

# Characters a spreadsheet may interpret as the start of a formula (CWE-1236).
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

# Conservative allowlist for anything interpolated into a response header.
_FILENAME_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]+")

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "Permissions-Policy": "geolocation=(), microphone=(), camera=(), payment=(), usb=()",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Content-Security-Policy": (
        "default-src 'self'; "
        # Alpine.js and Tailwind's runtime both require inline+eval. This CSP is
        # therefore a clickjacking/mixed-content control, NOT an XSS control --
        # XSS defence lives in the templates (Alpine x-text, Jinja autoescape).
        "script-src 'self' 'unsafe-inline' 'unsafe-eval'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self' data:; "
        "connect-src 'self'; "
        "object-src 'none'; "
        "base-uri 'none'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
}


def apply_security_headers(response):
    """Flask after_request hook: attach defence-in-depth headers to every response."""
    for header, value in SECURITY_HEADERS.items():
        response.headers.setdefault(header, value)
    # Never let a proxy (e.g. Cloudflare) or browser cache an authenticated/diagnostic
    # API body or the root HTML shell (so asset cache-busting tags take effect immediately).
    if request.path.startswith("/api/") or request.path == "/":
        response.headers["Cache-Control"] = "no-store"
    return response


def _constant_time_match(supplied: str, expected: str) -> bool:
    """Compare two secrets without leaking length or content via timing."""
    if not supplied or not expected:
        return False
    return hmac.compare_digest(supplied, expected)


def _extract_bearer(header_value: str) -> str:
    """Pull the token out of an 'Authorization: Bearer <token>' header."""
    if header_value.startswith("Bearer "):
        return header_value[7:].strip()
    return ""


def require_secret(env_var: str, header_names: Tuple[str, ...] = ("X-Auth-Token",)):
    """
    Gate a route behind a shared secret held in `env_var`.

    Deny-by-default: if the environment variable is unset or empty the route is
    refused outright rather than silently running unauthenticated. This is the
    opposite of the original /api/collect behaviour, where a missing CRON_SECRET
    disabled the check instead of the endpoint.
    """
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            expected = os.environ.get(env_var, "").strip()
            if not expected:
                logger.warning(
                    "Refused %s: %s is not configured (deny-by-default).",
                    request.path, env_var,
                )
                return jsonify({
                    "error": "Endpoint disabled",
                    "detail": f"{env_var} is not configured on this server.",
                }), 503

            supplied = _extract_bearer(request.headers.get("Authorization", ""))
            for name in header_names:
                if not supplied:
                    supplied = request.headers.get(name, "").strip()

            if not _constant_time_match(supplied, expected):
                logger.warning("Rejected unauthenticated request to %s", request.path)
                return jsonify({"error": "Unauthorized"}), 401

            return fn(*args, **kwargs)
        return wrapper
    return decorator


class RateLimiter:
    """
    Minimal in-process fixed-window rate limiter.

    Deliberately dependency-free. It is per-worker, so with N Gunicorn workers the
    effective ceiling is N*limit -- enough to blunt a naive flood against the
    expensive Cloud Monitoring paths, not a substitute for an edge WAF rule.
    """

    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window = window_seconds
        self._hits: Dict[str, List[float]] = {}
        self._lock = Lock()

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        cutoff = now - self.window
        with self._lock:
            bucket = [t for t in self._hits.get(key, []) if t > cutoff]
            if len(bucket) >= self.limit:
                self._hits[key] = bucket
                return False
            bucket.append(now)
            self._hits[key] = bucket
            # Opportunistic sweep so the dict cannot grow without bound.
            if len(self._hits) > 1024:
                for k in [k for k, v in self._hits.items() if not any(t > cutoff for t in v)]:
                    self._hits.pop(k, None)
            return True


def _env_flag(name: str, default: bool = False) -> bool:
    return os.environ.get(name, str(default)).strip().lower() in ("1", "true", "yes")


def _trusted_proxy_hops() -> int:
    """
    Number of right-most X-Forwarded-For entries appended by trusted infrastructure.

    1 = Cloud Run / single reverse proxy (client IP is the last entry).
    2 = Google external Application Load Balancer (appends "<client>, <lb-ip>").
    0 = ignore X-Forwarded-For entirely and use the socket peer address.
    """
    try:
        return max(0, int(os.environ.get("TRUSTED_PROXY_HOPS", "1")))
    except ValueError:
        return 1


def _client_key() -> str:
    """
    Identify the caller for rate-limiting purposes.

    Left-most X-Forwarded-For entries and CF-Connecting-IP are client-controlled,
    so trusting them lets any caller rotate the header to bypass rate limits.
    Only the hop appended by trusted infrastructure is used:
      * CF-Connecting-IP is honoured only when TRUST_CLOUDFLARE=true (origin must
        be locked to Cloudflare, otherwise the header is forgeable).
      * Otherwise the entry TRUSTED_PROXY_HOPS positions from the right is used.
    """
    if _env_flag("TRUST_CLOUDFLARE"):
        cf_ip = request.headers.get("CF-Connecting-IP", "").strip()
        if cf_ip:
            return cf_ip

    hops = _trusted_proxy_hops()
    if hops > 0:
        chain = [h.strip() for h in request.headers.get("X-Forwarded-For", "").split(",") if h.strip()]
        if len(chain) >= hops:
            return chain[-hops]
    return request.remote_addr or "unknown"


def rate_limit(limiter: "RateLimiter"):
    """Reject callers that exceed `limiter` for this route."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not limiter.allow(f"{request.endpoint}:{_client_key()}"):
                return jsonify({
                    "error": "Too Many Requests",
                    "detail": "Rate limit exceeded for this endpoint.",
                }), 429
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def validate_project_id(value: str) -> str:
    """
    Validate a GCP project ID before it is interpolated into an API URL path.

    Without this, an unauthenticated settings write could steer the Cloud
    Monitoring request path and make the server query arbitrary projects using
    its own service-account token.
    """
    candidate = (value or "").strip()
    if not _PROJECT_ID_RE.match(candidate):
        raise ValueError(
            "Invalid GCP project ID. Expected 6-30 lowercase alphanumeric "
            "characters or hyphens, starting with a letter."
        )
    return candidate


def redact_project_id(value: Optional[str]) -> str:
    """Render a project ID safe for a public response body."""
    if not value:
        return "[REDACTED_PROJECT_ID]"
    return f"{value[:2]}{'*' * 6}"


def safe_filename_component(value: str, fallback: str = "export") -> str:
    """
    Strip anything that could break out of a Content-Disposition header.

    Prevents reflected-file-download and header-parameter injection via the
    `period` query string or a mutated project ID.
    """
    cleaned = _FILENAME_SAFE_RE.sub("_", (value or "").strip())[:64].strip("._-")
    return cleaned or fallback


def csv_safe(value) -> str:
    """
    Neutralise spreadsheet formula injection in exported CSV cells (CWE-1236).

    Upstream Cloud Monitoring labels such as collector_id are operator-supplied
    and reach Excel/Sheets unmodified without this.
    """
    if value is None:
        return ""
    text = str(value)
    if text.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + text
    return text
