"""Comprehensive Prototype 2 Test Suite:
1. Monthly Pass subscription, 0 VND gate scan priority, card eligibility with 0 VND wallet balance,
   extension stacking, max_trips enforcement, and auto-renewal on expiry.
2. Automated Bank Account Statement Reconciliation (CSV/JSON import + SePay/Casso live API polling)
   running safely in parallel with Manual Admin Top-up Approval.
"""

from __future__ import annotations

import json
import urllib.request
from unittest.mock import MagicMock, patch

from backend.database import atomic_transaction
from tests.test_full_system import login_admin, register_and_login_user


def test_p2_01_monthly_pass_keeps_qr_active_at_zero_balance_and_scans_for_0_vnd(app):
    _, u_token, _, _ = register_and_login_user(app, "commuter_01", "commuter01@example.com", "Lê Hoàng Phúc")
    _, adm_token, _, _ = login_admin(app)
    client = app.test_client()
    u_auth = {"Authorization": f"Bearer {u_token}"}
    adm_auth = {"Authorization": f"Bearer {adm_token}"}

    # 1. Fund exact 100.000 VND
    ord_100k = client.post("/api/topups", headers=u_auth, json={"amount": 100000}).get_json()["data"]
    client.post(
        f"/api/admin/topups/{ord_100k['id']}/confirm",
        headers=adm_auth,
        json={"bank_reference": "STMT-P2-100K"},
    )

    # 2. Purchase 100.000 VND Standard Monthly Pass -> Wallet balance drops to 0 VND!
    plans = client.get("/api/passes/plans").get_json()["data"]["plans"]
    std_plan = next(p for p in plans if p["code"] == "MONTHLY_STANDARD")
    assert std_plan["price"] == 100000

    buy_res = client.post(
        "/api/passes/purchase",
        headers=u_auth,
        json={"plan_id": std_plan["id"], "auto_renew": False},
    )
    assert buy_res.status_code == 201
    buy_data = buy_res.get_json()["data"]
    assert buy_data["balance_after"] == 0
    assert buy_data["card_status"] == "ACTIVE"  # Still ACTIVE despite 0 VND wallet!

    # 3. Issue Dynamic QR & scan 3 times -> 0 VND deducted each time, QR rotates cleanly!
    for expected_trip_num in (1, 2, 3):
        qr = client.post("/api/qr/issue", headers=u_auth)
        assert qr.status_code == 200
        tok = qr.get_json()["data"]["qr_token"]

        scan = client.post("/api/qr/scan", headers=adm_auth, json={"qr_token": tok})
        assert scan.status_code == 200
        s_data = scan.get_json()["data"]
        assert s_data["payment_method"] == "MONTHLY_PASS"
        assert s_data["amount"] == 0
        assert s_data["balance_before"] == 0
        assert s_data["balance_after"] == 0
        assert s_data["qr_reissued"] is True

        # Old token is still strictly single-use!
        reuse = client.post("/api/qr/scan", headers=adm_auth, json={"qr_token": tok})
        assert reuse.status_code == 409

        my_pass = client.get("/api/passes/my", headers=u_auth).get_json()["data"]["active_pass"]
        assert my_pass["trips_used"] == expected_trip_num


