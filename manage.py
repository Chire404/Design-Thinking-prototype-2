"""Management CLI for VeloPass: database migrations, secure initial Admin creation, dev seeding & health check."""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
import sys

from backend import create_app
from backend.database import read_db, run_migrations
from backend.services.auth_service import create_admin_account, register_user
from backend.services.topup_service import admin_confirm_topup, create_topup_order


def cmd_migrate(_args: argparse.Namespace) -> None:
    app = create_app()
    applied = run_migrations(app.config["DATABASE_PATH"])
    if applied:
        print(f"[OK] Applied migrations: {', '.join(applied)}")
    else:
        print(f"[OK] Database schema is up-to-date at {app.config['DATABASE_PATH']}")


def cmd_create_admin(args: argparse.Namespace) -> None:
    app = create_app()
    with app.app_context():
        username = args.username or os.getenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
        email = args.email or os.getenv("BOOTSTRAP_ADMIN_EMAIL", "admin@velopass.local")
        password = args.password or os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or ""
        if not password:
            password = getpass.getpass("Nhập mật khẩu cho tài khoản Admin (>= 8 ký tự): ")
        admin = create_admin_account(
            username=username,
            email=email,
            password=password,
            full_name=args.full_name or "Quản trị viên VeloPass",
            super_admin=True,
        )
        print(f"[OK] Admin account ready -> username='{admin['username']}', role='{admin['role']}'")


def cmd_seed_dev(_args: argparse.Namespace) -> None:
    """Seed safe development accounts ONLY when APP_ENV != production."""
    app = create_app()
    if app.config["APP_ENV"] == "production":
        print("[ABORT] Cannot run development seed in production environment.", file=sys.stderr)
        sys.exit(1)

    with app.app_context():
        admin_pw = os.getenv("BOOTSTRAP_ADMIN_PASSWORD") or "Admin@123456"
        admin = create_admin_account(
            username="admin",
            email="admin@velopass.vn",
            password=admin_pw,
            full_name="Điều hành Trung tâm VeloPass",
            super_admin=True,
        )
        print(f"[SEED] Admin ready: username='admin' | password='{admin_pw}'")

        with read_db() as conn:
            existing_user = conn.execute("SELECT id FROM users WHERE username = 'nguyenvana'").fetchone()

        if not existing_user:
            user = register_user(
                username="nguyenvana",
                email="nguyenvana@velopass.vn",
                password="User@123456",
                full_name="Nguyễn Văn A",
                phone="0901234567",
            )
            order = create_topup_order(user["id"], 100000)
            admin_confirm_topup(
                admin_id=admin["id"],
                topup_id=order["id"],
                bank_reference=f"SEED-STMT-{secrets.token_hex(4).upper()}",
                note="Khởi tạo số dư mẫu môi trường phát triển",
                qr_hmac_secret=app.config["QR_HMAC_SECRET"],
                ip_address="127.0.0.1",
            )
            print(
                f"[SEED] Demo User ready: username='nguyenvana' | password='User@123456' "
                f"| card='{user['card_number']}' | balance='100.000 VNĐ'"
            )
        else:
            print("[SEED] Demo User 'nguyenvana' already exists.")


def cmd_check_health(_args: argparse.Namespace) -> None:
    app = create_app()
    with app.app_context():
        with read_db() as conn:
            tables = [
                r["name"]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                ).fetchall()
            ]
    print(f"[OK] Health check passed. Verified {len(tables)} tables: {', '.join(tables)}")


def main() -> None:
    parser = argparse.ArgumentParser(description="VeloPass Management CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("migrate", help="Apply SQL database migrations")

    p_admin = sub.add_parser("create-admin", help="Create initial SUPER_ADMIN account safely")
    p_admin.add_argument("--username", default="")
    p_admin.add_argument("--email", default="")
    p_admin.add_argument("--password", default="")
    p_admin.add_argument("--full-name", default="Quản trị viên VeloPass")

    sub.add_parser("seed", help="Seed development demo accounts (non-production only)")
    sub.add_parser("check-health", help="Verify database schema and app initialization")

    args = parser.parse_args()
    if args.command == "migrate":
        cmd_migrate(args)
    elif args.command == "create-admin":
        cmd_create_admin(args)
    elif args.command == "seed":
        cmd_seed_dev(args)
    elif args.command == "check-health":
        cmd_check_health(args)


if __name__ == "__main__":
    main()
