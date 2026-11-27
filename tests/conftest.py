"""Pytest fixtures providing isolated temporary SQLite database and authenticated clients."""

from __future__ import annotations

import hmac
import hashlib
import json
from pathlib import Path

import pytest

from backend import create_app
from backend.services.auth_service import create_admin_account
from backend.utils.security import rate_limiter

TEST_WEBHOOK_SECRET = "test-webhook-hmac-secret-key-velopass-2026-secure"
TEST_QR_SECRET = "test-qr-hmac-secret-key-velopass-2026-ultra-secure"
TEST_APP_SECRET = "test-app-secret-key-velopass-2026-ultra-secure-key"


@pytest.fixture()
def app(tmp_path: Path):
    rate_limiter.reset()
    db_file = tmp_path / "test_velopass.db"
    flask_app = create_app(
        {
            "APP_ENV": "testing",
            "DATABASE_PATH": str(db_file),
            "SECRET_KEY": TEST_APP_SECRET,
            "QR_HMAC_SECRET": TEST_QR_SECRET,
            "PAYMENT_WEBHOOK_SECRET": TEST_WEBHOOK_SECRET,
            "PAYMENT_WEBHOOK_PROVIDER": "SEPAY_COMPATIBLE",
            "RATE_LIMIT_ENABLED": False,
        }
    )
    with flask_app.app_context():
        create_admin_account(
            username="admin_gate",
            email="admin1@velopass.vn",
            password="AdminPassword@123",
            full_name="Quản Trị Cổng Số 1",
            super_admin=True,
        )
        create_admin_account(
            username="admin_gate_2",
            email="admin2@velopass.vn",
            password="AdminPassword@123",
            full_name="Quản Trị Cổng Số 2",
            super_admin=False,
        )
    yield flask_app


def sign_webhook_body(payload: dict, secret: str = TEST_WEBHOOK_SECRET) -> tuple[bytes, str]:
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return raw, f"sha256={sig}"
