"""Automated Bank Account Statement Reconciliation Engine (runs in parallel with Manual Admin Approval).

Capabilities:
1. Polls real external Bank Transaction APIs (SePay / Casso / Custom JSON API).
2. Imports & reconciles Bank Statement files/feeds (CSV or JSON).
3. Matches receiving bank account number, unique transfer reference (`VP...`), and exact VND amount.
4. Auto-approves matching `PENDING`/`PROCESSING` (and grace-window `EXPIRED`) top-up orders atomically.
5. Safely coexists with Manual Admin Approval (`ALREADY_SETTLED` protection prevents double crediting).
6. Flags `AMOUNT_MISMATCH`, `ACCOUNT_MISMATCH`, and `UNMATCHED` entries for Admin manual review.
"""

from __future__ import annotations

import csv
import io
import json
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta

from backend.database import atomic_transaction, read_db, row_to_dict, rows_to_dicts
from backend.services import AppError
from backend.services.card_wallet_service import emit_user_realtime_update
from backend.services.topup_service import _credit_topup_in_tx, expire_stale_topups_in_tx
from backend.utils.formatters import format_vnd, parse_iso, utc_now, utc_now_iso
from backend.utils.realtime import realtime_broker

_WORKER_STARTED = False
_WORKER_LOCK = threading.Lock()


def _clean_digits_alnum(val: str) -> str:
    return "".join(ch for ch in (val or "").upper() if ch.isalnum())


def _mask_token(token: str) -> str:
    if not token:
        return ""
    if len(token) <= 6:
        return "••••••"
    return "••••••••" + token[-4:]


def get_bank_sync_settings(include_raw_secret: bool = False) -> dict:
    with read_db() as conn:
        row = row_to_dict(conn.execute("SELECT * FROM bank_sync_settings WHERE id = 1").fetchone()) or {}
    if not include_raw_secret:
        raw_tok = row.pop("api_token", "")
        row["has_api_token"] = bool(raw_tok)
        row["api_token_masked"] = _mask_token(raw_tok)
    return row


def update_bank_sync_settings(admin_id: int, payload: dict, ip_address: str = "") -> dict:
    provider = str(payload.get("provider") or "SEPAY_API").upper()
    if provider not in ("SEPAY_API", "CASSO_API", "CUSTOM_JSON_API", "MANUAL_IMPORT_ONLY"):
        raise AppError("INVALID_PROVIDER", "Nhà cung cấp đồng bộ ngân hàng không hợp lệ.", 400)

    auto_reconcile = 1 if payload.get("auto_reconcile_enabled", True) in (True, 1, "1", "true") else 0
    bg_poll = 1 if payload.get("background_poll_enabled", False) in (True, 1, "1", "true") else 0
    api_url = str(payload.get("api_url") or "").strip()
    poll_sec = int(payload.get("poll_interval_seconds") or 20)
    strict_acc = 1 if payload.get("strict_account_match", True) in (True, 1, "1", "true") else 0
    grace_min = int(payload.get("allow_expired_grace_minutes") if payload.get("allow_expired_grace_minutes") is not None else 120)

    if not (10 <= poll_sec <= 3600):
        raise AppError("INVALID_INTERVAL", "Chu kỳ đồng bộ tự động phải từ 10 đến 3600 giây.", 400)
    if grace_min < 0 or grace_min > 10080:
        raise AppError("INVALID_GRACE", "Thời gian ân hạn đối soát đơn trễ không hợp lệ.", 400)

    now_iso = utc_now_iso()
    with atomic_transaction() as conn:
        existing = conn.execute("SELECT api_token FROM bank_sync_settings WHERE id = 1").fetchone()
        current_token = existing["api_token"] if existing else ""
        # Only overwrite token if admin explicitly supplied a new non-masked token
        incoming_tok = payload.get("api_token")
        if incoming_tok is not None and "••••" not in str(incoming_tok):
            new_token = str(incoming_tok).strip()
        else:
            new_token = current_token

        conn.execute(
            """
            UPDATE bank_sync_settings
            SET auto_reconcile_enabled = ?,
                background_poll_enabled = ?,
                provider = ?,
                api_url = ?,
                api_token = ?,
                poll_interval_seconds = ?,
                strict_account_match = ?,
                allow_expired_grace_minutes = ?,
                updated_by_admin_id = ?,
                updated_at = ?
            WHERE id = 1
            """,
            (
                auto_reconcile,
                bg_poll,
                provider,
                api_url,
                new_token,
                poll_sec,
                strict_acc,
                grace_min,
                admin_id,
                now_iso,
            ),
        )
        conn.execute(
            """
            INSERT INTO audit_logs (admin_id, action, target_type, target_id, metadata_json, ip_address, created_at)
            VALUES (?, 'UPDATE_BANK_SYNC_SETTINGS', 'BANK_SYNC_SETTINGS', '1', ?, ?, ?)
            """,
            (
                admin_id,
                json.dumps(
                    {
                        "provider": provider,
                        "auto_reconcile_enabled": auto_reconcile,
                        "background_poll_enabled": bg_poll,
                        "api_url": api_url,
                        "has_api_token": bool(new_token),
                        "poll_interval_seconds": poll_sec,
                        "strict_account_match": strict_acc,
                    },
                    ensure_ascii=False,
                ),
                ip_address[:64],
                now_iso,
            ),
        )
    return get_bank_sync_settings(include_raw_secret=False)


