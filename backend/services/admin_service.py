"""Admin Dashboard KPIs, User management, Ledger filtering, Bank/System config, and Audit Logs."""

from __future__ import annotations

import json
from datetime import timedelta

from backend.database import atomic_transaction, read_db, row_to_dict, rows_to_dicts
from backend.services import AppError
from backend.services.card_wallet_service import (
    compute_card_status,
    create_notification_in_tx,
    emit_user_realtime_update,
    get_system_settings_in_tx,
    sync_card_status_in_tx,
)
from backend.services.qr_service import _issue_qr_token_in_tx
from backend.services.topup_service import expire_stale_topups_in_tx
from backend.utils.formatters import format_vnd, generate_tx_code, utc_now, utc_now_iso
from backend.utils.security import validate_positive_vnd_integer


def get_admin_dashboard_metrics(period: str = "day") -> dict:
    """Return all 8 required executive KPIs plus time-series chart data by day/week/month."""
    period = (period or "day").lower()
    now_dt = utc_now()

    with read_db() as conn:
        settings = get_system_settings_in_tx(conn)
        fare = int(settings["fare_amount"])

        total_users = conn.execute("SELECT COUNT(*) AS c FROM users WHERE role = 'USER'").fetchone()["c"]
        active_cards = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM users u
            JOIN wallets w ON w.user_id = u.id
            WHERE u.role = 'USER' AND u.status = 'ACTIVE' AND w.status = 'ACTIVE'
              AND (
                w.balance >= ?
                OR EXISTS (
                    SELECT 1 FROM user_passes up
                    WHERE up.user_id = u.id AND up.status = 'ACTIVE' AND up.starts_at <= ? AND up.expires_at > ?
                )
              )
            """,
            (fare, utc_now_iso(), utc_now_iso()),
        ).fetchone()["c"]
        active_passes_count = conn.execute(
            "SELECT COUNT(*) AS c FROM user_passes WHERE status = 'ACTIVE' AND expires_at > ?",
            (utc_now_iso(),),
        ).fetchone()["c"]
        auto_approved_topups = conn.execute(
            "SELECT COUNT(*) AS c FROM topup_transactions WHERE status = 'SUCCESS' AND payment_provider = 'AUTO_BANK_RECONCILE'"
        ).fetchone()["c"]
        suspended_cards = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM users u
            JOIN wallets w ON w.user_id = u.id
            WHERE u.role = 'USER' AND (u.status = 'SUSPENDED' OR w.status = 'LOCKED')
            """
        ).fetchone()["c"]
        insufficient_cards = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM users u
            JOIN wallets w ON w.user_id = u.id
            WHERE u.role = 'USER' AND u.status = 'ACTIVE' AND w.status = 'ACTIVE' AND w.balance < ?
            """,
            (fare,),
        ).fetchone()["c"]

        total_topup = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM topup_transactions WHERE status = 'SUCCESS'"
        ).fetchone()["s"]
        total_spent = conn.execute(
            "SELECT COALESCE(SUM(amount), 0) AS s FROM usage_transactions WHERE status = 'SUCCESS'"
        ).fetchone()["s"]

        success_scans = conn.execute(
            "SELECT COUNT(*) AS c FROM scan_logs WHERE status = 'SUCCESS'"
        ).fetchone()["c"]
        failed_scans = conn.execute(
            "SELECT COUNT(*) AS c FROM scan_logs WHERE status = 'FAILED'"
        ).fetchone()["c"]
        pending_topups = conn.execute(
            "SELECT COUNT(*) AS c FROM topup_transactions WHERE status IN ('PENDING', 'PROCESSING')"
        ).fetchone()["c"]
        open_support = conn.execute(
            "SELECT COUNT(*) AS c FROM support_requests WHERE status != 'RESOLVED'"
        ).fetchone()["c"]

        # Build chart buckets
        if period == "month":
            days_back = 180
            group_expr = "substr(created_at, 1, 7)"  # YYYY-MM
            buckets = []
            y, m = now_dt.year, now_dt.month
            for i in range(5, -1, -1):
                mm = m - i
                yy = y
                while mm <= 0:
                    mm += 12
                    yy -= 1
                buckets.append(f"{yy:04d}-{mm:02d}")
        elif period == "week":
            days_back = 56
            group_expr = "strftime('%Y-W%W', substr(created_at, 1, 19))"
            buckets = []
            for i in range(7, -1, -1):
                d = now_dt - timedelta(days=i * 7)
                buckets.append(d.strftime("%Y-W%W"))
        else:
            days_back = 7
            group_expr = "substr(created_at, 1, 10)"  # YYYY-MM-DD
            buckets = [(now_dt - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]

        cutoff_iso = (now_dt - timedelta(days=days_back + 7)).strftime("%Y-%m-%dT00:00:00Z")

        topup_series = {
            r["bucket"]: int(r["total"])
            for r in conn.execute(
                f"""
                SELECT {group_expr} AS bucket, COALESCE(SUM(amount), 0) AS total
                FROM topup_transactions
                WHERE status = 'SUCCESS' AND created_at >= ?
                GROUP BY bucket
                """,
                (cutoff_iso,),
            ).fetchall()
        }
        usage_series = {
            r["bucket"]: {"revenue": int(r["total"]), "count": int(r["cnt"])}
            for r in conn.execute(
                f"""
                SELECT {group_expr} AS bucket, COALESCE(SUM(amount), 0) AS total, COUNT(*) AS cnt
                FROM usage_transactions
                WHERE status = 'SUCCESS' AND created_at >= ?
                GROUP BY bucket
                """,
                (cutoff_iso,),
            ).fetchall()
        }

        chart = [
            {
                "label": b,
                "topup_amount": topup_series.get(b, 0),
                "usage_amount": usage_series.get(b, {}).get("revenue", 0),
                "scan_count": usage_series.get(b, {}).get("count", 0),
            }
            for b in buckets
        ]

        recent_scans = rows_to_dicts(
            conn.execute(
                """
                SELECT sl.*, u.full_name AS user_name, a.full_name AS admin_name
                FROM scan_logs sl
                LEFT JOIN users u ON u.id = sl.user_id
                LEFT JOIN users a ON a.id = sl.admin_id
                ORDER BY sl.id DESC LIMIT 15
                """
            ).fetchall()
        )
        for s in recent_scans:
            s["amount_formatted"] = f"-{format_vnd(s['amount'])}" if s["amount"] else "0 VNĐ"
            s["balance_before_formatted"] = (
                format_vnd(s["balance_before"]) if s["balance_before"] is not None else "—"
            )
            s["balance_after_formatted"] = (
                format_vnd(s["balance_after"]) if s["balance_after"] is not None else "—"
            )

    return {
        "kpis": {
            "total_users": int(total_users),
            "active_cards": int(active_cards),
            "suspended_cards": int(suspended_cards),
            "insufficient_cards": int(insufficient_cards),
            "total_topup": int(total_topup),
            "total_topup_formatted": format_vnd(total_topup),
            "total_spent": int(total_spent),
            "total_spent_formatted": format_vnd(total_spent),
            "success_scans": int(success_scans),
            "failed_scans": int(failed_scans),
            "pending_topups": int(pending_topups),
            "open_support": int(open_support),
            "active_passes": int(active_passes_count),
            "auto_approved_topups": int(auto_approved_topups),
        },
        "period": period,
        "chart": chart,
        "recent_scans": recent_scans,
    }


def list_users_for_admin(search: str = "", status_filter: str = "") -> list[dict]:
    search = (search or "").strip()
    status_filter = (status_filter or "").strip().upper()
    now_iso = utc_now_iso()

    with read_db() as conn:
        settings = get_system_settings_in_tx(conn)
        fare = int(settings["fare_amount"])

        sql = """
            SELECT u.id, u.username, u.full_name, u.email, u.phone, u.role, u.status, u.created_at,
                   w.balance, w.total_topup, w.total_spent, w.status AS wallet_status,
                   vc.id AS card_id, vc.card_number, vc.issued_at,
                   EXISTS(
                       SELECT 1 FROM qr_tokens qt
                       WHERE qt.card_id = vc.id AND qt.status = 'ACTIVE' AND qt.expires_at > ?
                   ) AS has_live_qr_token,
                   EXISTS(
                       SELECT 1 FROM user_passes up
                       WHERE up.user_id = u.id AND up.status = 'ACTIVE' AND up.starts_at <= ? AND up.expires_at > ?
                   ) AS has_active_pass
            FROM users u
            JOIN wallets w ON w.user_id = u.id
            JOIN virtual_cards vc ON vc.user_id = u.id
            WHERE u.role = 'USER'
        """
        params: list = [now_iso, now_iso, now_iso]

        if search:
            like = f"%{search}%"
            sql += " AND (u.username LIKE ? OR u.full_name LIKE ? OR u.email LIKE ? OR vc.card_number LIKE ?)"
            params.extend([like, like, like, like])

        if status_filter in ("ACTIVE", "SUSPENDED", "PENDING"):
            sql += " AND u.status = ?"
            params.append(status_filter)

        sql += " ORDER BY u.id DESC LIMIT 200"
        rows = rows_to_dicts(conn.execute(sql, params).fetchall())

    for r in rows:
        eff_status = "SUSPENDED" if r["wallet_status"] == "LOCKED" else r["status"]
        r["has_active_pass"] = bool(r["has_active_pass"])
        r["card_status"] = compute_card_status(
            eff_status, int(r["balance"]), fare, has_active_pass=r["has_active_pass"]
        )
        r["qr_enabled"] = r["card_status"] == "ACTIVE"
        r["balance_formatted"] = format_vnd(r["balance"])
        r["total_topup_formatted"] = format_vnd(r["total_topup"])
        r["total_spent_formatted"] = format_vnd(r["total_spent"])
    return rows


def get_admin_user_detail(target_user_id: int) -> dict:
    now_iso = utc_now_iso()
    with read_db() as conn:
        settings = get_system_settings_in_tx(conn)
        fare = int(settings["fare_amount"])

        user = row_to_dict(
            conn.execute(
                "SELECT id, username, full_name, email, phone, role, status, created_at, updated_at FROM users WHERE id = ?",
                (target_user_id,),
            ).fetchone()
        )
        if not user:
            raise AppError("USER_NOT_FOUND", "Không tìm thấy người dùng.", 404)

        wallet = row_to_dict(conn.execute("SELECT * FROM wallets WHERE user_id = ?", (target_user_id,)).fetchone())
        card = row_to_dict(conn.execute("SELECT * FROM virtual_cards WHERE user_id = ?", (target_user_id,)).fetchone())
        active_qr = row_to_dict(
            conn.execute(
                "SELECT id, expires_at, created_at FROM qr_tokens WHERE card_id = ? AND status = 'ACTIVE' AND expires_at > ?",
                (card["id"], now_iso),
            ).fetchone()
        )

        passes = rows_to_dicts(
            conn.execute(
                "SELECT * FROM user_passes WHERE user_id = ? ORDER BY id DESC LIMIT 15",
                (target_user_id,),
            ).fetchall()
        )
        has_active_pass = any(
            p["status"] == "ACTIVE" and p["starts_at"] <= now_iso < p["expires_at"] for p in passes
        )

        eff_status = "SUSPENDED" if wallet["status"] == "LOCKED" else user["status"]
        card["status"] = compute_card_status(
            eff_status, int(wallet["balance"]), fare, has_active_pass=has_active_pass
        )
        card["qr_enabled"] = card["status"] == "ACTIVE"
        card["has_active_pass"] = has_active_pass
        card["active_qr_token"] = active_qr

        wallet["balance_formatted"] = format_vnd(wallet["balance"])
        wallet["total_topup_formatted"] = format_vnd(wallet["total_topup"])
        wallet["total_spent_formatted"] = format_vnd(wallet["total_spent"])

        topups = rows_to_dicts(
            conn.execute(
                "SELECT * FROM topup_transactions WHERE user_id = ? ORDER BY id DESC LIMIT 30",
                (target_user_id,),
            ).fetchall()
        )
        for t in topups:
            t["amount_formatted"] = format_vnd(t["amount"])

        usages = rows_to_dicts(
            conn.execute(
                """
                SELECT ut.*, a.full_name AS admin_name
                FROM usage_transactions ut
                LEFT JOIN users a ON a.id = ut.admin_id
                WHERE ut.user_id = ?
                ORDER BY ut.id DESC LIMIT 30
                """,
                (target_user_id,),
            ).fetchall()
        )
        for u in usages:
            u["amount_formatted"] = (
                "0 VNĐ (Vé Tháng)" if u.get("payment_method") == "MONTHLY_PASS" else f"-{format_vnd(u['amount'])}"
            )
            u["balance_before_formatted"] = format_vnd(u["balance_before"])
            u["balance_after_formatted"] = format_vnd(u["balance_after"])

        support_tickets = rows_to_dicts(
            conn.execute(
                "SELECT * FROM support_requests WHERE user_id = ? ORDER BY id DESC LIMIT 20",
                (target_user_id,),
            ).fetchall()
        )

    return {
        "user": user,
        "wallet": wallet,
        "card": card,
        "passes": passes,
        "topups": topups,
        "usages": usages,
        "support_requests": support_tickets,
    }


def set_user_account_status(
    admin_id: int,
    target_user_id: int,
    suspend: bool,
    reason: str,
    qr_hmac_secret: str,
    ip_address: str = "",
) -> dict:
    """Suspend or Activate a User account, revoke/reactivate QR, and write immutable Audit Log."""
    new_status = "SUSPENDED" if suspend else "ACTIVE"
    action_name = "SUSPEND_USER" if suspend else "ACTIVATE_USER"
    reason = (reason or ("Khóa theo quyết định quản trị" if suspend else "Mở khóa tài khoản")).strip()[:250]
    now_iso = utc_now_iso()
    post_events: list[tuple[int, str, dict]] = []

    with atomic_transaction() as conn:
        target = conn.execute("SELECT id, username, role, status FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if not target:
            raise AppError("USER_NOT_FOUND", "Không tìm thấy người dùng.", 404)
        if target["role"] != "USER":
            raise AppError("FORBIDDEN_TARGET", "Không thể khóa tài khoản Quản trị viên tại mục này.", 403)

        conn.execute(
            "UPDATE users SET status = ?, updated_at = ? WHERE id = ?",
            (new_status, now_iso, target_user_id),
        )
        conn.execute(
            "UPDATE virtual_cards SET card_version = card_version + 1, updated_at = ? WHERE user_id = ?",
            (now_iso, target_user_id),
        )
        card = sync_card_status_in_tx(conn, target_user_id)
        settings = get_system_settings_in_tx(conn)

        next_qr = None
        if not suspend and card["status"] == "ACTIVE":
            next_qr = _issue_qr_token_in_tx(
                conn=conn,
                user_id=target_user_id,
                card_id=int(card["id"]),
                card_version=int(card["card_version"]),
                ttl_seconds=int(settings["qr_ttl_seconds"]),
                qr_hmac_secret=qr_hmac_secret,
            )

        conn.execute(
            """
            INSERT INTO audit_logs (admin_id, action, target_type, target_id, metadata_json, ip_address, created_at)
            VALUES (?, ?, 'USER', ?, ?, ?, ?)
            """,
            (
                admin_id,
                action_name,
                str(target_user_id),
                json.dumps(
                    {
                        "username": target["username"],
                        "previous_status": target["status"],
                        "new_status": new_status,
                        "reason": reason,
                    },
                    ensure_ascii=False,
                ),
                ip_address[:64],
                now_iso,
            ),
        )

        notif = create_notification_in_tx(
            conn,
            user_id=target_user_id,
            notif_type="ACCOUNT_SUSPENDED" if suspend else "ACCOUNT_ACTIVATED",
            title="Tài khoản đã bị tạm khóa" if suspend else "Tài khoản đã được mở khóa",
            message=(
                f"Tài khoản của bạn đã bị tạm khóa: {reason}"
                if suspend
                else f"Tài khoản và thẻ điện tử của bạn đã hoạt động trở lại ({reason})."
            ),
            metadata={"status": new_status, "reason": reason},
        )
        if notif:
            post_events.append((target_user_id, "notification", notif))

        post_events.append(
            (
                target_user_id,
                "account_status_changed",
                {
                    "user_status": new_status,
                    "card_status": card["status"],
                    "next_qr": next_qr,
                    "reason": reason,
                },
            )
        )

    for uid, ev, data in post_events:
        emit_user_realtime_update(uid, ev, data)

    return {"user_id": target_user_id, "status": new_status, "card_status": card["status"]}


def admin_adjust_wallet_balance(
    admin_id: int,
    target_user_id: int,
    direction: str,
    raw_amount,
    reason: str,
    qr_hmac_secret: str,
    ip_address: str = "",
) -> dict:
    """Create an explicit Wallet Ledger adjustment entry with mandatory reason & AuditLog (Section 14.2)."""
    direction = (direction or "").upper()
    if direction not in ("CREDIT", "DEBIT"):
        raise AppError("INVALID_DIRECTION", "Loại điều chỉnh phải là CREDIT hoặc DEBIT.", 400)
    reason = (reason or "").strip()
    if len(reason) < 5:
        raise AppError("REASON_REQUIRED", "Bắt buộc nhập lý do điều chỉnh số dư tối thiểu 5 ký tự.", 400)

    try:
        amount = validate_positive_vnd_integer(raw_amount, "Số tiền điều chỉnh")
    except ValueError as exc:
        raise AppError("INVALID_AMOUNT", str(exc), 400) from exc

    now_iso = utc_now_iso()
    post_events: list[tuple[int, str, dict]] = []

    with atomic_transaction() as conn:
        wallet = conn.execute("SELECT * FROM wallets WHERE user_id = ?", (target_user_id,)).fetchone()
        if not wallet:
            raise AppError("USER_NOT_FOUND", "Không tìm thấy ví người dùng.", 404)

        balance_before = int(wallet["balance"])
        if direction == "DEBIT" and balance_before < amount:
            raise AppError("INSUFFICIENT_BALANCE", "Không thể điều chỉnh giảm vượt quá số dư hiện tại (số dư không được âm).", 400)

        balance_after = balance_before + amount if direction == "CREDIT" else balance_before - amount
        tx_type = "ADJUSTMENT_CREDIT" if direction == "CREDIT" else "ADJUSTMENT_DEBIT"
        tx_code = generate_tx_code("ADJ")

        if direction == "CREDIT":
            conn.execute(
                "UPDATE wallets SET balance = balance + ?, updated_at = ? WHERE id = ?",
                (amount, now_iso, wallet["id"]),
            )
        else:
            cur = conn.execute(
                "UPDATE wallets SET balance = balance - ?, updated_at = ? WHERE id = ? AND balance >= ?",
                (amount, now_iso, wallet["id"], amount),
            )
            if cur.rowcount != 1:
                raise AppError("INSUFFICIENT_BALANCE", "Số dư không đủ.", 400)

        conn.execute(
            """
            INSERT INTO wallet_transactions (
                transaction_code, user_id, wallet_id, transaction_type, approval_mode,
                amount, balance_before, balance_after, reference_type, reference_id,
                admin_id, description, created_at
            ) VALUES (?, ?, ?, ?, 'MANUAL', ?, ?, ?, 'ADMIN_ADJUSTMENT', ?, ?, ?, ?)
            """,
            (
                tx_code,
                target_user_id,
                wallet["id"],
                tx_type,
                amount,
                balance_before,
                balance_after,
                tx_code,
                admin_id,
                f"Điều chỉnh số dư [THỦ CÔNG] bởi Admin: {reason}",
                now_iso,
            ),
        )

        settings = get_system_settings_in_tx(conn)
        card = sync_card_status_in_tx(conn, target_user_id)
        next_qr = None
        if card["status"] == "ACTIVE":
            next_qr = _issue_qr_token_in_tx(
                conn=conn,
                user_id=target_user_id,
                card_id=int(card["id"]),
                card_version=int(card["card_version"]),
                ttl_seconds=int(settings["qr_ttl_seconds"]),
                qr_hmac_secret=qr_hmac_secret,
            )

        conn.execute(
            """
            INSERT INTO audit_logs (admin_id, action, target_type, target_id, metadata_json, ip_address, created_at)
            VALUES (?, 'ADJUST_WALLET_BALANCE', 'WALLET', ?, ?, ?, ?)
            """,
            (
                admin_id,
                str(wallet["id"]),
                json.dumps(
                    {
                        "transaction_code": tx_code,
                        "user_id": target_user_id,
                        "direction": direction,
                        "amount": amount,
                        "balance_before": balance_before,
                        "balance_after": balance_after,
                        "reason": reason,
                    },
                    ensure_ascii=False,
                ),
                ip_address[:64],
                now_iso,
            ),
        )

        post_events.append(
            (
                target_user_id,
                "topup_confirmed",
                {
                    "transaction_code": tx_code,
                    "amount": amount,
                    "balance_before": balance_before,
                    "balance_after": balance_after,
                    "balance_formatted": format_vnd(balance_after),
                    "card_status": card["status"],
                    "next_qr": next_qr,
                },
            )
        )

    for uid, ev, data in post_events:
        emit_user_realtime_update(uid, ev, data)

    return {
        "transaction_code": tx_code,
        "balance_before": balance_before,
        "balance_after": balance_after,
        "balance_after_formatted": format_vnd(balance_after),
        "card_status": card["status"],
    }


def list_admin_transactions(
    tx_type: str = "",
    approval_mode: str = "",
    user_query: str = "",
    date_from: str = "",
    date_to: str = "",
    min_amount: str = "",
    max_amount: str = "",
) -> list[dict]:
    """Filterable Wallet Ledger with explicit TỰ ĐỘNG / THỦ CÔNG approval badges."""
    sql = """
        SELECT wt.*, u.username, u.full_name AS user_name, a.full_name AS admin_name
        FROM wallet_transactions wt
        JOIN users u ON u.id = wt.user_id
        LEFT JOIN users a ON a.id = wt.admin_id
        WHERE 1 = 1
    """
    params: list = []

    if tx_type:
        sql += " AND wt.transaction_type = ?"
        params.append(tx_type.upper())
    if approval_mode and approval_mode.upper() in ("AUTO", "MANUAL"):
        sql += " AND wt.approval_mode = ?"
        params.append(approval_mode.upper())
    if user_query:
        like = f"%{user_query.strip()}%"
        sql += " AND (u.username LIKE ? OR u.full_name LIKE ? OR u.email LIKE ? OR wt.transaction_code LIKE ?)"
        params.extend([like, like, like, like])
    if date_from:
        sql += " AND wt.created_at >= ?"
        params.append(f"{date_from.strip()}T00:00:00Z")
    if date_to:
        sql += " AND wt.created_at <= ?"
        params.append(f"{date_to.strip()}T23:59:59.999999Z")
    if min_amount and str(min_amount).isdigit():
        sql += " AND wt.amount >= ?"
        params.append(int(min_amount))
    if max_amount and str(max_amount).isdigit():
        sql += " AND wt.amount <= ?"
        params.append(int(max_amount))

    sql += " ORDER BY wt.id DESC LIMIT 250"

    with read_db() as conn:
        rows = rows_to_dicts(conn.execute(sql, params).fetchall())

    for r in rows:
        is_debit = r["transaction_type"] in ("USAGE", "PASS_PURCHASE", "ADJUSTMENT_DEBIT")
        r["signed_amount_formatted"] = f"{'-' if is_debit else '+'}{format_vnd(r['amount'])}"
        r["amount_formatted"] = format_vnd(r["amount"])
        r["balance_before_formatted"] = format_vnd(r["balance_before"])
        r["balance_after_formatted"] = format_vnd(r["balance_after"])
        mode = (r.get("approval_mode") or ("MANUAL" if r.get("admin_id") and r["transaction_type"] in ("TOPUP", "ADJUSTMENT_CREDIT", "ADJUSTMENT_DEBIT") else "AUTO")).upper()
        r["approval_mode"] = mode
        r["approval_badge_text"] = "THỦ CÔNG" if mode == "MANUAL" else "TỰ ĐỘNG"
        r["status"] = "SUCCESS"
    return rows


def list_admin_topups(
    status_filter: str = "",
    user_query: str = "",
    channel_filter: str = "",
    approval_mode_filter: str = "",
) -> list[dict]:
    from backend.services.topup_service import _enrich_topup_row

    with atomic_transaction() as conn:
        expire_stale_topups_in_tx(conn)
        sql = """
            SELECT tt.*, u.username, u.full_name AS user_name, u.email, a.full_name AS admin_name,
                   pp.name AS target_plan_name
            FROM topup_transactions tt
            JOIN users u ON u.id = tt.user_id
            LEFT JOIN users a ON a.id = tt.confirmed_by_admin_id
            LEFT JOIN pass_plans pp ON pp.id = tt.target_plan_id
            WHERE 1 = 1
        """
        params: list = []
        if status_filter:
            sql += " AND tt.status = ?"
            params.append(status_filter.upper())
        if channel_filter and channel_filter.upper() in ("AUTO", "MANUAL"):
            sql += " AND tt.topup_channel = ?"
            params.append(channel_filter.upper())
        if approval_mode_filter and approval_mode_filter.upper() in ("AUTO", "MANUAL"):
            sql += " AND tt.approval_mode = ?"
            params.append(approval_mode_filter.upper())
        if user_query:
            like = f"%{user_query.strip()}%"
            sql += " AND (u.username LIKE ? OR u.full_name LIKE ? OR tt.transaction_code LIKE ? OR tt.transfer_content LIKE ?)"
            params.extend([like, like, like, like])
        sql += " ORDER BY tt.id DESC LIMIT 200"
        rows = rows_to_dicts(conn.execute(sql, params).fetchall())

    return [_enrich_topup_row(r) for r in rows]


def get_bank_settings() -> dict:
    with read_db() as conn:
        row = row_to_dict(conn.execute("SELECT * FROM bank_settings WHERE id = 1").fetchone())
    return row or {}


def update_bank_settings(admin_id: int, admin_role: str, payload: dict, ip_address: str = "") -> dict:
    """Update receiving bank settings (Section 6: restricted to authorized Admin)."""
    if admin_role not in ("ADMIN", "SUPER_ADMIN"):
        raise AppError("FORBIDDEN", "Bạn không có quyền cập nhật thông tin ngân hàng nhận tiền.", 403)

    bank_name = str(payload.get("bank_name") or "").strip()
    bank_short_name = str(payload.get("bank_short_name") or "VCB").strip()[:20]
    bank_bin = "".join(ch for ch in str(payload.get("bank_bin") or "970436") if ch.isdigit()) or "970436"
    account_number = str(payload.get("account_number") or "").strip()
    account_holder = str(payload.get("account_holder") or "").strip().upper()
    bank_qr_url = str(payload.get("bank_qr_url") or "").strip()
    transfer_prefix = "".join(ch for ch in str(payload.get("transfer_prefix") or "VP").upper() if ch.isalnum())[:6] or "VP"
    payment_instructions = str(payload.get("payment_instructions") or "").strip()
    is_active = 1 if payload.get("is_active", True) in (True, 1, "1", "true") else 0

    if not bank_name or not account_number or not account_holder:
        raise AppError("INVALID_BANK_SETTINGS", "Tên ngân hàng, số tài khoản và chủ tài khoản không được để trống.", 400)

    now_iso = utc_now_iso()
    with atomic_transaction() as conn:
        conn.execute(
            """
            UPDATE bank_settings
            SET bank_name = ?, bank_short_name = ?, bank_bin = ?, account_number = ?,
                account_holder = ?, bank_qr_url = ?, transfer_prefix = ?,
                payment_instructions = ?, is_active = ?, updated_by_admin_id = ?, updated_at = ?
            WHERE id = 1
            """,
            (
                bank_name,
                bank_short_name,
                bank_bin,
                account_number,
                account_holder,
                bank_qr_url,
                transfer_prefix,
                payment_instructions,
                is_active,
                admin_id,
                now_iso,
            ),
        )
        conn.execute(
            """
            INSERT INTO audit_logs (admin_id, action, target_type, target_id, metadata_json, ip_address, created_at)
            VALUES (?, 'UPDATE_BANK_SETTINGS', 'BANK_SETTINGS', '1', ?, ?, ?)
            """,
            (
                admin_id,
                json.dumps(
                    {
                        "bank_name": bank_name,
                        "bank_short_name": bank_short_name,
                        "account_number": account_number,
                        "account_holder": account_holder,
                        "is_active": is_active,
                    },
                    ensure_ascii=False,
                ),
                ip_address[:64],
                now_iso,
            ),
        )
    return get_bank_settings()


def get_system_configuration() -> dict:
    with read_db() as conn:
        settings = get_system_settings_in_tx(conn)
        history = rows_to_dicts(
            conn.execute(
                """
                SELECT fh.*, u.full_name AS admin_name
                FROM fare_history fh
                LEFT JOIN users u ON u.id = fh.changed_by_admin_id
                ORDER BY fh.id DESC LIMIT 20
                """
            ).fetchall()
        )
    for h in history:
        h["old_fare_formatted"] = format_vnd(h["old_fare"])
        h["new_fare_formatted"] = format_vnd(h["new_fare"])
    return {"settings": settings, "fare_history": history}


def update_system_configuration(admin_id: int, payload: dict, ip_address: str = "") -> dict:
    """Update fare price (recording immutable Fare History), QR TTL, and notification settings."""
    now_iso = utc_now_iso()
    with atomic_transaction() as conn:
        current = get_system_settings_in_tx(conn)
        old_fare = int(current["fare_amount"])

        new_fare = (
            validate_positive_vnd_integer(payload["fare_amount"], "Giá lượt sử dụng")
            if "fare_amount" in payload
            else old_fare
        )
        qr_ttl = int(payload.get("qr_ttl_seconds", current["qr_ttl_seconds"]))
        low_bal = int(payload.get("low_balance_threshold", current["low_balance_threshold"]))
        notif_enabled = 1 if payload.get("notifications_enabled", current["notifications_enabled"]) in (True, 1, "1", "true") else 0

        if not (15 <= qr_ttl <= 600):
            raise AppError("INVALID_TTL", "Thời hạn token QR phải từ 15 đến 600 giây.", 400)
        if low_bal < 0:
            raise AppError("INVALID_THRESHOLD", "Ngưỡng cảnh báo số dư thấp không hợp lệ.", 400)

        fare_reason = str(payload.get("fare_change_reason") or "Cập nhật cấu hình giá vé").strip()[:200]

        conn.execute(
            """
            UPDATE system_settings
            SET fare_amount = ?, qr_ttl_seconds = ?, low_balance_threshold = ?,
                notifications_enabled = ?, updated_by_admin_id = ?, updated_at = ?
            WHERE id = 1
            """,
            (new_fare, qr_ttl, low_bal, notif_enabled, admin_id, now_iso),
        )

        if new_fare != old_fare:
            conn.execute(
                """
                INSERT INTO fare_history (old_fare, new_fare, reason, changed_by_admin_id, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (old_fare, new_fare, fare_reason, admin_id, now_iso),
            )
            # Re-evaluate active/insufficient card statuses against new fare
            conn.execute(
                """
                UPDATE virtual_cards
                SET status = CASE
                    WHEN (SELECT balance FROM wallets w WHERE w.user_id = virtual_cards.user_id) >= ? THEN 'ACTIVE'
                    ELSE 'INSUFFICIENT_BALANCE'
                END,
                updated_at = ?
                WHERE status IN ('ACTIVE', 'INSUFFICIENT_BALANCE')
                  AND (SELECT status FROM users u WHERE u.id = virtual_cards.user_id) = 'ACTIVE'
                """,
                (new_fare, now_iso),
            )

        conn.execute(
            """
            INSERT INTO audit_logs (admin_id, action, target_type, target_id, metadata_json, ip_address, created_at)
            VALUES (?, 'UPDATE_SYSTEM_SETTINGS', 'SYSTEM_SETTINGS', '1', ?, ?, ?)
            """,
            (
                admin_id,
                json.dumps(
                    {
                        "old_fare": old_fare,
                        "new_fare": new_fare,
                        "qr_ttl_seconds": qr_ttl,
                        "low_balance_threshold": low_bal,
                        "notifications_enabled": notif_enabled,
                    },
                    ensure_ascii=False,
                ),
                ip_address[:64],
                now_iso,
            ),
        )

    return get_system_configuration()


