"""Authentication, registration, brute-force lockouts, sessions, and password resets."""

from __future__ import annotations

import secrets
from datetime import timedelta

from backend.database import atomic_transaction, read_db, row_to_dict
from backend.services import AppError
from backend.services.card_wallet_service import create_notification_in_tx, get_system_settings_in_tx
from backend.utils.formatters import format_vnd, generate_card_number, parse_iso, utc_now, utc_now_iso
from backend.utils.security import (
    EMAIL_RE,
    USERNAME_RE,
    generate_session_token,
    hash_password,
    sha256_hex,
    verify_password,
)

MAX_FAILED_LOGINS = 5
LOCKOUT_MINUTES = 15


def register_user(
    username: str,
    email: str,
    password: str,
    full_name: str = "",
    phone: str = "",
    explicit_role_attempt: str | None = None,
) -> dict:
    """Register a standard USER account with an initialized Wallet (0 VND) and Virtual Card.

    Public registration NEVER creates ADMIN accounts.
    """
    if explicit_role_attempt and explicit_role_attempt.upper() != "USER":
        raise AppError("FORBIDDEN_ROLE", "Không được phép tự đăng ký hoặc nâng cấp quyền Quản trị viên.", 403)

    username = (username or "").strip()
    email = (email or "").strip().lower()
    full_name = (full_name or username).strip()
    phone = (phone or "").strip()

    if not USERNAME_RE.match(username):
        raise AppError(
            "INVALID_USERNAME",
            "Tên đăng nhập phải từ 3–32 ký tự và chỉ gồm chữ cái, số, dấu chấm, gạch ngang hoặc gạch dưới.",
            400,
        )
    if not EMAIL_RE.match(email):
        raise AppError("INVALID_EMAIL", "Địa chỉ email không hợp lệ.", 400)
    if not password or len(password) < 8:
        raise AppError("WEAK_PASSWORD", "Mật khẩu phải có ít nhất 8 ký tự.", 400)
    if len(full_name) < 2 or len(full_name) > 80:
        raise AppError("INVALID_NAME", "Họ và tên phải từ 2–80 ký tự.", 400)

    pw_hash = hash_password(password)
    now = utc_now_iso()

    with atomic_transaction() as conn:
        dup = conn.execute(
            "SELECT id, username, email FROM users WHERE username = ? OR email = ?",
            (username, email),
        ).fetchone()
        if dup:
            if dup["username"].lower() == username.lower():
                raise AppError("USERNAME_EXISTS", "Tên đăng nhập đã được sử dụng.", 409)
            raise AppError("EMAIL_EXISTS", "Email này đã được đăng ký.", 409)

        cur = conn.execute(
            """
            INSERT INTO users (username, full_name, email, phone, password_hash, role, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'USER', 'ACTIVE', ?, ?)
            """,
            (username, full_name, email, phone, pw_hash, now, now),
        )
        user_id = int(cur.lastrowid)

        conn.execute(
            """
            INSERT INTO wallets (user_id, balance, total_topup, total_spent, currency, status, created_at, updated_at)
            VALUES (?, 0, 0, 0, 'VND', 'ACTIVE', ?, ?)
            """,
            (user_id, now, now),
        )

        # Generate collision-checked Virtual Card number
        card_number = generate_card_number()
        while conn.execute("SELECT 1 FROM virtual_cards WHERE card_number = ?", (card_number,)).fetchone():
            card_number = generate_card_number()

        conn.execute(
            """
            INSERT INTO virtual_cards (user_id, card_number, card_version, status, issued_at, updated_at)
            VALUES (?, ?, 1, 'INSUFFICIENT_BALANCE', ?, ?)
            """,
            (user_id, card_number, now, now),
        )

        settings = get_system_settings_in_tx(conn)
        fare = int(settings["fare_amount"])
        create_notification_in_tx(
            conn,
            user_id=user_id,
            notif_type="WELCOME",
            title="Phát hành thẻ VeloPass thành công",
            message=(
                f"Thẻ {card_number} đã sẵn sàng. Hãy nạp tối thiểu {format_vnd(fare)} "
                "để kích hoạt mã QR qua cổng."
            ),
            metadata={"card_number": card_number},
        )

    return {
        "id": user_id,
        "username": username,
        "full_name": full_name,
        "email": email,
        "phone": phone,
        "role": "USER",
        "status": "ACTIVE",
        "card_number": card_number,
    }