def test_p2_02_monthly_pass_stacking_auto_renew_and_expiry_lock(app):
    _, u_token, _, _ = register_and_login_user(app, "commuter_02", "commuter02@example.com", "Võ Minh Tâm")
    _, adm_token, _, _ = login_admin(app)
    client = app.test_client()
    u_auth = {"Authorization": f"Bearer {u_token}"}
    adm_auth = {"Authorization": f"Bearer {adm_token}"}

    # Fund 90.000 VND
    ord_90k = client.post("/api/topups", headers=u_auth, json={"amount": 90000}).get_json()["data"]
    client.post(
        f"/api/admin/topups/{ord_90k['id']}/confirm",
        headers=adm_auth,
        json={"bank_reference": "STMT-P2-90K"},
    )

    plans = client.get("/api/passes/plans").get_json()["data"]["plans"]
    weekly = next(p for p in plans if p["code"] == "WEEKLY_COMMUTER")  # 30.000 VND / 7 days

    # Buy 1st 7-day pass (balance -> 60.000 VND) and 2nd stacked 7-day pass (balance -> 30.000 VND)
    p1 = client.post("/api/passes/purchase", headers=u_auth, json={"plan_id": weekly["id"]}).get_json()["data"]
    assert p1["is_extension"] is False
    p2 = client.post("/api/passes/purchase", headers=u_auth, json={"plan_id": weekly["id"]}).get_json()["data"]
    assert p2["is_extension"] is True
    assert p2["pass"]["starts_at"] == p1["pass"]["expires_at"]

    # Test auto-renewal when pass expires and auto_renew=True
    with app.app_context():
        with atomic_transaction() as conn:
            conn.execute("DELETE FROM user_passes WHERE id = ?", (p2["pass"]["id"],))
            conn.execute(
                "UPDATE user_passes SET auto_renew = 1, expires_at = '2020-01-01T00:00:00.000000Z' WHERE id = ?",
                (p1["pass"]["id"],),
            )

    # Triggering /api/passes/my expires old pass and auto-renews using remaining 30.000 VND!
    overview = client.get("/api/passes/my", headers=u_auth).get_json()["data"]
    assert overview["active_pass"] is not None
    assert overview["active_pass"]["id"] != p1["pass"]["id"]
    bal_after_renew = client.get("/api/user/balance", headers=u_auth).get_json()["data"]["wallet"]["balance"]
    assert bal_after_renew == 0

    # Now disable auto-renew and expire the renewed pass while wallet is 0 VND -> QR locks immediately!
    renewed_id = overview["active_pass"]["id"]
    client.post(f"/api/passes/{renewed_id}/auto-renew", headers=u_auth, json={"auto_renew": False})
    with app.app_context():
        with atomic_transaction() as conn:
            conn.execute(
                "UPDATE user_passes SET expires_at = '2020-01-01T00:00:00.000000Z' WHERE id = ?",
                (renewed_id,),
            )

    locked_qr = client.post("/api/qr/issue", headers=u_auth)
    assert locked_qr.status_code == 400
    assert locked_qr.get_json()["error"]["code"] == "INSUFFICIENT_BALANCE"


def test_p2_03_auto_bank_statement_reconciliation_csv_and_discrepancy_protection(app):
    _, tok_a, _, _ = register_and_login_user(app, "auto_user_a", "autoa@example.com", "Người Dùng A")
    _, tok_b, _, _ = register_and_login_user(app, "auto_user_b", "autob@example.com", "Người Dùng B")
    _, adm_token, _, _ = login_admin(app)
    client = app.test_client()
    adm_auth = {"Authorization": f"Bearer {adm_token}"}

    ord_a = client.post("/api/topups", headers={"Authorization": f"Bearer {tok_a}"}, json={"amount": 100000}).get_json()["data"]
    ord_b = client.post("/api/topups", headers={"Authorization": f"Bearer {tok_b}"}, json={"amount": 50000}).get_json()["data"]

    receiving_acc = ord_a["bank"]["account_number"]

    csv_statement = "\n".join(
        [
            "bank_tx_id,account_number,amount_in,description,tx_time",
            f"FT263310001,{receiving_acc},100000,MBVCB.88991.NGUYEN VAN A CK {ord_a['transfer_content']} NAP VI,2026-11-27T14:00:00Z",
            f"FT263310002,{receiving_acc},45000,CHUYEN THIEU TIEN {ord_b['transfer_content']},2026-11-27T14:01:00Z",
            f"FT263310003,9999999999,50000,SAI SO TAI KHOAN {ord_b['transfer_content']},2026-11-27T14:02:00Z",
            f"FT263310004,{receiving_acc},80000,KHACH QUEN GHI MA NOI DUNG,2026-11-27T14:03:00Z",
        ]
    )

    imp_res = client.post(
        "/api/admin/reconciliation/import",
        headers=adm_auth,
        json={"content": csv_statement, "format": "csv"},
    )
    assert imp_res.status_code == 200
    stats = imp_res.get_json()["data"]
    assert stats["auto_approved"] == 1
    assert stats["amount_mismatch"] == 1
    assert stats["account_mismatch"] == 1
    assert stats["unmatched"] == 1

    # User A got auto-credited 100.000 VND and card is ACTIVE!
    snap_a = client.get("/api/user/balance", headers={"Authorization": f"Bearer {tok_a}"}).get_json()["data"]
    assert snap_a["wallet"]["balance"] == 100000
    assert snap_a["card_status"] == "ACTIVE"

    # User B (transferred 45.000 instead of 50.000) was NOT auto-credited!
    snap_b = client.get("/api/user/balance", headers={"Authorization": f"Bearer {tok_b}"}).get_json()["data"]
    assert snap_b["wallet"]["balance"] == 0

    # Re-importing the exact same statement skips all duplicates (zero double-crediting!)
    imp_dup = client.post(
        "/api/admin/reconciliation/import",
        headers=adm_auth,
        json={"content": csv_statement, "format": "csv"},
    ).get_json()["data"]
    assert imp_dup["skipped_duplicates"] == 4
    assert imp_dup["auto_approved"] == 0
    assert client.get("/api/user/balance", headers={"Authorization": f"Bearer {tok_a}"}).get_json()["data"]["wallet"]["balance"] == 100000


