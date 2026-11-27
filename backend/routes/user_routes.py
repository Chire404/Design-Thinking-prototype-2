"""User profile, Virtual Card, Wallet balance, personal Transaction History, Support & Real-time SSE."""

from __future__ import annotations

import queue

from flask import Blueprint, Response, g, request, stream_with_context

from backend.database import atomic_transaction, read_db, rows_to_dicts
from backend.routes import api_success, require_auth
from backend.services import AppError
from backend.services.card_wallet_service import get_user_wallet_and_card_snapshot
from backend.utils.formatters import format_vnd, utc_now_iso
from backend.utils.realtime import realtime_broker

user_bp = Blueprint("user_api", __name__)


@user_bp.get("/api/user/profile")
@require_auth
def get_profile():
    snap = get_user_wallet_and_card_snapshot(int(g.auth["user_id"]))
    return api_success({"user": snap["user"], "card": snap["card"]})


@user_bp.put("/api/user/profile")
@require_auth
def update_profile():
    body = request.get_json(silent=True) or {}
    if "role" in body or "balance" in body or "status" in body:
        raise AppError("FORBIDDEN_FIELD", "Không được phép tự chỉnh sửa vai trò, trạng thái hoặc số dư.", 403)

    full_name = str(body.get("full_name") or "").strip()
    phone = str(body.get("phone") or "").strip()[:20]
    if len(full_name) < 2 or len(full_name) > 80:
        raise AppError("INVALID_NAME", "Họ và tên phải từ 2–80 ký tự.", 400)

    user_id = int(g.auth["user_id"])
    with atomic_transaction() as conn:
        conn.execute(
            "UPDATE users SET full_name = ?, phone = ?, updated_at = ? WHERE id = ?",
            (full_name, phone, utc_now_iso(), user_id),
        )
    snap = get_user_wallet_and_card_snapshot(user_id)
    return api_success({"user": snap["user"]}, "Đã cập nhật hồ sơ cá nhân.")


@user_bp.get("/api/user/dashboard")
@require_auth
def get_dashboard():
    snap = get_user_wallet_and_card_snapshot(int(g.auth["user_id"]))
    return api_success(snap)


@user_bp.get("/api/user/card")
@require_auth
def get_card():
    snap = get_user_wallet_and_card_snapshot(int(g.auth["user_id"]))
    return api_success({"card": snap["card"], "wallet": snap["wallet"], "user": snap["user"]})


