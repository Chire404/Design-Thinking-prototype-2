"""Monthly Pass (Vé Tháng / Vé Tuần) subscription plans, purchase, stacking renewal & active pass lookup."""

from __future__ import annotations

import json
import math
import sqlite3
from datetime import timedelta

from backend.database import atomic_transaction, read_db, row_to_dict, rows_to_dicts
from backend.services import AppError
from backend.utils.formatters import format_vnd, generate_tx_code, parse_iso, utc_now, utc_now_iso
from backend.utils.realtime import realtime_broker
from backend.utils.security import validate_positive_vnd_integer


def _enrich_pass_dict(p: dict | None, now_dt=None) -> dict | None:
    if not p:
        return None
    now_dt = now_dt or utc_now()
    p["price_paid_formatted"] = format_vnd(p["price_paid"])
    exp_dt = parse_iso(p["expires_at"])
    start_dt = parse_iso(p["starts_at"])
    remaining_sec = max(0.0, (exp_dt - now_dt).total_seconds())
    p["remaining_days"] = int(math.ceil(remaining_sec / 86400.0)) if remaining_sec > 0 else 0
    p["remaining_hours"] = int(math.ceil(remaining_sec / 3600.0)) if remaining_sec > 0 else 0
    p["is_currently_valid"] = (
        p["status"] == "ACTIVE"
        and start_dt <= now_dt < exp_dt
        and (p["max_trips"] is None or int(p["trips_used"]) < int(p["max_trips"]))
    )
    p["trips_label"] = (
        f"{p['trips_used']} lượt (Không giới hạn)"
        if p["max_trips"] is None
        else f"{p['trips_used']} / {p['max_trips']} lượt"
    )
    return p


