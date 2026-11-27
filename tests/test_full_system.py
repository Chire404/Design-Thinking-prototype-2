"""Comprehensive End-to-End Automated Test Suite covering all 30 mandatory specification test cases."""

from __future__ import annotations

import base64
import threading

from tests.conftest import TEST_WEBHOOK_SECRET, sign_webhook_body


def register_and_login_user(app, username="user_a", email="user_a@example.com", full_name="Nguyễn Văn A"):
    client = app.test_client()
    reg_res = client.post(
        "/api/auth/register",
        json={
            "username": username,
            "email": email,
            "password": "UserPass@123",
            "full_name": full_name,
            "phone": "0909111222",
        },
    )
    assert reg_res.status_code == 201
    login_res = client.post(
        "/api/auth/login",
        json={"identifier": username, "password": "UserPass@123", "remember_me": True},
    )
    assert login_res.status_code == 200
    login_data = login_res.get_json()["data"]
    token = login_data["access_token"]
    csrf = login_data["csrf_token"]
    return client, token, csrf, reg_res.get_json()["data"]


def login_admin(app, username="admin_gate", password="AdminPassword@123"):
    client = app.test_client()
    res = client.post("/api/auth/login", json={"identifier": username, "password": password})
    assert res.status_code == 200
    data = res.get_json()["data"]
    return client, data["access_token"], data["csrf_token"], data["user"]


# ============================================================================
# PHẦN 1: KIỂM THỬ USER (TEST CASES 1 -> 5)
# ============================================================================


def test_01_user_register_success_and_blocks_self_admin_escalation(app):
    client = app.test_client()

    # Attempt privilege escalation during registration -> MUST FAIL with 403
    bad_res = client.post(
        "/api/auth/register",
        json={
            "username": "hacker",
            "email": "hacker@example.com",
            "password": "Password@123",
            "full_name": "Hacker",
            "role": "ADMIN",
        },
    )
    assert bad_res.status_code == 403

    # Valid registration -> creates USER, Wallet (0 VND), and Virtual Card
    ok_res = client.post(
        "/api/auth/register",
        json={
            "username": "khachhang01",
            "email": "khachhang01@example.com",
            "password": "Password@123",
            "full_name": "Trần Thị B",
        },
    )
    assert ok_res.status_code == 201
    data = ok_res.get_json()["data"]
    assert data["role"] == "USER"
    assert data["card_number"].startswith("VP-")


def test_02_user_login_success_by_username_and_email(app):
    _, _, _, _ = register_and_login_user(app, "minhkhoi", "minhkhoi@example.com", "Lê Minh Khôi")
    c2 = app.test_client()
    res_email = c2.post(
        "/api/auth/login",
        json={"identifier": "minhkhoi@example.com", "password": "UserPass@123"},
    )
    assert res_email.status_code == 200
    payload = res_email.get_json()["data"]
    assert payload["redirect_to"] == "/app"
    assert payload["user"]["username"] == "minhkhoi"


def test_03_user_cannot_access_admin_dashboard_or_admin_apis(app):
    u_client, u_token, u_csrf, _ = register_and_login_user(app)
    headers = {"Authorization": f"Bearer {u_token}", "X-CSRF-Token": u_csrf}

    # Page route /admin returns 403 Forbidden
    page_res = u_client.get("/admin")
    assert page_res.status_code == 403

    # All Admin API endpoints & QR Scanner endpoint return 403 Forbidden
    for method, url in [
        ("GET", "/api/admin/dashboard"),
        ("GET", "/api/admin/users"),
        ("GET", "/api/admin/transactions"),
        ("PUT", "/api/admin/settings/bank"),
        ("POST", "/api/qr/scan"),
    ]:
        resp = u_client.open(url, method=method, headers=headers, json={})
        assert resp.status_code == 403


