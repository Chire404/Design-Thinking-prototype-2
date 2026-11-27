"""User profile, Virtual Card, Wallet balance, personal Transaction History, Support & Real-time SSE."""

from __future__ import annotations

import queue

from flask import Blueprint, Response, g, request, stream_with_context

from backend.database import atomic_transaction, read_db, rows_to_dicts
from backend.routes import api_success, require_auth
from backend.services import AppError
from backend.services.card_wallet_service import PRIORITY_LABELS, get_user_wallet_and_card_snapshot
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
    if "role" in body or "balance" in body or "status" in body or "priority_status" in body:
        raise AppError("FORBIDDEN_FIELD", "Không được phép tự chỉnh sửa vai trò, trạng thái hoặc số dư.", 403)

    full_name = str(body.get("full_name") or "").strip()
    phone = str(body.get("phone") or "").strip()[:20]
    raw_plate = str(body.get("vehicle_plate") or "").strip().upper()
    vehicle_plate = "".join(ch for ch in raw_plate if ch.isalnum() or ch in ("-", ".", " "))[:20].strip()

    if len(full_name) < 2 or len(full_name) > 80:
        raise AppError("INVALID_NAME", "Họ và tên phải từ 2–80 ký tự.", 400)

    user_id = int(g.auth["user_id"])
    with atomic_transaction() as conn:
        if "vehicle_plate" in body:
            conn.execute(
                "UPDATE users SET full_name = ?, phone = ?, vehicle_plate = ?, updated_at = ? WHERE id = ?",
                (full_name, phone, vehicle_plate, utc_now_iso(), user_id),
            )
        else:
            conn.execute(
                "UPDATE users SET full_name = ?, phone = ?, updated_at = ? WHERE id = ?",
                (full_name, phone, utc_now_iso(), user_id),
            )
    snap = get_user_wallet_and_card_snapshot(user_id)
    return api_success({"user": snap["user"]}, "Đã cập nhật hồ sơ cá nhân và biển số xe.")


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


@user_bp.get("/api/user/priority")
@require_auth
def get_my_priority_registrations():
    user_id = int(g.auth["user_id"])
    snap = get_user_wallet_and_card_snapshot(user_id)
    with read_db() as conn:
        rows = rows_to_dicts(
            conn.execute(
                "SELECT id, priority_type, id_card_code, organization_name, proof_note, status, admin_note, created_at, reviewed_at FROM priority_requests WHERE user_id = ? ORDER BY id DESC LIMIT 15",
                (user_id,),
            ).fetchall()
        )
    for r in rows:
        r["priority_label"] = PRIORITY_LABELS.get(r["priority_type"], r["priority_type"])
    return api_success({"user": snap["user"], "requests": rows})


@user_bp.post("/api/user/priority")
@require_auth
def submit_priority_registration():
    body = request.get_json(silent=True) or {}
    p_type = str(body.get("priority_type") or "").strip().upper()
    valid_types = ("STUDENT", "SENIOR", "DISABLED", "CONTRIBUTOR", "STAFF")
    if p_type not in valid_types:
        raise AppError("INVALID_PRIORITY_TYPE", "Vui lòng chọn nhóm đối tượng ưu tiên hợp lệ.", 400)

    id_card_code = str(body.get("id_card_code") or "").strip()[:50]
    org_name = str(body.get("organization_name") or "").strip()[:120]
    proof_note = str(body.get("proof_note") or "").strip()[:500]
    proof_img = str(body.get("proof_image_data") or "").strip()

    if len(id_card_code) < 4 or len(org_name) < 3:
        raise AppError(
            "INVALID_PRIORITY_INFO",
            "Vui lòng nhập đầy đủ Mã thẻ HSSV / CCCD (>= 4 ký tự) và Trường học / Đơn vị xác nhận (>= 3 ký tự).",
            400,
        )
    if not proof_img or not proof_img.startswith("data:image/"):
        raise AppError(
            "PROOF_IMAGE_REQUIRED",
            "Bắt buộc tải lên ảnh chụp minh chứng (Thẻ sinh viên, ảnh chụp Portal trường hoặc giấy xác nhận ưu tiên).",
            400,
        )
    if len(proof_img) > 2_500_000:
        raise AppError("INVALID_PROOF_IMAGE", "Ảnh minh chứng vượt quá dung lượng cho phép (tối đa 2MB sau nén).", 400)

    user_id = int(g.auth["user_id"])
    now_iso = utc_now_iso()

    with atomic_transaction() as conn:
        existing_pending = conn.execute(
            "SELECT id FROM priority_requests WHERE user_id = ? AND status = 'PENDING'",
            (user_id,),
        ).fetchone()
        if existing_pending:
            raise AppError(
                "ALREADY_PENDING",
                "Bạn đang có một hồ sơ đăng ký đối tượng ưu tiên chờ Admin xét duyệt.",
                409,
            )

        cur = conn.execute(
            """
            INSERT INTO priority_requests (
                user_id, priority_type, id_card_code, organization_name,
                proof_note, proof_image_data, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'PENDING', ?, ?)
            """,
            (user_id, p_type, id_card_code, org_name, proof_note, proof_img, now_iso, now_iso),
        )
        req_id = int(cur.lastrowid)

        conn.execute(
            """
            UPDATE users
            SET priority_type = ?, priority_status = 'PENDING',
                priority_card_code = ?, priority_org = ?, updated_at = ?
            WHERE id = ?
            """,
            (p_type, id_card_code, org_name, now_iso, user_id),
        )

    realtime_broker.publish_to_admins(
        "priority_submitted",
        {"request_id": req_id, "user_id": user_id, "priority_type": p_type, "label": PRIORITY_LABELS[p_type]},
    )
    return api_success(
        {"id": req_id, "status": "PENDING", "priority_type": p_type, "priority_label": PRIORITY_LABELS[p_type]},
        "Đã gửi hồ sơ đăng ký đối tượng ưu tiên! Quản trị viên sẽ kiểm tra và phê duyệt.",
        201,
    )


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
