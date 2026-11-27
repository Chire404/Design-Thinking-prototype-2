"""Environment-driven configuration for VeloPass."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_FILE = PROJECT_ROOT / ".env"

_PLACEHOLDER_SECRETS = {
    "",
    "change-this-secret-key-in-production-min-32-bytes",
    "change-this-qr-signing-secret-in-production-min-32-bytes",
}


def load_dotenv_file(env_path: Path = DEFAULT_ENV_FILE) -> None:
    """Lightweight .env loader without external runtime dependencies."""
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


load_dotenv_file()


class Config:
    """Centralized application settings loaded from environment variables."""

    def __init__(self, overrides: dict | None = None) -> None:
        overrides = overrides or {}

        self.APP_ENV: str = str(overrides.get("APP_ENV", os.getenv("APP_ENV", "development"))).lower()
        self.APP_NAME: str = str(overrides.get("APP_NAME", os.getenv("APP_NAME", "VeloPass")))
        self.APP_HOST: str = str(overrides.get("APP_HOST", os.getenv("APP_HOST", "127.0.0.1")))
        self.APP_PORT: int = int(overrides.get("APP_PORT", os.getenv("APP_PORT", "5002")))

        raw_secret = str(overrides.get("SECRET_KEY", os.getenv("SECRET_KEY", "")))
        raw_qr_secret = str(overrides.get("QR_HMAC_SECRET", os.getenv("QR_HMAC_SECRET", "")))

        if self.APP_ENV == "production":
            if raw_secret in _PLACEHOLDER_SECRETS or len(raw_secret) < 32:
                raise RuntimeError("SECRET_KEY must be set to a strong >=32 character value in production.")
            if raw_qr_secret in _PLACEHOLDER_SECRETS or len(raw_qr_secret) < 32:
                raise RuntimeError("QR_HMAC_SECRET must be set to a strong >=32 character value in production.")

        self.SECRET_KEY: str = (
            raw_secret if raw_secret not in _PLACEHOLDER_SECRETS else "dev-only-velopass-secret-key-not-for-production-use"
        )
        self.QR_HMAC_SECRET: str = (
            raw_qr_secret if raw_qr_secret not in _PLACEHOLDER_SECRETS else "dev-only-velopass-qr-hmac-secret-not-for-prod"
        )

        self.PAYMENT_WEBHOOK_PROVIDER: str = str(
            overrides.get("PAYMENT_WEBHOOK_PROVIDER", os.getenv("PAYMENT_WEBHOOK_PROVIDER", "SEPAY_COMPATIBLE"))
        )
        self.PAYMENT_WEBHOOK_SECRET: str = str(
            overrides.get("PAYMENT_WEBHOOK_SECRET", os.getenv("PAYMENT_WEBHOOK_SECRET", ""))
        )

        # Prototype 2: Optional env defaults for Automated Bank Account Reconciliation
        self.BANK_SYNC_PROVIDER: str = str(
            overrides.get("BANK_SYNC_PROVIDER", os.getenv("BANK_SYNC_PROVIDER", "SEPAY_API"))
        )
        self.BANK_SYNC_API_URL: str = str(
            overrides.get("BANK_SYNC_API_URL", os.getenv("BANK_SYNC_API_URL", ""))
        )
        self.BANK_SYNC_API_TOKEN: str = str(
            overrides.get("BANK_SYNC_API_TOKEN", os.getenv("BANK_SYNC_API_TOKEN", ""))
        )
        bg_poll = overrides.get("BANK_SYNC_BACKGROUND_ENABLED", os.getenv("BANK_SYNC_BACKGROUND_ENABLED", "false"))
        self.BANK_SYNC_BACKGROUND_ENABLED: bool = str(bg_poll).lower() in ("1", "true", "yes")
        self.BANK_SYNC_INTERVAL_SECONDS: int = int(
            overrides.get("BANK_SYNC_INTERVAL_SECONDS", os.getenv("BANK_SYNC_INTERVAL_SECONDS", "20"))
        )

        db_rel = str(overrides.get("DATABASE_PATH", os.getenv("DATABASE_PATH", "data/velopass.db")))
        db_path = Path(db_rel)
        if not db_path.is_absolute():
            db_path = PROJECT_ROOT / db_path
        self.DATABASE_PATH: Path = db_path

        cookie_sec = overrides.get("SESSION_COOKIE_SECURE", os.getenv("SESSION_COOKIE_SECURE", "false"))
        self.SESSION_COOKIE_SECURE: bool = str(cookie_sec).lower() in ("1", "true", "yes") or self.APP_ENV == "production"
        self.SESSION_TTL_HOURS: int = int(overrides.get("SESSION_TTL_HOURS", os.getenv("SESSION_TTL_HOURS", "24")))
        self.REMEMBER_ME_DAYS: int = int(overrides.get("REMEMBER_ME_DAYS", os.getenv("REMEMBER_ME_DAYS", "30")))

        self.DEFAULT_FARE_VND: int = int(overrides.get("DEFAULT_FARE_VND", os.getenv("DEFAULT_FARE_VND", "4000")))
        self.DEFAULT_QR_TTL_SECONDS: int = int(
            overrides.get("DEFAULT_QR_TTL_SECONDS", os.getenv("DEFAULT_QR_TTL_SECONDS", "90"))
        )
        self.LOW_BALANCE_THRESHOLD_VND: int = int(
            overrides.get("LOW_BALANCE_THRESHOLD_VND", os.getenv("LOW_BALANCE_THRESHOLD_VND", "20000"))
        )
        self.TOPUP_EXPIRY_MINUTES: int = int(
            overrides.get("TOPUP_EXPIRY_MINUTES", os.getenv("TOPUP_EXPIRY_MINUTES", "30"))
        )

        cors_raw = str(overrides.get("CORS_ALLOWED_ORIGINS", os.getenv("CORS_ALLOWED_ORIGINS", "")))
        self.CORS_ALLOWED_ORIGINS: list[str] = [o.strip() for o in cors_raw.split(",") if o.strip()]

        self.RATE_LIMIT_ENABLED: bool = bool(overrides.get("RATE_LIMIT_ENABLED", True))

    def to_dict(self) -> dict:
        return {k: v for k, v in self.__dict__.items() if k.isupper()}
