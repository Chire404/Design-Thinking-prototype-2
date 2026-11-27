"""SQLite WAL connection manager, atomic BEGIN IMMEDIATE transactions, migrations & seeding."""

from __future__ import annotations

import sqlite3
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from flask import current_app, has_app_context

from backend.config import PROJECT_ROOT
from backend.utils.formatters import utc_now_iso

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
_WRITE_LOCK = threading.RLock()


def resolve_db_path(db_path: Path | str | None = None) -> Path:
    if db_path is not None:
        p = Path(db_path)
    elif has_app_context():
        p = Path(current_app.config["DATABASE_PATH"])
    else:
        p = PROJECT_ROOT / "data" / "velopass.db"
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def open_connection(db_path: Path | str | None = None) -> sqlite3.Connection:
    """Open a configured SQLite connection with foreign keys and WAL enabled."""
    target = resolve_db_path(db_path)
    conn = sqlite3.connect(
        str(target),
        timeout=30.0,
        isolation_level=None,  # Explicit transaction control
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 30000;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


@contextmanager
def read_db(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Read-oriented database connection context manager."""
    conn = open_connection(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def atomic_transaction(db_path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    """Execute an atomic database write transaction with BEGIN IMMEDIATE and rollback safety.

    Combines SQLite's BEGIN IMMEDIATE reserved write lock with a process write lock and
    retry backoff for transient SQLITE_BUSY contention so concurrent scans/webhooks
    are strictly serialized without partial state or double-charging.
    """
    with _WRITE_LOCK:
        conn = open_connection(db_path)
        begun = False
        try:
            for attempt in range(8):
                try:
                    conn.execute("BEGIN IMMEDIATE;")
                    begun = True
                    break
                except sqlite3.OperationalError as exc:
                    if "locked" in str(exc).lower() and attempt < 7:
                        time.sleep(0.015 * (attempt + 1))
                        continue
                    raise
            yield conn
            conn.execute("COMMIT;")
        except Exception:
            if begun:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    pass
            raise
        finally:
            conn.close()


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    return dict(row)


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict]:
    return [dict(r) for r in rows]


def run_migrations(db_path: Path | str | None = None) -> list[str]:
    """Apply pending SQL migrations in deterministic order and initialize default singleton settings."""
    applied: list[str] = []
    target = resolve_db_path(db_path)
    conn = open_connection(target)
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version TEXT PRIMARY KEY,
                applied_at TEXT NOT NULL
            );
            """
        )
        existing = {
            row["version"]
            for row in conn.execute("SELECT version FROM schema_migrations").fetchall()
        }
        sql_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
        for sql_file in sql_files:
            version = sql_file.name
            if version in existing:
                continue
            script = sql_file.read_text(encoding="utf-8")
            conn.executescript(script)
            conn.execute(
                "INSERT INTO schema_migrations (version, applied_at) VALUES (?, ?)",
                (version, utc_now_iso()),
            )
            applied.append(version)

        now = utc_now_iso()
        conn.execute("BEGIN IMMEDIATE;")
        conn.execute(
            """
            INSERT OR IGNORE INTO bank_settings (
                id, bank_name, bank_short_name, bank_bin, account_number,
                account_holder, bank_qr_url, transfer_prefix, payment_instructions,
                is_active, updated_at
            ) VALUES (
                1, 'Ngân hàng TMCP Ngoại thương Việt Nam (Vietcombank)', 'VCB', '970436',
                '1048689999', 'CONG TY TNHH VELOPASS TRANSIT', '', 'VP',
                'Chuyển khoản đúng số tiền và giữ nguyên nội dung chuyển khoản để hệ thống tự động đối soát.',
                1, ?
            );
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO system_settings (
                id, fare_amount, qr_ttl_seconds, low_balance_threshold,
                topup_min_amount, topup_max_amount, topup_expiry_minutes,
                notifications_enabled, updated_at
            ) VALUES (1, 4000, 90, 20000, 4000, 10000000, 30, 1, ?);
            """,
            (now,),
        )
        # Prototype 2 Defaults: Monthly Pass Plans & Bank Reconciliation Settings
        conn.execute(
            """
            INSERT OR IGNORE INTO pass_plans (
                id, code, name, description, price, duration_days, max_trips, badge_text, is_active, created_at, updated_at
            ) VALUES
            (
                1, 'MONTHLY_STANDARD', 'Vé Tháng Tiêu Chuẩn (30 Ngày)',
                'Đi lại không giới hạn số lượt quét QR trong 30 ngày liên tục. Tiết kiệm tối ưu cho người đi làm hằng ngày.',
                100000, 30, NULL, 'KHÔNG GIỚI HẠN LƯỢT', 1, ?, ?
            ),
            (
                2, 'MONTHLY_STUDENT', 'Vé Tháng Ưu Đãi HSSV (30 Ngày)',
                'Gói vé tháng ưu đãi dành cho học sinh, sinh viên — quét cổng 0 VNĐ không giới hạn lượt trong 30 ngày.',
                60000, 30, NULL, 'ƯU ĐÃI SINH VIÊN', 1, ?, ?
            ),
            (
                3, 'WEEKLY_COMMUTER', 'Vé Tuần Linh Hoạt (7 Ngày)',
                'Trọn gói 7 ngày di chuyển không giới hạn số lần quét cổng.',
                30000, 7, NULL, 'GÓI 7 NGÀY', 1, ?, ?
            );
            """,
            (now, now, now, now, now, now),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO bank_sync_settings (
                id, auto_reconcile_enabled, background_poll_enabled, provider,
                api_url, api_token, poll_interval_seconds, strict_account_match,
                allow_expired_grace_minutes, updated_at
            ) VALUES (
                1, 1, 0, 'SEPAY_API', '', '', 20, 1, 120, ?
            );
            """,
            (now,),
        )
        conn.execute("COMMIT;")
    finally:
        conn.close()
    return applied
