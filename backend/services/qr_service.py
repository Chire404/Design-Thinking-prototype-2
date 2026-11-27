"""Dynamic single-use QR generation, verification, Monthly Pass 0 VND priority & atomic 4.000 VND fare deduction."""

from __future__ import annotations

import secrets
import sqlite3
from datetime import timedelta

from backend.database import atomic_transaction, read_db
from backend.services import AppError
from backend.services.card_wallet_service import (
    compute_card_status,
    create_notification_in_tx,
    emit_user_realtime_update,
    get_system_settings_in_tx,
    sync_card_status_in_tx,
)
from backend.services.pass_service import get_active_pass_in_tx, get_active_pass_readonly
from backend.utils.formatters import format_vnd, generate_tx_code, parse_iso, utc_now, utc_now_iso
from backend.utils.qr_svg import generate_qr_svg_data_uri
from backend.utils.realtime import realtime_broker
from backend.utils.security import (
    create_signed_qr_token,
    sha256_hex,
    verify_signed_qr_token_structure,
)


def _issue_qr_token_in_tx(
    conn: sqlite3.Connection,
    user_id: int,
    card_id: int,
    card_version: int,
    ttl_seconds: int,
    qr_hmac_secret: str,
) -> dict:
    """Revoke any existing ACTIVE QR token for this card and issue one fresh single-use token."""
    now_dt = utc_now()
    now_iso = utc_now_iso()
    expires_dt = now_dt + timedelta(seconds=ttl_seconds)
    expires_iso = expires_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    conn.execute(
        "UPDATE qr_tokens SET status = 'REVOKED' WHERE card_id = ? AND status = 'ACTIVE'",
        (card_id,),
    )

    raw_token, token_hash = create_signed_qr_token(qr_hmac_secret)
    cur = conn.execute(
        """
        INSERT INTO qr_tokens (user_id, card_id, card_version, token_hash, expires_at, status, created_at)
        VALUES (?, ?, ?, ?, ?, 'ACTIVE', ?)
        """,
        (user_id, card_id, card_version, token_hash, expires_iso, now_iso),
    )
    return {
        "token_id": int(cur.lastrowid),
        "qr_token": raw_token,
        "qr_svg_data_uri": generate_qr_svg_data_uri(raw_token, box_size=9, border=3),
        "expires_at": expires_iso,
        "ttl_seconds": ttl_seconds,
    }


