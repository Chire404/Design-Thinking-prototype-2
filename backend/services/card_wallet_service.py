"""Wallet ledger, Virtual Card state machine (with Monthly Pass priority), and notification helpers."""

from __future__ import annotations

import json
import sqlite3

from backend.database import read_db, row_to_dict, rows_to_dicts
from backend.services import AppError
from backend.services.pass_service import (
    get_active_pass_in_tx,
    get_active_pass_readonly,
    list_pass_plans,
)
from backend.utils.formatters import format_vnd, utc_now_iso
from backend.utils.realtime import realtime_broker


def get_system_settings_in_tx(conn: sqlite3.Connection) -> dict:
    row = conn.execute("SELECT * FROM system_settings WHERE id = 1").fetchone()
    if row is None:
        return {
            "fare_amount": 4000,
            "qr_ttl_seconds": 90,
            "low_balance_threshold": 20000,
            "topup_min_amount": 4000,
            "topup_max_amount": 10000000,
            "topup_expiry_minutes": 30,
            "notifications_enabled": 1,
        }
    return dict(row)


def compute_card_status(
    user_status: str,
    balance: int,
    fare_amount: int,
    has_active_pass: bool = False,
) -> str:
    """Compute canonical Virtual Card status from backend state.

    A card is ACTIVE whenever User is ACTIVE and EITHER holds a valid Monthly Pass OR has balance >= fare_amount.
    """
    if user_status == "SUSPENDED":
        return "SUSPENDED"
    if user_status == "PENDING":
        return "PENDING"
    if has_active_pass or balance >= fare_amount:
        return "ACTIVE"
    return "INSUFFICIENT_BALANCE"


