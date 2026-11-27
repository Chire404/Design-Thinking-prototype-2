"""Flask Application Factory with security headers, CORS, error handling, and automatic migrations."""

from __future__ import annotations

import logging

from flask import Flask, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from backend.config import PROJECT_ROOT, Config
from backend.database import atomic_transaction, run_migrations
from backend.routes import api_error
from backend.routes.admin_routes import admin_bp
from backend.routes.auth_routes import auth_bp
from backend.routes.page_routes import pages_bp
from backend.routes.pass_routes import pass_bp
from backend.routes.qr_routes import qr_bp
from backend.routes.topup_routes import topup_bp
from backend.routes.user_routes import user_bp
from backend.services import AppError
from backend.services.bank_reconciliation_service import start_background_reconciliation_daemon


def create_app(config_overrides: dict | None = None) -> Flask:
    cfg = Config(config_overrides)

    app = Flask(
        __name__,
        template_folder=str(PROJECT_ROOT / "frontend" / "templates"),
        static_folder=str(PROJECT_ROOT / "frontend" / "static"),
        static_url_path="/static",
    )
    app.config.from_mapping(cfg.to_dict())
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

    logging.basicConfig(
        level=logging.INFO if cfg.APP_ENV != "testing" else logging.WARNING,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    with app.app_context():
        run_migrations(cfg.DATABASE_PATH)
        if cfg.BANK_SYNC_API_URL or cfg.BANK_SYNC_API_TOKEN:
            with atomic_transaction(cfg.DATABASE_PATH) as conn:
                conn.execute(
                    """
                    UPDATE bank_sync_settings
                    SET provider = ?,
                        api_url = CASE WHEN ? != '' THEN ? ELSE api_url END,
                        api_token = CASE WHEN ? != '' THEN ? ELSE api_token END,
                        background_poll_enabled = CASE WHEN ? = 1 THEN 1 ELSE background_poll_enabled END,
                        poll_interval_seconds = ?
                    WHERE id = 1
                    """,
                    (
                        cfg.BANK_SYNC_PROVIDER,
                        cfg.BANK_SYNC_API_URL,
                        cfg.BANK_SYNC_API_URL,
                        cfg.BANK_SYNC_API_TOKEN,
                        cfg.BANK_SYNC_API_TOKEN,
                        1 if cfg.BANK_SYNC_BACKGROUND_ENABLED else 0,
                        cfg.BANK_SYNC_INTERVAL_SECONDS,
                    ),
                )

    app.register_blueprint(auth_bp)
    app.register_blueprint(user_bp)
    app.register_blueprint(topup_bp)
    app.register_blueprint(pass_bp)
    app.register_blueprint(qr_bp)
    app.register_blueprint(admin_bp)
    app.register_blueprint(pages_bp)

    start_background_reconciliation_daemon(app)

    @app.after_request
    def apply_security_and_cors_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["Permissions-Policy"] = "camera=(self), microphone=(), geolocation=()"
        if cfg.APP_ENV == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        origin = request.headers.get("Origin")
        allowed = cfg.CORS_ALLOWED_ORIGINS
        if origin and origin in allowed:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Credentials"] = "true"
            response.headers["Access-Control-Allow-Headers"] = (
                "Content-Type, Authorization, X-CSRF-Token, Idempotency-Key"
            )
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        return response

    @app.errorhandler(AppError)
    def handle_app_error(err: AppError):
        return api_error(err.code, err.message, err.http_status, err.details)

    @app.errorhandler(HTTPException)
    def handle_http_exception(err: HTTPException):
        if request.path.startswith("/api/"):
            return api_error(f"HTTP_{err.code}", err.description or "Yêu cầu không hợp lệ.", err.code or 400)
        return err

    @app.errorhandler(Exception)
    def handle_unexpected_exception(err: Exception):
        app.logger.exception("Unhandled exception on %s %s", request.method, request.path)
        if request.path.startswith("/api/"):
            return api_error("SERVER_ERROR", "Đã xảy ra lỗi máy chủ nội bộ. Vui lòng thử lại.", 500)
        return "500 Internal Server Error", 500

    return app