def test_04_user_views_exact_balance_and_cannot_tamper_balance(app):
    u_client, u_token, _, _ = register_and_login_user(app)
    auth = {"Authorization": f"Bearer {u_token}"}

    bal_res = u_client.get("/api/user/balance", headers=auth)
    assert bal_res.status_code == 200
    wallet = bal_res.get_json()["data"]["wallet"]
    assert wallet["balance"] == 0
    assert wallet["balance_formatted"] == "0 VNĐ"

    # Attempt to modify balance via POST/PUT -> 403 Forbidden
    tamper_bal = u_client.post("/api/user/balance", headers=auth, json={"balance": 9999999})
    assert tamper_bal.status_code == 403

    tamper_prof = u_client.put("/api/user/profile", headers=auth, json={"full_name": "A", "balance": 500000})
    assert tamper_prof.status_code == 403


def test_05_user_only_views_personal_transaction_history(app):
    _, token_a, _, _ = register_and_login_user(app, "alice_vp", "alice@example.com", "Alice")
    _, token_b, _, _ = register_and_login_user(app, "bob_vp", "bob@example.com", "Bob")

    client = app.test_client()
    # Alice creates a 50.000 VND top-up
    t_alice = client.post(
        "/api/topups",
        headers={"Authorization": f"Bearer {token_a}"},
        json={"amount": 50000},
    ).get_json()["data"]

    # Bob creates a 100.000 VND top-up
    client.post(
        "/api/topups",
        headers={"Authorization": f"Bearer {token_b}"},
        json={"amount": 100000},
    )

    # Bob inspects history -> ONLY sees Bob's 100.000 VND top-up, never Alice's
    bob_hist = client.get("/api/user/transactions", headers={"Authorization": f"Bearer {token_b}"}).get_json()["data"]
    assert len(bob_hist["topups"]) == 1
    assert bob_hist["topups"][0]["amount"] == 100000

    # Bob attempts to view Alice's top-up detail directly -> 403 Forbidden
    forbidden = client.get(
        f"/api/topups/{t_alice['id']}",
        headers={"Authorization": f"Bearer {token_b}"},
    )
    assert forbidden.status_code == 403


# ============================================================================
# PHẦN 2: KIỂM THỬ TOP-UP & WEBHOOK & DUYỆT THỦ CÔNG (TEST CASES 6 -> 11)
# ============================================================================


def test_06_and_07_create_100k_topup_and_verify_vietqr_unique_code(app):
    _, u_token, _, _ = register_and_login_user(app)
    client = app.test_client()

    res = client.post(
        "/api/topups",
        headers={"Authorization": f"Bearer {u_token}"},
        json={"amount": 100000},
    )
    assert res.status_code == 201
    order = res.get_json()["data"]
    assert order["amount"] == 100000
    assert order["amount_formatted"] == "100.000 VNĐ"
    assert order["status"] == "PENDING"
    assert order["transfer_content"].startswith("VP")

    # Test 7: EMVCo VietQR contains exact amount '100000' and unique transfer_content
    emv = order["qr"]["emv_payload"]
    assert "5406100000" in emv
    assert order["transfer_content"] in emv
    assert order["qr"]["qr_svg_data_uri"].startswith("data:image/svg+xml;base64,")


def test_08_and_09_signed_webhook_credits_once_and_blocks_duplicate_replay(app):
    _, u_token, _, _ = register_and_login_user(app)
    client = app.test_client()

    order = client.post(
        "/api/topups",
        headers={"Authorization": f"Bearer {u_token}"},
        json={"amount": 100000},
    ).get_json()["data"]

    webhook_payload = {
        "event_id": "EVT-BANK-0001",
        "bank_reference": "VCB-FT-99887766",
        "amount": 100000,
        "content": f"CHUYEN KHOAN {order['transfer_content']} NAP VI",
    }
    raw_body, sig = sign_webhook_body(webhook_payload)

    # Test 8: Valid webhook credits 100.000 VND once
    w_res1 = client.post(
        "/api/payment/webhook",
        data=raw_body,
        headers={"Content-Type": "application/json", "X-Webhook-Signature": sig},
    )
    assert w_res1.status_code == 200
    assert w_res1.get_json()["data"]["duplicate"] is False
    assert w_res1.get_json()["data"]["balance_after"] == 100000

    # Test 9: Duplicate replay of the exact same webhook OR same bank_reference NEVER credits twice!
    w_res2 = client.post(
        "/api/payment/webhook",
        data=raw_body,
        headers={"Content-Type": "application/json", "X-Webhook-Signature": sig},
    )
    assert w_res2.status_code == 200
    assert w_res2.get_json()["data"]["duplicate"] is True

    bal = client.get("/api/user/balance", headers={"Authorization": f"Bearer {u_token}"}).get_json()["data"]
    assert bal["wallet"]["balance"] == 100000
    assert bal["card_status"] == "ACTIVE"