def test_p2_04_parallel_manual_approval_and_auto_reconciliation_never_double_credit(app):
    _, tok_u, _, _ = register_and_login_user(app, "parallel_u", "par@example.com", "Trần Song Song")
    _, adm_token, _, _ = login_admin(app)
    client = app.test_client()
    u_auth = {"Authorization": f"Bearer {tok_u}"}
    adm_auth = {"Authorization": f"Bearer {adm_token}"}

    # Order 1: Manually approved by Admin first!
    ord_manual = client.post("/api/topups", headers=u_auth, json={"amount": 60000}).get_json()["data"]
    client.post(
        f"/api/admin/topups/{ord_manual['id']}/confirm",
        headers=adm_auth,
        json={"bank_reference": "MANUAL-REF-60K", "note": "Admin duyệt nhanh"},
    )
    assert client.get("/api/user/balance", headers=u_auth).get_json()["data"]["wallet"]["balance"] == 60000

    # Order 2: Pending 40.000 VND where customer forgot transfer code in bank memo
    ord_typo = client.post("/api/topups", headers=u_auth, json={"amount": 40000}).get_json()["data"]
    acc_num = ord_manual["bank"]["account_number"]

    # Bank statement arrives containing BOTH Order 1's transfer AND Order 2's code-less transfer
    json_statement = json.dumps(
        [
            {
                "bank_tx_id": "BANK-TX-FOR-ORD1",
                "account_number": acc_num,
                "amount_in": 60000,
                "description": f"IBFT {ord_manual['transfer_content']}",
            },
            {
                "bank_tx_id": "BANK-TX-TYPO-40K",
                "account_number": acc_num,
                "amount_in": 40000,
                "description": "TRAN SONG SONG NAP TIEN QUEN MA",
            },
        ]
    )
    rec = client.post(
        "/api/admin/reconciliation/import",
        headers=adm_auth,
        json={"content": json_statement, "format": "json"},
    ).get_json()["data"]

    assert rec["already_settled"] == 1
    assert rec["unmatched"] == 1
    assert rec["auto_approved"] == 0
    # Balance is STILL 60.000 VND (Order 1 was NOT credited a second time!)
    assert client.get("/api/user/balance", headers=u_auth).get_json()["data"]["wallet"]["balance"] == 60000

    # Now Admin manually links the UNMATCHED 40.000 VND bank statement entry to Order 2!
    overview = client.get("/api/admin/reconciliation?status=UNMATCHED", headers=adm_auth).get_json()["data"]
    unmatched_entry = overview["entries"][0]
    link_res = client.post(
        f"/api/admin/reconciliation/entries/{unmatched_entry['id']}/link",
        headers=adm_auth,
        json={"topup_id": ord_typo["id"]},
    )
    assert link_res.status_code == 200
    assert client.get("/api/user/balance", headers=u_auth).get_json()["data"]["wallet"]["balance"] == 100000


