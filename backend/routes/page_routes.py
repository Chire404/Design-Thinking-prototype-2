"""HTML Page routes (Auth, User Transit App, Admin Control Center, PWA manifest & Service Worker)."""

from __future__ import annotations

from flask import Blueprint, current_app, redirect, render_template, send_from_directory

from backend.routes import api_success, extract_session_from_request
from backend.services.auth_service import get_authenticated_session

pages_bp = Blueprint("pages", __name__)


def _current_session() -> dict | None:
    raw, _ = extract_session_from_request()
    return get_authenticated_session(raw)


@pages_bp.get("/")
def root_index():
    sess = _current_session()
    if not sess:
        return redirect("/login")
    if sess["role"] in ("ADMIN", "SUPER_ADMIN"):
        return redirect("/admin")
    return redirect("/app")


@pages_bp.get("/login")
@pages_bp.get("/register")
@pages_bp.get("/forgot-password")
def auth_pages():
    sess = _current_session()
    if sess:
        return redirect("/admin" if sess["role"] in ("ADMIN", "SUPER_ADMIN") else "/app")
    return render_template("auth.html", app_name=current_app.config["APP_NAME"])


@pages_bp.get("/app")
@pages_bp.get("/app/<path:subpath>")
def user_app_page(subpath: str = ""):
    sess = _current_session()
    if not sess:
        return redirect("/login")
    if sess["role"] in ("ADMIN", "SUPER_ADMIN"):
        return redirect("/admin")
    return render_template("user_app.html", app_name=current_app.config["APP_NAME"], session_user=sess)


@pages_bp.get("/admin")
@pages_bp.get("/admin/<path:subpath>")
def admin_app_page(subpath: str = ""):
    sess = _current_session()
    if not sess:
        return redirect("/login")
    if sess["role"] not in ("ADMIN", "SUPER_ADMIN") or sess["status"] != "ACTIVE":
        # Strict Backend Authorization: standard USER gets HTTP 403 Forbidden
        return (
            render_template(
                "auth.html",
                app_name=current_app.config["APP_NAME"],
                forbidden_notice="403 Forbidden — Tài khoản người dùng không được phép truy cập khu vực Quản trị.",
            ),
            403,
        )
    return render_template("admin_app.html", app_name=current_app.config["APP_NAME"], session_user=sess)


@pages_bp.get("/manifest.json")
def pwa_manifest():
    return send_from_directory(current_app.static_folder, "manifest.json", mimetype="application/manifest+json")


@pages_bp.get("/sw.js")
def pwa_service_worker():
    resp = send_from_directory(current_app.static_folder, "sw.js", mimetype="application/javascript")
    resp.headers["Service-Worker-Allowed"] = "/"
    resp.headers["Cache-Control"] = "no-cache"
    return resp


@pages_bp.get("/api/health")
def health_check():
    return api_success({"status": "healthy", "service": current_app.config["APP_NAME"]})
