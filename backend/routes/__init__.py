"""Shared API response helpers, authentication/authorization decorators, and CSRF/Rate-limit guards."""

from __future__ import annotations

import functools
import hmac

from flask import current_app, g, jsonify, request

from backend.services import AppError
from backend.services.auth_service import get_authenticated_session
from backend.utils.security import rate_limiter

SESSION_COOKIE_NAME = "vp_session"
CSRF_COOKIE_NAME = "vp_csrf"


def api_success(data=None, message: str = "OK", status: int = 200):
    return jsonify({"ok": True, "message": message, "data": data if data is not None else {}}), status


def api_error(code: str, message: str, status: int = 400, details: dict | None = None):
    return (
        jsonify(
            {
                "ok": False,
                "error": {
                    "code": code,
                    "message": message,
                    "details": details or {},
                },
            }
        ),
        status,
    )


def extract_session_from_request() -> tuple[str | None, str]:
    """Return (raw_token, source) where source is 'bearer' or 'cookie'."""
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.lower().startswith("bearer "):
        return auth_header[7:].strip(), "bearer"
    cookie_token = request.cookies.get(SESSION_COOKIE_NAME)
    if cookie_token:
        return cookie_token.strip(), "cookie"
    return None, "none"


def client_ip() -> str:
    forwarded = request.headers.get("X-Forwarded-For", "")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.remote_addr or "127.0.0.1"


def require_auth(fn):
    """Require valid server-side session and enforce CSRF on cookie-authenticated mutations."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        raw_token, source = extract_session_from_request()
        session = get_authenticated_session(raw_token)
        if not session:
            raise AppError("UNAUTHORIZED", "Phiên đăng nhập không hợp lệ hoặc đã hết hạn. Vui lòng đăng nhập lại.", 401)

        if source == "cookie" and request.method in ("POST", "PUT", "PATCH", "DELETE"):
            header_csrf = request.headers.get("X-CSRF-Token", "").strip()
            expected_csrf = session.get("csrf_token") or ""
            if not header_csrf or not hmac.compare_digest(header_csrf, expected_csrf):
                raise AppError("CSRF_INVALID", "Xác thực bảo mật phiên (CSRF) không hợp lệ. Hãy tải lại trang.", 403)

        g.auth = session
        g.session_token = raw_token
        return fn(*args, **kwargs)

    return wrapper


def require_admin(fn):
    """Strictly require ADMIN or SUPER_ADMIN role with ACTIVE status on Backend."""

    @functools.wraps(fn)
    @require_auth
    def wrapper(*args, **kwargs):
        role = g.auth.get("role")
        status = g.auth.get("status")
        if role not in ("ADMIN", "SUPER_ADMIN") or status != "ACTIVE":
            raise AppError("FORBIDDEN", "Khu vực này chỉ dành cho Quản trị viên hệ thống.", 403)
        return fn(*args, **kwargs)

    return wrapper


def rate_limit(bucket_prefix: str, max_requests: int, window_seconds: int):
    """Apply per-IP sliding window rate limiting."""

    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if current_app.config.get("RATE_LIMIT_ENABLED", True):
                key = f"{bucket_prefix}:{client_ip()}"
                allowed, retry_after = rate_limiter.allow(key, max_requests, window_seconds)
                if not allowed:
                    raise AppError(
                        "RATE_LIMITED",
                        f"Thao tác quá nhanh. Vui lòng thử lại sau {retry_after} giây.",
                        429,
                        {"retry_after": retry_after},
                    )
            return fn(*args, **kwargs)

        return wrapper

    return decorator