def test_p2_05_live_sepay_api_polling_and_user_auto_check_endpoint(app):
    _, tok_u, _, _ = register_and_login_user(app, "sepay_user", "sepay@example.com", "Ngô Thanh Tùng")
    _, adm_token, _, _ = login_admin(app)
    client = app.test_client()
    u_auth = {"Authorization": f"Bearer {tok_u}"}
    adm_auth = {"Authorization": f"Bearer {adm_token}"}

    # Admin configures SEPAY_API token
    cfg_res = client.put(
        "/api/admin/reconciliation/settings",
        headers=adm_auth,
        json={
            "provider": "SEPAY_API",
            "api_url": "https://api.mockbank.local/transactions",
            "api_token": "sepay-live-secret-token-1234",
            "auto_reconcile_enabled": 1,
            "background_poll_enabled": 0,
            "poll_interval_seconds": 20,
            "strict_account_match": 1,
        },
    )
    assert cfg_res.status_code == 200
    assert cfg_res.get_json()["data"]["api_token_masked"].endswith("1234")

    order = client.post("/api/topups", headers=u_auth, json={"amount": 50000}).get_json()["data"]

    fake_sepay_response = json.dumps(
        {
            "status": 200,
            "transactions": [
                {
                    "id": "990011",
                    "reference_number": "VCB-AUTO-990011",
                    "account_number": order["bank"]["account_number"],
                    "amount_in": "50000",
                    "transaction_content": f"CHUYEN TIEN {order['transfer_content']}",
                    "transaction_date": "2026-11-27 14:10:00",
                }
            ],
        }
    ).encode("utf-8")

    mock_http_resp = MagicMock()
    mock_http_resp.read.return_value = fake_sepay_response
    mock_http_resp.__enter__.return_value = mock_http_resp
    mock_http_resp.__exit__.return_value = False

    with patch.object(urllib.request, "urlopen", return_value=mock_http_resp):
        # User clicks "Đối chiếu giao dịch ngân hàng tự động ngay" -> polls bank API & auto-approves!
        chk = client.post(f"/api/topups/{order['id']}/auto-check", headers=u_auth)
        assert chk.status_code == 200
        chk_data = chk.get_json()["data"]
        assert chk_data["live_bank_api_checked"] is True
        assert chk_data["status"] == "SUCCESS"

    bal = client.get("/api/user/balance", headers=u_auth).get_json()["data"]
    assert bal["wallet"]["balance"] == 50000
    assert bal["card_status"] == "ACTIVE"


def test_p2_06_admin_custom_pass_plan_with_max_trips_cap(app):
    _, tok_u, _, _ = register_and_login_user(app, "capped_pass", "cap@example.com", "Đỗ Hải Đăng")
    _, adm_token, _, _ = login_admin(app)
    client = app.test_client()
    u_auth = {"Authorization": f"Bearer {tok_u}"}
    adm_auth = {"Authorization": f"Bearer {adm_token}"}

    # Admin creates a 2-trip promo pass costing 10.000 VND
    plan_res = client.post(
        "/api/admin/pass-plans",
        headers=adm_auth,
        json={
            "code": "PROMO_2_TRIPS",
            "name": "Gói Thử Nghiệm 2 Lượt",
            "badge_text": "2 LƯỢT",
            "price": 10000,
            "duration_days": 7,
            "max_trips": 2,
            "is_active": 1,
            "description": "Đi 2 lượt miễn phí qua cổng",
        },
    )
    assert plan_res.status_code == 200
    promo_plan = plan_res.get_json()["data"]["plan"]

    # Fund exact 10.000 VND & buy promo pass -> wallet balance = 0 VND
    ord_10k = client.post("/api/topups", headers=u_auth, json={"amount": 10000}).get_json()["data"]
    client.post(f"/api/admin/topups/{ord_10k['id']}/confirm", headers=adm_auth, json={"bank_reference": "PROMO-10K"})
    client.post("/api/passes/purchase", headers=u_auth, json={"plan_id": promo_plan["id"]})

    # Trip 1 -> 0 VND, pass still has 1 trip left -> qr_reissued = True
    t1 = client.post("/api/qr/issue", headers=u_auth).get_json()["data"]["qr_token"]
    s1 = client.post("/api/qr/scan", headers=adm_auth, json={"qr_token": t1}).get_json()["data"]
    assert s1["payment_method"] == "MONTHLY_PASS"
    assert s1["qr_reissued"] is True

    # Trip 2 -> 0 VND, max_trips (2) reached & wallet is 0 VND -> pass expires & card locks QR!
    t2 = client.post("/api/qr/issue", headers=u_auth).get_json()["data"]["qr_token"]
    s2 = client.post("/api/qr/scan", headers=adm_auth, json={"qr_token": t2}).get_json()["data"]
    assert s2["payment_method"] == "MONTHLY_PASS"
    assert s2["card_status_after"] == "INSUFFICIENT_BALANCE"
    assert s2["qr_reissued"] is False



