"""Top-up creation, inspection, user transfer notification, and signed Payment Webhook APIs."""

from __future__ import annotations

import hmac

from flask import Blueprint, current_app, g, jsonify, request

from backend.routes import api_success, rate_limit, require_auth
from backend.services import AppError
from backend.services.bank_reconciliation_service import (
    _normalize_remote_api_payload,
    get_bank_sync_settings,
    poll_external_bank_api_and_reconcile,
    reconcile_bank_entries,
)
from backend.services.topup_service import (
    cancel_user_topup,
    create_topup_order,
    get_topup_detail,
    get_user_topups,
    process_payment_webhook,
    user_report_transferred,
)

topup_bp = Blueprint("topup_api", __name__)


@topup_bp.post("/api/topups")
@require_auth
@rate_limit("create_topup", max_requests=20, window_seconds=60)
def create_topup():
    body = request.get_json(silent=True) or {}
    order = create_topup_order(
        user_id=int(g.auth["user_id"]),
        raw_amount=body.get("amount"),
        topup_channel=body.get("topup_channel", "AUTO"),
        payment_method_detail=body.get("payment_method_detail", "BANK_TRANSFER"),
        payer_note=body.get("note", ""),
        purpose=body.get("purpose", "WALLET"),
        target_plan_id=body.get("target_plan_id"),
    )
    msg = (
        "Đã gửi Request nạp tiền thủ công tới Quản trị viên."
        if order["topup_channel"] == "MANUAL"
        else "Đã tạo đơn nạp tiền tự động và mã VietQR chuyển khoản."
    )
    return api_success(order, msg, 201)


@topup_bp.get("/api/topups")
@require_auth
def list_topups():
    items = get_user_topups(int(g.auth["user_id"]))
    return api_success({"topups": items})


@topup_bp.get("/api/topups/<int:topup_id>")
@require_auth
def get_topup(topup_id: int):
    is_admin = g.auth.get("role") in ("ADMIN", "SUPER_ADMIN")
    detail = get_topup_detail(int(g.auth["user_id"]), topup_id, is_admin=is_admin)
    return api_success(detail)


@topup_bp.post("/api/topups/<int:topup_id>/report-transferred")
@require_auth
def report_transferred(topup_id: int):
    body = request.get_json(silent=True) or {}
    updated = user_report_transferred(
        user_id=int(g.auth["user_id"]),
        topup_id=topup_id,
        note=body.get("note", ""),
    )
    return api_success(
        updated,
        "Đã ghi nhận thông báo chuyển khoản. Hệ thống/Admin sẽ đối soát sao kê ngân hàng trước khi cộng tiền.",
    )


@topup_bp.post("/api/topups/<int:topup_id>/cancel")
@require_auth
def cancel_topup(topup_id: int):
    updated = cancel_user_topup(int(g.auth["user_id"]), topup_id)
    return api_success(updated, "Đã hủy yêu cầu nạp tiền.")


@topup_bp.post("/api/topups/<int:topup_id>/auto-check")
@require_auth
@rate_limit("topup_auto_check", max_requests=10, window_seconds=60)
def auto_check_topup_from_bank(topup_id: int):
    """Trigger on-demand bank API reconciliation (if configured) and return live Top-up status."""
    user_id = int(g.auth["user_id"])
    cfg = get_bank_sync_settings(include_raw_secret=True)
    sync_triggered = False
    if cfg.get("provider") != "MANUAL_IMPORT_ONLY" and (cfg.get("api_url") or cfg.get("api_token")):
        try:
            poll_external_bank_api_and_reconcile(qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]))
            sync_triggered = True
        except Exception:
            sync_triggered = False

    detail = get_topup_detail(user_id, topup_id)
    detail["live_bank_api_checked"] = sync_triggered
    if detail["status"] == "SUCCESS":
        msg = "Đã tìm thấy giao dịch khớp trong tài khoản ngân hàng — Tiền đã được cộng vào ví!"
    elif sync_triggered:
        msg = "Đã quét tài khoản ngân hàng trực tuyến nhưng chưa thấy khoản chuyển khớp mã nội dung."
    else:
        msg = "Đang chờ biến động sao kê ngân hàng (tự động duyệt ngay khi khớp sao kê hoặc Admin xác nhận)."
    return api_success(detail, msg)


@topup_bp.post("/api/payment/webhook")
@rate_limit("payment_webhook", max_requests=60, window_seconds=60)
def payment_webhook():
    raw_body = request.get_data(cache=True)
    signature = (
        request.headers.get("X-Webhook-Signature")
        or request.headers.get("X-Signature")
        or request.headers.get("Authorization", "")
    )
    payload = request.get_json(silent=True) or {}
    result = process_payment_webhook(
        raw_body=raw_body,
        signature_header=signature,
        payload=payload,
        webhook_secret=str(current_app.config.get("PAYMENT_WEBHOOK_SECRET", "")),
        provider_name=str(current_app.config.get("PAYMENT_WEBHOOK_PROVIDER", "SEPAY_COMPATIBLE")),
        qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]),
    )
    return api_success(result, "Xử lý webhook thanh toán hoàn tất.", 200)


@topup_bp.post("/api/webhooks/sepay")
@rate_limit("sepay_webhook", max_requests=120, window_seconds=60)
def sepay_live_webhook():
    """Native SePay Webhook IPN receiver (`Authorization: Apikey <API_KEY>` -> returns `{"success": true}`)."""
    sync_cfg = get_bank_sync_settings(include_raw_secret=True)
    expected_key = (sync_cfg.get("api_token") or current_app.config.get("PAYMENT_WEBHOOK_SECRET") or "").strip()
    if not expected_key:
        raise AppError(
            "SEPAY_KEY_NOT_CONFIGURED",
            "Chưa lưu API Token / Secret Key trong Cấu hình đối soát SePay.",
            503,
        )

    auth_header = (request.headers.get("Authorization") or request.headers.get("X-Api-Key") or "").strip()
    provided_key = auth_header
    for prefix in ("apikey ", "bearer "):
        if auth_header.lower().startswith(prefix):
            provided_key = auth_header[len(prefix) :].strip()
            break

    if not provided_key or not hmac.compare_digest(provided_key, expected_key):
        return jsonify({"success": False, "ok": False, "message": "Invalid SePay API Key"}), 401

    payload = request.get_json(silent=True) or {}
    entries = _normalize_remote_api_payload("SEPAY_API", payload)
    stats = reconcile_bank_entries(
        entries,
        source="WEBHOOK",
        qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]),
    )
    return (
        jsonify(
            {
                "success": True,
                "ok": True,
                "message": "Đã tiếp nhận và đối soát biến động SePay.",
                "data": stats,
            }
        ),
        200,
    )
