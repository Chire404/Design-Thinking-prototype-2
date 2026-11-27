"""Authentication API endpoints (/api/auth/*)."""

from __future__ import annotations

from flask import Blueprint, current_app, g, make_response, request

from backend.routes import (
    CSRF_COOKIE_NAME,
    SESSION_COOKIE_NAME,
    api_success,
    client_ip,
    extract_session_from_request,
    rate_limit,
    require_auth,
)
from backend.services.auth_service import (
    change_user_password,
    confirm_password_reset,
    login_user,
    logout_session,
    register_user,
    request_password_reset,
)

auth_bp = Blueprint("auth_api", __name__, url_prefix="/api/auth")


@auth_bp.post("/register")
@rate_limit("auth_register", max_requests=10, window_seconds=60)
def register():
    body = request.get_json(silent=True) or {}
    created = register_user(
        username=body.get("username", ""),
        email=body.get("email", ""),
        password=body.get("password", ""),
        full_name=body.get("full_name", ""),
        phone=body.get("phone", ""),
        explicit_role_attempt=body.get("role"),
    )
    return api_success(created, "Đăng ký tài khoản và phát hành thẻ điện tử thành công.", 201)


@auth_bp.post("/login")
@rate_limit("auth_login", max_requests=15, window_seconds=60)
def login():
    body = request.get_json(silent=True) or {}
    identifier = body.get("identifier") or body.get("email") or body.get("username") or ""
    password = body.get("password") or ""
    remember_me = bool(body.get("remember_me", False))

    auth_result = login_user(
        identifier=identifier,
        password=password,
        remember_me=remember_me,
        ip_address=client_ip(),
        user_agent=request.headers.get("User-Agent", ""),
        session_ttl_hours=int(current_app.config["SESSION_TTL_HOURS"]),
        remember_me_days=int(current_app.config["REMEMBER_ME_DAYS"]),
    )

    user = auth_result["user"]
    redirect_to = "/admin" if user["role"] in ("ADMIN", "SUPER_ADMIN") else "/app"

    max_age = (
        int(current_app.config["REMEMBER_ME_DAYS"]) * 86400
        if remember_me
        else int(current_app.config["SESSION_TTL_HOURS"]) * 3600
    )
    secure_cookie = bool(current_app.config["SESSION_COOKIE_SECURE"])

    resp_tuple = api_success(
        {
            "user": user,
            "csrf_token": auth_result["csrf_token"],
            "access_token": auth_result["session_token"],
            "expires_at": auth_result["expires_at"],
            "redirect_to": redirect_to,
        },
        "Đăng nhập thành công.",
        200,
    )
    resp = make_response(resp_tuple)
    resp.set_cookie(
        SESSION_COOKIE_NAME,
        auth_result["session_token"],
        max_age=max_age if remember_me else None,
        httponly=True,
        secure=secure_cookie,
        samesite="Lax",
        path="/",
    )
    resp.set_cookie(
        CSRF_COOKIE_NAME,
        auth_result["csrf_token"],
        max_age=max_age if remember_me else None,
        httponly=False,
        secure=secure_cookie,
        samesite="Lax",
        path="/",
    )
    return resp


@auth_bp.post("/logout")
def logout():
    raw_token, _ = extract_session_from_request()
    logout_session(raw_token)
    resp = make_response(api_success({}, "Đã đăng xuất an toàn.", 200))
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    resp.delete_cookie(CSRF_COOKIE_NAME, path="/")
    return resp


@auth_bp.get("/me")
@require_auth
def me():
    session = g.auth
    return api_success(
        {
            "user": {
                "id": session["user_id"],
                "username": session["username"],
                "full_name": session["full_name"],
                "email": session["email"],
                "phone": session["phone"],
                "role": session["role"],
                "status": session["status"],
            },
            "csrf_token": session["csrf_token"],
        }
    )


@auth_bp.post("/forgot-password")
@rate_limit("auth_forgot", max_requests=6, window_seconds=60)
def forgot_password():
    body = request.get_json(silent=True) or {}
    info = request_password_reset(body.get("email", ""))
    payload = {"sent": True, "expires_in_minutes": info["expires_in_minutes"]}
    if current_app.config["APP_ENV"] != "production" and "dev_reset_token" in info:
        payload["reset_token_preview"] = info["dev_reset_token"]
    return api_success(
        payload,
        "Nếu email tồn tại trong hệ thống, mã khôi phục mật khẩu (hiệu lực 15 phút) đã được khởi tạo.",
    )


@auth_bp.post("/reset-password")
@rate_limit("auth_reset", max_requests=8, window_seconds=60)
def reset_password():
    body = request.get_json(silent=True) or {}
    confirm_password_reset(
        raw_reset_token=body.get("token", ""),
        new_password=body.get("new_password", ""),
    )
    return api_success({}, "Đặt lại mật khẩu thành công. Vui lòng đăng nhập lại.")


@auth_bp.post("/change-password")
@require_auth
def change_password():
    body = request.get_json(silent=True) or {}
    change_user_password(
        user_id=int(g.auth["user_id"]),
        current_password=body.get("current_password", ""),
        new_password=body.get("new_password", ""),
    )
    return api_success({}, "Cập nhật mật khẩu thành công.")