def test_p2_07_split_single_vs_monthly_qr_tabs_home_pass_topup_dual_channels_and_admin_badges(app):
    _, tok_u, _, _ = register_and_login_user(app, "split_tester", "split@example.com", "Hoàng Tách Biệt")
    _, adm_token, _, _ = login_admin(app)
    client = app.test_client()
    u_auth = {"Authorization": f"Bearer {tok_u}"}
    adm_auth = {"Authorization": f"Bearer {adm_token}"}

    # 1. With 0 VND & no Monthly Pass, BOTH explicit SINGLE and MONTHLY QR tabs reject cleanly
    r_single_empty = client.post("/api/qr/issue", headers=u_auth, json={"ticket_mode": "SINGLE"})
    assert r_single_empty.status_code == 400
    assert r_single_empty.get_json()["error"]["code"] == "INSUFFICIENT_BALANCE"

    r_monthly_empty = client.post("/api/qr/issue", headers=u_auth, json={"ticket_mode": "MONTHLY"})
    assert r_monthly_empty.status_code == 400
    assert r_monthly_empty.get_json()["error"]["code"] == "NO_ACTIVE_PASS"

    # 2. Direct Monthly Pass AUTO Top-up (from Home Page module: purpose='MONTHLY_PASS', topup_channel='AUTO')
    plans = client.get("/api/passes/plans").get_json()["data"]["plans"]
    weekly_plan = next(p for p in plans if p["code"] == "WEEKLY_COMMUTER")  # 30.000 VND

    auto_pass_order = client.post(
        "/api/topups",
        headers=u_auth,
        json={
            "topup_channel": "AUTO",
            "purpose": "MONTHLY_PASS",
            "target_plan_id": weekly_plan["id"],
        },
    ).get_json()["data"]
    assert auto_pass_order["amount"] == 30000
    assert auto_pass_order["topup_channel"] == "AUTO"
    assert auto_pass_order["purpose"] == "MONTHLY_PASS"

    # Settle via Auto Bank Statement Reconciliation -> Auto-activates Monthly Pass AND marks TỰ ĐỘNG!
    acc_num = auto_pass_order["bank"]["account_number"]
    csv_auto = f"bank_tx_id,account_number,amount_in,description\nFT-AUTO-PASS-01,{acc_num},30000,CK {auto_pass_order['transfer_content']}"
    imp = client.post(
        "/api/admin/reconciliation/import",
        headers=adm_auth,
        json={"content": csv_auto, "format": "csv"},
    ).get_json()["data"]
    assert imp["auto_approved"] == 1

    # Verify Monthly Pass is now ACTIVE while Wallet Balance is 0 VND (spent on pass):
    # - MONTHLY tab issues QR & scans for 0 VND!
    # - SINGLE tab STILL rejects because Wallet Balance is 0 VND (< 4.000 VND)!
    m_qr = client.post("/api/qr/issue", headers=u_auth, json={"ticket_mode": "MONTHLY"})
    assert m_qr.status_code == 200
    assert m_qr.get_json()["data"]["ticket_mode"] == "MONTHLY"

    s_qr_still_empty = client.post("/api/qr/issue", headers=u_auth, json={"ticket_mode": "SINGLE"})
    assert s_qr_still_empty.status_code == 400

    # 3. Create a MANUAL Top-up Request (Cash at counter: topup_channel='MANUAL', 20.000 VND for Wallet)
    manual_order = client.post(
        "/api/topups",
        headers=u_auth,
        json={
            "topup_channel": "MANUAL",
            "payment_method_detail": "CASH_COUNTER",
            "purpose": "WALLET",
            "amount": 20000,
            "note": "Nộp tiền mặt tại Quầy Bến Thành #BL999",
        },
    ).get_json()["data"]
    assert manual_order["topup_channel"] == "MANUAL"
    assert manual_order["status"] == "PROCESSING"
    assert manual_order["qr"] is None  # Manual request has no auto bank QR

    # Even if bank statement has that code, MANUAL channel is NOT auto-approved by bank reconciler!
    csv_try = f"bank_tx_id,account_number,amount_in,description\nFT-TRY-MANUAL,{acc_num},20000,{manual_order['transfer_content']}"
    imp_manual = client.post(
        "/api/admin/reconciliation/import",
        headers=adm_auth,
        json={"content": csv_try, "format": "csv"},
    ).get_json()["data"]
    assert imp_manual["auto_approved"] == 0

    # Admin manually approves the cash request -> marks THỦ CÔNG ('MANUAL')!
    adm_conf = client.post(
        f"/api/admin/topups/{manual_order['id']}/confirm",
        headers=adm_auth,
        json={"bank_reference": "CASH-RECEIPT-BL999", "note": "Đã thu tiền mặt tại quầy"},
    ).get_json()["data"]
    assert adm_conf["approval_mode"] == "MANUAL"
    assert adm_conf["approval_badge_text"] == "THỦ CÔNG"

    # Now BOTH SINGLE QR (Wallet = 20.000 VND -> -4.000 VND) and MONTHLY QR (Pass -> 0 VND) work independently!
    s_tok = client.post("/api/qr/issue", headers=u_auth, json={"ticket_mode": "SINGLE"}).get_json()["data"]["qr_token"]
    m_tok = client.post("/api/qr/issue", headers=u_auth, json={"ticket_mode": "MONTHLY"}).get_json()["data"]["qr_token"]

    scan_single = client.post("/api/qr/scan", headers=adm_auth, json={"qr_token": s_tok}).get_json()["data"]
    assert scan_single["payment_method"] == "WALLET"
    assert scan_single["amount"] == 4000
    assert scan_single["balance_after"] == 16000

    scan_monthly = client.post("/api/qr/scan", headers=adm_auth, json={"qr_token": m_tok}).get_json()["data"]
    assert scan_monthly["payment_method"] == "MONTHLY_PASS"
    assert scan_monthly["amount"] == 0
    assert scan_monthly["balance_after"] == 16000

    # 4. Verify Admin Transactions endpoint marks TỰ ĐỘNG vs THỦ CÔNG and filters accurately!
    auto_txs = client.get("/api/admin/transactions?user=split_tester&approval_mode=AUTO", headers=adm_auth).get_json()["data"]["transactions"]
    manual_txs = client.get("/api/admin/transactions?user=split_tester&approval_mode=MANUAL", headers=adm_auth).get_json()["data"]["transactions"]

    assert len(auto_txs) >= 2
    assert all(t["approval_badge_text"] == "TỰ ĐỘNG" for t in auto_txs)
    assert len(manual_txs) == 1
    assert manual_txs[0]["approval_badge_text"] == "THỦ CÔNG"
    assert manual_txs[0]["amount"] == 20000