def create_admin_account(
    username: str,
    email: str,
    password: str,
    full_name: str = "System Administrator",
    super_admin: bool = True,
) -> dict:
    """CLI/internal-only helper to create an initial ADMIN/SUPER_ADMIN account safely."""
    username = (username or "").strip()
    email = (email or "").strip().lower()
    if not USERNAME_RE.match(username):
        raise ValueError("Invalid admin username.")
    if not EMAIL_RE.match(email):
        raise ValueError("Invalid admin email.")
    if not password or len(password) < 8:
        raise ValueError("Admin password must be at least 8 characters.")

    role = "SUPER_ADMIN" if super_admin else "ADMIN"
    pw_hash = hash_password(password)
    now = utc_now_iso()

    with atomic_transaction() as conn:
        existing = conn.execute(
            "SELECT id, username, role FROM users WHERE username = ? OR email = ?",
            (username, email),
        ).fetchone()
        if existing:
            return dict(existing)

        cur = conn.execute(
            """
            INSERT INTO users (username, full_name, email, phone, password_hash, role, status, created_at, updated_at)
            VALUES (?, ?, ?, '', ?, ?, 'ACTIVE', ?, ?)
            """,
            (username, full_name, email, pw_hash, role, now, now),
        )
        admin_id = int(cur.lastrowid)
        # Provision wallet & card as well so schema invariants hold uniformly
        conn.execute(
            """
            INSERT INTO wallets (user_id, balance, total_topup, total_spent, currency, status, created_at, updated_at)
            VALUES (?, 0, 0, 0, 'VND', 'ACTIVE', ?, ?)
            """,
            (admin_id, now, now),
        )
        card_number = generate_card_number()
        conn.execute(
            """
            INSERT INTO virtual_cards (user_id, card_number, card_version, status, issued_at, updated_at)
            VALUES (?, ?, 1, 'INSUFFICIENT_BALANCE', ?, ?)
            """,
            (admin_id, card_number, now, now),
        )

    return {"id": admin_id, "username": username, "email": email, "role": role, "status": "ACTIVE"}