def list_audit_logs(limit: int = 100) -> list[dict]:
    with read_db() as conn:
        rows = rows_to_dicts(
            conn.execute(
                """
                SELECT al.*, u.username AS admin_username, u.full_name AS admin_name
                FROM audit_logs al
                JOIN users u ON u.id = al.admin_id
                ORDER BY al.id DESC LIMIT ?
                """,
                (min(max(1, limit), 500),),
            ).fetchall()
        )
    return rows


def list_support_requests(status_filter: str = "") -> list[dict]:
    with read_db() as conn:
        sql = """
            SELECT sr.*, u.username, u.full_name AS user_name, u.email, vc.card_number
            FROM support_requests sr
            JOIN users u ON u.id = sr.user_id
            LEFT JOIN virtual_cards vc ON vc.user_id = sr.user_id
            WHERE 1 = 1
        """
        params: list = []
        if status_filter:
            sql += " AND sr.status = ?"
            params.append(status_filter.upper())
        sql += " ORDER BY sr.id DESC LIMIT 100"
        return rows_to_dicts(conn.execute(sql, params).fetchall())


def resolve_support_request(admin_id: int, request_id: int, status: str, admin_note: str, ip_address: str = "") -> dict:
    status = (status or "RESOLVED").upper()
    if status not in ("OPEN", "IN_PROGRESS", "RESOLVED"):
        raise AppError("INVALID_STATUS", "Trạng thái yêu cầu hỗ trợ không hợp lệ.", 400)
    now_iso = utc_now_iso()
    with atomic_transaction() as conn:
        sr = conn.execute("SELECT * FROM support_requests WHERE id = ?", (request_id,)).fetchone()
        if not sr:
            raise AppError("NOT_FOUND", "Không tìm thấy yêu cầu hỗ trợ.", 404)
        conn.execute(
            """
            UPDATE support_requests
            SET status = ?, admin_note = ?, resolved_by_admin_id = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, (admin_note or "")[:400], admin_id, now_iso, request_id),
        )
        conn.execute(
            """
            INSERT INTO audit_logs (admin_id, action, target_type, target_id, metadata_json, ip_address, created_at)
            VALUES (?, 'UPDATE_SUPPORT_REQUEST', 'SUPPORT_REQUEST', ?, ?, ?, ?)
            """,
            (
                admin_id,
                str(request_id),
                json.dumps({"status": status, "admin_note": admin_note}, ensure_ascii=False),
                ip_address[:64],
                now_iso,
            ),
        )
        notif = create_notification_in_tx(
            conn,
            user_id=int(sr["user_id"]),
            notif_type="SUPPORT_UPDATE",
            title=f"Phản hồi yêu cầu hỗ trợ #{request_id}",
            message=f"Trạng thái: {status}. Phản hồi: {admin_note or 'Đã ghi nhận xử lý.'}",
            metadata={"support_id": request_id, "status": status},
        )
    if notif:
        emit_user_realtime_update(int(sr["user_id"]), "notification", notif)
    return {"id": request_id, "status": status}