def test_p2_08_vehicle_plate_mandatory_priority_proof_upload_and_student_pass_gate(app):
    _, tok_u, _, _ = register_and_login_user(app, "sv_bk_2026", "svbk@example.com", "Nguyễn Minh Khôi")
    _, adm_token, _, _ = login_admin(app)
    client = app.test_client()
    u_auth = {"Authorization": f"Bearer {tok_u}"}
    adm_auth = {"Authorization": f"Bearer {adm_token}"}

    # 1. Update personal profile with Vehicle License Plate (Biển số xe)
    prof_res = client.put(
        "/api/user/profile",
        headers=u_auth,
        json={"full_name": "Nguyễn Minh Khôi", "phone": "0912345678", "vehicle_plate": "59-x3 686.88"},
    )
    assert prof_res.status_code == 200
    assert prof_res.get_json()["data"]["user"]["vehicle_plate"] == "59-X3 686.88"

    # Admin can search user directly by license plate
    adm_search = client.get("/api/admin/users?search=59-X3", headers=adm_auth).get_json()["data"]["users"]
    assert len(adm_search) == 1
    assert adm_search[0]["vehicle_plate"] == "59-X3 686.88"

    # 2. Student Discount Monthly Pass (MONTHLY_STUDENT) is HIDDEN from normal user in ALL user endpoints
    adm_plans = client.get("/api/admin/pass-plans", headers=adm_auth).get_json()["data"]["plans"]
    student_plan = next(p for p in adm_plans if p["code"] == "MONTHLY_STUDENT")
    assert student_plan["requires_priority"] == 1
    assert student_plan["price"] == 60000

    public_codes = {p["code"] for p in client.get("/api/passes/plans", headers=u_auth).get_json()["data"]["plans"]}
    my_pass_codes = {p["code"] for p in client.get("/api/passes/my", headers=u_auth).get_json()["data"]["plans"]}
    dash_codes = {p["code"] for p in client.get("/api/user/dashboard", headers=u_auth).get_json()["data"]["pass_plans"]}
    assert "MONTHLY_STUDENT" not in public_codes
    assert "MONTHLY_STUDENT" not in my_pass_codes
    assert "MONTHLY_STUDENT" not in dash_codes

    # Fund wallet 60.000 VND so balance is NOT the blocker — priority gate MUST block purchase & top-up!
    ord_60k = client.post("/api/topups", headers=u_auth, json={"amount": 60000}).get_json()["data"]
    client.post(
        f"/api/admin/topups/{ord_60k['id']}/confirm",
        headers=adm_auth,
        json={"bank_reference": "STMT-SV-60K"},
    )

    blocked_buy = client.post("/api/passes/purchase", headers=u_auth, json={"plan_id": student_plan["id"]})
    assert blocked_buy.status_code == 403
    assert blocked_buy.get_json()["error"]["code"] == "PRIORITY_REQUIRED"

    blocked_topup = client.post(
        "/api/topups",
        headers=u_auth,
        json={"topup_channel": "AUTO", "purpose": "MONTHLY_PASS", "target_plan_id": student_plan["id"]},
    )
    assert blocked_topup.status_code == 403
    assert blocked_topup.get_json()["error"]["code"] == "PRIORITY_REQUIRED"

    # 3. Priority Registration REQUIRES proof image upload (Thẻ sinh viên / Portal screenshot)
    missing_proof = client.post(
        "/api/user/priority",
        headers=u_auth,
        json={
            "priority_type": "STUDENT",
            "id_card_code": "22115678",
            "organization_name": "ĐH Bách Khoa TP.HCM",
            "proof_note": "Sinh viên chính quy K22",
        },
    )
    assert missing_proof.status_code == 400
    assert missing_proof.get_json()["error"]["code"] == "PROOF_IMAGE_REQUIRED"

    fake_png_data_uri = (
        "data:image/png;base64,"
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg=="
    )
    submit_ok = client.post(
        "/api/user/priority",
        headers=u_auth,
        json={
            "priority_type": "STUDENT",
            "id_card_code": "22115678",
            "organization_name": "ĐH Bách Khoa TP.HCM",
            "proof_note": "Ảnh chụp thẻ sinh viên & portal BK",
            "proof_image_data": fake_png_data_uri,
        },
    )
    assert submit_ok.status_code == 201
    prio_req_id = submit_ok.get_json()["data"]["id"]

    # While still PENDING (unapproved), MONTHLY_STUDENT MUST remain hidden and locked!
    pending_codes = {p["code"] for p in client.get("/api/passes/my", headers=u_auth).get_json()["data"]["plans"]}
    assert "MONTHLY_STUDENT" not in pending_codes
    still_blocked = client.post("/api/passes/purchase", headers=u_auth, json={"plan_id": student_plan["id"]})
    assert still_blocked.status_code == 403

    # 4. Admin inspects proof image & approves Priority Request -> unlocks MONTHLY_STUDENT!
    adm_reqs = client.get("/api/admin/priority-requests?status=PENDING", headers=adm_auth).get_json()["data"]["priority_requests"]
    target_req = next(r for r in adm_reqs if r["id"] == prio_req_id)
    assert target_req["proof_image_data"].startswith("data:image/png;base64,")
    assert target_req["vehicle_plate"] == "59-X3 686.88"

    rev_res = client.post(
        f"/api/admin/priority-requests/{prio_req_id}/review",
        headers=adm_auth,
        json={"action": "APPROVE", "admin_note": "Thẻ sinh viên hợp lệ"},
    )
    assert rev_res.status_code == 200
    assert rev_res.get_json()["data"]["status"] == "APPROVED"

    unlocked_codes = {p["code"] for p in client.get("/api/passes/my", headers=u_auth).get_json()["data"]["plans"]}
    assert "MONTHLY_STUDENT" in unlocked_codes

    # 5. Approved Student purchases MONTHLY_STUDENT (60.000 VND) and scans through Gate -> shows plate & priority!
    buy_sv = client.post("/api/passes/purchase", headers=u_auth, json={"plan_id": student_plan["id"]})
    assert buy_sv.status_code == 201
    assert buy_sv.get_json()["data"]["balance_after"] == 0

    m_tok = client.post("/api/qr/issue", headers=u_auth, json={"ticket_mode": "MONTHLY"}).get_json()["data"]["qr_token"]
    scan_res = client.post("/api/qr/scan", headers=adm_auth, json={"qr_token": m_tok}).get_json()["data"]
    assert scan_res["payment_method"] == "MONTHLY_PASS"
    assert scan_res["amount"] == 0
    assert scan_res["vehicle_plate"] == "59-X3 686.88"
    assert scan_res["priority_label"] == "Học sinh / Sinh viên"