def _parse_int_amount(raw_val) -> int:
    if raw_val is None or isinstance(raw_val, bool):
        return 0
    if isinstance(raw_val, int):
        return raw_val
    if isinstance(raw_val, float):
        return int(round(raw_val))
    s = str(raw_val).strip()
    # Handle formatted strings like "100,000" or "100.000" or "+100000 VND"
    s_clean = re.sub(r"[^\d-]", "", s)
    if not s_clean or s_clean == "-":
        return 0
    return int(s_clean)


def reconcile_bank_entries(
    normalized_entries: list[dict],
    source: str,
    qr_hmac_secret: str,
    db_path=None,
) -> dict:
    """Match incoming bank account credit entries against Top-up Orders and auto-approve valid matches.

    Runs safely in parallel with Manual Admin Approval:
    - Already manually approved orders are marked `ALREADY_SETTLED` (never double-credited).
    - Exact account + transfer_content + amount matches are atomically auto-approved (`MATCHED_AUTO_APPROVED`).
    - Discrepancies (`AMOUNT_MISMATCH`, `ACCOUNT_MISMATCH`, `UNMATCHED`) are persisted for Admin review.
    """
    stats = {
        "total_received": len(normalized_entries),
        "ingested": 0,
        "skipped_duplicates": 0,
        "skipped_non_credit": 0,
        "auto_approved": 0,
        "already_settled": 0,
        "amount_mismatch": 0,
        "account_mismatch": 0,
        "unmatched": 0,
        "approved_orders": [],
    }

    post_user_events: list[tuple[int, str, dict]] = []
    post_admin_events: list[dict] = []

    with atomic_transaction(db_path) as conn:
        expire_stale_topups_in_tx(conn)
        sync_cfg = row_to_dict(conn.execute("SELECT * FROM bank_sync_settings WHERE id = 1").fetchone()) or {}
        bank_cfg = row_to_dict(conn.execute("SELECT * FROM bank_settings WHERE id = 1").fetchone()) or {}

        auto_enabled = bool(int(sync_cfg.get("auto_reconcile_enabled", 1)))
        strict_acc = bool(int(sync_cfg.get("strict_account_match", 1)))
        grace_minutes = int(sync_cfg.get("allow_expired_grace_minutes", 120))
        expected_acc = _clean_digits_alnum(bank_cfg.get("account_number") or "")

        now_dt = utc_now()
        now_iso = utc_now_iso()

        # Preload candidate transfer_content codes for fast substring matching inside messy bank descriptions
        candidate_orders = rows_to_dicts(
            conn.execute(
                """
                SELECT id, transfer_content FROM topup_transactions
                ORDER BY id DESC LIMIT 500
                """
            ).fetchall()
        )

        for item in normalized_entries:
            bank_tx_id = str(item.get("bank_tx_id") or "").strip()
            amount_in = _parse_int_amount(item.get("amount_in"))
            description = str(item.get("description") or "").strip()
            acc_num = str(item.get("account_number") or "").strip()
            tx_time = str(item.get("tx_time") or now_iso).strip() or now_iso
            raw_json = json.dumps(item.get("raw") or item, ensure_ascii=False)

            if not bank_tx_id:
                continue
            if amount_in <= 0:
                stats["skipped_non_credit"] += 1
                continue

            # 1. Deduplicate by bank_tx_id in bank_statement_entries
            dup = conn.execute(
                "SELECT id FROM bank_statement_entries WHERE bank_tx_id = ?",
                (bank_tx_id,),
            ).fetchone()
            if dup:
                stats["skipped_duplicates"] += 1
                continue

            # Also check if an Admin already manually entered this exact bank_tx_id as bank_reference!
            manual_settled = conn.execute(
                "SELECT id, transaction_code, transfer_content FROM topup_transactions WHERE bank_reference = ?",
                (bank_tx_id,),
            ).fetchone()
            if manual_settled:
                conn.execute(
                    """
                    INSERT INTO bank_statement_entries (
                        bank_tx_id, source, account_number, amount_in, description,
                        extracted_code, tx_time, match_status, matched_topup_id,
                        reconciled_note, raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ALREADY_SETTLED', ?, ?, ?, ?)
                    """,
                    (
                        bank_tx_id,
                        source,
                        acc_num,
                        amount_in,
                        description,
                        manual_settled["transfer_content"],
                        tx_time,
                        manual_settled["id"],
                        f"Mã tham chiếu đã được Admin duyệt trước đó cho đơn {manual_settled['transaction_code']}.",
                        raw_json,
                        now_iso,
                    ),
                )
                stats["ingested"] += 1
                stats["already_settled"] += 1
                continue

            # 2. Verify receiving account number if provided & strict_account_match is on
            incoming_acc_clean = _clean_digits_alnum(acc_num)
            if strict_acc and expected_acc and incoming_acc_clean and incoming_acc_clean != expected_acc:
                conn.execute(
                    """
                    INSERT INTO bank_statement_entries (
                        bank_tx_id, source, account_number, amount_in, description,
                        extracted_code, tx_time, match_status, matched_topup_id,
                        reconciled_note, raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, '', ?, 'ACCOUNT_MISMATCH', NULL, ?, ?, ?)
                    """,
                    (
                        bank_tx_id,
                        source,
                        acc_num,
                        amount_in,
                        description,
                        tx_time,
                        f"Số tài khoản nhận ({acc_num}) không khớp tài khoản cấu hình ({bank_cfg.get('account_number')}).",
                        raw_json,
                        now_iso,
                    ),
                )
                stats["ingested"] += 1
                stats["account_mismatch"] += 1
                continue

            # 3. Extract transfer_content token & locate Top-up Order
            upper_desc = description.upper()
            upper_compact = _clean_digits_alnum(description)
            matched_row = None
            extracted_code = ""

            for cand in candidate_orders:
                code = cand["transfer_content"].upper()
                if code and (code in upper_desc or code in upper_compact):
                    matched_row = conn.execute(
                        "SELECT * FROM topup_transactions WHERE id = ?",
                        (cand["id"],),
                    ).fetchone()
                    extracted_code = code
                    break

            if not matched_row:
                for tok in re.findall(r"[A-Z0-9]{8,24}", upper_desc):
                    row_try = conn.execute(
                        "SELECT * FROM topup_transactions WHERE transfer_content = ?",
                        (tok,),
                    ).fetchone()
                    if row_try:
                        matched_row = row_try
                        extracted_code = tok
                        break

            if not matched_row:
                conn.execute(
                    """
                    INSERT INTO bank_statement_entries (
                        bank_tx_id, source, account_number, amount_in, description,
                        extracted_code, tx_time, match_status, matched_topup_id,
                        reconciled_note, raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, '', ?, 'UNMATCHED', NULL, ?, ?, ?)
                    """,
                    (
                        bank_tx_id,
                        source,
                        acc_num,
                        amount_in,
                        description,
                        tx_time,
                        "Không tìm thấy đơn nạp khớp mã nội dung chuyển khoản — chờ Admin kiểm tra thủ công.",
                        raw_json,
                        now_iso,
                    ),
                )
                stats["ingested"] += 1
                stats["unmatched"] += 1
                continue

            # 4. If order was already settled (e.g., by Manual Admin Approval or Webhook)
            if matched_row["status"] == "SUCCESS":
                conn.execute(
                    """
                    INSERT INTO bank_statement_entries (
                        bank_tx_id, source, account_number, amount_in, description,
                        extracted_code, tx_time, match_status, matched_topup_id,
                        reconciled_note, raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'ALREADY_SETTLED', ?, ?, ?, ?)
                    """,
                    (
                        bank_tx_id,
                        source,
                        acc_num,
                        amount_in,
                        description,
                        extracted_code,
                        tx_time,
                        matched_row["id"],
                        f"Đơn {matched_row['transaction_code']} đã hoàn tất trước đó — bỏ qua cộng tiền lặp.",
                        raw_json,
                        now_iso,
                    ),
                )
                stats["ingested"] += 1
                stats["already_settled"] += 1
                continue

            # 5. Check exact amount match
            expected_amt = int(matched_row["amount"])
            if amount_in != expected_amt:
                conn.execute(
                    """
                    INSERT INTO bank_statement_entries (
                        bank_tx_id, source, account_number, amount_in, description,
                        extracted_code, tx_time, match_status, matched_topup_id,
                        reconciled_note, raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'AMOUNT_MISMATCH', ?, ?, ?, ?)
                    """,
                    (
                        bank_tx_id,
                        source,
                        acc_num,
                        amount_in,
                        description,
                        extracted_code,
                        tx_time,
                        matched_row["id"],
                        f"Lệch số tiền: Ngân hàng nhận {format_vnd(amount_in)} != Đơn tạo {format_vnd(expected_amt)}. Cần Admin xử lý thủ công.",
                        raw_json,
                        now_iso,
                    ),
                )
                stats["ingested"] += 1
                stats["amount_mismatch"] += 1
                continue

            # 6. Verify order eligibility & auto_reconcile toggle
            order_status = matched_row["status"]
            eligible_for_auto = order_status in ("PENDING", "PROCESSING")
            if order_status == "EXPIRED" and grace_minutes > 0:
                exp_dt = parse_iso(matched_row["expires_at"])
                if now_dt <= exp_dt + timedelta(minutes=grace_minutes):
                    eligible_for_auto = True
                    # Re-open EXPIRED -> PROCESSING inside tx so _credit_topup_in_tx settles it cleanly
                    conn.execute(
                        "UPDATE topup_transactions SET status = 'PROCESSING', updated_at = ? WHERE id = ? AND status = 'EXPIRED'",
                        (now_iso, matched_row["id"]),
                    )
                    matched_row = conn.execute("SELECT * FROM topup_transactions WHERE id = ?", (matched_row["id"],)).fetchone()

            if not auto_enabled or not eligible_for_auto:
                reason_note = (
                    "Chế độ tự động duyệt đang tắt — chờ Admin duyệt thủ công."
                    if not auto_enabled
                    else f"Đơn đang ở trạng thái {order_status} ngoài khung tự động duyệt — chờ Admin kiểm tra."
                )
                conn.execute(
                    """
                    INSERT INTO bank_statement_entries (
                        bank_tx_id, source, account_number, amount_in, description,
                        extracted_code, tx_time, match_status, matched_topup_id,
                        reconciled_note, raw_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, 'UNMATCHED', ?, ?, ?, ?)
                    """,
                    (
                        bank_tx_id,
                        source,
                        acc_num,
                        amount_in,
                        description,
                        extracted_code,
                        tx_time,
                        matched_row["id"],
                        reason_note,
                        raw_json,
                        now_iso,
                    ),
                )
                stats["ingested"] += 1
                stats["unmatched"] += 1
                continue

            # 7. Exact match! Auto-approve & credit User Wallet atomically!
            summary, u_events = _credit_topup_in_tx(
                conn=conn,
                topup_row=matched_row,
                provider="AUTO_BANK_RECONCILE",
                bank_reference=bank_tx_id,
                qr_hmac_secret=qr_hmac_secret,
                admin_id=None,
                note=f"Tự động duyệt qua đối chiếu tài khoản ngân hàng ({source})",
            )
            conn.execute(
                """
                INSERT INTO bank_statement_entries (
                    bank_tx_id, source, account_number, amount_in, description,
                    extracted_code, tx_time, match_status, matched_topup_id,
                    reconciled_note, raw_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'MATCHED_AUTO_APPROVED', ?, ?, ?, ?)
                """,
                (
                    bank_tx_id,
                    source,
                    acc_num,
                    amount_in,
                    description,
                    extracted_code,
                    tx_time,
                    matched_row["id"],
                    f"Tự động khớp & cộng +{format_vnd(amount_in)} cho đơn {matched_row['transaction_code']}.",
                    raw_json,
                    now_iso,
                ),
            )
            stats["ingested"] += 1
            stats["auto_approved"] += 1
            stats["approved_orders"].append(summary)
            post_user_events.extend(u_events)
            post_admin_events.append(summary)

    for uid, ev, data in post_user_events:
        emit_user_realtime_update(uid, ev, data)
    for adm_summary in post_admin_events:
        realtime_broker.publish_to_admins("topup_confirmed", adm_summary)

    return stats