def test_10_unverified_topup_or_bad_webhook_never_credits_money(app):
    _, u_token, _, _ = register_and_login_user(app)
    client = app.test_client()

    order = client.post(
        "/api/topups",
        headers={"Authorization": f"Bearer {u_token}"},
        json={"amount": 100000},
    ).get_json()["data"]

    # User clicks "Tôi đã chuyển khoản" -> status becomes PROCESSING, balance MUST remain 0 VND!
    rep = client.post(
        f"/api/topups/{order['id']}/report-transferred",
        headers={"Authorization": f"Bearer {u_token}"},
        json={"note": "Đã chuyển trên app ngân hàng"},
    )
    assert rep.status_code == 200
    assert rep.get_json()["data"]["status"] == "PROCESSING"

    # Forged webhook signature -> rejected 401
    forged_payload = {
        "event_id": "EVT-FORGED",
        "bank_reference": "FAKE-REF-1",
        "amount": 100000,
        "content": order["transfer_content"],
    }
    raw_forged, _ = sign_webhook_body(forged_payload, secret="wrong-secret-key")
    bad_wh = client.post(
        "/api/payment/webhook",
        data=raw_forged,
        headers={"Content-Type": "application/json", "X-Webhook-Signature": "sha256=deadbeef"},
    )
    assert bad_wh.status_code == 401

    # Mismatched amount webhook (50.000 instead of 100.000) -> rejected 400
    wrong_amt_payload = {
        "event_id": "EVT-WRONG-AMT",
        "bank_reference": "REAL-REF-WRONG-AMT",
        "amount": 50000,
        "content": order["transfer_content"],
    }
    raw_wrong_amt, sig_wrong = sign_webhook_body(wrong_amt_payload, secret=TEST_WEBHOOK_SECRET)
    wrong_wh = client.post(
        "/api/payment/webhook",
        data=raw_wrong_amt,
        headers={"Content-Type": "application/json", "X-Webhook-Signature": sig_wrong},
    )
    assert wrong_wh.status_code == 400

    # Balance is still strictly 0 VND
    bal = client.get("/api/user/balance", headers={"Authorization": f"Bearer {u_token}"}).get_json()["data"]
    assert bal["wallet"]["balance"] == 0


def test_11_admin_manual_confirmation_from_bank_statement_succeeds(app):
    _, u_token, _, _ = register_and_login_user(app)
    _, adm_token, _, _ = login_admin(app)
    client = app.test_client()

    order = client.post(
        "/api/topups",
        headers={"Authorization": f"Bearer {u_token}"},
        json={"amount": 100000},
    ).get_json()["data"]

    confirm_res = client.post(
        f"/api/admin/topups/{order['id']}/confirm",
        headers={"Authorization": f"Bearer {adm_token}"},
        json={"bank_reference": "STMT-VCB-20261126-001", "note": "Khớp sao kê Vietcombank"},
    )
    assert confirm_res.status_code == 200
    c_data = confirm_res.get_json()["data"]
    assert c_data["balance_after"] == 100000
    assert c_data["card_status"] == "ACTIVE"

    # Double-confirming same topup is rejected with 409
    dup_confirm = client.post(
        f"/api/admin/topups/{order['id']}/confirm",
        headers={"Authorization": f"Bearer {adm_token}"},
        json={"bank_reference": "STMT-VCB-20261126-002"},
    )
    assert dup_confirm.status_code == 409