@user_bp.route("/api/user/balance", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@require_auth
def get_balance():
    if request.method != "GET":
        raise AppError("FORBIDDEN", "Người dùng không được phép tự chỉnh sửa số dư ví.", 403)
    snap = get_user_wallet_and_card_snapshot(int(g.auth["user_id"]))
    return api_success({"wallet": snap["wallet"], "card_status": snap["card"]["status"]})


@user_bp.get("/api/user/transactions")
@require_auth
def get_user_transactions():
    """Return strictly the authenticated User's own top-up history, usage history, and wallet ledger."""
    user_id = int(g.auth["user_id"])
    category = (request.args.get("type") or "all").lower()

    with read_db() as conn:
        topups = rows_to_dicts(
            conn.execute(
                """
                SELECT id, transaction_code, transfer_content, amount, bank_name, bank_short_name,
                       payment_provider, status, expires_at, created_at, confirmed_at
                FROM topup_transactions
                WHERE user_id = ?
                ORDER BY id DESC LIMIT 100
                """,
                (user_id,),
            ).fetchall()
        )
        for t in topups:
            t["amount_formatted"] = format_vnd(t["amount"])

        usages = rows_to_dicts(
            conn.execute(
                """
                SELECT id, transaction_code, amount, payment_method, pass_id,
                       balance_before, balance_after, status, created_at
                FROM usage_transactions
                WHERE user_id = ?
                ORDER BY id DESC LIMIT 100
                """,
                (user_id,),
            ).fetchall()
        )
        for u in usages:
            u["amount_formatted"] = (
                "0 VNĐ (Vé Tháng)" if u.get("payment_method") == "MONTHLY_PASS" else f"-{format_vnd(u['amount'])}"
            )
            u["balance_before_formatted"] = format_vnd(u["balance_before"])
            u["balance_after_formatted"] = format_vnd(u["balance_after"])

        ledger = rows_to_dicts(
            conn.execute(
                """
                SELECT id, transaction_code, transaction_type, amount, balance_before,
                       balance_after, reference_type, reference_id, description, created_at
                FROM wallet_transactions
                WHERE user_id = ?
                ORDER BY id DESC LIMIT 150
                """,
                (user_id,),
            ).fetchall()
        )
        for item in ledger:
            is_debit = item["transaction_type"] in ("USAGE", "PASS_PURCHASE", "ADJUSTMENT_DEBIT")
            item["signed_amount_formatted"] = f"{'-' if is_debit else '+'}{format_vnd(item['amount'])}"
            item["balance_before_formatted"] = format_vnd(item["balance_before"])
            item["balance_after_formatted"] = format_vnd(item["balance_after"])

    if category == "topup":
        return api_success({"topups": topups})
    if category == "usage":
        return api_success({"usages": usages})
    return api_success({"topups": topups, "usages": usages, "ledger": ledger})


@user_bp.get("/api/user/notifications")
@require_auth
def list_notifications():
    user_id = int(g.auth["user_id"])
    with read_db() as conn:
        items = rows_to_dicts(
            conn.execute(
                "SELECT * FROM notifications WHERE user_id = ? ORDER BY id DESC LIMIT 50",
                (user_id,),
            ).fetchall()
        )
        unread = conn.execute(
            "SELECT COUNT(*) AS cnt FROM notifications WHERE user_id = ? AND is_read = 0",
            (user_id,),
        ).fetchone()["cnt"]
    return api_success({"notifications": items, "unread_count": int(unread)})


@user_bp.post("/api/user/notifications/read-all")
@require_auth
def mark_notifications_read():
    user_id = int(g.auth["user_id"])
    with atomic_transaction() as conn:
        conn.execute("UPDATE notifications SET is_read = 1 WHERE user_id = ? AND is_read = 0", (user_id,))
    return api_success({}, "Đã đánh dấu đã đọc tất cả thông báo.")


@user_bp.get("/api/user/support")
@require_auth
def list_my_support_requests():
    user_id = int(g.auth["user_id"])
    with read_db() as conn:
        rows = rows_to_dicts(
            conn.execute(
                "SELECT * FROM support_requests WHERE user_id = ? ORDER BY id DESC LIMIT 30",
                (user_id,),
            ).fetchall()
        )
    return api_success({"support_requests": rows})


@user_bp.post("/api/user/support")
@require_auth
def create_support_request():
    body = request.get_json(silent=True) or {}
    subject = str(body.get("subject") or "").strip()
    message = str(body.get("message") or "").strip()
    if len(subject) < 4 or len(message) < 8:
        raise AppError("INVALID_SUPPORT", "Tiêu đề (>=4 ký tự) và nội dung yêu cầu hỗ trợ (>=8 ký tự) không hợp lệ.", 400)

    user_id = int(g.auth["user_id"])
    now_iso = utc_now_iso()
    with atomic_transaction() as conn:
        cur = conn.execute(
            """
            INSERT INTO support_requests (user_id, subject, message, status, created_at, updated_at)
            VALUES (?, ?, ?, 'OPEN', ?, ?)
            """,
            (user_id, subject[:120], message[:1000], now_iso, now_iso),
        )
        req_id = int(cur.lastrowid)
    return api_success({"id": req_id, "status": "OPEN"}, "Đã gửi yêu cầu hỗ trợ tới bộ phận Quản trị.", 201)


@user_bp.get("/api/notifications/stream")
@require_auth
def sse_user_stream():
    """Server-Sent Events real-time stream pushing instant QR scan & top-up balance updates."""
    user_id = int(g.auth["user_id"])

    def event_stream():
        q = realtime_broker.subscribe_user(user_id)
        try:
            yield 'event: connected\ndata: {"ok": true}\n\n'
            while True:
                try:
                    msg = q.get(timeout=15.0)
                    yield msg
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            realtime_broker.unsubscribe_user(user_id, q)

    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    return Response(stream_with_context(event_stream()), headers=headers)