def sync_card_status_in_tx(conn: sqlite3.Connection, user_id: int) -> dict:
    """Synchronize virtual_cards.status with user status, Monthly Pass, wallet balance, and fare rules."""
    settings = get_system_settings_in_tx(conn)
    fare = int(settings["fare_amount"])
    user_row = conn.execute("SELECT id, status FROM users WHERE id = ?", (user_id,)).fetchone()
    wallet_row = conn.execute("SELECT id, balance, status FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
    card_row = conn.execute("SELECT * FROM virtual_cards WHERE user_id = ?", (user_id,)).fetchone()
    if not user_row or not wallet_row or not card_row:
        raise AppError("USER_NOT_FOUND", "Không tìm thấy thông tin thẻ hoặc ví người dùng.", 404)

    active_pass = get_active_pass_in_tx(conn, user_id)
    effective_user_status = "SUSPENDED" if wallet_row["status"] == "LOCKED" else user_row["status"]
    new_status = compute_card_status(
        effective_user_status,
        int(wallet_row["balance"]),
        fare,
        has_active_pass=(active_pass is not None),
    )
    now = utc_now_iso()

    if card_row["status"] != new_status:
        conn.execute(
            "UPDATE virtual_cards SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now, card_row["id"]),
        )

    if new_status != "ACTIVE":
        conn.execute(
            "UPDATE qr_tokens SET status = 'REVOKED' WHERE card_id = ? AND status = 'ACTIVE'",
            (card_row["id"],),
        )

    updated = conn.execute("SELECT * FROM virtual_cards WHERE id = ?", (card_row["id"],)).fetchone()
    return dict(updated)


def create_notification_in_tx(
    conn: sqlite3.Connection,
    user_id: int,
    notif_type: str,
    title: str,
    message: str,
    metadata: dict | None = None,
) -> dict | None:
    settings = get_system_settings_in_tx(conn)
    if not int(settings.get("notifications_enabled", 1)):
        return None
    now = utc_now_iso()
    meta_json = json.dumps(metadata or {}, ensure_ascii=False)
    cur = conn.execute(
        """
        INSERT INTO notifications (user_id, type, title, message, metadata_json, is_read, created_at)
        VALUES (?, ?, ?, ?, ?, 0, ?)
        """,
        (user_id, notif_type, title, message, meta_json, now),
    )
    return {
        "id": cur.lastrowid,
        "user_id": user_id,
        "type": notif_type,
        "title": title,
        "message": message,
        "metadata": metadata or {},
        "is_read": 0,
        "created_at": now,
    }


def emit_user_realtime_update(user_id: int, event_type: str, payload: dict) -> None:
    realtime_broker.publish_to_user(user_id, event_type, payload)


def get_user_wallet_and_card_snapshot(user_id: int) -> dict:
    """Return full User dashboard/card/wallet/monthly-pass snapshot with formatted VND strings."""
    with read_db() as conn:
        settings = get_system_settings_in_tx(conn)
        fare = int(settings["fare_amount"])
        user = row_to_dict(
            conn.execute(
                "SELECT id, username, full_name, email, phone, role, status, created_at FROM users WHERE id = ?",
                (user_id,),
            ).fetchone()
        )
        if not user:
            raise AppError("USER_NOT_FOUND", "Không tìm thấy tài khoản.", 404)

        wallet = row_to_dict(conn.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,)).fetchone())
        card = row_to_dict(conn.execute("SELECT * FROM virtual_cards WHERE user_id = ?", (user_id,)).fetchone())
        active_pass = get_active_pass_readonly(conn, user_id)

        expected_card_status = compute_card_status(
            "SUSPENDED" if wallet["status"] == "LOCKED" else user["status"],
            int(wallet["balance"]),
            fare,
            has_active_pass=(active_pass is not None),
        )
        card["status"] = expected_card_status
        card["qr_eligible"] = expected_card_status == "ACTIVE"
        card["has_active_pass"] = active_pass is not None
        card["fare_amount"] = fare
        card["fare_formatted"] = format_vnd(fare)

        wallet["balance_formatted"] = format_vnd(wallet["balance"])
        wallet["total_topup_formatted"] = format_vnd(wallet["total_topup"])
        wallet["total_spent_formatted"] = format_vnd(wallet["total_spent"])
        wallet["remaining_trips"] = int(wallet["balance"]) // fare if fare > 0 else 0
        wallet["low_balance"] = (
            active_pass is None and 0 < int(wallet["balance"]) < int(settings["low_balance_threshold"])
        )

        recent_usage = rows_to_dicts(
            conn.execute(
                """
                SELECT id, transaction_code, amount, payment_method, pass_id,
                       balance_before, balance_after, status, created_at
                FROM usage_transactions
                WHERE user_id = ?
                ORDER BY id DESC LIMIT 10
                """,
                (user_id,),
            ).fetchall()
        )
        for u in recent_usage:
            if u.get("payment_method") == "MONTHLY_PASS":
                u["amount_formatted"] = "0 VNĐ (Vé Tháng)"
            else:
                u["amount_formatted"] = f"-{format_vnd(u['amount'])}"
            u["balance_before_formatted"] = format_vnd(u["balance_before"])
            u["balance_after_formatted"] = format_vnd(u["balance_after"])

        recent_topups = rows_to_dicts(
            conn.execute(
                """
                SELECT id, transaction_code, transfer_content, amount, bank_name, bank_short_name,
                       payment_provider, status, expires_at, created_at, confirmed_at
                FROM topup_transactions
                WHERE user_id = ?
                ORDER BY id DESC LIMIT 10
                """,
                (user_id,),
            ).fetchall()
        )
        for t in recent_topups:
            t["amount_formatted"] = format_vnd(t["amount"])

        notifications = rows_to_dicts(
            conn.execute(
                """
                SELECT id, type, title, message, metadata_json, is_read, created_at
                FROM notifications
                WHERE user_id = ?
                ORDER BY id DESC LIMIT 20
                """,
                (user_id,),
            ).fetchall()
        )
        unread_count = conn.execute(
            "SELECT COUNT(*) AS cnt FROM notifications WHERE user_id = ? AND is_read = 0",
            (user_id,),
        ).fetchone()["cnt"]

        sync_cfg = row_to_dict(
            conn.execute(
                "SELECT auto_reconcile_enabled, background_poll_enabled, provider FROM bank_sync_settings WHERE id = 1"
            ).fetchone()
        ) or {}

    return {
        "user": user,
        "wallet": wallet,
        "card": card,
        "active_pass": active_pass,
        "pass_plans": list_pass_plans(include_inactive=False),
        "bank_auto_sync": {
            "auto_reconcile_enabled": bool(int(sync_cfg.get("auto_reconcile_enabled", 1))),
            "background_poll_enabled": bool(int(sync_cfg.get("background_poll_enabled", 0))),
            "provider": sync_cfg.get("provider", "SEPAY_API"),
        },
        "system": {
            "fare_amount": fare,
            "fare_formatted": format_vnd(fare),
            "qr_ttl_seconds": int(settings["qr_ttl_seconds"]),
            "low_balance_threshold": int(settings["low_balance_threshold"]),
            "topup_min_amount": int(settings["topup_min_amount"]),
            "topup_max_amount": int(settings["topup_max_amount"]),
        },
        "recent_usage": recent_usage,
        "recent_topups": recent_topups,
        "notifications": notifications,
        "unread_notifications": int(unread_count),
    }