def expire_and_autorenew_passes_in_tx(conn: sqlite3.Connection, user_id: int | None = None) -> list[tuple[int, str, dict]]:
    """Mark expired passes as EXPIRED and execute optional auto-renewal if wallet balance suffices."""
    now_dt = utc_now()
    now_iso = utc_now_iso()
    events: list[tuple[int, str, dict]] = []

    if user_id is not None:
        expiring = rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM user_passes
                WHERE user_id = ? AND status = 'ACTIVE'
                  AND (expires_at <= ? OR (max_trips IS NOT NULL AND trips_used >= max_trips))
                """,
                (user_id, now_iso),
            ).fetchall()
        )
    else:
        expiring = rows_to_dicts(
            conn.execute(
                """
                SELECT * FROM user_passes
                WHERE status = 'ACTIVE'
                  AND (expires_at <= ? OR (max_trips IS NOT NULL AND trips_used >= max_trips))
                """,
                (now_iso,),
            ).fetchall()
        )

    for p in expiring:
        conn.execute(
            "UPDATE user_passes SET status = 'EXPIRED', updated_at = ? WHERE id = ? AND status = 'ACTIVE'",
            (now_iso, p["id"]),
        )
        if not int(p.get("auto_renew", 0)):
            continue

        uid = int(p["user_id"])
        # Only auto-renew if user has no other active/queued pass and sufficient wallet balance
        still_active = conn.execute(
            "SELECT 1 FROM user_passes WHERE user_id = ? AND status = 'ACTIVE' AND expires_at > ?",
            (uid, now_iso),
        ).fetchone()
        if still_active:
            continue

        user = conn.execute("SELECT status FROM users WHERE id = ?", (uid,)).fetchone()
        plan = conn.execute("SELECT * FROM pass_plans WHERE id = ? AND is_active = 1", (p["plan_id"],)).fetchone()
        wallet = conn.execute("SELECT * FROM wallets WHERE user_id = ?", (uid,)).fetchone()
        if not user or user["status"] != "ACTIVE" or not plan or not wallet or wallet["status"] != "ACTIVE":
            continue

        price = int(plan["price"])
        bal_before = int(wallet["balance"])
        if bal_before < price:
            continue

        ded = conn.execute(
            """
            UPDATE wallets
            SET balance = balance - ?, total_spent = total_spent + ?, updated_at = ?
            WHERE id = ? AND balance >= ?
            """,
            (price, price, now_iso, wallet["id"], price),
        )
        if ded.rowcount != 1:
            continue

        bal_after = bal_before - price
        w_tx_code = generate_tx_code("WLT")
        new_pass_code = generate_tx_code("PASS")
        new_exp_iso = (now_dt + timedelta(days=int(plan["duration_days"]))).strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        conn.execute(
            """
            INSERT INTO wallet_transactions (
                transaction_code, user_id, wallet_id, transaction_type,
                amount, balance_before, balance_after, reference_type, reference_id,
                description, created_at
            ) VALUES (?, ?, ?, 'PASS_PURCHASE', ?, ?, ?, 'USER_PASS', ?, ?, ?)
            """,
            (
                w_tx_code,
                uid,
                wallet["id"],
                price,
                bal_before,
                bal_after,
                new_pass_code,
                f"Tự động gia hạn {plan['name']} (-{format_vnd(price)})",
                now_iso,
            ),
        )
        conn.execute(
            """
            INSERT INTO user_passes (
                pass_code, user_id, card_id, plan_id, plan_code, plan_name,
                price_paid, duration_days, max_trips, trips_used, auto_renew,
                starts_at, expires_at, status, wallet_tx_code, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 1, ?, ?, 'ACTIVE', ?, ?, ?)
            """,
            (
                new_pass_code,
                uid,
                p["card_id"],
                plan["id"],
                plan["code"],
                plan["name"],
                price,
                int(plan["duration_days"]),
                plan["max_trips"],
                now_iso,
                new_exp_iso,
                w_tx_code,
                now_iso,
                now_iso,
            ),
        )
        conn.execute(
            """
            INSERT INTO notifications (user_id, type, title, message, metadata_json, is_read, created_at)
            VALUES (?, 'PASS_RENEWED', 'Tự động gia hạn Vé Tháng thành công', ?, '{}', 0, ?)
            """,
            (
                uid,
                f"{plan['name']} ({new_pass_code}) đã được gia hạn thêm {plan['duration_days']} ngày. Số dư ví còn {format_vnd(bal_after)}.",
                now_iso,
            ),
        )
        events.append((uid, "pass_updated", {"pass_code": new_pass_code, "auto_renewed": True}))

    return events


def get_active_pass_in_tx(conn: sqlite3.Connection, user_id: int) -> dict | None:
    """Return currently active & valid Monthly Pass for `user_id` inside a transaction (or None)."""
    expire_and_autorenew_passes_in_tx(conn, user_id=user_id)
    now_iso = utc_now_iso()
    row = row_to_dict(
        conn.execute(
            """
            SELECT * FROM user_passes
            WHERE user_id = ? AND status = 'ACTIVE'
              AND starts_at <= ? AND expires_at > ?
              AND (max_trips IS NULL OR trips_used < max_trips)
            ORDER BY starts_at ASC, id ASC LIMIT 1
            """,
            (user_id, now_iso, now_iso),
        ).fetchone()
    )
    return _enrich_pass_dict(row)


def get_active_pass_readonly(conn: sqlite3.Connection, user_id: int) -> dict | None:
    """Read-only check for currently valid Monthly Pass without mutating rows."""
    now_iso = utc_now_iso()
    row = row_to_dict(
        conn.execute(
            """
            SELECT * FROM user_passes
            WHERE user_id = ? AND status = 'ACTIVE'
              AND starts_at <= ? AND expires_at > ?
              AND (max_trips IS NULL OR trips_used < max_trips)
            ORDER BY starts_at ASC, id ASC LIMIT 1
            """,
            (user_id, now_iso, now_iso),
        ).fetchone()
    )
    return _enrich_pass_dict(row)


def list_pass_plans(include_inactive: bool = False, user_id: int | None = None, is_admin_view: bool = False) -> list[dict]:
    """List Monthly/Weekly Pass plans.

    For non-admin views, plans with `requires_priority = 1` (e.g. Student Discount Pass) are STRICTLY HIDDEN
    unless `user_id` has `priority_status == 'APPROVED'`.
    """
    with read_db() as conn:
        user_priority_approved = False
        if user_id is not None:
            u_row = conn.execute("SELECT priority_status FROM users WHERE id = ?", (int(user_id),)).fetchone()
            if u_row and ("priority_status" in u_row.keys()) and u_row["priority_status"] == "APPROVED":
                user_priority_approved = True

        conditions: list[str] = []
        if not include_inactive:
            conditions.append("is_active = 1")
        if not is_admin_view and not user_priority_approved:
            conditions.append("COALESCE(requires_priority, 0) = 0")

        sql = "SELECT * FROM pass_plans"
        if conditions:
            sql += " WHERE " + " AND ".join(conditions)
        sql += " ORDER BY price ASC, id ASC"
        rows = rows_to_dicts(conn.execute(sql).fetchall())

    for r in rows:
        r["requires_priority"] = int(r.get("requires_priority") or 0)
        r["price_formatted"] = format_vnd(r["price"])
        r["daily_cost_formatted"] = format_vnd(max(1, round(int(r["price"]) / max(1, int(r["duration_days"])))))
    return rows


def get_user_passes_overview(user_id: int) -> dict:
    with atomic_transaction() as conn:
        events = expire_and_autorenew_passes_in_tx(conn, user_id=user_id)
        active = get_active_pass_in_tx(conn, user_id=user_id)
        history = rows_to_dicts(
            conn.execute(
                "SELECT * FROM user_passes WHERE user_id = ? ORDER BY id DESC LIMIT 30",
                (user_id,),
            ).fetchall()
        )
    for uid, ev, data in events:
        realtime_broker.publish_to_user(uid, ev, data)
    now_dt = utc_now()
    for h in history:
        _enrich_pass_dict(h, now_dt=now_dt)
    return {
        "active_pass": active,
        "passes": history,
        "plans": list_pass_plans(include_inactive=False, user_id=user_id),
    }


def purchase_monthly_pass(
    user_id: int,
    plan_id: int,
    auto_renew: bool,
    qr_hmac_secret: str,
) -> dict:
    """Purchase or extend a Monthly Pass using User's Wallet balance inside an atomic transaction."""
    from backend.services.card_wallet_service import (
        create_notification_in_tx,
        emit_user_realtime_update,
        get_system_settings_in_tx,
        sync_card_status_in_tx,
    )
    from backend.services.qr_service import _issue_qr_token_in_tx

    post_events: list[tuple[int, str, dict]] = []

    with atomic_transaction() as conn:
        expire_and_autorenew_passes_in_tx(conn, user_id=user_id)
        user = conn.execute("SELECT id, status, full_name, priority_status FROM users WHERE id = ?", (user_id,)).fetchone()
        if not user:
            raise AppError("USER_NOT_FOUND", "Không tìm thấy người dùng.", 404)
        if user["status"] != "ACTIVE":
            raise AppError("SUSPENDED_ACCOUNT", "Tài khoản hiện không hoạt động, không thể đăng ký Vé Tháng.", 403)

        plan = row_to_dict(conn.execute("SELECT * FROM pass_plans WHERE id = ?", (int(plan_id),)).fetchone())
        if not plan or not int(plan["is_active"]):
            raise AppError("PLAN_NOT_FOUND", "Gói Vé Tháng không tồn tại hoặc đã ngừng cung cấp.", 404)

        if int(plan.get("requires_priority") or 0) == 1 and user["priority_status"] != "APPROVED":
            raise AppError(
                "PRIORITY_REQUIRED",
                "Gói vé ưu đãi này chỉ dành cho tài khoản đã đăng ký và được Admin phê duyệt Đối tượng Ưu tiên (HSSV/...).",
                403,
            )

        wallet = conn.execute("SELECT * FROM wallets WHERE user_id = ?", (user_id,)).fetchone()
        card = conn.execute("SELECT * FROM virtual_cards WHERE user_id = ?", (user_id,)).fetchone()
        if wallet["status"] != "ACTIVE":
            raise AppError("WALLET_LOCKED", "Ví đang bị khóa, không thể thanh toán.", 403)

        price = int(plan["price"])
        balance_before = int(wallet["balance"])
        if balance_before < price:
            raise AppError(
                "INSUFFICIENT_BALANCE",
                f"Số dư ví ({format_vnd(balance_before)}) không đủ để mua {plan['name']} ({format_vnd(price)}). Vui lòng nạp thêm tiền!",
                400,
                {
                    "balance": balance_before,
                    "required": price,
                    "shortfall": price - balance_before,
                    "shortfall_formatted": format_vnd(price - balance_before),
                },
            )

        now_dt = utc_now()
        now_iso = utc_now_iso()

        ded = conn.execute(
            """
            UPDATE wallets
            SET balance = balance - ?, total_spent = total_spent + ?, updated_at = ?
            WHERE id = ? AND user_id = ? AND balance >= ?
            """,
            (price, price, now_iso, wallet["id"], user_id, price),
        )
        if ded.rowcount != 1:
            raise AppError("INSUFFICIENT_BALANCE", "Số dư ví không đủ để thanh toán gói vé.", 400)

        balance_after = balance_before - price
        pass_code = generate_tx_code("PASS")
        w_tx_code = generate_tx_code("WLT")

        # Stack seamlessly after any existing unexpired ACTIVE pass so zero remaining days are lost!
        latest_row = conn.execute(
            """
            SELECT MAX(expires_at) AS max_exp
            FROM user_passes
            WHERE user_id = ? AND status = 'ACTIVE' AND expires_at > ?
            """,
            (user_id, now_iso),
        ).fetchone()

        if latest_row and latest_row["max_exp"]:
            starts_dt = parse_iso(latest_row["max_exp"])
            is_extension = True
        else:
            starts_dt = now_dt
            is_extension = False

        expires_dt = starts_dt + timedelta(days=int(plan["duration_days"]))
        starts_iso = starts_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        expires_iso = expires_dt.strftime("%Y-%m-%dT%H:%M:%S.%fZ")

        conn.execute(
            """
            INSERT INTO wallet_transactions (
                transaction_code, user_id, wallet_id, transaction_type,
                amount, balance_before, balance_after, reference_type, reference_id,
                description, created_at
            ) VALUES (?, ?, ?, 'PASS_PURCHASE', ?, ?, ?, 'USER_PASS', ?, ?, ?)
            """,
            (
                w_tx_code,
                user_id,
                wallet["id"],
                price,
                balance_before,
                balance_after,
                pass_code,
                f"Đăng ký {plan['name']} ({pass_code})",
                now_iso,
            ),
        )

        cur = conn.execute(
            """
            INSERT INTO user_passes (
                pass_code, user_id, card_id, plan_id, plan_code, plan_name,
                price_paid, duration_days, max_trips, trips_used, auto_renew,
                starts_at, expires_at, status, wallet_tx_code, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, 'ACTIVE', ?, ?, ?)
            """,
            (
                pass_code,
                user_id,
                card["id"],
                plan["id"],
                plan["code"],
                plan["name"],
                price,
                int(plan["duration_days"]),
                plan["max_trips"],
                1 if auto_renew else 0,
                starts_iso,
                expires_iso,
                w_tx_code,
                now_iso,
                now_iso,
            ),
        )
        new_pass_id = int(cur.lastrowid)

        # Ensure card status is ACTIVE (even if balance_after == 0 VND!) and issue Dynamic QR
        settings = get_system_settings_in_tx(conn)
        synced_card = sync_card_status_in_tx(conn, user_id)
        next_qr = None
        if synced_card["status"] == "ACTIVE":
            next_qr = _issue_qr_token_in_tx(
                conn=conn,
                user_id=user_id,
                card_id=int(synced_card["id"]),
                card_version=int(synced_card["card_version"]),
                ttl_seconds=int(settings["qr_ttl_seconds"]),
                qr_hmac_secret=qr_hmac_secret,
                ticket_mode="MONTHLY",
            )

        created_pass = row_to_dict(conn.execute("SELECT * FROM user_passes WHERE id = ?", (new_pass_id,)).fetchone())
        _enrich_pass_dict(created_pass, now_dt=now_dt)

        notif = create_notification_in_tx(
            conn,
            user_id=user_id,
            notif_type="PASS_PURCHASED",
            title="Kích hoạt Vé Tháng thành công!" if not is_extension else "Gia hạn cộng dồn Vé Tháng thành công!",
            message=(
                f"{plan['name']} ({pass_code}) có hiệu lực đến {expires_iso[:10]}. "
                f"Mỗi lượt quét QR qua cổng được miễn phí 0 VNĐ!"
            ),
            metadata={"pass_code": pass_code, "plan_name": plan["name"], "expires_at": expires_iso},
        )
        if notif:
            post_events.append((user_id, "notification", notif))
        post_events.append(
            (
                user_id,
                "pass_updated",
                {
                    "pass": created_pass,
                    "balance_after": balance_after,
                    "balance_formatted": format_vnd(balance_after),
                    "card_status": synced_card["status"],
                    "next_qr": next_qr,
                },
            )
        )

    for uid, ev, data in post_events:
        emit_user_realtime_update(uid, ev, data)
    realtime_broker.publish_to_admins(
        "pass_purchased",
        {"pass_code": pass_code, "user_id": user_id, "plan_name": plan["name"], "price_paid": price},
    )

    return {
        "pass": created_pass,
        "is_extension": is_extension,
        "balance_before": balance_before,
        "balance_after": balance_after,
        "balance_after_formatted": format_vnd(balance_after),
        "card_status": synced_card["status"],
        "next_qr": next_qr,
    }


