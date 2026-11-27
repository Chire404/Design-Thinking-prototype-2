"""Admin Management API endpoints (/api/admin/*) strictly protected by @require_admin."""

from __future__ import annotations

import queue

from flask import Blueprint, Response, current_app, g, request, stream_with_context

from backend.database import read_db, rows_to_dicts
from backend.routes import api_success, client_ip, require_admin
from backend.services.admin_service import (
    admin_adjust_wallet_balance,
    get_admin_dashboard_metrics,
    get_admin_user_detail,
    get_bank_settings,
    get_system_configuration,
    list_admin_topups,
    list_admin_transactions,
    list_audit_logs,
    list_support_requests,
    list_users_for_admin,
    resolve_support_request,
    set_user_account_status,
    update_bank_settings,
    update_system_configuration,
)
from backend.services.bank_reconciliation_service import (
    admin_manual_link_statement_entry,
    get_bank_sync_settings,
    list_bank_statement_entries,
    parse_and_reconcile_statement_text,
    poll_external_bank_api_and_reconcile,
    update_bank_sync_settings,
)
from backend.services.pass_service import (
    admin_list_user_passes,
    admin_upsert_pass_plan,
    list_pass_plans,
)
from backend.services.topup_service import admin_confirm_topup, admin_reject_topup
from backend.utils.formatters import format_vnd
from backend.utils.realtime import realtime_broker

admin_bp = Blueprint("admin_api", __name__, url_prefix="/api/admin")


@admin_bp.get("/dashboard")
@require_admin
def dashboard():
    period = request.args.get("period", "day")
    data = get_admin_dashboard_metrics(period=period)
    return api_success(data)


@admin_bp.get("/users")
@require_admin
def users_list():
    search = request.args.get("search", "")
    status = request.args.get("status", "")
    users = list_users_for_admin(search=search, status_filter=status)
    return api_success({"users": users})


@admin_bp.get("/users/<int:user_id>")
@require_admin
def user_detail(user_id: int):
    detail = get_admin_user_detail(user_id)
    return api_success(detail)


@admin_bp.post("/users/<int:user_id>/suspend")
@require_admin
def suspend_user(user_id: int):
    body = request.get_json(silent=True) or {}
    res = set_user_account_status(
        admin_id=int(g.auth["user_id"]),
        target_user_id=user_id,
        suspend=True,
        reason=body.get("reason", "Khóa tài khoản theo quyết định quản trị"),
        qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]),
        ip_address=client_ip(),
    )
    return api_success(res, "Đã khóa tài khoản và vô hiệu hóa mã QR của người dùng.")


@admin_bp.post("/users/<int:user_id>/activate")
@require_admin
def activate_user(user_id: int):
    body = request.get_json(silent=True) or {}
    res = set_user_account_status(
        admin_id=int(g.auth["user_id"]),
        target_user_id=user_id,
        suspend=False,
        reason=body.get("reason", "Mở khóa tài khoản"),
        qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]),
        ip_address=client_ip(),
    )
    return api_success(res, "Đã mở khóa tài khoản người dùng.")


@admin_bp.post("/users/<int:user_id>/adjust-balance")
@require_admin
def adjust_balance(user_id: int):
    body = request.get_json(silent=True) or {}
    res = admin_adjust_wallet_balance(
        admin_id=int(g.auth["user_id"]),
        target_user_id=user_id,
        direction=body.get("direction", ""),
        raw_amount=body.get("amount"),
        reason=body.get("reason", ""),
        qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]),
        ip_address=client_ip(),
    )
    return api_success(res, "Đã tạo giao dịch điều chỉnh số dư và ghi nhận Audit Log.")


@admin_bp.get("/transactions")
@require_admin
def transactions_list():
    items = list_admin_transactions(
        tx_type=request.args.get("type", ""),
        user_query=request.args.get("user", ""),
        date_from=request.args.get("date_from", ""),
        date_to=request.args.get("date_to", ""),
        min_amount=request.args.get("min_amount", ""),
        max_amount=request.args.get("max_amount", ""),
    )
    return api_success({"transactions": items})