# ============================================================================
# PHẦN 3: DYNAMIC QR, QUY TẮC 4.000 VNĐ & CONCURRENCY (TEST CASES 12 -> 20)
# ============================================================================


def test_12_to_17_complete_25_scan_countdown_from_100k_to_0_and_qr_lock(app):
    _, u_token, _, _ = register_and_login_user(app)
    _, adm_token, _, _ = login_admin(app)
    client = app.test_client()
    u_auth = {"Authorization": f"Bearer {u_token}"}
    adm_auth = {"Authorization": f"Bearer {adm_token}"}

    # Top up 100.000 VND
    order = client.post("/api/topups", headers=u_auth, json={"amount": 100000}).get_json()["data"]
    client.post(
        f"/api/admin/topups/{order['id']}/confirm",
        headers=adm_auth,
        json={"bank_reference": "STMT-100K-INIT"},
    )

    # Test 12: User with 100.000 VND is issued an active Dynamic QR
    qr1 = client.post("/api/qr/issue", headers=u_auth).get_json()["data"]
    assert qr1["qr_active"] is True
    first_token = qr1["qr_token"]

    # Test 13: Admin scans 1st time -> balance becomes 96.000 VND
    scan1 = client.post("/api/qr/scan", headers=adm_auth, json={"qr_token": first_token})
    assert scan1.status_code == 200
    s1_data = scan1.get_json()["data"]
    assert s1_data["amount"] == 4000
    assert s1_data["balance_before"] == 100000
    assert s1_data["balance_after"] == 96000
    assert s1_data["qr_reissued"] is True

    # Test 17: Reusing the old token immediately fails with 409 QR_ALREADY_USED
    reuse = client.post("/api/qr/scan", headers=adm_auth, json={"qr_token": first_token})
    assert reuse.status_code == 409
    assert reuse.get_json()["error"]["code"] == "QR_ALREADY_USED"

    # Test 14: Scan 23 more times (scans #2 through #24) -> balance reaches 4.000 VND!
    for expected_after in range(92000, 0, -4000):
        tok = client.post("/api/qr/issue", headers=u_auth).get_json()["data"]["qr_token"]
        sc = client.post("/api/qr/scan", headers=adm_auth, json={"qr_token": tok})
        assert sc.status_code == 200
        assert sc.get_json()["data"]["balance_after"] == expected_after
        if expected_after == 4000:
            break

    # Verify at exactly 4.000 VND, card is STILL ACTIVE and entitled to 1 final scan!
    status_at_4k = client.get("/api/qr/current", headers=u_auth).get_json()["data"]
    assert status_at_4k["balance"] == 4000
    assert status_at_4k["card_status"] == "ACTIVE"
    assert status_at_4k["qr_eligible"] is True

    # Test 15: Scan 25th time -> balance drops from 4.000 VND to 0 VND!
    last_tok = client.post("/api/qr/issue", headers=u_auth).get_json()["data"]["qr_token"]
    scan25 = client.post("/api/qr/scan", headers=adm_auth, json={"qr_token": last_tok})
    assert scan25.status_code == 200
    s25_data = scan25.get_json()["data"]
    assert s25_data["balance_before"] == 4000
    assert s25_data["balance_after"] == 0
    assert s25_data["card_status_after"] == "INSUFFICIENT_BALANCE"
    assert s25_data["qr_reissued"] is False

    # Test 16: QR Code is now immediately DISABLED
    locked_issue = client.post("/api/qr/issue", headers=u_auth)
    assert locked_issue.status_code == 400
    assert locked_issue.get_json()["error"]["code"] == "INSUFFICIENT_BALANCE"