def _normalize_remote_api_payload(provider: str, payload) -> list[dict]:
    """Normalize SePay, Casso, or Custom JSON Bank Statement API responses into standard entries."""
    out: list[dict] = []
    if provider == "SEPAY_API":
        records = payload.get("transactions", []) if isinstance(payload, dict) else []
        for r in records:
            out.append(
                {
                    "bank_tx_id": str(r.get("reference_number") or r.get("id") or "").strip(),
                    "account_number": str(r.get("account_number") or ""),
                    "amount_in": _parse_int_amount(r.get("amount_in") or r.get("transferAmount") or 0),
                    "description": str(r.get("transaction_content") or r.get("content") or ""),
                    "tx_time": str(r.get("transaction_date") or utc_now_iso()),
                    "raw": r,
                }
            )
    elif provider == "CASSO_API":
        data_obj = payload.get("data", {}) if isinstance(payload, dict) else {}
        records = data_obj.get("records", []) if isinstance(data_obj, dict) else []
        for r in records:
            amt = _parse_int_amount(r.get("amount") or 0)
            if amt > 0:
                out.append(
                    {
                        "bank_tx_id": str(r.get("tid") or r.get("id") or "").strip(),
                        "account_number": str(r.get("subAccId") or r.get("account_number") or ""),
                        "amount_in": amt,
                        "description": str(r.get("description") or ""),
                        "tx_time": str(r.get("when") or utc_now_iso()),
                        "raw": r,
                    }
                )
    else:
        records = (
            payload
            if isinstance(payload, list)
            else payload.get("transactions") or payload.get("data") or []
            if isinstance(payload, dict)
            else []
        )
        for r in records:
            if not isinstance(r, dict):
                continue
            out.append(
                {
                    "bank_tx_id": str(r.get("bank_tx_id") or r.get("reference") or r.get("id") or "").strip(),
                    "account_number": str(r.get("account_number") or r.get("account") or ""),
                    "amount_in": _parse_int_amount(r.get("amount_in") or r.get("amount") or r.get("credit") or 0),
                    "description": str(r.get("description") or r.get("content") or r.get("memo") or ""),
                    "tx_time": str(r.get("tx_time") or r.get("created_at") or utc_now_iso()),
                    "raw": r,
                }
            )
    return out


