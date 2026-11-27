"""Monthly Pass API endpoints (/api/passes/*)."""

from __future__ import annotations

from flask import Blueprint, current_app, g, request

from backend.routes import api_success, rate_limit, require_auth
from backend.services.pass_service import (
    get_user_passes_overview,
    list_pass_plans,
    purchase_monthly_pass,
    set_pass_auto_renew,
)

pass_bp = Blueprint("pass_api", __name__, url_prefix="/api/passes")


@pass_bp.get("/plans")
def get_plans():
    return api_success({"plans": list_pass_plans(include_inactive=False)})


@pass_bp.get("/my")
@require_auth
def get_my_passes():
    data = get_user_passes_overview(int(g.auth["user_id"]))
    return api_success(data)


@pass_bp.post("/purchase")
@require_auth
@rate_limit("pass_purchase", max_requests=10, window_seconds=60)
def purchase_pass():
    body = request.get_json(silent=True) or {}
    res = purchase_monthly_pass(
        user_id=int(g.auth["user_id"]),
        plan_id=int(body.get("plan_id") or 0),
        auto_renew=bool(body.get("auto_renew", False)),
        qr_hmac_secret=str(current_app.config["QR_HMAC_SECRET"]),
    )
    msg = (
        "Đã gia hạn cộng dồn Vé Tháng thành công!"
        if res["is_extension"]
        else "Đã kích hoạt Vé Tháng thành công! Bạn có thể quét QR qua cổng miễn phí 0 VNĐ."
    )
    return api_success(res, msg, 201)


@pass_bp.post("/<int:pass_id>/auto-renew")
@require_auth
def toggle_auto_renew(pass_id: int):
    body = request.get_json(silent=True) or {}
    updated = set_pass_auto_renew(
        user_id=int(g.auth["user_id"]),
        pass_id=pass_id,
        auto_renew=bool(body.get("auto_renew", False)),
    )
    return api_success({"pass": updated}, "Đã cập nhật tùy chọn tự động gia hạn Vé Tháng.")
