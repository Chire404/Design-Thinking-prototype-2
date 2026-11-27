"""Cryptographic helpers, rate limiting, input validation, and webhook HMAC verification."""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import defaultdict, deque

from werkzeug.security import check_password_hash, generate_password_hash

USERNAME_RE = re.compile(r"^[a-zA-Z0-9_.-]{3,32}$")
EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")


def hash_password(password: str) -> str:
    return generate_password_hash(password, method="scrypt")


def verify_password(password_hash: str, password: str) -> bool:
    try:
        return check_password_hash(password_hash, password)
    except ValueError:
        return False


def sha256_hex(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def generate_session_token() -> tuple[str, str, str]:
    """Return (raw_session_token, session_token_hash, csrf_token)."""
    raw = secrets.token_urlsafe(36)
    csrf = secrets.token_hex(24)
    return raw, sha256_hex(raw), csrf


def create_signed_qr_token(qr_hmac_secret: str) -> tuple[str, str]:
    """Generate a high-entropy single-use QR token containing zero PII or balance.

    Format: VPQR1.<nonce>.<hmac16>
    Returns (raw_qr_token, sha256_token_hash).
    """
    nonce = secrets.token_urlsafe(24)
    sig = hmac.new(
        qr_hmac_secret.encode("utf-8"),
        f"VPQR1.{nonce}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:20]
    raw_token = f"VPQR1.{nonce}.{sig}"
    return raw_token, sha256_hex(raw_token)


def verify_signed_qr_token_structure(raw_token: str, qr_hmac_secret: str) -> bool:
    """Fast constant-time structural & HMAC check before database lookup."""
    if not raw_token or not isinstance(raw_token, str):
        return False
    parts = raw_token.strip().split(".")
    if len(parts) != 3 or parts[0] != "VPQR1" or len(parts[1]) < 16 or len(parts[2]) != 20:
        return False
    expected = hmac.new(
        qr_hmac_secret.encode("utf-8"),
        f"VPQR1.{parts[1]}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:20]
    return hmac.compare_digest(parts[2], expected)


def verify_webhook_signature(raw_body: bytes, signature_header: str, webhook_secret: str) -> bool:
    """Verify HMAC-SHA256 webhook signature (supports plain hex or 'sha256=<hex>')."""
    if not webhook_secret or not signature_header:
        return False
    cleaned = signature_header.strip()
    if cleaned.lower().startswith("sha256="):
        cleaned = cleaned.split("=", 1)[1].strip()
    expected = hmac.new(webhook_secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(cleaned.lower(), expected.lower())


def validate_positive_vnd_integer(value, field_name: str = "amount") -> int:
    """Ensure monetary input is strictly an exact integer (rejects floats, booleans, negatives)."""
    if isinstance(value, bool):
        raise ValueError(f"{field_name} không hợp lệ.")
    if isinstance(value, float):
        raise ValueError(f"{field_name} phải là số nguyên VNĐ, không nhận số thập phân.")
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped.isdigit():
            raise ValueError(f"{field_name} phải là số nguyên dương VNĐ.")
        parsed = int(stripped)
    elif isinstance(value, int):
        parsed = value
    else:
        raise ValueError(f"{field_name} không hợp lệ.")
    if parsed <= 0:
        raise ValueError(f"{field_name} phải lớn hơn 0 VNĐ.")
    return parsed


class SlidingWindowRateLimiter:
    """Thread-safe sliding window rate limiter for login, webhook, and mutation endpoints."""

    def __init__(self) -> None:
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key: str, max_requests: int, window_seconds: int) -> tuple[bool, int]:
        now = time.monotonic()
        cutoff = now - window_seconds
        with self._lock:
            dq = self._buckets[key]
            while dq and dq[0] <= cutoff:
                dq.popleft()
            if len(dq) >= max_requests:
                retry_after = max(1, int(window_seconds - (now - dq[0])) + 1)
                return False, retry_after
            dq.append(now)
            return True, 0

    def reset(self) -> None:
        with self._lock:
            self._buckets.clear()


rate_limiter = SlidingWindowRateLimiter()