def poll_external_bank_api_and_reconcile(qr_hmac_secret: str, db_path=None) -> dict:
    """Fetch recent transactions from configured Bank API (SePay/Casso/Custom) and run auto-reconciliation."""
    with read_db(db_path) as conn:
        cfg = row_to_dict(conn.execute("SELECT * FROM bank_sync_settings WHERE id = 1").fetchone()) or {}

    provider = cfg.get("provider") or "SEPAY_API"
    api_url = (cfg.get("api_url") or "").strip()
    api_token = (cfg.get("api_token") or "").strip()

    if provider == "MANUAL_IMPORT_ONLY" or (not api_url and not api_token):
        msg = "Chưa cấu hình API URL / Token ngân hàng trực tuyến. Hãy cấu hình SePay/Casso API hoặc tải lên sao kê CSV/JSON (song song duyệt thủ công)."
        with atomic_transaction(db_path) as conn:
            conn.execute(
                "UPDATE bank_sync_settings SET last_synced_at = ?, last_sync_status = 'NOT_CONFIGURED', last_sync_message = ? WHERE id = 1",
                (utc_now_iso(), msg),
            )
        raise AppError("BANK_API_NOT_CONFIGURED", msg, 400)

    if not api_url:
        if provider == "SEPAY_API":
            api_url = "https://my.sepay.vn/userapi/transactions/list?limit=50"
        elif provider == "CASSO_API":
            api_url = "https://oauth.casso.vn/v2/transactions?pageSize=50&sort=DESC"

    headers = {"Accept": "application/json", "User-Agent": "VeloPass-Reconciler/2.0"}
    if api_token:
        if provider == "CASSO_API":
            headers["Authorization"] = f"Apikey {api_token}"
        else:
            headers["Authorization"] = f"Bearer {api_token}"

    req = urllib.request.Request(api_url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            raw_bytes = resp.read()
            payload = json.loads(raw_bytes.decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as exc:
        err_msg = f"Lỗi kết nối API ngân hàng ({provider}): {exc}"
        with atomic_transaction(db_path) as conn:
            conn.execute(
                "UPDATE bank_sync_settings SET last_synced_at = ?, last_sync_status = 'ERROR', last_sync_message = ? WHERE id = 1",
                (utc_now_iso(), err_msg[:240]),
            )
        raise AppError("BANK_API_FETCH_FAILED", err_msg, 502) from exc

    entries = _normalize_remote_api_payload(provider, payload)
    result = reconcile_bank_entries(entries, source="API_POLL", qr_hmac_secret=qr_hmac_secret, db_path=db_path)

    ok_msg = (
        f"Đồng bộ {provider} thành công: nhận {result['total_received']} dòng, "
        f"tự động duyệt {result['auto_approved']} đơn nạp."
    )
    with atomic_transaction(db_path) as conn:
        conn.execute(
            "UPDATE bank_sync_settings SET last_synced_at = ?, last_sync_status = 'OK', last_sync_message = ? WHERE id = 1",
            (utc_now_iso(), ok_msg),
        )
    return result


def parse_and_reconcile_statement_text(
    content_text: str,
    fmt: str,
    qr_hmac_secret: str,
) -> dict:
    """Parse Bank Statement CSV or JSON text and auto-reconcile against pending Top-up orders."""
    content_text = (content_text or "").strip()
    if not content_text:
        raise AppError("EMPTY_STATEMENT", "Dữ liệu sao kê trống. Vui lòng chọn file hoặc dán nội dung CSV/JSON.", 400)

    fmt = (fmt or "auto").lower()
    entries: list[dict] = []

    if fmt == "json" or (fmt == "auto" and content_text.startswith(("[", "{"))):
        try:
            parsed = json.loads(content_text)
        except ValueError as exc:
            raise AppError("INVALID_JSON", f"Định dạng JSON sao kê không hợp lệ: {exc}", 400) from exc
        entries = _normalize_remote_api_payload("CUSTOM_JSON_API", parsed)
    else:
        reader = csv.DictReader(io.StringIO(content_text))
        if not reader.fieldnames:
            raise AppError("INVALID_CSV", "File CSV sao kê thiếu dòng tiêu đề cột.", 400)

        for idx, row in enumerate(reader, start=1):
            lowered = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            tx_id = (
                lowered.get("bank_tx_id")
                or lowered.get("reference")
                or lowered.get("ref")
                or lowered.get("ma_gd")
                or lowered.get("ft")
                or lowered.get("id")
                or ""
            )
            amt = _parse_int_amount(
                lowered.get("amount_in")
                or lowered.get("amount")
                or lowered.get("credit")
                or lowered.get("so_tien")
                or lowered.get("ghi_co")
                or "0"
            )
            desc = (
                lowered.get("description")
                or lowered.get("content")
                or lowered.get("noi_dung")
                or lowered.get("dien_giai")
                or lowered.get("memo")
                or ""
            )
            acc = (
                lowered.get("account_number")
                or lowered.get("account")
                or lowered.get("so_tk")
                or ""
            )
            tx_time = (
                lowered.get("tx_time")
                or lowered.get("date")
                or lowered.get("ngay_gd")
                or utc_now_iso()
            )
            if not tx_id and desc and amt > 0:
                tx_id = f"CSV-{idx}-{_clean_digits_alnum(desc)[:12]}-{amt}"
            entries.append(
                {
                    "bank_tx_id": tx_id,
                    "account_number": acc,
                    "amount_in": amt,
                    "description": desc,
                    "tx_time": tx_time,
                    "raw": row,
                }
            )

    if not entries:
        raise AppError("NO_VALID_ROWS", "Không tìm thấy dòng giao dịch hợp lệ nào trong dữ liệu sao kê.", 400)

    res = reconcile_bank_entries(entries, source="STATEMENT_IMPORT", qr_hmac_secret=qr_hmac_secret)
    with atomic_transaction() as conn:
        conn.execute(
            "UPDATE bank_sync_settings SET last_synced_at = ?, last_sync_status = 'IMPORT_OK', last_sync_message = ? WHERE id = 1",
            (
                utc_now_iso(),
                f"Đối soát sao kê: xử lý {res['ingested']} giao dịch mới, tự động duyệt {res['auto_approved']} đơn.",
            ),
        )
    return res


def list_bank_statement_entries(match_status: str = "", limit: int = 100) -> dict:
    with read_db() as conn:
        sql = """
            SELECT bse.*, tt.transaction_code AS topup_tx_code, tt.amount AS expected_amount,
                   u.full_name AS user_name, u.username
            FROM bank_statement_entries bse
            LEFT JOIN topup_transactions tt ON tt.id = bse.matched_topup_id
            LEFT JOIN users u ON u.id = tt.user_id
            WHERE 1 = 1
        """
        params: list = []
        if match_status:
            sql += " AND bse.match_status = ?"
            params.append(match_status.upper())
        sql += " ORDER BY bse.id DESC LIMIT ?"
        params.append(min(max(1, limit), 300))
        rows = rows_to_dicts(conn.execute(sql, params).fetchall())

        counts = {
            r["match_status"]: int(r["cnt"])
            for r in conn.execute(
                "SELECT match_status, COUNT(*) AS cnt FROM bank_statement_entries GROUP BY match_status"
            ).fetchall()
        }

    for r in rows:
        r["amount_in_formatted"] = format_vnd(r["amount_in"])
        r["expected_amount_formatted"] = format_vnd(r["expected_amount"]) if r.get("expected_amount") else "—"

    return {"entries": rows, "summary_counts": counts}


def admin_manual_link_statement_entry(
    admin_id: int,
    entry_id: int,
    topup_id: int,
    qr_hmac_secret: str,
    ip_address: str = "",
) -> dict:
    """Allow Admin to manually bind an UNMATCHED bank statement entry to a PENDING/PROCESSING top-up order."""
    post_events: list[tuple[int, str, dict]] = []
    now_iso = utc_now_iso()

    with atomic_transaction() as conn:
        entry = conn.execute("SELECT * FROM bank_statement_entries WHERE id = ?", (entry_id,)).fetchone()
        if not entry:
            raise AppError("ENTRY_NOT_FOUND", "Không tìm thấy bút toán sao kê ngân hàng.", 404)
        if entry["match_status"] in ("MATCHED_AUTO_APPROVED", "ALREADY_SETTLED"):
            raise AppError("ENTRY_ALREADY_USED", "Bút toán sao kê này đã được đối soát hoàn tất trước đó.", 409)

        topup = conn.execute("SELECT * FROM topup_transactions WHERE id = ?", (topup_id,)).fetchone()
        if not topup:
            raise AppError("TOPUP_NOT_FOUND", "Không tìm thấy đơn nạp tiền cần ghép.", 404)
        if topup["status"] not in ("PENDING", "PROCESSING"):
            raise AppError("INVALID_TOPUP_STATE", f"Đơn nạp hiện ở trạng thái {topup['status']}, không thể ghép.", 400)

        if int(entry["amount_in"]) != int(topup["amount"]):
            raise AppError(
                "AMOUNT_MISMATCH",
                f"Số tiền sao kê ({format_vnd(entry['amount_in'])}) không khớp với đơn nạp ({format_vnd(topup['amount'])}).",
                400,
            )

        summary, post_events = _credit_topup_in_tx(
            conn=conn,
            topup_row=topup,
            provider="MANUAL_STATEMENT_LINK",
            bank_reference=entry["bank_tx_id"],
            qr_hmac_secret=qr_hmac_secret,
            admin_id=admin_id,
            note=f"Admin ghép thủ công bút toán sao kê #{entry_id} ({entry['bank_tx_id']})",
        )
        conn.execute(
            """
            UPDATE bank_statement_entries
            SET match_status = 'ALREADY_SETTLED',
                matched_topup_id = ?,
                reconciled_note = ?
            WHERE id = ?
            """,
            (topup_id, f"Admin ghép & duyệt thủ công vào đơn {topup['transaction_code']}", entry_id),
        )
        conn.execute(
            """
            INSERT INTO audit_logs (admin_id, action, target_type, target_id, metadata_json, ip_address, created_at)
            VALUES (?, 'LINK_STATEMENT_TO_TOPUP', 'BANK_STATEMENT_ENTRY', ?, ?, ?, ?)
            """,
            (
                admin_id,
                str(entry_id),
                json.dumps(
                    {
                        "bank_tx_id": entry["bank_tx_id"],
                        "topup_id": topup_id,
                        "transaction_code": topup["transaction_code"],
                        "amount": entry["amount_in"],
                    },
                    ensure_ascii=False,
                ),
                ip_address[:64],
                now_iso,
            ),
        )

    for uid, ev, data in post_events:
        emit_user_realtime_update(uid, ev, data)
    realtime_broker.publish_to_admins("topup_confirmed", summary)
    return summary


def start_background_reconciliation_daemon(app) -> None:
    """Start a non-blocking daemon thread that polls configured Bank API when enabled."""
    global _WORKER_STARTED
    if app.config.get("APP_ENV") == "testing":
        return
    with _WORKER_LOCK:
        if _WORKER_STARTED:
            return
        _WORKER_STARTED = True

    db_path = app.config["DATABASE_PATH"]
    qr_secret = str(app.config["QR_HMAC_SECRET"])

    def _loop() -> None:
        while True:
            sleep_sec = 20
            try:
                with read_db(db_path) as conn:
                    cfg = conn.execute("SELECT * FROM bank_sync_settings WHERE id = 1").fetchone()
                if cfg:
                    sleep_sec = max(10, int(cfg["poll_interval_seconds"] or 20))
                    if int(cfg["background_poll_enabled"]) and (cfg["api_url"] or cfg["api_token"]):
                        poll_external_bank_api_and_reconcile(qr_hmac_secret=qr_secret, db_path=db_path)
            except Exception:
                pass
            time.sleep(sleep_sec)

    t = threading.Thread(target=_loop, name="velopass-bank-reconciler", daemon=True)
    t.start()
