"""Top-up order lifecycle, VietQR payload generation, signed Webhook processor, and Admin manual verification."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta

from backend.database import atomic_transaction, read_db, row_to_dict, rows_to_dicts
from backend.services import AppError
from backend.services.card_wallet_service import (
    create_notification_in_tx,
    emit_user_realtime_update,
    get_system_settings_in_tx,
    sync_card_status_in_tx,
)
from backend.services.qr_service import _issue_qr_token_in_tx
from backend.utils.formatters import (
    format_vnd,
    generate_transfer_content,
    generate_tx_code,
    parse_iso,
    utc_now,
    utc_now_iso,
)
from backend.utils.qr_svg import build_topup_qr_bundle
from backend.utils.realtime import realtime_broker
from backend.utils.security import validate_positive_vnd_integer, verify_webhook_signature


def expire_stale_topups_in_tx(conn: sqlite3.Connection) -> None:
    now_iso = utc_now_iso()
    conn.execute(
        """
        UPDATE topup_transactions
        SET status = 'EXPIRED', updated_at = ?
        WHERE status = 'PENDING' AND expires_at <= ?
        """,
        (now_iso, now_iso),
    )


def create_topup_order(user_id: int, raw_amount) -> dict:
    """Create a PENDING Top-up order with a unique transfer code and EMVCo VietQR."""
    try:
        amount = validate_positive_vnd_integer(raw_amount, "Số tiền nạp")
    except ValueError as exc:
        raise AppError("INVALID_AMOUNT", str(exc), 400) from exc

    post_events: list[tuple[int, str, dict]] = []

    with atomic_transaction() as conn:
        expire_stale_topups_in_tx(conn)
        settings = get_system_settings_in_tx(conn)
        min_amt = int(settings["topup_min_amount"])
        max_amt = int(settings["topup_max_amount"])
        expiry_min = int(settings["topup_expiry_minutes"])

        if amount < min_amt or amount > max_amt:
            raise AppError(
                "AMOUNT_OUT_OF_RANGE",
                f"Số tiền nạp phải từ {format_vnd(min_amt)} đến {format_vnd(max_amt)}.",
                400,
            )

        user = conn.execute("SELECT id, status FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise AppError("USER_NOT_FOUND", "Không tìm thấy người dùng.", 404)
        if user["status"] == "SUSPENDED":
            raise AppError("SUSPENDED_ACCOUNT", "Tài khoản đang bị khóa, không thể tạo lệnh nạp tiền.", 403)

        bank = row_to_dict(conn.execute("SELECT * FROM bank_settings WHERE id = 1").fetchone())
        if not bank or not int(bank["is_active"]):
            raise AppError(
                "BANK_UNAVAILABLE",
                "Hệ thống tài khoản ngân hàng nhận tiền đang bảo trì. Vui lòng thử lại sau.",
                503,
            )

        tx_code = generate_tx_code("TOP")
        transfer_content = generate_transfer_content(bank.get("transfer_prefix") or "VP")
        while conn.execute(
            "SELECT 1 FROM topup_transactions WHERE transfer_content = ?", (transfer_content,)
        ).fetchone():
            transfer_content = generate_transfer_content(bank.get("transfer_prefix") or "VP")

        now_dt = utc_now()
        now_iso = utc_now_iso()
        expires_iso = (now_dt + timedelta(minutes=expiry_min)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        cur = conn.execute(
            """
            INSERT INTO topup_transactions (
                user_id, transaction_code, transfer_content, amount,
                bank_name, bank_short_name, bank_bin, account_number, account_holder,
                payment_provider, status, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'BANK_TRANSFER', 'PENDING', ?, ?, ?)
            """,
            (
                user_id,
                tx_code,
                transfer_content,
                amount,
                bank["bank_name"],
                bank["bank_short_name"],
                bank["bank_bin"],
                bank["account_number"],
                bank["account_holder"],
                expires_iso,
                now_iso,
                now_iso,
            ),
        )
        topup_id = int(cur.lastrowid)

        notif = create_notification_in_tx(
            conn,
            user_id=user_id,
            notif_type="TOPUP_PENDING",
            title="Đơn nạp tiền đang chờ thanh toán",
            message=(
                f"Mã đơn {tx_code}: Chuyển khoản đúng {format_vnd(amount)} "
                f"với nội dung {transfer_content}."
            ),
            metadata={"topup_id": topup_id, "transaction_code": tx_code, "amount": amount},
        )
        if notif:
            post_events.append((user_id, "notification", notif))

    for uid, ev, data in post_events:
        emit_user_realtime_update(uid, ev, data)
    realtime_broker.publish_to_admins(
        "topup_created",
        {"topup_id": topup_id, "transaction_code": tx_code, "amount": amount, "transfer_content": transfer_content},
    )

    qr_bundle = build_topup_qr_bundle(bank, amount, transfer_content)
    return {
        "id": topup_id,
        "transaction_code": tx_code,
        "transfer_content": transfer_content,
        "amount": amount,
        "amount_formatted": format_vnd(amount),
        "status": "PENDING",
        "expires_at": expires_iso,
        "created_at": now_iso,
        "bank": {
            "bank_name": bank["bank_name"],
            "bank_short_name": bank["bank_short_name"],
            "bank_bin": bank["bank_bin"],
            "account_number": bank["account_number"],
            "account_holder": bank["account_holder"],
            "payment_instructions": bank["payment_instructions"],
        },
        "qr": qr_bundle,
    }


def get_user_topups(user_id: int) -> list[dict]:
    with atomic_transaction() as conn:
        expire_stale_topups_in_tx(conn)
        rows = rows_to_dicts(
            conn.execute(
                "SELECT * FROM topup_transactions WHERE user_id = ? ORDER BY id DESC LIMIT 50",
                (user_id,),
            ).fetchall()
        )
    for r in rows:
        r["amount_formatted"] = format_vnd(r["amount"])
    return rows


def get_topup_detail(user_id: int, topup_id: int, is_admin: bool = False) -> dict:
    with atomic_transaction() as conn:
        expire_stale_topups_in_tx(conn)
        row = row_to_dict(conn.execute("SELECT * FROM topup_transactions WHERE id = ?", (topup_id,)).fetchone())
        if not row:
            raise AppError("TOPUP_NOT_FOUND", "Không tìm thấy giao dịch nạp tiền.", 404)
        if not is_admin and int(row["user_id"]) != int(user_id):
            raise AppError("FORBIDDEN", "Bạn không có quyền xem giao dịch nạp tiền này.", 403)
        bank = row_to_dict(conn.execute("SELECT * FROM bank_settings WHERE id = 1").fetchone()) or {}

    row["amount_formatted"] = format_vnd(row["amount"])
    if row["status"] in ("PENDING", "PROCESSING"):
        row["qr"] = build_topup_qr_bundle(
            {
                "bank_bin": row.get("bank_bin") or bank.get("bank_bin") or "970436",
                "account_number": row["account_number"],
                "bank_qr_url": bank.get("bank_qr_url") or "",
            },
            int(row["amount"]),
            row["transfer_content"],
        )
    return row


def user_report_transferred(user_id: int, topup_id: int, note: str = "") -> dict:
    """Allow User to mark PENDING -> PROCESSING for Admin statement review.

    SECURITY: Never credits balance! Wallet balance is ONLY credited by verified webhook or Admin confirmation.
    """
    now_iso = utc_now_iso()
    with atomic_transaction() as conn:
        expire_stale_topups_in_tx(conn)
        row = conn.execute(
            "SELECT * FROM topup_transactions WHERE id = ? AND user_id = ?",
            (topup_id, user_id),
        ).fetchone()
        if not row:
            raise AppError("TOPUP_NOT_FOUND", "Không tìm thấy yêu cầu nạp tiền.", 404)
        if row["status"] != "PENDING":
            raise AppError("INVALID_STATUS", f"Giao dịch hiện ở trạng thái {row['status']}, không thể báo chuyển khoản.", 400)

        conn.execute(
            """
            UPDATE topup_transactions
            SET status = 'PROCESSING', user_reported_at = ?, note = ?, updated_at = ?
            WHERE id = ? AND status = 'PENDING'
            """,
            (now_iso, (note or "")[:200], now_iso, topup_id),
        )

    realtime_broker.publish_to_admins(
        "topup_processing",
        {"topup_id": topup_id, "transaction_code": row["transaction_code"], "amount": row["amount"]},
    )
    return get_topup_detail(user_id, topup_id)


def cancel_user_topup(user_id: int, topup_id: int) -> dict:
    now_iso = utc_now_iso()
    with atomic_transaction() as conn:
        row = conn.execute(
            "SELECT * FROM topup_transactions WHERE id = ? AND user_id = ?",
            (topup_id, user_id),
        ).fetchone()
        if not row:
            raise AppError("TOPUP_NOT_FOUND", "Không tìm thấy giao dịch nạp tiền.", 404)
        if row["status"] != "PENDING":
            raise AppError("CANNOT_CANCEL", "Chỉ có thể hủy đơn nạp tiền đang ở trạng thái chờ thanh toán (PENDING).", 400)
        conn.execute(
            "UPDATE topup_transactions SET status = 'CANCELLED', updated_at = ? WHERE id = ? AND status = 'PENDING'",
            (now_iso, topup_id),
        )
    return get_topup_detail(user_id, topup_id)


def _credit_topup_in_tx(
    conn: sqlite3.Connection,
    topup_row: sqlite3.Row,
    provider: str,
    bank_reference: str,
    qr_hmac_secret: str,
    admin_id: int | None = None,
    note: str = "",
) -> tuple[dict, list[tuple[int, str, dict]]]:
    """Atomically settle a PENDING/PROCESSING top-up, credit Wallet, record Ledger, and reactivate QR if eligible."""
    now_iso = utc_now_iso()
    topup_id = int(topup_row["id"])
    user_id = int(topup_row["user_id"])
    amount = int(topup_row["amount"])

    # Conditional update ensures even concurrent webhook + admin click NEVER credit twice!
    upd = conn.execute(
        """
        UPDATE topup_transactions
        SET status = 'SUCCESS',
            payment_provider = ?,
            bank_reference = ?,
            confirmed_by_admin_id = ?,
            note = CASE WHEN ? != '' THEN ? ELSE note END,
            confirmed_at = ?,
            updated_at = ?
        WHERE id = ? AND status IN ('PENDING', 'PROCESSING')
        """,
        (provider, bank_reference, admin_id, note, note, now_iso, now_iso, topup_id),
    )
    if upd.rowcount != 1:
        raise AppError("ALREADY_PROCESSED", "Giao dịch nạp tiền này đã được xử lý trước đó.", 409)

    wallet = conn.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
    balance_before = int(wallet["balance"])
    balance_after = balance_before + amount

    conn.execute(
        """
        UPDATE wallets
        SET balance = balance + ?,
            total_topup = total_topup + ?,
            updated_at = ?
        WHERE id = ? AND user_id = ?
        """,
        (amount, amount, now_iso, wallet["id"], user_id),
    )

    wallet_tx_code = generate_tx_code("WLT")
    conn.execute(
        """
        INSERT INTO wallet_transactions (
            transaction_code, user_id, wallet_id, transaction_type,
            amount, balance_before, balance_after, reference_type, reference_id,
            admin_id, description, created_at
        ) VALUES (?, ?, ?, 'TOPUP', ?, ?, ?, 'TOPUP_TRANSACTION', ?, ?, ?, ?)
        """,
        (
            wallet_tx_code,
            user_id,
            wallet["id"],
            amount,
            balance_before,
            balance_after,
            topup_row["transaction_code"],
            admin_id,
            f"Nạp tiền thành công ({topup_row['transaction_code']}) +{format_vnd(amount)}",
            now_iso,
        ),
    )

    # Sync card status (reactivating INSUFFICIENT_BALANCE -> ACTIVE when balance >= 4.000 VND!)
    settings = get_system_settings_in_tx(conn)
    card = sync_card_status_in_tx(conn, user_id)
    next_qr = None
    if card["status"] == "ACTIVE":
        next_qr = _issue_qr_token_in_tx(
            conn=conn,
            user_id=user_id,
            card_id=int(card["id"]),
            card_version=int(card["card_version"]),
            ttl_seconds=int(settings["qr_ttl_seconds"]),
            qr_hmac_secret=qr_hmac_secret,
        )

    events: list[tuple[int, str, dict]] = []
    notif = create_notification_in_tx(
        conn,
        user_id=user_id,
        notif_type="TOPUP_SUCCESS",
        title="Nạp tiền thành công",
        message=f"Ví của bạn vừa được cộng +{format_vnd(amount)}. Số dư mới: {format_vnd(balance_after)}.",
        metadata={
            "topup_id": topup_id,
            "transaction_code": topup_row["transaction_code"],
            "amount": amount,
            "balance_after": balance_after,
        },
    )
    if notif:
        events.append((user_id, "notification", notif))

    events.append(
        (
            user_id,
            "topup_confirmed",
            {
                "topup_id": topup_id,
                "transaction_code": topup_row["transaction_code"],
                "amount": amount,
                "amount_formatted": format_vnd(amount),
                "balance_before": balance_before,
                "balance_after": balance_after,
                "balance_formatted": format_vnd(balance_after),
                "card_status": card["status"],
                "next_qr": next_qr,
            },
        )
    )

    summary = {
        "topup_id": topup_id,
        "transaction_code": topup_row["transaction_code"],
        "user_id": user_id,
        "amount": amount,
        "amount_formatted": format_vnd(amount),
        "balance_before": balance_before,
        "balance_after": balance_after,
        "balance_after_formatted": format_vnd(balance_after),
        "card_status": card["status"],
        "bank_reference": bank_reference,
        "status": "SUCCESS",
    }
    return summary, events


def process_payment_webhook(
    raw_body: bytes,
    signature_header: str,
    payload: dict,
    webhook_secret: str,
    provider_name: str,
    qr_hmac_secret: str,
) -> dict:
    """Process authenticated bank payment webhook with replay/duplicate protection."""
    if not webhook_secret:
        raise AppError(
            "WEBHOOK_NOT_CONFIGURED",
            "PAYMENT_WEBHOOK_SECRET chưa được cấu hình. Hệ thống đang ở chế độ Admin duyệt sao kê thủ công.",
            503,
        )

    if not verify_webhook_signature(raw_body, signature_header, webhook_secret):
        raise AppError("INVALID_WEBHOOK_SIGNATURE", "Chữ ký xác thực webhook không hợp lệ.", 401)

    bank_ref = str(payload.get("bank_reference") or payload.get("referenceCode") or payload.get("id") or "").strip()
    event_key = str(payload.get("event_id") or payload.get("id") or bank_ref).strip()
    raw_content = str(
        payload.get("transfer_content") or payload.get("content") or payload.get("description") or ""
    ).upper()

    if not bank_ref or not event_key:
        raise AppError("INVALID_WEBHOOK_PAYLOAD", "Thiếu mã tham chiếu giao dịch ngân hàng (bank_reference).", 400)

    try:
        amount = validate_positive_vnd_integer(
            payload.get("amount") or payload.get("transferAmount"),
            "Webhook amount",
        )
    except ValueError as exc:
        raise AppError("INVALID_WEBHOOK_AMOUNT", str(exc), 400) from exc

    events_to_emit: list[tuple[int, str, dict]] = []

    with atomic_transaction() as conn:
        # 1. Idempotency check on webhook event_key OR bank_reference
        dup = conn.execute(
            "SELECT id, status, topup_id FROM payment_webhook_events WHERE event_key = ? OR bank_reference = ?",
            (event_key, bank_ref),
        ).fetchone()
        if dup:
            return {
                "duplicate": True,
                "status": "ALREADY_PROCESSED",
                "topup_id": dup["topup_id"],
                "message": "Sự kiện webhook trùng lặp đã được bỏ qua an toàn.",
            }

        # 2. Match transfer_content cleanly (exact match or token inside transfer description)
        topup_row = conn.execute(
            "SELECT * FROM topup_transactions WHERE transfer_content = ?",
            (raw_content.strip(),),
        ).fetchone()
        if not topup_row:
            tokens = re.findall(r"[A-Z0-9]{8,20}", raw_content)
            for tok in tokens:
                candidate = conn.execute(
                    "SELECT * FROM topup_transactions WHERE transfer_content = ?",
                    (tok,),
                ).fetchone()
                if candidate:
                    topup_row = candidate
                    break

        now_iso = utc_now_iso()
        payload_str = json.dumps(payload, ensure_ascii=False)

        if not topup_row:
            raise AppError("ORDER_NOT_FOUND", "Không tìm thấy lệnh nạp tiền khớp với nội dung chuyển khoản.", 404)

        webhook_err: AppError | None = None
        already_confirmed_resp: dict | None = None

        if int(topup_row["amount"]) != amount:
            conn.execute(
                """
                INSERT INTO payment_webhook_events (
                    event_key, provider, bank_reference, transfer_content,
                    amount, topup_id, status, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'REJECTED', ?, ?)
                """,
                (event_key, provider_name, bank_ref, topup_row["transfer_content"], amount, topup_row["id"], payload_str, now_iso),
            )
            webhook_err = AppError(
                "AMOUNT_MISMATCH",
                f"Số tiền webhook ({format_vnd(amount)}) không khớp với đơn nạp ({format_vnd(topup_row['amount'])}).",
                400,
            )
        elif topup_row["status"] == "SUCCESS":
            conn.execute(
                """
                INSERT INTO payment_webhook_events (
                    event_key, provider, bank_reference, transfer_content,
                    amount, topup_id, status, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'IGNORED', ?, ?)
                """,
                (event_key, provider_name, bank_ref, topup_row["transfer_content"], amount, topup_row["id"], payload_str, now_iso),
            )
            already_confirmed_resp = {
                "duplicate": True,
                "status": "ALREADY_CONFIRMED",
                "topup_id": topup_row["id"],
            }
        elif topup_row["status"] not in ("PENDING", "PROCESSING"):
            webhook_err = AppError(
                "INVALID_ORDER_STATE",
                f"Đơn nạp tiền đang ở trạng thái {topup_row['status']}, không thể tự động cộng tiền.",
                400,
            )
        else:
            summary, events_to_emit = _credit_topup_in_tx(
                conn=conn,
                topup_row=topup_row,
                provider=provider_name,
                bank_reference=bank_ref,
                qr_hmac_secret=qr_hmac_secret,
                admin_id=None,
                note="Xác nhận tự động qua Payment Webhook",
            )

            conn.execute(
                """
                INSERT INTO payment_webhook_events (
                    event_key, provider, bank_reference, transfer_content,
                    amount, topup_id, status, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'PROCESSED', ?, ?)
                """,
                (event_key, provider_name, bank_ref, topup_row["transfer_content"], amount, topup_row["id"], payload_str, now_iso),
            )

    if webhook_err is not None:
        raise webhook_err
    if already_confirmed_resp is not None:
        return already_confirmed_resp

    for uid, ev, data in events_to_emit:
        emit_user_realtime_update(uid, ev, data)
    realtime_broker.publish_to_admins("topup_confirmed", summary)

    return {"duplicate": False, **summary}


def admin_confirm_topup(
    admin_id: int,
    topup_id: int,
    bank_reference: str,
    note: str,
    qr_hmac_secret: str,
    ip_address: str = "",
) -> dict:
    """Manual Admin verification against real bank statement when webhook is not connected."""
    bank_ref = (bank_reference or "").strip()
    if not bank_ref:
        raise AppError(
            "MISSING_BANK_REFERENCE",
            "Vui lòng nhập mã giao dịch sao kê ngân hàng (Bank Reference) để đối soát.",
            400,
        )

    events_to_emit: list[tuple[int, str, dict]] = []

    with atomic_transaction() as conn:
        admin = conn.execute("SELECT id, role, status FROM users WHERE id = ?", (admin_id,)).fetchone()
        if not admin or admin["role"] not in ("ADMIN", "SUPER_ADMIN") or admin["status"] != "ACTIVE":
            raise AppError("FORBIDDEN", "Bạn không có quyền xác nhận giao dịch nạp tiền.", 403)

        dup_ref = conn.execute(
            "SELECT id, transaction_code FROM topup_transactions WHERE bank_reference = ? AND id != ?",
            (bank_ref, topup_id),
        ).fetchone()
        if dup_ref:
            raise AppError(
                "DUPLICATE_BANK_REFERENCE",
                f"Mã tham chiếu ngân hàng '{bank_ref}' đã được dùng cho đơn {dup_ref['transaction_code']}.",
                409,
            )

        topup_row = conn.execute("SELECT * FROM topup_transactions WHERE id = ?", (topup_id,)).fetchone()
        if not topup_row:
            raise AppError("TOPUP_NOT_FOUND", "Không tìm thấy yêu cầu nạp tiền.", 404)
        if topup_row["status"] == "SUCCESS":
            raise AppError("ALREADY_CONFIRMED", "Giao dịch nạp tiền này đã được duyệt trước đó.", 409)
        if topup_row["status"] not in ("PENDING", "PROCESSING"):
            raise AppError(
                "INVALID_TOPUP_STATE",
                f"Không thể xác nhận đơn ở trạng thái {topup_row['status']}.",
                400,
            )

        summary, events_to_emit = _credit_topup_in_tx(
            conn=conn,
            topup_row=topup_row,
            provider="MANUAL_STATEMENT_VERIFY",
            bank_reference=bank_ref,
            qr_hmac_secret=qr_hmac_secret,
            admin_id=admin_id,
            note=(note or "Đối soát sao kê thủ công bởi Admin")[:250],
        )

        conn.execute(
            """
            INSERT INTO audit_logs (admin_id, action, target_type, target_id, metadata_json, ip_address, created_at)
            VALUES (?, 'CONFIRM_TOPUP', 'TOPUP_TRANSACTION', ?, ?, ?, ?)
            """,
            (
                admin_id,
                str(topup_id),
                json.dumps(
                    {
                        "transaction_code": topup_row["transaction_code"],
                        "user_id": topup_row["user_id"],
                        "amount": topup_row["amount"],
                        "bank_reference": bank_ref,
                        "note": note,
                    },
                    ensure_ascii=False,
                ),
                ip_address[:64],
                utc_now_iso(),
            ),
        )

    for uid, ev, data in events_to_emit:
        emit_user_realtime_update(uid, ev, data)
    realtime_broker.publish_to_admins("topup_confirmed", summary)

    return summary


def admin_reject_topup(admin_id: int, topup_id: int, reason: str, ip_address: str = "") -> dict:
    reason = (reason or "Không tìm thấy giao dịch khớp trên sao kê").strip()[:250]
    now_iso = utc_now_iso()
    post_events: list[tuple[int, str, dict]] = []

    with atomic_transaction() as conn:
        admin = conn.execute("SELECT id, role, status FROM users WHERE id = ?", (admin_id,)).fetchone()
        if not admin or admin["role"] not in ("ADMIN", "SUPER_ADMIN") or admin["status"] != "ACTIVE":
            raise AppError("FORBIDDEN", "Bạn không có quyền từ chối giao dịch.", 403)

        topup_row = conn.execute("SELECT * FROM topup_transactions WHERE id = ?", (topup_id,)).fetchone()
        if not topup_row:
            raise AppError("TOPUP_NOT_FOUND", "Không tìm thấy yêu cầu nạp tiền.", 404)
        if topup_row["status"] not in ("PENDING", "PROCESSING"):
            raise AppError("INVALID_TOPUP_STATE", f"Không thể từ chối đơn ở trạng thái {topup_row['status']}.", 400)

        conn.execute(
            """
            UPDATE topup_transactions
            SET status = 'FAILED', confirmed_by_admin_id = ?, note = ?, updated_at = ?
            WHERE id = ?
            """,
            (admin_id, reason, now_iso, topup_id),
        )
        conn.execute(
            """
            INSERT INTO audit_logs (admin_id, action, target_type, target_id, metadata_json, ip_address, created_at)
            VALUES (?, 'REJECT_TOPUP', 'TOPUP_TRANSACTION', ?, ?, ?, ?)
            """,
            (
                admin_id,
                str(topup_id),
                json.dumps({"transaction_code": topup_row["transaction_code"], "reason": reason}, ensure_ascii=False),
                ip_address[:64],
                now_iso,
            ),
        )

        notif = create_notification_in_tx(
            conn,
            user_id=int(topup_row["user_id"]),
            notif_type="TOPUP_FAILED",
            title="Yêu cầu nạp tiền không được xác nhận",
            message=f"Đơn {topup_row['transaction_code']} ({format_vnd(topup_row['amount'])}) bị từ chối: {reason}",
            metadata={"topup_id": topup_id, "reason": reason},
        )
        if notif:
            post_events.append((int(topup_row["user_id"]), "notification", notif))

    for uid, ev, data in post_events:
        emit_user_realtime_update(uid, ev, data)

    return get_topup_detail(int(topup_row["user_id"]), topup_id, is_admin=True)