def issue_or_refresh_user_qr(user_id: int, qr_hmac_secret: str) -> dict:
    """Check User, Monthly Pass & Wallet eligibility and issue a single-use Dynamic QR code."""
    with atomic_transaction() as conn:
        settings = get_system_settings_in_tx(conn)
        fare = int(settings["fare_amount"])
        ttl = int(settings["qr_ttl_seconds"])
        now_iso = utc_now_iso()

        conn.execute(
            "UPDATE qr_tokens SET status = 'EXPIRED' WHERE user_id = ? AND status = 'ACTIVE' AND expires_at <= ?",
            (user_id, now_iso),
        )

        user = conn.execute("SELECT id, status, full_name FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise AppError("USER_NOT_FOUND", "Không tìm thấy người dùng.", 404)

        wallet = conn.execute("SELECT id, balance, status FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
        active_pass = get_active_pass_in_tx(conn, user_id)
        card = sync_card_status_in_tx(conn, user_id)
        balance = int(wallet["balance"])

        if user["status"] == "SUSPENDED" or wallet["status"] == "LOCKED":
            raise AppError(
                "SUSPENDED_ACCOUNT",
                "Tài khoản đang bị tạm khóa. Không thể kích hoạt mã QR.",
                403,
                {"card_status": "SUSPENDED", "balance": balance, "balance_formatted": format_vnd(balance)},
            )

        if user["status"] != "ACTIVE":
            raise AppError(
                "ACCOUNT_PENDING",
                "Tài khoản đang chờ xác minh.",
                403,
                {"card_status": card["status"], "balance": balance, "balance_formatted": format_vnd(balance)},
            )

        if active_pass is None and balance < fare:
            raise AppError(
                "INSUFFICIENT_BALANCE",
                f"Số dư hiện tại ({format_vnd(balance)}) dưới mức tối thiểu {format_vnd(fare)} và chưa có Vé Tháng. Vui lòng nạp thêm tiền hoặc mua Vé Tháng.",
                400,
                {
                    "card_status": "INSUFFICIENT_BALANCE",
                    "balance": balance,
                    "balance_formatted": format_vnd(balance),
                    "required_fare": fare,
                    "required_fare_formatted": format_vnd(fare),
                },
            )

        issued = _issue_qr_token_in_tx(
            conn=conn,
            user_id=user_id,
            card_id=int(card["id"]),
            card_version=int(card["card_version"]),
            ttl_seconds=ttl,
            qr_hmac_secret=qr_hmac_secret,
        )

    return {
        **issued,
        "card_number": card["card_number"],
        "card_status": "ACTIVE",
        "has_active_pass": active_pass is not None,
        "active_pass": active_pass,
        "balance": balance,
        "balance_formatted": format_vnd(balance),
        "fare_amount": 0 if active_pass else fare,
        "fare_formatted": "0 VNĐ (Vé Tháng)" if active_pass else format_vnd(fare),
        "qr_active": True,
    }


def get_current_qr_status(user_id: int, qr_hmac_secret: str, issue_if_missing: bool = False) -> dict:
    """Inspect active QR metadata for User's Virtual Card; optionally issue if eligible and missing."""
    now_dt = utc_now()
    now_iso = utc_now_iso()
    with read_db() as conn:
        settings = get_system_settings_in_tx(conn)
        fare = int(settings["fare_amount"])
        user = conn.execute("SELECT id, status FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise AppError("USER_NOT_FOUND", "Không tìm thấy người dùng.", 404)
        wallet = conn.execute("SELECT balance, status FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
        card = conn.execute("SELECT * FROM virtual_cards WHERE user_id = ?", (user_id,)).fetchone()
        active_pass = get_active_pass_readonly(conn, user_id)

        balance = int(wallet["balance"])
        eff_status = "SUSPENDED" if wallet["status"] == "LOCKED" else user["status"]
        card_status = compute_card_status(eff_status, balance, fare, has_active_pass=(active_pass is not None))

        active_row = conn.execute(
            """
            SELECT id, expires_at, created_at FROM qr_tokens
            WHERE card_id = ? AND status = 'ACTIVE' AND expires_at > ?
            ORDER BY id DESC LIMIT 1
            """,
            (card["id"], now_iso),
        ).fetchone()

    if card_status == "ACTIVE" and issue_if_missing and active_row is None:
        return issue_or_refresh_user_qr(user_id, qr_hmac_secret)

    remaining_seconds = 0
    if active_row:
        remaining_seconds = max(0, int((parse_iso(active_row["expires_at"]) - now_dt).total_seconds()))

    return {
        "card_number": card["card_number"],
        "card_status": card_status,
        "qr_eligible": card_status == "ACTIVE",
        "has_active_pass": active_pass is not None,
        "active_pass": active_pass,
        "has_active_token": bool(active_row and card_status == "ACTIVE"),
        "active_token_id": active_row["id"] if active_row and card_status == "ACTIVE" else None,
        "expires_at": active_row["expires_at"] if active_row and card_status == "ACTIVE" else None,
        "remaining_seconds": remaining_seconds if card_status == "ACTIVE" else 0,
        "balance": balance,
        "balance_formatted": format_vnd(balance),
        "fare_amount": 0 if active_pass else fare,
        "fare_formatted": "0 VNĐ (Vé Tháng)" if active_pass else format_vnd(fare),
    }


def _record_failed_scan(
    conn: sqlite3.Connection,
    admin_id: int,
    error_code: str,
    message: str,
    idempotency_key: str = "",
    user_id: int | None = None,
    card_number: str = "",
    balance_before: int | None = None,
) -> None:
    conn.execute(
        """
        INSERT INTO scan_logs (
            admin_id, user_id, card_number, idempotency_key, status,
            error_code, message, amount, balance_before, balance_after, created_at
        ) VALUES (?, ?, ?, ?, 'FAILED', ?, ?, 0, ?, ?, ?)
        """,
        (
            admin_id,
            user_id,
            card_number,
            idempotency_key,
            error_code,
            message,
            balance_before,
            balance_before,
            utc_now_iso(),
        ),
    )


def scan_and_deduct_qr(
    admin_id: int,
    raw_qr_token: str,
    qr_hmac_secret: str,
    idempotency_key: str | None = None,
) -> dict:
    """Execute atomic QR verification with Monthly Pass (0 VND) priority or 4.000 VND Wallet deduction."""
    raw_qr_token = (raw_qr_token or "").strip()
    idem_key = (idempotency_key or "").strip() or f"SCAN-{secrets.token_hex(12)}"

    if not verify_signed_qr_token_structure(raw_qr_token, qr_hmac_secret):
        with atomic_transaction() as conn:
            admin = conn.execute("SELECT id, role, status FROM users WHERE id = ?", (admin_id,)).fetchone()
            if not admin or admin["role"] not in ("ADMIN", "SUPER_ADMIN") or admin["status"] != "ACTIVE":
                raise AppError("FORBIDDEN", "Bạn không có quyền sử dụng thiết bị quét QR.", 403)
            _record_failed_scan(
                conn,
                admin_id=admin_id,
                error_code="INVALID_QR",
                message="Mã QR không hợp lệ hoặc không thuộc hệ thống VeloPass.",
                idempotency_key=idem_key,
            )
        realtime_broker.publish_to_admins(
            "scan_completed",
            {"status": "FAILED", "error_code": "INVALID_QR", "message": "Mã QR không hợp lệ."},
        )
        raise AppError("INVALID_QR", "Mã QR không hợp lệ hoặc không thuộc hệ thống VeloPass.", 400)

    token_hash = sha256_hex(raw_qr_token)
    post_commit_user_events: list[tuple[int, str, dict]] = []
    post_commit_admin_event: dict | None = None

    with atomic_transaction() as conn:
        admin = conn.execute(
            "SELECT id, username, full_name, role, status FROM users WHERE id = ?",
            (admin_id,),
        ).fetchone()
        if not admin or admin["role"] not in ("ADMIN", "SUPER_ADMIN") or admin["status"] != "ACTIVE":
            raise AppError("FORBIDDEN", "Tài khoản quản trị không hợp lệ hoặc đã bị khóa.", 403)

        existing_tx = conn.execute(
            """
            SELECT ut.*, u.full_name AS user_full_name, u.username AS user_username, vc.card_number, vc.status AS card_status
            FROM usage_transactions ut
            JOIN users u ON u.id = ut.user_id
            JOIN virtual_cards vc ON vc.id = ut.card_id
            WHERE ut.idempotency_key = ?
            """,
            (idem_key,),
        ).fetchone()
        if existing_tx:
            is_pass = existing_tx["payment_method"] == "MONTHLY_PASS"
            return {
                "status": "SUCCESS",
                "idempotent_replay": True,
                "payment_method": existing_tx["payment_method"],
                "transaction_code": existing_tx["transaction_code"],
                "user_id": existing_tx["user_id"],
                "user_name": existing_tx["user_full_name"],
                "username": existing_tx["user_username"],
                "card_number": existing_tx["card_number"],
                "card_status_after": existing_tx["card_status"],
                "amount": int(existing_tx["amount"]),
                "amount_formatted": "0 VNĐ (Vé Tháng)" if is_pass else f"-{format_vnd(existing_tx['amount'])}",
                "balance_before": int(existing_tx["balance_before"]),
                "balance_before_formatted": format_vnd(existing_tx["balance_before"]),
                "balance_after": int(existing_tx["balance_after"]),
                "balance_after_formatted": format_vnd(existing_tx["balance_after"]),
                "scanned_at": existing_tx["created_at"],
                "qr_reissued": existing_tx["card_status"] == "ACTIVE",
            }

        settings = get_system_settings_in_tx(conn)
        fare = int(settings["fare_amount"])
        ttl = int(settings["qr_ttl_seconds"])
        low_threshold = int(settings["low_balance_threshold"])
        now_dt = utc_now()
        now_iso = utc_now_iso()

        qr_row = conn.execute("SELECT * FROM qr_tokens WHERE token_hash = ?", (token_hash,)).fetchone()
        if not qr_row:
            _record_failed_scan(
                conn,
                admin_id=admin_id,
                error_code="INVALID_QR",
                message="Không tìm thấy mã QR trong hệ thống.",
                idempotency_key=idem_key,
            )
            rejected: AppError | None = AppError("INVALID_QR", "Mã QR không tồn tại hoặc không hợp lệ.", 400)
        else:
            rejected = None

        active_pass = None
        if rejected is None:
            user_id = int(qr_row["user_id"])
            user = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            card = conn.execute("SELECT * FROM virtual_cards WHERE id = ?", (qr_row["card_id"],)).fetchone()
            wallet = conn.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,)).fetchone()

            if not user or not card or not wallet:
                _record_failed_scan(
                    conn,
                    admin_id=admin_id,
                    error_code="USER_NOT_FOUND",
                    message="Không tìm thấy hồ sơ người dùng liên kết với thẻ.",
                    idempotency_key=idem_key,
                )
                rejected = AppError("USER_NOT_FOUND", "Không tìm thấy người dùng liên kết với mã QR.", 404)
            else:
                active_pass = get_active_pass_in_tx(conn, user_id)

        if rejected is None:
            card_number = card["card_number"]
            balance_before = int(wallet["balance"])

            if qr_row["status"] == "USED" or qr_row["used_at"] is not None:
                _record_failed_scan(
                    conn,
                    admin_id=admin_id,
                    user_id=user_id,
                    card_number=card_number,
                    balance_before=balance_before,
                    error_code="QR_ALREADY_USED",
                    message="Mã QR này đã được quét sử dụng trước đó.",
                    idempotency_key=idem_key,
                )
                rejected = AppError("QR_ALREADY_USED", "Mã QR này đã được sử dụng. Không thể quét lại.", 409)
            elif qr_row["status"] == "REVOKED":
                _record_failed_scan(
                    conn,
                    admin_id=admin_id,
                    user_id=user_id,
                    card_number=card_number,
                    balance_before=balance_before,
                    error_code="EXPIRED_QR",
                    message="Mã QR cũ đã bị thu hồi do thẻ đã làm mới mã QR.",
                    idempotency_key=idem_key,
                )
                rejected = AppError("EXPIRED_QR", "Mã QR đã hết hiệu lực (đã có mã QR mới).", 400)
            elif qr_row["status"] == "EXPIRED" or parse_iso(qr_row["expires_at"]) <= now_dt:
                conn.execute(
                    "UPDATE qr_tokens SET status = 'EXPIRED' WHERE id = ? AND status = 'ACTIVE'",
                    (qr_row["id"],),
                )
                _record_failed_scan(
                    conn,
                    admin_id=admin_id,
                    user_id=user_id,
                    card_number=card_number,
                    balance_before=balance_before,
                    error_code="EXPIRED_QR",
                    message="Mã QR đã hết thời hạn hiệu lực.",
                    idempotency_key=idem_key,
                )
                rejected = AppError("EXPIRED_QR", "Mã QR đã hết hạn. Vui lòng yêu cầu hành khách mở lại thẻ.", 400)
            elif user["status"] == "SUSPENDED" or wallet["status"] == "LOCKED":
                conn.execute("UPDATE qr_tokens SET status = 'REVOKED' WHERE id = ?", (qr_row["id"],))
                conn.execute(
                    "UPDATE virtual_cards SET status = 'SUSPENDED', updated_at = ? WHERE id = ?",
                    (now_iso, card["id"]),
                )
                _record_failed_scan(
                    conn,
                    admin_id=admin_id,
                    user_id=user_id,
                    card_number=card_number,
                    balance_before=balance_before,
                    error_code="SUSPENDED_ACCOUNT",
                    message="Tài khoản thẻ đang bị khóa bởi Quản trị viên.",
                    idempotency_key=idem_key,
                )
                rejected = AppError("SUSPENDED_ACCOUNT", "Tài khoản người dùng đang bị khóa.", 403)
            elif user["status"] != "ACTIVE" or int(qr_row["card_version"]) != int(card["card_version"]):
                _record_failed_scan(
                    conn,
                    admin_id=admin_id,
                    user_id=user_id,
                    card_number=card_number,
                    balance_before=balance_before,
                    error_code="INVALID_QR",
                    message="Trạng thái tài khoản hoặc phiên bản thẻ không hợp lệ.",
                    idempotency_key=idem_key,
                )
                rejected = AppError("INVALID_QR", "Thẻ chưa sẵn sàng sử dụng hoặc phiên bản mã QR không khớp.", 400)
            elif active_pass is None and balance_before < fare:
                conn.execute("UPDATE qr_tokens SET status = 'REVOKED' WHERE id = ?", (qr_row["id"],))
                conn.execute(
                    "UPDATE virtual_cards SET status = 'INSUFFICIENT_BALANCE', updated_at = ? WHERE id = ?",
                    (now_iso, card["id"]),
                )
                _record_failed_scan(
                    conn,
                    admin_id=admin_id,
                    user_id=user_id,
                    card_number=card_number,
                    balance_before=balance_before,
                    error_code="INSUFFICIENT_BALANCE",
                    message=f"Số dư không đủ ({format_vnd(balance_before)} < {format_vnd(fare)}) và không có Vé Tháng.",
                    idempotency_key=idem_key,
                )
                rejected = AppError(
                    "INSUFFICIENT_BALANCE",
                    f"Số dư không đủ để qua cổng ({format_vnd(balance_before)} / cần {format_vnd(fare)}).",
                    400,
                )

        if rejected is None:
            # Atomic conditional mark QR token USED (strictly single-use whether Monthly Pass or Wallet!)
            claim_cur = conn.execute(
                """
                UPDATE qr_tokens
                SET status = 'USED', used_at = ?
                WHERE id = ? AND status = 'ACTIVE' AND used_at IS NULL AND expires_at > ?
                """,
                (now_iso, qr_row["id"], now_iso),
            )
            if claim_cur.rowcount != 1:
                raise AppError("QR_ALREADY_USED", "Mã QR này vừa được xử lý ở thiết bị khác.", 409)

            usage_tx_code = generate_tx_code("USE")

            if active_pass is not None:
                # PRIORITY 1: VALID MONTHLY PASS -> 0 VND DEDUCTION!
                payment_method = "MONTHLY_PASS"
                effective_amount = 0
                balance_after = balance_before
                pass_id = int(active_pass["id"])

                conn.execute(
                    "UPDATE user_passes SET trips_used = trips_used + 1, updated_at = ? WHERE id = ?",
                    (now_iso, pass_id),
                )
                if active_pass["max_trips"] is not None and (int(active_pass["trips_used"]) + 1) >= int(active_pass["max_trips"]):
                    conn.execute(
                        "UPDATE user_passes SET status = 'EXPIRED', updated_at = ? WHERE id = ?",
                        (now_iso, pass_id),
                    )
            else:
                # PRIORITY 2: STANDARD WALLET PAY-PER-RIDE (4.000 VND)
                payment_method = "WALLET"
                effective_amount = fare
                pass_id = None

                deduct_cur = conn.execute(
                    """
                    UPDATE wallets
                    SET balance = balance - ?,
                        total_spent = total_spent + ?,
                        updated_at = ?
                    WHERE id = ? AND user_id = ? AND status = 'ACTIVE' AND balance >= ?
                    """,
                    (fare, fare, now_iso, wallet["id"], user_id, fare),
                )
                if deduct_cur.rowcount != 1:
                    raise AppError("INSUFFICIENT_BALANCE", "Số dư ví không đủ để thực hiện giao dịch.", 400)
                balance_after = balance_before - fare

                wallet_tx_code = generate_tx_code("WLT")
                conn.execute(
                    """
                    INSERT INTO wallet_transactions (
                        transaction_code, user_id, wallet_id, transaction_type,
                        amount, balance_before, balance_after, reference_type, reference_id,
                        admin_id, description, created_at
                    ) VALUES (?, ?, ?, 'USAGE', ?, ?, ?, 'USAGE_TRANSACTION', ?, ?, ?, ?)
                    """,
                    (
                        wallet_tx_code,
                        user_id,
                        wallet["id"],
                        fare,
                        balance_before,
                        balance_after,
                        usage_tx_code,
                        admin_id,
                        f"Quét thẻ {card_number} qua cổng kiểm soát (-{format_vnd(fare)})",
                        now_iso,
                    ),
                )

            usage_cur = conn.execute(
                """
                INSERT INTO usage_transactions (
                    transaction_code, user_id, card_id, qr_token_id, admin_id,
                    amount, payment_method, pass_id, balance_before, balance_after,
                    idempotency_key, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SUCCESS', ?)
                """,
                (
                    usage_tx_code,
                    user_id,
                    card["id"],
                    qr_row["id"],
                    admin_id,
                    effective_amount,
                    payment_method,
                    pass_id,
                    balance_before,
                    balance_after,
                    idem_key,
                    now_iso,
                ),
            )
            usage_id = int(usage_cur.lastrowid)

            # Re-check post-scan eligibility (still has active pass OR balance_after >= fare)
            remaining_pass = get_active_pass_in_tx(conn, user_id)
            next_qr_bundle = None
            if remaining_pass is not None or balance_after >= fare:
                new_card_status = "ACTIVE"
                next_qr_bundle = _issue_qr_token_in_tx(
                    conn=conn,
                    user_id=user_id,
                    card_id=int(card["id"]),
                    card_version=int(card["card_version"]),
                    ttl_seconds=ttl,
                    qr_hmac_secret=qr_hmac_secret,
                )
            else:
                new_card_status = "INSUFFICIENT_BALANCE"

            conn.execute(
                "UPDATE virtual_cards SET status = ?, updated_at = ? WHERE id = ?",
                (new_card_status, now_iso, card["id"]),
            )

            scan_msg = (
                f"Quét hợp lệ bằng Vé Tháng {active_pass['pass_code']} (0 VNĐ)"
                if payment_method == "MONTHLY_PASS"
                else f"Quét hợp lệ thẻ {card_number} (-{format_vnd(fare)})"
            )
            conn.execute(
                """
                INSERT INTO scan_logs (
                    admin_id, user_id, card_number, usage_transaction_id, idempotency_key,
                    status, error_code, message, amount, balance_before, balance_after, created_at
                ) VALUES (?, ?, ?, ?, ?, 'SUCCESS', '', ?, ?, ?, ?, ?)
                """,
                (
                    admin_id,
                    user_id,
                    card_number,
                    usage_id,
                    idem_key,
                    scan_msg,
                    effective_amount,
                    balance_before,
                    balance_after,
                    now_iso,
                ),
            )

            if payment_method == "MONTHLY_PASS":
                n_used = create_notification_in_tx(
                    conn,
                    user_id=user_id,
                    notif_type="QR_USED_PASS",
                    title="Qua cổng bằng Vé Tháng (0 VNĐ)",
                    message=(
                        f"Lượt đi {usage_tx_code} được miễn phí bằng {active_pass['plan_name']}. "
                        f"Số dư ví giữ nguyên {format_vnd(balance_after)}."
                    ),
                    metadata={"transaction_code": usage_tx_code, "payment_method": "MONTHLY_PASS"},
                )
            else:
                n_used = create_notification_in_tx(
                    conn,
                    user_id=user_id,
                    notif_type="QR_USED",
                    title="Quét thẻ qua cổng thành công",
                    message=f"Đã trừ {format_vnd(fare)} cho lượt đi mã {usage_tx_code}. Số dư còn {format_vnd(balance_after)}.",
                    metadata={"transaction_code": usage_tx_code, "amount": fare, "balance_after": balance_after},
                )
            if n_used:
                post_commit_user_events.append((user_id, "notification", n_used))

            if remaining_pass is None:
                if balance_after < fare:
                    n_empty = create_notification_in_tx(
                        conn,
                        user_id=user_id,
                        notif_type="INSUFFICIENT_BALANCE",
                        title="Số dư không đủ — Mã QR đã tạm khóa",
                        message=(
                            f"Số dư hiện tại ({format_vnd(balance_after)}) dưới mức {format_vnd(fare)}. "
                            "Vui lòng nạp thêm tiền hoặc mua Vé Tháng để mở lại mã QR."
                        ),
                        metadata={"balance": balance_after, "required_fare": fare},
                    )
                    if n_empty:
                        post_commit_user_events.append((user_id, "notification", n_empty))
                elif balance_after < low_threshold:
                    n_low = create_notification_in_tx(
                        conn,
                        user_id=user_id,
                        notif_type="LOW_BALANCE",
                        title="Cảnh báo số dư thấp",
                        message=f"Số dư ví còn {format_vnd(balance_after)} (dưới {format_vnd(low_threshold)}). Nên nạp thêm sớm.",
                        metadata={"balance": balance_after},
                    )
                    if n_low:
                        post_commit_user_events.append((user_id, "notification", n_low))

            amt_fmt = "0 VNĐ (Vé Tháng)" if payment_method == "MONTHLY_PASS" else f"-{format_vnd(effective_amount)}"
            result_payload = {
                "status": "SUCCESS",
                "idempotent_replay": False,
                "payment_method": payment_method,
                "pass_name": active_pass["plan_name"] if active_pass else None,
                "pass_code": active_pass["pass_code"] if active_pass else None,
                "transaction_code": usage_tx_code,
                "user_id": user_id,
                "user_name": user["full_name"],
                "username": user["username"],
                "card_number": card_number,
                "card_status_after": new_card_status,
                "amount": effective_amount,
                "amount_formatted": amt_fmt,
                "balance_before": balance_before,
                "balance_before_formatted": format_vnd(balance_before),
                "balance_after": balance_after,
                "balance_after_formatted": format_vnd(balance_after),
                "scanned_at": now_iso,
                "admin_name": admin["full_name"],
                "qr_reissued": next_qr_bundle is not None,
            }

            post_commit_user_events.append(
                (
                    user_id,
                    "card_scanned",
                    {
                        "transaction_code": usage_tx_code,
                        "payment_method": payment_method,
                        "amount": effective_amount,
                        "amount_formatted": amt_fmt,
                        "balance_before": balance_before,
                        "balance_after": balance_after,
                        "balance_formatted": format_vnd(balance_after),
                        "card_status": new_card_status,
                        "next_qr": next_qr_bundle,
                        "scanned_at": now_iso,
                    },
                )
            )
            post_commit_admin_event = result_payload

    if rejected is not None:
        realtime_broker.publish_to_admins(
            "scan_completed",
            {"status": "FAILED", "error_code": rejected.code, "message": rejected.message},
        )
        raise rejected

    for uid, ev_name, ev_data in post_commit_user_events:
        emit_user_realtime_update(uid, ev_name, ev_data)
    if post_commit_admin_event:
        realtime_broker.publish_to_admins("scan_completed", post_commit_admin_event)

    return result_payload
