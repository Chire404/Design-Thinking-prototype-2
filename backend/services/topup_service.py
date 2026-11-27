"""Top-up order lifecycle: Split AUTO (Bank Transfer/VietQR) vs MANUAL (Cash/Counter/Other) + Direct Monthly Pass Top-up."""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import timedelta

from backend.database import atomic_transaction, row_to_dict, rows_to_dicts
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

MANUAL_METHODS = {
    "CASH_COUNTER": "Tiền mặt tại quầy giao dịch",
    "E_WALLET_OTHER": "Ví điện tử ngoài (MoMo / ZaloPay / Khác)",
    "BANK_MANUAL": "Chuyển khoản thủ công / Ủy nhiệm chi",
    "OTHER": "Hình thức thanh toán thủ công khác",
}


def _enrich_topup_row(r: dict) -> dict:
    r["amount_formatted"] = format_vnd(r["amount"])
    ch = (r.get("topup_channel") or "AUTO").upper()
    r["topup_channel"] = ch
    r["channel_label"] = "Nạp Tự Động (Bank QR)" if ch == "AUTO" else "Request Thủ Công"
    detail = r.get("payment_method_detail") or ("BANK_TRANSFER" if ch == "AUTO" else "CASH_COUNTER")
    r["payment_method_label"] = "Chuyển khoản VietQR tự động" if detail == "BANK_TRANSFER" else MANUAL_METHODS.get(detail, detail)
    app_mode = r.get("approval_mode")
    if r.get("status") == "SUCCESS" and not app_mode:
        app_mode = "MANUAL" if r.get("confirmed_by_admin_id") else "AUTO"
        r["approval_mode"] = app_mode
    r["approval_badge_text"] = (
        "TỰ ĐỘNG" if app_mode == "AUTO" else "THỦ CÔNG" if app_mode == "MANUAL" else "CHỜ DUYỆT"
    )
    return r


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