def test_18_and_19_balance_3999_cannot_scan_and_20k_topup_reactivates_qr(app):
    _, u_token, _, user_info = register_and_login_user(app)
    _, adm_token, _, _ = login_admin(app)
    client = app.test_client()
    u_auth = {"Authorization": f"Bearer {u_token}"}
    adm_auth = {"Authorization": f"Bearer {adm_token}"}

    # Credit exact 3.999 VND via Admin controlled adjustment
    adj = client.post(
        f"/api/admin/users/{user_info['id']}/adjust-balance",
        headers=adm_auth,
        json={"direction": "CREDIT", "amount": 3999, "reason": "Kiểm thử ngưỡng 3.999 VNĐ"},
    )
    assert adj.status_code == 200
    assert adj.get_json()["data"]["balance_after"] == 3999
    assert adj.get_json()["data"]["card_status"] == "INSUFFICIENT_BALANCE"

    # Test 18: User with 3.999 VND CANNOT issue or scan QR
    issue_3999 = client.post("/api/qr/issue", headers=u_auth)
    assert issue_3999.status_code == 400
    assert issue_3999.get_json()["error"]["code"] == "INSUFFICIENT_BALANCE"

    # Test 19: User tops up 20.000 VND -> balance becomes 23.999 VND & QR reactivates automatically!
    order = client.post("/api/topups", headers=u_auth, json={"amount": 20000}).get_json()["data"]
    client.post(
        f"/api/admin/topups/{order['id']}/confirm",
        headers=adm_auth,
        json={"bank_reference": "STMT-REACTIVATE-20K"},
    )

    cur_qr = client.get("/api/qr/current", headers=u_auth).get_json()["data"]
    assert cur_qr["balance"] == 23999
    assert cur_qr["card_status"] == "ACTIVE"
    assert cur_qr["has_active_token"] is True

    reissued = client.post("/api/qr/issue", headers=u_auth)
    assert reissued.status_code == 200
    assert reissued.get_json()["data"]["qr_active"] is True


