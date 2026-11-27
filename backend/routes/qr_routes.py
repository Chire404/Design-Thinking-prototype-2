"""Dynamic QR issuance, inspection, refresh, and Admin Scanner atomic verification endpoints."""

from __future__ import annotations

from flask import Blueprint, current_app, g, request

from backend.routes import api_success, rate_limit, require_admin, require_auth
from backend.services.qr_service import (
    get_current_qr_status,
    issue_or_refresh_user_qr,
    scan_and_deduct_qr,
)

qr_bp = Blueprint("qr_api", __name__, url_prefix="/api/qr")


@qr_bp.post("/issue")
@require_auth
@rate_limit("qr_issue", max_requests=30, window_seconds=60)
def issue_qr():
    qr_bundle = issue_or_refresh_user_qr(
        user_id=int(g.auth["user_id"]),
        qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]),
    )
    return api_success(qr_bundle, "Đã cấp mã QR động mới cho thẻ điện tử.")


@qr_bp.post("/refresh")
@require_auth
@rate_limit("qr_refresh", max_requests=30, window_seconds=60)
def refresh_qr():
    qr_bundle = issue_or_refresh_user_qr(
        user_id=int(g.auth["user_id"]),
        qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]),
    )
    return api_success(qr_bundle, "Đã làm mới mã QR động.")


@qr_bp.get("/current")
@require_auth
def current_qr():
    issue_flag = str(request.args.get("issue", "0")).lower() in ("1", "true", "yes")
    info = get_current_qr_status(
        user_id=int(g.auth["user_id"]),
        qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]),
        issue_if_missing=issue_flag,
    )
    return api_success(info)


@qr_bp.post("/scan")
@require_admin
@rate_limit("qr_scan", max_requests=180, window_seconds=60)
def scan_qr():
    body = request.get_json(silent=True) or {}
    idem_key = (
        request.headers.get("Idempotency-Key")
        or body.get("idempotency_key")
        or ""
    )
    result = scan_and_deduct_qr(
        admin_id=int(g.auth["user_id"]),
        raw_qr_token=body.get("qr_token") or body.get("token") or "",
        qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]),
        idempotency_key=idem_key,
    )
    return api_success(result, "Quét thẻ và trừ tiền thành công.")