def create_topup_order(
    user_id: int,
    raw_amount,
    topup_channel: str = "AUTO",
    payment_method_detail: str = "BANK_TRANSFER",
    payer_note: str = "",
    purpose: str = "WALLET",
    target_plan_id: int | None = None,
) -> dict:
    """Create either:
    - AUTO Top-up Request (`topup_channel='AUTO'`, VietQR Bank Transfer, auto-reconciled)
    - MANUAL Top-up Request (`topup_channel='MANUAL'`, Cash / Counter / Other, sent to Admin Manual Queue)
    Supports both `purpose='WALLET'` and `purpose='MONTHLY_PASS'` (auto-activates Monthly Pass once approved!).
    """
    channel = "MANUAL" if str(topup_channel or "AUTO").upper() == "MANUAL" else "AUTO"
    purpose_norm = "MONTHLY_PASS" if str(purpose or "WALLET").upper() == "MONTHLY_PASS" else "WALLET"
    post_events: list[tuple[int, str, dict]] = []

    with atomic_transaction() as conn:
        expire_stale_topups_in_tx(conn)
        settings = get_system_settings_in_tx(conn)
        min_amt = int(settings["topup_min_amount"])
        max_amt = int(settings["topup_max_amount"])
        expiry_min = int(settings["topup_expiry_minutes"])

        user = conn.execute("SELECT id, status, priority_status FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise AppError("USER_NOT_FOUND", "Không tìm thấy người dùng.", 404)
        if user["status"] == "SUSPENDED":
            raise AppError("SUSPENDED_ACCOUNT", "Tài khoản đang bị khóa, không thể tạo lệnh nạp tiền.", 403)

        plan_row = None
        if purpose_norm == "MONTHLY_PASS":
            if not target_plan_id:
                raise AppError("MISSING_PLAN", "Vui lòng chọn gói Vé Tháng cần nạp.", 400)
            plan_row = row_to_dict(
                conn.execute("SELECT * FROM pass_plans WHERE id = ? AND is_active = 1", (int(target_plan_id),)).fetchone()
            )
            if not plan_row:
                raise AppError("PLAN_NOT_FOUND", "Gói Vé Tháng không tồn tại hoặc đã ngừng cung cấp.", 404)
            if int(plan_row.get("requires_priority") or 0) == 1 and user["priority_status"] != "APPROVED":
                raise AppError(
                    "PRIORITY_REQUIRED",
                    "Gói vé ưu đãi HSSV chỉ dành cho tài khoản đã đăng ký và được Admin phê duyệt Đối tượng Ưu tiên!",
                    403,
                )
            amount = int(plan_row["price"])
            plan_id_val = int(plan_row["id"])
        else:
            try:
                amount = validate_positive_vnd_integer(raw_amount, "Số tiền nạp")
            except ValueError as exc:
                raise AppError("INVALID_AMOUNT", str(exc), 400) from exc
            if amount < min_amt or amount > max_amt:
                raise AppError(
                    "AMOUNT_OUT_OF_RANGE",
                    f"Số tiền nạp phải từ {format_vnd(min_amt)} đến {format_vnd(max_amt)}.",
                    400,
                )
            plan_id_val = None

        bank = row_to_dict(conn.execute("SELECT * FROM bank_settings WHERE id = 1").fetchone())
        if channel == "AUTO" and (not bank or not int(bank["is_active"])):
            raise AppError(
                "BANK_UNAVAILABLE",
                "Kênh chuyển khoản ngân hàng tự động đang bảo trì. Bạn có thể gửi Request nạp thủ công.",
                503,
            )
        bank = bank or {
            "bank_name": "Quầy Giao Dịch VeloPass",
            "bank_short_name": "COUNTER",
            "bank_bin": "970436",
            "account_number": "COUNTER-CASH",
            "account_holder": "VELOPASS COUNTER",
            "transfer_prefix": "VP",
            "payment_instructions": "Nộp tại quầy hoặc cung cấp biên lai để Admin xác nhận.",
        }

        tx_code = generate_tx_code("TOP")
        prefix = (bank.get("transfer_prefix") or "VP") if channel == "AUTO" else "VPM"
        transfer_content = generate_transfer_content(prefix)
        while conn.execute(
            "SELECT 1 FROM topup_transactions WHERE transfer_content = ?", (transfer_content,)
        ).fetchone():
            transfer_content = generate_transfer_content(prefix)

        now_dt = utc_now()
        now_iso = utc_now_iso()
        expires_iso = (now_dt + timedelta(minutes=expiry_min if channel == "AUTO" else 1440)).strftime(
            "%Y-%m-%dT%H:%M:%S.%fZ"
        )

        if channel == "MANUAL":
            method_key = str(payment_method_detail or "CASH_COUNTER").upper()
            if method_key not in MANUAL_METHODS:
                method_key = "OTHER"
            clean_note = (payer_note or "").strip()[:240] or f"Yêu cầu nạp thủ công ({MANUAL_METHODS[method_key]})"
            init_status = "PROCESSING"
            reported_at = now_iso
            provider_tag = f"MANUAL_REQ_{method_key}"
        else:
            method_key = "BANK_TRANSFER"
            clean_note = (
                f"Nạp tự động kích hoạt {plan_row['name']}" if plan_row else (payer_note or "").strip()[:240]
            )
            init_status = "PENDING"
            reported_at = None
            provider_tag = "BANK_TRANSFER"

        cur = conn.execute(
            """
            INSERT INTO topup_transactions (
                user_id, transaction_code, transfer_content, amount,
                bank_name, bank_short_name, bank_bin, account_number, account_holder,
                payment_provider, topup_channel, payment_method_detail, purpose, target_plan_id,
                user_reported_at, note, status, expires_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                tx_code,
                transfer_content,
                amount,
                bank["bank_name"] if channel == "AUTO" else MANUAL_METHODS[method_key],
                bank["bank_short_name"] if channel == "AUTO" else method_key[:12],
                bank["bank_bin"],
                bank["account_number"],
                bank["account_holder"],
                provider_tag,
                channel,
                method_key,
                purpose_norm,
                plan_id_val,
                reported_at,
                clean_note,
                init_status,
                expires_iso,
                now_iso,
                now_iso,
            ),
        )
        topup_id = int(cur.lastrowid)

        purpose_desc = f" (cho gói {plan_row['name']})" if plan_row else ""
        notif_title = (
            "Đã gửi Request nạp tiền thủ công"
            if channel == "MANUAL"
            else "Đơn nạp tiền tự động đang chờ chuyển khoản"
        )
        notif_msg = (
            f"Mã yêu cầu {tx_code}{purpose_desc}: {format_vnd(amount)} ({MANUAL_METHODS[method_key]}). Đang chờ Admin xác nhận."
            if channel == "MANUAL"
            else f"Mã đơn {tx_code}{purpose_desc}: Chuyển khoản đúng {format_vnd(amount)} với nội dung {transfer_content} để tự động duyệt."
        )
        notif = create_notification_in_tx(
            conn,
            user_id=user_id,
            notif_type="TOPUP_PENDING",
            title=notif_title,
            message=notif_msg,
            metadata={"topup_id": topup_id, "transaction_code": tx_code, "amount": amount, "channel": channel},
        )
        if notif:
            post_events.append((user_id, "notification", notif))

    for uid, ev, data in post_events:
        emit_user_realtime_update(uid, ev, data)
    realtime_broker.publish_to_admins(
        "topup_processing" if channel == "MANUAL" else "topup_created",
        {
            "topup_id": topup_id,
            "transaction_code": tx_code,
            "amount": amount,
            "topup_channel": channel,
            "purpose": purpose_norm,
            "transfer_content": transfer_content,
        },
    )

    qr_bundle = build_topup_qr_bundle(bank, amount, transfer_content) if channel == "AUTO" else None
    result = _enrich_topup_row(
        {
            "id": topup_id,
            "transaction_code": tx_code,
            "transfer_content": transfer_content,
            "amount": amount,
            "topup_channel": channel,
            "payment_method_detail": method_key,
            "purpose": purpose_norm,
            "target_plan_id": plan_id_val,
            "target_plan_name": plan_row["name"] if plan_row else None,
            "note": clean_note,
            "status": init_status,
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
    )
    return result


def get_user_topups(user_id: int) -> list[dict]:
    with atomic_transaction() as conn:
        expire_stale_topups_in_tx(conn)
        rows = rows_to_dicts(
            conn.execute(
                "SELECT * FROM topup_transactions WHERE user_id = ? ORDER BY id DESC LIMIT 60",
                (user_id,),
            ).fetchall()
        )
    return [_enrich_topup_row(r) for r in rows]


def get_topup_detail(user_id: int, topup_id: int, is_admin: bool = False) -> dict:
    with atomic_transaction() as conn:
        expire_stale_topups_in_tx(conn)
        row = row_to_dict(conn.execute("SELECT * FROM topup_transactions WHERE id = ?", (topup_id,)).fetchone())
        if not row:
            raise AppError("TOPUP_NOT_FOUND", "Không tìm thấy giao dịch nạp tiền.", 404)
        if not is_admin and int(row["user_id"]) != int(user_id):
            raise AppError("FORBIDDEN", "Bạn không có quyền xem giao dịch nạp tiền này.", 403)
        bank = row_to_dict(conn.execute("SELECT * FROM bank_settings WHERE id = 1").fetchone()) or {}
        if row.get("target_plan_id"):
            pl = conn.execute("SELECT name FROM pass_plans WHERE id = ?", (row["target_plan_id"],)).fetchone()
            row["target_plan_name"] = pl["name"] if pl else None

    _enrich_topup_row(row)
    if row["topup_channel"] == "AUTO" and row["status"] in ("PENDING", "PROCESSING"):
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
            raise AppError("INVALID_STATUS", f"Giao dịch hiện ở trạng thái {row['status']}.", 400)

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
        if row["status"] not in ("PENDING", "PROCESSING"):
            raise AppError("CANNOT_CANCEL", "Chỉ có thể hủy đơn nạp tiền chưa được xác nhận.", 400)
        conn.execute(
            "UPDATE topup_transactions SET status = 'CANCELLED', updated_at = ? WHERE id = ? AND status IN ('PENDING', 'PROCESSING')",
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
    """Atomically settle a top-up, record AUTO/MANUAL approval_mode, and auto-activate Monthly Pass if `purpose == 'MONTHLY_PASS'`."""
    now_dt = utc_now()
    now_iso = utc_now_iso()
    topup_id = int(topup_row["id"])
    user_id = int(topup_row["user_id"])
    amount = int(topup_row["amount"])
    approval_mode = "MANUAL" if admin_id is not None else "AUTO"
    mode_vi = "THỦ CÔNG" if approval_mode == "MANUAL" else "TỰ ĐỘNG"

    upd = conn.execute(
        """
        UPDATE topup_transactions
        SET status = 'SUCCESS',
            payment_provider = ?,
            approval_mode = ?,
            bank_reference = ?,
            confirmed_by_admin_id = ?,
            note = CASE WHEN ? != '' THEN ? ELSE note END,
            confirmed_at = ?,
            updated_at = ?
        WHERE id = ? AND status IN ('PENDING', 'PROCESSING')
        """,
        (provider, approval_mode, bank_reference, admin_id, note, note, now_iso, now_iso, topup_id),
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
            transaction_code, user_id, wallet_id, transaction_type, approval_mode,
            amount, balance_before, balance_after, reference_type, reference_id,
            admin_id, description, created_at
        ) VALUES (?, ?, ?, 'TOPUP', ?, ?, ?, ?, 'TOPUP_TRANSACTION', ?, ?, ?, ?)
        """,
        (
            wallet_tx_code,
            user_id,
            wallet["id"],
            approval_mode,
            amount,
            balance_before,
            balance_after,
            topup_row["transaction_code"],
            admin_id,
            f"Nạp tiền [{mode_vi}] ({topup_row['transaction_code']}) +{format_vnd(amount)}",
            now_iso,
        ),
    )

    events: list[tuple[int, str, dict]] = []
    activated_pass_code = None

    # If this top-up was initiated directly for a Monthly Pass (from Home page), auto-activate the pass right now!
    purpose_col = topup_row["purpose"] if "purpose" in topup_row.keys() else "WALLET"
    target_plan_id = topup_row["target_plan_id"] if "target_plan_id" in topup_row.keys() else None

    if purpose_col == "MONTHLY_PASS" and target_plan_id:
        plan = conn.execute("SELECT * FROM pass_plans WHERE id = ?", (int(target_plan_id),)).fetchone()
        card_row = conn.execute("SELECT * FROM virtual_cards WHERE user_id = ?", (user_id,)).fetchone()
        if plan and card_row and balance_after >= int(plan["price"]):
            p_price = int(plan["price"])
            pass_bal_before = balance_after
            balance_after = pass_bal_before - p_price
            pass_code = generate_tx_code("PASS")
            pass_wtx = generate_tx_code("WLT")

            conn.execute(
                "UPDATE wallets SET balance = ?, total_spent = total_spent + ?, updated_at = ? WHERE id = ?",
                (balance_after, p_price, now_iso, wallet["id"]),
            )
            conn.execute(
                """
                INSERT INTO wallet_transactions (
                    transaction_code, user_id, wallet_id, transaction_type, approval_mode,
                    amount, balance_before, balance_after, reference_type, reference_id,
                    admin_id, description, created_at
                ) VALUES (?, ?, ?, 'PASS_PURCHASE', ?, ?, ?, ?, 'USER_PASS', ?, ?, ?, ?)
                """,
                (
                    pass_wtx,
                    user_id,
                    wallet["id"],
                    approval_mode,
                    p_price,
                    pass_bal_before,
                    balance_after,
                    pass_code,
                    admin_id,
                    f"Tự động kích hoạt {plan['name']} từ đơn nạp {topup_row['transaction_code']}",
                    now_iso,
                ),
            )

            latest_exp = conn.execute(
                "SELECT MAX(expires_at) AS max_exp FROM user_passes WHERE user_id = ? AND status = 'ACTIVE' AND expires_at > ?",
                (user_id, now_iso),
            ).fetchone()
            starts_dt = parse_iso(latest_exp["max_exp"]) if (latest_exp and latest_exp["max_exp"]) else now_dt
            expires_dt = starts_dt + timedelta(days=int(plan["duration_days"]))

            conn.execute(
                """
                INSERT INTO user_passes (
                    pass_code, user_id, card_id, plan_id, plan_code, plan_name,
                    price_paid, duration_days, max_trips, trips_used, auto_renew,
                    starts_at, expires_at, status, wallet_tx_code, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, 'ACTIVE', ?, ?, ?)
                """,
                (
                    pass_code,
                    user_id,
                    card_row["id"],
                    plan["id"],
                    plan["code"],
                    plan["name"],
                    p_price,
                    int(plan["duration_days"]),
                    plan["max_trips"],
                    starts_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    expires_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    pass_wtx,
                    now_iso,
                    now_iso,
                ),
            )
            activated_pass_code = pass_code

    settings = get_system_settings_in_tx(conn)
    card = sync_card_status_in_tx(conn, user_id)
    next_qr = None
    if card["status"] == "ACTIVE":
        qr_mode = "MONTHLY" if activated_pass_code else ("SINGLE" if balance_after >= int(settings["fare_amount"]) else "MONTHLY")
        next_qr = _issue_qr_token_in_tx(
            conn=conn,
            user_id=user_id,
            card_id=int(card["id"]),
            card_version=int(card["card_version"]),
            ttl_seconds=int(settings["qr_ttl_seconds"]),
            qr_hmac_secret=qr_hmac_secret,
            ticket_mode=qr_mode,
        )

    notif_msg = (
        f"Đã nạp +{format_vnd(amount)} ({mode_vi}) và kích hoạt Vé Tháng {activated_pass_code} thành công!"
        if activated_pass_code
        else f"Ví của bạn vừa được cộng +{format_vnd(amount)} ({mode_vi}). Số dư mới: {format_vnd(balance_after)}."
    )
    notif = create_notification_in_tx(
        conn,
        user_id=user_id,
        notif_type="TOPUP_SUCCESS",
        title="Nạp Vé Tháng thành công!" if activated_pass_code else "Nạp tiền thành công!",
        message=notif_msg,
        metadata={
            "topup_id": topup_id,
            "transaction_code": topup_row["transaction_code"],
            "approval_mode": approval_mode,
            "activated_pass_code": activated_pass_code,
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
                "approval_mode": approval_mode,
                "activated_pass_code": activated_pass_code,
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
        "approval_mode": approval_mode,
        "approval_badge_text": mode_vi,
        "activated_pass_code": activated_pass_code,
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
    """Process authenticated bank payment webhook with replay/duplicate protection (marks approval_mode = AUTO)."""
    if not webhook_secret:
        raise AppError(
            "WEBHOOK_NOT_CONFIGURED",
            "PAYMENT_WEBHOOK_SECRET chưa được cấu hình. Hệ thống đang ở chế độ đối soát sao kê / Admin duyệt.",
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

        topup_row = conn.execute(
            "SELECT * FROM topup_transactions WHERE transfer_content = ?",
            (raw_content.strip(),),
        ).fetchone()
        if not topup_row:
            tokens = re.findall(r"[A-Z0-9]{8,24}", raw_content)
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
                note="Xác nhận TỰ ĐỘNG qua Payment Webhook",
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
    """Manual Admin verification (marks approval_mode = MANUAL)."""
    bank_ref = (bank_reference or "").strip()
    if not bank_ref:
        raise AppError(
            "MISSING_BANK_REFERENCE",
            "Vui lòng nhập mã giao dịch / mã biên lai (Reference) để xác nhận duyệt thủ công.",
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
                f"Mã tham chiếu/biên lai '{bank_ref}' đã được dùng cho đơn {dup_ref['transaction_code']}.",
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
            provider="MANUAL_ADMIN_APPROVAL",
            bank_reference=bank_ref,
            qr_hmac_secret=qr_hmac_secret,
            admin_id=admin_id,
            note=(note or "Duyệt THỦ CÔNG bởi Admin")[:250],
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
                        "approval_mode": "MANUAL",
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
    reason = (reason or "Không hợp lệ hoặc chưa nhận được thanh toán").strip()[:250]
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
            SET status = 'FAILED', approval_mode = 'MANUAL', confirmed_by_admin_id = ?, note = ?, updated_at = ?
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
            title="Yêu cầu nạp tiền bị từ chối",
            message=f"Đơn {topup_row['transaction_code']} ({format_vnd(topup_row['amount'])}) bị từ chối: {reason}",
            metadata={"topup_id": topup_id, "reason": reason},
        )
        if notif:
            post_events.append((int(topup_row["user_id"]), "notification", notif))

    for uid, ev, data in post_events:
        emit_user_realtime_update(uid, ev, data)

    return get_topup_detail(int(topup_row["user_id"]), topup_id, is_admin=True)