@admin_bp.get("/scan-logs")
@require_admin
def scan_logs_list():
    status = (request.args.get("status") or "").upper()
    with read_db() as conn:
        sql = """
            SELECT sl.*, u.full_name AS user_name, u.username, a.full_name AS admin_name
            FROM scan_logs sl
            LEFT JOIN users u ON u.id = sl.user_id
            LEFT JOIN users a ON a.id = sl.admin_id
            WHERE 1 = 1
        """
        params: list = []
        if status in ("SUCCESS", "FAILED"):
            sql += " AND sl.status = ?"
            params.append(status)
        sql += " ORDER BY sl.id DESC LIMIT 100"
        rows = rows_to_dicts(conn.execute(sql, params).fetchall())
    for r in rows:
        r["amount_formatted"] = f"-{format_vnd(r['amount'])}" if r["amount"] else "0 VNĐ"
        r["balance_before_formatted"] = format_vnd(r["balance_before"]) if r["balance_before"] is not None else "—"
        r["balance_after_formatted"] = format_vnd(r["balance_after"]) if r["balance_after"] is not None else "—"
    return api_success({"scan_logs": rows})


@admin_bp.get("/topups")
@require_admin
def topups_list():
    items = list_admin_topups(
        status_filter=request.args.get("status", ""),
        user_query=request.args.get("user", ""),
    )
    return api_success({"topups": items})


@admin_bp.post("/topups/<int:topup_id>/confirm")
@require_admin
def confirm_topup(topup_id: int):
    body = request.get_json(silent=True) or {}
    res = admin_confirm_topup(
        admin_id=int(g.auth["user_id"]),
        topup_id=topup_id,
        bank_reference=body.get("bank_reference", ""),
        note=body.get("note", ""),
        qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]),
        ip_address=client_ip(),
    )
    return api_success(res, "Đã xác nhận giao dịch sao kê và cộng tiền vào ví người dùng.")


@admin_bp.post("/topups/<int:topup_id>/reject")
@require_admin
def reject_topup(topup_id: int):
    body = request.get_json(silent=True) or {}
    res = admin_reject_topup(
        admin_id=int(g.auth["user_id"]),
        topup_id=topup_id,
        reason=body.get("reason", ""),
        ip_address=client_ip(),
    )
    return api_success(res, "Đã từ chối yêu cầu nạp tiền.")


@admin_bp.get("/settings/bank")
@require_admin
def get_bank():
    return api_success(get_bank_settings())


@admin_bp.put("/settings/bank")
@require_admin
def put_bank():
    body = request.get_json(silent=True) or {}
    updated = update_bank_settings(
        admin_id=int(g.auth["user_id"]),
        admin_role=str(g.auth["role"]),
        payload=body,
        ip_address=client_ip(),
    )
    return api_success(updated, "Đã lưu cấu hình ngân hàng nhận tiền.")


@admin_bp.get("/settings/system")
@require_admin
def get_sys_settings():
    cfg = get_system_configuration()
    cfg["webhook_configured"] = bool(current_app.config.get("PAYMENT_WEBHOOK_SECRET"))
    cfg["webhook_provider"] = current_app.config.get("PAYMENT_WEBHOOK_PROVIDER", "SEPAY_COMPATIBLE")
    return api_success(cfg)


@admin_bp.put("/settings/system")
@require_admin
def put_sys_settings():
    body = request.get_json(silent=True) or {}
    updated = update_system_configuration(
        admin_id=int(g.auth["user_id"]),
        payload=body,
        ip_address=client_ip(),
    )
    return api_success(updated, "Đã cập nhật cấu hình vận hành hệ thống.")


@admin_bp.get("/audit-logs")
@require_admin
def audit_logs():
    limit = int(request.args.get("limit", "100"))
    return api_success({"audit_logs": list_audit_logs(limit=limit)})


# --- PROTOTYPE 2: MONTHLY PASS MANAGEMENT ---


@admin_bp.get("/pass-plans")
@require_admin
def get_all_pass_plans():
    return api_success({"plans": list_pass_plans(include_inactive=True)})


@admin_bp.post("/pass-plans")
@require_admin
def save_pass_plan():
    body = request.get_json(silent=True) or {}
    saved = admin_upsert_pass_plan(
        admin_id=int(g.auth["user_id"]),
        payload=body,
        ip_address=client_ip(),
    )
    return api_success({"plan": saved}, "Đã lưu gói Vé Tháng thành công.")


@admin_bp.get("/passes")
@require_admin
def get_subscriber_passes():
    items = admin_list_user_passes(
        status_filter=request.args.get("status", ""),
        search=request.args.get("search", ""),
    )
    return api_success({"passes": items})