def test_20_concurrent_two_admins_scanning_same_token_deducts_only_once(app):
    _, u_token, _, _ = register_and_login_user(app, "race_user", "race@example.com", "Phạm Quốc Bảo")
    _, adm1_token, _, _ = login_admin(app, "admin_gate")
    _, adm2_token, _, _ = login_admin(app, "admin_gate_2")

    setup_client = app.test_client()
    order = setup_client.post(
        "/api/topups",
        headers={"Authorization": f"Bearer {u_token}"},
        json={"amount": 50000},
    ).get_json()["data"]
    setup_client.post(
        f"/api/admin/topups/{order['id']}/confirm",
        headers={"Authorization": f"Bearer {adm1_token}"},
        json={"bank_reference": "STMT-RACE-50K"},
    )

    shared_qr_token = setup_client.post(
        "/api/qr/issue",
        headers={"Authorization": f"Bearer {u_token}"},
    ).get_json()["data"]["qr_token"]

    barrier = threading.Barrier(2)
    results = []

    def worker_scan(admin_bearer: str, gate_label: str):
        local_client = app.test_client()
        barrier.wait()
        resp = local_client.post(
            "/api/qr/scan",
            headers={"Authorization": f"Bearer {admin_bearer}"},
            json={"qr_token": shared_qr_token, "idempotency_key": f"CONCURRENT-{gate_label}"},
        )
        results.append((resp.status_code, resp.get_json()))

    t1 = threading.Thread(target=worker_scan, args=(adm1_token, "GATE1"))
    t2 = threading.Thread(target=worker_scan, args=(adm2_token, "GATE2"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    statuses = sorted(r[0] for r in results)
    assert statuses == [200, 409], f"Expected exactly one 200 and one 409, got {results}"

    # Verify balance was deducted ONLY ONCE: 50.000 - 4.000 = 46.000 VND!
    final_bal = setup_client.get(
        "/api/user/balance",
        headers={"Authorization": f"Bearer {u_token}"},
    ).get_json()["data"]["wallet"]["balance"]
    assert final_bal == 46000

    # Also verify Idempotency-Key network retry returns 200 replay without charging again
    winning_idx = 0 if results[0][0] == 200 else 1
    winning_gate = "GATE1" if winning_idx == 0 and results[0][0] == 200 else None
    for code, body in results:
        if code == 200:
            tx_code = body["data"]["transaction_code"]
            break

    # Retry with explicit new idempotency test on next token
    next_qr = setup_client.post("/api/qr/issue", headers={"Authorization": f"Bearer {u_token}"}).get_json()["data"]["qr_token"]
    r_first = setup_client.post(
        "/api/qr/scan",
        headers={"Authorization": f"Bearer {adm1_token}", "Idempotency-Key": "RETRY-KEY-999"},
        json={"qr_token": next_qr},
    )
    r_retry = setup_client.post(
        "/api/qr/scan",
        headers={"Authorization": f"Bearer {adm1_token}", "Idempotency-Key": "RETRY-KEY-999"},
        json={"qr_token": next_qr},
    )
    assert r_first.status_code == 200
    assert r_retry.status_code == 200
    assert r_retry.get_json()["data"]["idempotent_replay"] is True
    assert r_first.get_json()["data"]["transaction_code"] == r_retry.get_json()["data"]["transaction_code"]
    assert setup_client.get("/api/user/balance", headers={"Authorization": f"Bearer {u_token}"}).get_json()["data"]["wallet"]["balance"] == 42000


# ============================================================================
# PHẦN 4: KIỂM THỬ ADMIN, LEDGER & AUDIT LOGS (TEST CASES 21 -> 25)
# ============================================================================


def test_21_to_25_admin_operations_ledger_and_audit_logs(app):
    _, u_token, _, u_info = register_and_login_user(app, "hoangnam", "nam@example.com", "Hoàng Nam")
    adm_client, adm_token, adm_csrf, _ = login_admin(app)
    adm_auth = {"Authorization": f"Bearer {adm_token}", "X-CSRF-Token": adm_csrf}
    u_auth = {"Authorization": f"Bearer {u_token}"}

    # Test 21: Admin dashboard metrics & periods
    for p in ("day", "week", "month"):
        dash = adm_client.get(f"/api/admin/dashboard?period={p}", headers=adm_auth)
        assert dash.status_code == 200
        assert "total_users" in dash.get_json()["data"]["kpis"]

    # Test 22: Admin page & camera permissions policy header
    adm_page = adm_client.get("/admin")
    assert adm_page.status_code == 200
    assert "camera=(self)" in adm_page.headers.get("Permissions-Policy", "")
    html_text = adm_page.get_data(as_text=True)
    assert 'id="scanner-video"' in html_text
    assert 'id="scanner-camera-select"' in html_text

    # Top-up & scan once to populate ledger
    t_ord = adm_client.post("/api/topups", headers=u_auth, json={"amount": 40000}).get_json()["data"]
    adm_client.post(
        f"/api/admin/topups/{t_ord['id']}/confirm",
        headers=adm_auth,
        json={"bank_reference": "STMT-ADMIN-40K"},
    )
    tok = adm_client.post("/api/qr/issue", headers=u_auth).get_json()["data"]["qr_token"]
    adm_client.post("/api/qr/scan", headers=adm_auth, json={"qr_token": tok})

    # Test 23: Suspend User -> QR immediately disabled; Activate User -> QR restored
    sus = adm_client.post(
        f"/api/admin/users/{u_info['id']}/suspend",
        headers=adm_auth,
        json={"reason": "Kiểm tra khóa thẻ"},
    )
    assert sus.status_code == 200
    assert sus.get_json()["data"]["card_status"] == "SUSPENDED"
    assert adm_client.post("/api/qr/issue", headers=u_auth).status_code == 403

    act = adm_client.post(
        f"/api/admin/users/{u_info['id']}/activate",
        headers=adm_auth,
        json={"reason": "Mở khóa thẻ"},
    )
    assert act.status_code == 200
    assert act.get_json()["data"]["card_status"] == "ACTIVE"

    # Update bank settings & fare settings (recording fare_history)
    bank_upd = adm_client.put(
        "/api/admin/settings/bank",
        headers=adm_auth,
        json={
            "bank_name": "Ngân hàng Quân Đội (MBBank)",
            "bank_short_name": "MB",
            "bank_bin": "970422",
            "account_number": "0888999777",
            "account_holder": "VELOPASS CORP",
            "transfer_prefix": "VPMB",
            "payment_instructions": "Chuyển nhanh Napas 247",
            "is_active": 1,
        },
    )
    assert bank_upd.status_code == 200

    sys_upd = adm_client.put(
        "/api/admin/settings/system",
        headers=adm_auth,
        json={"fare_amount": 5000, "qr_ttl_seconds": 90, "low_balance_threshold": 20000, "fare_change_reason": "Điều chỉnh thử nghiệm"},
    )
    assert sys_upd.status_code == 200
    assert len(sys_upd.get_json()["data"]["fare_history"]) == 1

    # Test 24: Transaction history & filters accuracy
    tx_res = adm_client.get("/api/admin/transactions?user=hoangnam", headers=adm_auth).get_json()["data"]["transactions"]
    assert len(tx_res) == 2
    assert tx_res[0]["transaction_type"] == "USAGE"
    assert tx_res[0]["balance_before"] == 40000
    assert tx_res[0]["balance_after"] == 36000

    # Test 25: Audit logs recorded all critical admin actions
    logs = adm_client.get("/api/admin/audit-logs", headers=adm_auth).get_json()["data"]["audit_logs"]
    actions = {item["action"] for item in logs}
    assert {"CONFIRM_TOPUP", "SUSPEND_USER", "ACTIVATE_USER", "UPDATE_BANK_SETTINGS", "UPDATE_SYSTEM_SETTINGS"}.issubset(actions)


# ============================================================================
# PHẦN 5: KIỂM THỬ GIAO DIỆN, MOBILE, PWA, QR SVG & CAMERA (TEST CASES 26 -> 30)
# ============================================================================


def test_26_to_30_ui_mobile_bottom_nav_pwa_qr_privacy_and_camera_assets(app):
    u_client, u_token, _, u_info = register_and_login_user(app, "ui_tester", "uitest@example.com", "Đặng Thu Hà")
    _, adm_token, _, _ = login_admin(app)

    # Fund account so QR is active
    order = u_client.post(
        "/api/topups",
        headers={"Authorization": f"Bearer {u_token}"},
        json={"amount": 20000},
    ).get_json()["data"]
    u_client.post(
        f"/api/admin/topups/{order['id']}/confirm",
        headers={"Authorization": f"Bearer {adm_token}"},
        json={"bank_reference": "STMT-UI-20K"},
    )

    # Test 26: Mobile viewport, PWA Manifest & Safe Service Worker
    manifest = u_client.get("/manifest.json")
    assert manifest.status_code == 200
    m_json = manifest.get_json()
    assert m_json["display"] == "standalone"
    assert m_json["start_url"] == "/app"

    sw = u_client.get("/sw.js")
    assert sw.status_code == 200
    assert 'url.pathname.startsWith("/api/")' in sw.get_data(as_text=True)

    # Test 27 & 28: User App HTML has Mobile Bottom Navigation (5 items) & Desktop Sidebar Rail, Auth page hides Admin signup
    login_html = app.test_client().get("/login").get_data(as_text=True)
    assert "SUPER_ADMIN" not in login_html

    app_page = u_client.get("/app")
    assert app_page.status_code == 200
    app_html = app_page.get_data(as_text=True)
    assert 'class="desktop-rail"' in app_html
    assert 'class="bottom-nav bottom-nav--6"' in app_html or 'class="bottom-nav"' in app_html
    for label in ("Trang chủ", "Vé lượt", "Vé tháng", "Nạp tiền", "Lịch sử", "Tài khoản"):
        assert label in app_html

    # Test 29: QR SVG renders cleanly and QR token contains ZERO PII or balance
    qr_data = u_client.post("/api/qr/issue", headers={"Authorization": f"Bearer {u_token}"}).get_json()["data"]
    raw_tok = qr_data["qr_token"]
    assert raw_tok.startswith("VPQR1.")
    assert "ui_tester" not in raw_tok
    assert "uitest@example.com" not in raw_tok
    assert "20000" not in raw_tok
    assert u_info["card_number"] not in raw_tok

    svg_b64 = qr_data["qr_svg_data_uri"].split(",", 1)[1]
    svg_xml = base64.b64decode(svg_b64).decode("utf-8")
    assert "<svg" in svg_xml and "<path" in svg_xml

    # Test 30: Camera QR Scanner JS & local jsQR decoder asset are served properly
    jsqr_res = u_client.get("/static/vendor/jsQR.js")
    assert jsqr_res.status_code == 200
    assert len(jsqr_res.data) > 10000

    scanner_js = u_client.get("/static/js/admin_app.js").get_data(as_text=True)
    assert "navigator.mediaDevices.getUserMedia" in scanner_js
    assert "BarcodeDetector" in scanner_js
    assert "window.jsQR" in scanner_js


def test_31_security_brute_force_lockout_password_reset_and_expired_qr(app):
    _, u_token, _, u_info = register_and_login_user(app, "sec_user", "sec@example.com", "Bảo Mật")
    _, adm_token, _, _ = login_admin(app)
    client = app.test_client()

    # Fund & issue QR, then manually backdate expires_at in DB to test EXPIRED_QR
    ord_data = client.post(
        "/api/topups",
        headers={"Authorization": f"Bearer {u_token}"},
        json={"amount": 10000},
    ).get_json()["data"]
    client.post(
        f"/api/admin/topups/{ord_data['id']}/confirm",
        headers={"Authorization": f"Bearer {adm_token}"},
        json={"bank_reference": "STMT-SEC-10K"},
    )
    qr = client.post("/api/qr/issue", headers={"Authorization": f"Bearer {u_token}"}).get_json()["data"]

    from backend.database import atomic_transaction

    with app.app_context():
        with atomic_transaction() as conn:
            conn.execute("UPDATE qr_tokens SET expires_at = '2020-01-01T00:00:00.000000Z' WHERE id = ?", (qr["token_id"],))

    exp_scan = client.post(
        "/api/qr/scan",
        headers={"Authorization": f"Bearer {adm_token}"},
        json={"qr_token": qr["qr_token"]},
    )
    assert exp_scan.status_code == 400
    assert exp_scan.get_json()["error"]["code"] == "EXPIRED_QR"

    # Brute-force protection: 5 wrong passwords -> 6th attempt returns 429 ACCOUNT_TEMP_LOCKED
    for _ in range(5):
        r = client.post("/api/auth/login", json={"identifier": "sec_user", "password": "WrongPassword!"})
        assert r.status_code == 401
    locked = client.post("/api/auth/login", json={"identifier": "sec_user", "password": "UserPass@123"})
    assert locked.status_code == 429
    assert locked.get_json()["error"]["code"] == "ACCOUNT_TEMP_LOCKED"

    # Forgot password & reset password clears lockout and sets new password
    fp = client.post("/api/auth/forgot-password", json={"email": "sec@example.com"}).get_json()["data"]
    rst = client.post(
        "/api/auth/reset-password",
        json={"token": fp["reset_token_preview"], "new_password": "NewStrongPassword@99"},
    )
    assert rst.status_code == 200

    relogin = client.post("/api/auth/login", json={"identifier": "sec_user", "password": "NewStrongPassword@99"})
    assert relogin.status_code == 200