def set_pass_auto_renew(user_id: int, pass_id: int, auto_renew: bool) -> dict:
    now_iso = utc_now_iso()
    with atomic_transaction() as conn:
        row = conn.execute(
            "SELECT * FROM user_passes WHERE id = ? AND user_id = ?",
            (pass_id, user_id),
        ).fetchone()
        if not row:
            raise AppError("PASS_NOT_FOUND", "Không tìm thấy thẻ vé tháng.", 404)
        conn.execute(
            "UPDATE user_passes SET auto_renew = ?, updated_at = ? WHERE id = ?",
            (1 if auto_renew else 0, now_iso, pass_id),
        )
        updated = row_to_dict(conn.execute("SELECT * FROM user_passes WHERE id = ?", (pass_id,)).fetchone())
    return _enrich_pass_dict(updated)


def admin_upsert_pass_plan(admin_id: int, payload: dict, ip_address: str = "") -> dict:
    """Create or update a Monthly/Weekly Pass package from Admin Dashboard."""
    plan_id = payload.get("id")
    code = "".join(ch for ch in str(payload.get("code") or "").upper() if ch.isalnum() or ch == "_")[:32]
    name = str(payload.get("name") or "").strip()
    description = str(payload.get("description") or "").strip()[:300]
    badge_text = str(payload.get("badge_text") or "UNLIMITED").strip()[:30]
    price = validate_positive_vnd_integer(payload.get("price"), "Giá gói vé")
    duration_days = int(payload.get("duration_days") or 30)
    raw_max_trips = payload.get("max_trips")
    max_trips = int(raw_max_trips) if raw_max_trips not in (None, "", 0, "0") else None
    is_active = 1 if payload.get("is_active", True) in (True, 1, "1", "true") else 0
    requires_priority = 1 if payload.get("requires_priority", False) in (True, 1, "1", "true") else 0

    if not code or len(name) < 3 or duration_days < 1 or duration_days > 365:
        raise AppError("INVALID_PLAN", "Mã gói, tên gói (>=3 ký tự) và thời hạn (1–365 ngày) không hợp lệ.", 400)

    now_iso = utc_now_iso()
    with atomic_transaction() as conn:
        if plan_id:
            conn.execute(
                """
                UPDATE pass_plans
                SET code = ?, name = ?, description = ?, price = ?, duration_days = ?,
                    max_trips = ?, badge_text = ?, is_active = ?, requires_priority = ?, updated_at = ?
                WHERE id = ?
                """,
                (code, name, description, price, duration_days, max_trips, badge_text, is_active, requires_priority, now_iso, int(plan_id)),
            )
            target_id = int(plan_id)
            action = "UPDATE_PASS_PLAN"
        else:
            cur = conn.execute(
                """
                INSERT INTO pass_plans (
                    code, name, description, price, duration_days, max_trips, badge_text, is_active, requires_priority, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (code, name, description, price, duration_days, max_trips, badge_text, is_active, requires_priority, now_iso, now_iso),
            )
            target_id = int(cur.lastrowid)
            action = "CREATE_PASS_PLAN"

        conn.execute(
            """
            INSERT INTO audit_logs (admin_id, action, target_type, target_id, metadata_json, ip_address, created_at)
            VALUES (?, ?, 'PASS_PLAN', ?, ?, ?, ?)
            """,
            (
                admin_id,
                action,
                str(target_id),
                json.dumps({"code": code, "name": name, "price": price, "duration_days": duration_days, "is_active": is_active}, ensure_ascii=False),
                ip_address[:64],
                now_iso,
            ),
        )
        saved = row_to_dict(conn.execute("SELECT * FROM pass_plans WHERE id = ?", (target_id,)).fetchone())
    saved["price_formatted"] = format_vnd(saved["price"])
    return saved


def admin_list_user_passes(status_filter: str = "", search: str = "") -> list[dict]:
    with atomic_transaction() as conn:
        expire_and_autorenew_passes_in_tx(conn)
        sql = """
            SELECT up.*, u.username, u.full_name AS user_name, vc.card_number
            FROM user_passes up
            JOIN users u ON u.id = up.user_id
            JOIN virtual_cards vc ON vc.id = up.card_id
            WHERE 1 = 1
        """
        params: list = []
        if status_filter:
            sql += " AND up.status = ?"
            params.append(status_filter.upper())
        if search:
            like = f"%{search.strip()}%"
            sql += " AND (u.username LIKE ? OR u.full_name LIKE ? OR up.pass_code LIKE ? OR vc.card_number LIKE ?)"
            params.extend([like, like, like, like])
        sql += " ORDER BY up.id DESC LIMIT 200"
        rows = rows_to_dicts(conn.execute(sql).fetchall())
    now_dt = utc_now()
    for r in rows:
        _enrich_pass_dict(r, now_dt=now_dt)
    return rows