# --- PROTOTYPE 2: AUTOMATED BANK ACCOUNT RECONCILIATION (SONG SONG DUYỆT THỦ CÔNG) ---


@admin_bp.get("/reconciliation")
@require_admin
def get_reconciliation_overview():
    match_status = request.args.get("status", "")
    statement_data = list_bank_statement_entries(match_status=match_status, limit=150)
    pending_orders = list_admin_topups(status_filter="", user_query="")
    pending_candidates = [o for o in pending_orders if o["status"] in ("PENDING", "PROCESSING")]
    return api_success(
        {
            "sync_settings": get_bank_sync_settings(include_raw_secret=False),
            "entries": statement_data["entries"],
            "summary_counts": statement_data["summary_counts"],
            "pending_topups": pending_candidates,
        }
    )


@admin_bp.put("/reconciliation/settings")
@require_admin
def put_reconciliation_settings():
    body = request.get_json(silent=True) or {}
    updated = update_bank_sync_settings(
        admin_id=int(g.auth["user_id"]),
        payload=body,
        ip_address=client_ip(),
    )
    return api_success(updated, "Đã lưu cấu hình đối soát ngân hàng tự động.")


@admin_bp.post("/reconciliation/sync-now")
@require_admin
def sync_bank_api_now():
    res = poll_external_bank_api_and_reconcile(
        qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]),
    )
    return api_success(
        res,
        f"Đồng bộ ngân hàng hoàn tất: nhận {res['total_received']} giao dịch, tự động duyệt {res['auto_approved']} đơn nạp!",
    )


@admin_bp.post("/reconciliation/import")
@require_admin
def import_bank_statement():
    fmt = "auto"
    content_text = ""
    if request.files and "file" in request.files:
        f = request.files["file"]
        filename = (f.filename or "").lower()
        if filename.endswith(".json"):
            fmt = "json"
        elif filename.endswith(".csv"):
            fmt = "csv"
        raw = f.read()
        content_text = raw.decode("utf-8-sig", errors="replace")
    else:
        body = request.get_json(silent=True) or {}
        content_text = str(body.get("content") or "")
        fmt = str(body.get("format") or "auto")

    res = parse_and_reconcile_statement_text(
        content_text=content_text,
        fmt=fmt,
        qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]),
    )
    return api_success(
        res,
        f"Đã đối chiếu {res['ingested']} giao dịch sao kê mới — Tự động duyệt {res['auto_approved']} đơn nạp khớp!",
    )


@admin_bp.post("/reconciliation/entries/<int:entry_id>/link")
@require_admin
def manual_link_entry(entry_id: int):
    body = request.get_json(silent=True) or {}
    res = admin_manual_link_statement_entry(
        admin_id=int(g.auth["user_id"]),
        entry_id=entry_id,
        topup_id=int(body.get("topup_id") or 0),
        qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]),
        ip_address=client_ip(),
    )
    return api_success(res, "Đã ghép thủ công bút toán sao kê và cộng tiền vào ví người dùng.")


@admin_bp.get("/support")
@require_admin
def support_list():
    status = request.args.get("status", "")
    return api_success({"support_requests": list_support_requests(status_filter=status)})


@admin_bp.post("/support/<int:request_id>/resolve")
@require_admin
def support_resolve(request_id: int):
    body = request.get_json(silent=True) or {}
    res = resolve_support_request(
        admin_id=int(g.auth["user_id"]),
        request_id=request_id,
        status=body.get("status", "RESOLVED"),
        admin_note=body.get("admin_note", ""),
        ip_address=client_ip(),
    )
    return api_success(res, "Đã cập nhật trạng thái xử lý hỗ trợ.")


@admin_bp.get("/stream")
@require_admin
def sse_admin_stream():
    def event_stream():
        q = realtime_broker.subscribe_admin()
        try:
            yield 'event: connected\ndata: {"ok": true}\n\n'
            while True:
                try:
                    msg = q.get(timeout=15.0)
                    yield msg
                except queue.Empty:
                    yield ": keepalive\n\n"
        finally:
            realtime_broker.unsubscribe_admin(q)

    headers = {
        "Content-Type": "text/event-stream; charset=utf-8",
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }
    return Response(stream_with_context(event_stream()), headers=headers)