def login_user(
    identifier: str,
    password: str,
    remember_me: bool = False,
    ip_address: str = "",
    user_agent: str = "",
    session_ttl_hours: int = 24,
    remember_me_days: int = 30,
) -> dict:
    """Authenticate by email or username, enforce brute-force lockout, and issue session."""
    identifier = (identifier or "").strip()
    if not identifier or not password:
        raise AppError("INVALID_CREDENTIALS", "Vui lòng nhập đầy đủ Email/Username và mật khẩu.", 400)

    now_dt = utc_now()
    now_iso = utc_now_iso()
    deferred_error: AppError | None = None

    with atomic_transaction() as conn:
        user = conn.execute(
            "SELECT * FROM users WHERE username = ? OR email = ?",
            (identifier, identifier.lower()),
        ).fetchone()

        if not user:
            raise AppError("INVALID_CREDENTIALS", "Thông tin đăng nhập không chính xác.", 401)

        if user["locked_until"]:
            locked_until_dt = parse_iso(user["locked_until"])
            if locked_until_dt > now_dt:
                remaining_min = max(1, int((locked_until_dt - now_dt).total_seconds() // 60) + 1)
                raise AppError(
                    "ACCOUNT_TEMP_LOCKED",
                    f"Tài khoản tạm khóa do nhập sai mật khẩu nhiều lần. Vui lòng thử lại sau {remaining_min} phút.",
                    429,
                )

        if not verify_password(user["password_hash"], password):
            attempts = int(user["failed_login_attempts"]) + 1
            locked_until = None
            if attempts >= MAX_FAILED_LOGINS:
                locked_until = (now_dt + timedelta(minutes=LOCKOUT_MINUTES)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            conn.execute(
                "UPDATE users SET failed_login_attempts = ?, locked_until = ?, updated_at = ? WHERE id = ?",
                (attempts, locked_until, now_iso, user["id"]),
            )
            deferred_error = AppError("INVALID_CREDENTIALS", "Thông tin đăng nhập không chính xác.", 401)
        elif user["role"] in ("ADMIN", "SUPER_ADMIN") and user["status"] != "ACTIVE":
            deferred_error = AppError("ADMIN_SUSPENDED", "Tài khoản quản trị viên này đã bị vô hiệu hóa.", 403)
        else:
            # Reset failed login counter
            conn.execute(
                "UPDATE users SET failed_login_attempts = 0, locked_until = NULL, updated_at = ? WHERE id = ?",
                (now_iso, user["id"]),
            )

            ttl = timedelta(days=remember_me_days) if remember_me else timedelta(hours=session_ttl_hours)
            expires_at = (now_dt + ttl).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
            raw_token, token_hash, csrf_token = generate_session_token()

            conn.execute(
                """
                INSERT INTO sessions (user_id, token_hash, csrf_token, remember_me, ip_address, user_agent, expires_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    user["id"],
                    token_hash,
                    csrf_token,
                    1 if remember_me else 0,
                    ip_address[:64],
                    user_agent[:255],
                    expires_at,
                    now_iso,
                ),
            )

    if deferred_error is not None:
        raise deferred_error

    return {
        "session_token": raw_token,
        "csrf_token": csrf_token,
        "expires_at": expires_at,
        "remember_me": bool(remember_me),
        "user": {
            "id": user["id"],
            "username": user["username"],
            "full_name": user["full_name"],
            "email": user["email"],
            "phone": user["phone"],
            "role": user["role"],
            "status": user["status"],
        },
    }


def get_authenticated_session(raw_token: str | None) -> dict | None:
    if not raw_token:
        return None
    token_hash = sha256_hex(raw_token)
    now_dt = utc_now()
    with read_db() as conn:
        row = conn.execute(
            """
            SELECT s.id AS session_id, s.csrf_token, s.expires_at, s.revoked_at,
                   u.id AS user_id, u.username, u.full_name, u.email, u.phone, u.role, u.status
            FROM sessions s
            JOIN users u ON u.id = s.user_id
            WHERE s.token_hash = ?
            """,
            (token_hash,),
        ).fetchone()
        if not row or row["revoked_at"] is not None:
            return None
        if parse_iso(row["expires_at"]) <= now_dt:
            return None
        return dict(row)


def logout_session(raw_token: str | None) -> None:
    if not raw_token:
        return
    token_hash = sha256_hex(raw_token)
    with atomic_transaction() as conn:
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE token_hash = ? AND revoked_at IS NULL",
            (utc_now_iso(), token_hash),
        )


def request_password_reset(email: str) -> dict:
    """Issue single-use password reset token (15 minutes TTL)."""
    email = (email or "").strip().lower()
    if not EMAIL_RE.match(email):
        raise AppError("INVALID_EMAIL", "Vui lòng nhập địa chỉ email hợp lệ.", 400)

    raw_reset = secrets.token_urlsafe(24)
    token_hash = sha256_hex(raw_reset)
    now_dt = utc_now()
    expires_at = (now_dt + timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

    with atomic_transaction() as conn:
        user = row_to_dict(conn.execute("SELECT id, email FROM users WHERE email = ?", (email,)).fetchone())
        if not user:
            # Avoid user enumeration by returning generic success message
            return {"sent": True, "expires_in_minutes": 15}

        conn.execute(
            """
            INSERT INTO password_reset_tokens (user_id, token_hash, expires_at, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (user["id"], token_hash, expires_at, utc_now_iso()),
        )

    return {"sent": True, "expires_in_minutes": 15, "dev_reset_token": raw_reset}


def confirm_password_reset(raw_reset_token: str, new_password: str) -> None:
    if not raw_reset_token or not new_password or len(new_password) < 8:
        raise AppError("INVALID_RESET", "Mã khôi phục không hợp lệ hoặc mật khẩu dưới 8 ký tự.", 400)

    token_hash = sha256_hex(raw_reset_token.strip())
    now_dt = utc_now()
    now_iso = utc_now_iso()
    pw_hash = hash_password(new_password)

    with atomic_transaction() as conn:
        row = conn.execute(
            "SELECT * FROM password_reset_tokens WHERE token_hash = ?",
            (token_hash,),
        ).fetchone()
        if not row or row["used_at"] is not None or parse_iso(row["expires_at"]) <= now_dt:
            raise AppError("EXPIRED_RESET_TOKEN", "Mã đặt lại mật khẩu không hợp lệ hoặc đã hết hạn.", 400)

        conn.execute("UPDATE password_reset_tokens SET used_at = ? WHERE id = ?", (now_iso, row["id"]))
        conn.execute(
            "UPDATE users SET password_hash = ?, failed_login_attempts = 0, locked_until = NULL, updated_at = ? WHERE id = ?",
            (pw_hash, now_iso, row["user_id"]),
        )
        conn.execute(
            "UPDATE sessions SET revoked_at = ? WHERE user_id = ? AND revoked_at IS NULL",
            (now_iso, row["user_id"]),
        )


def change_user_password(user_id: int, current_password: str, new_password: str) -> None:
    if not new_password or len(new_password) < 8:
        raise AppError("WEAK_PASSWORD", "Mật khẩu mới phải có ít nhất 8 ký tự.", 400)

    with atomic_transaction() as conn:
        user = conn.execute("SELECT id, password_hash FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user or not verify_password(user["password_hash"], current_password or ""):
            raise AppError("WRONG_PASSWORD", "Mật khẩu hiện tại không chính xác.", 400)
        conn.execute(
            "UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?",
            (hash_password(new_password), utc_now_iso(), user_id),
        )
