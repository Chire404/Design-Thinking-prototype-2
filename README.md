# VeloPass (Prototype 2) — Thẻ Vé Điện Tử, Vé Tháng (Monthly Pass) & Đối Soát Ngân Hàng Tự Động

**VeloPass Prototype 2** mở rộng hệ thống thẻ giao thông điện tử cá nhân (**Virtual Transit E-Card**) với **Gói Vé Tháng / Vé Tuần (Monthly Pass)** và **Hệ thống Đối Chiếu Tài Khoản Ngân Hàng Tự Động (Auto-Approve Top-up)** vận hành **song song** với cơ chế **Admin duyệt thủ công**.

---

## Điểm nâng cấp nổi bật ở Prototype 2

1. **Hệ thống Vé Tháng Điện Tử (`Monthly Pass` — `/api/passes/*`)**:
   - Cung cấp các gói vé định kỳ cấu hình động bởi Admin (*Vé Tháng Tiêu Chuẩn 100.000 VNĐ/30 ngày*, *Vé Tháng HSSV 60.000 VNĐ/30 ngày*, *Vé Tuần Linh Hoạt 30.000 VNĐ/7 ngày*).
   - Hành khách đăng ký trực tiếp bằng **Số dư Ví** (hỗ trợ **gia hạn cộng dồn ngày nối tiếp** không mất ngày còn lại và tùy chọn **tự động gia hạn**).
   - **Ưu tiên quét 0 VNĐ & giữ mở QR**: Ngay cả khi số dư ví bằng `0 VNĐ`, nếu hành khách đang có **Vé Tháng hiệu lực**, thẻ vẫn duy trì trạng thái `ACTIVE`, cấp Dynamic QR bình thường và mỗi lượt Admin quét qua cổng tự động ưu tiên trừ **`0 VNĐ` (`MONTHLY_PASS`)** trước khi dùng đến tiền trong ví.

2. **Đối Chiếu Tài Khoản Ngân Hàng Tự Động song song Duyệt Thủ Công (`/api/admin/reconciliation/*`)**:
   - **Tự động đối chiếu 3 lớp**: Kiểm tra đồng thời **Số tài khoản ngân hàng nhận** + **Mã nội dung chuyển khoản riêng (`VP...`)** + **Khớp chính xác 100% số tiền VNĐ**.
   - **Đa nguồn dữ liệu sao kê**:
     - Quét API trực tuyến định kỳ hoặc tức thì từ **SePay API**, **Casso API**, hoặc **Custom Bank JSON API**.
     - Tải lên / Dán trực tiếp **file sao kê CSV / JSON** xuất từ E-Banking để tự động duyệt hàng loạt đơn khớp trong 1 cú nhấp chuột.
   - **Song song an toàn với Duyệt thủ công**: Admin vẫn có thể duyệt/từ chối thủ công bất kỳ đơn `PENDING`/`PROCESSING` nào. Nếu đơn đã được Admin duyệt trước đó, bộ đối soát tự động nhận diện `ALREADY_SETTLED` (chống cộng tiền hai lần tuyệt đối). Các giao dịch lệch số tiền (`AMOUNT_MISMATCH`) hoặc ghi sai nội dung (`UNMATCHED`) được giữ lại để Admin ghép đơn thủ công có kiểm soát.

---

## 1. Giới thiệu dự án

VeloPass được thiết kế thành hai phân hệ độc lập nhưng đồng bộ thời gian thực (**Real-time SSE**):

- **USER APP (`/app`)**: Trải nghiệm ứng dụng thẻ giao thông điện tử ưu tiên di động (*Mobile-first PWA*) gồm thẻ điện tử cá nhân VeloPass, mã QR động tự xoay vòng, ví số dư VNĐ, tạo đơn nạp tiền VietQR tự động điền nội dung riêng, lịch sử sử dụng/nạp tiền, thông báo thời gian thực và gửi yêu cầu hỗ trợ.
- **ADMIN APP (`/admin`)**: Trung tâm điều hành & thiết bị kiểm soát cổng vé gồm Dashboard 8 chỉ số KPI + biểu đồ theo Ngày/Tuần/Tháng, **Camera QR Scanner thật** (tự nhận diện qua `BarcodeDetector` & `jsQR`, âm báo cổng kiểm soát, khóa chống quét trùng lặp), đối soát duyệt nạp tiền từ sao kê, quản lý khóa/mở khóa User, sổ cái giao dịch, cấu hình ngân hàng/giá vé (có lưu lịch sử giá) và nhật ký kiểm toán (**Audit Logs**).

### Quy tắc cốt lõi (4.000 VNĐ / lượt)
1. Mỗi lượt Admin quét mã QR hợp lệ trừ chính xác **4.000 VNĐ** (mặc định, có thể cấu hình và lưu lịch sử thay đổi giá) bên trong một **Database Transaction (`BEGIN IMMEDIATE`)** nguyên tử.
2. Token QR được băm **SHA-256** trong cơ sở dữ liệu, không chứa thông tin cá nhân hay số dư trong mã QR, có thời hạn ngắn (`90 giây`) và **chỉ sử dụng thành công đúng một lần**.
3. Sau khi trừ tiền:
   - Nếu số dư mới **>= 4.000 VNĐ** (kể cả đúng `4.000 VNĐ`), thẻ duy trì trạng thái `ACTIVE` và hệ thống lập tức cấp token QR mới đẩy về màn hình User qua **SSE** mà không cần tải lại trang.
   - Nếu số dư mới **< 4.000 VNĐ** (ví dụ `3.999 VNĐ` hoặc `0 VNĐ`), thẻ lập tức chuyển sang `INSUFFICIENT_BALANCE` và vô hiệu hóa mã QR.
4. Khi User nạp thêm tiền hợp lệ và số dư đạt từ `4.000 VNĐ` trở lên, thẻ và mã QR tự động kích hoạt trở lại.

---

## 2. Kiến trúc hệ thống

```text
+-----------------------------------+       +-----------------------------------+
|         USER APP (PWA)            |       |     ADMIN OPS & GATE SCANNER      |
|  - Virtual Card + Dynamic QR      |       |  - Real Camera (getUserMedia)     |
|  - Wallet & VietQR Top-up         |       |  - BarcodeDetector + jsQR Engine  |
|  - Bottom Nav (Mobile) / Rail     |       |  - Dashboard, Users, Bank, Audit  |
+-----------------+-----------------+       +-----------------+-----------------+
                  |  HTTPS / Fetch + CSRF                     |  HTTPS + Idempotency-Key
                  |  SSE (/api/notifications/stream)          |  SSE (/api/admin/stream)
                  v                                           v
+-------------------------------------------------------------------------------+
|                        FLASK APPLICATION & SECURITY LAYER                     |
|  - Server-side Revocable Sessions (HttpOnly Cookie + Bearer Token + CSRF)     |
|  - Role-based Authorization (@require_auth / @require_admin)                  |
|  - Sliding-window Rate Limiter + Brute-force Account Lockout                  |
|  - HMAC-SHA256 Signed Payment Webhook & HMAC-Signed QR Token Pre-Verification |
+---------------------------------------+---------------------------------------+
                                        |
                                        v
+-------------------------------------------------------------------------------+
|               SQLITE WAL RELATIONAL ENGINE (ATOMIC TRANSACTIONS)              |
|  BEGIN IMMEDIATE -> Conditional Token Claim -> Conditional Balance Deduct     |
|  -> Immutable WalletTransactions Ledger -> UsageTransactions (UNIQUE Key)     |
|  -> Next Dynamic QR / Card Lock -> AuditLogs & ScanLogs -> COMMIT             |
+-------------------------------------------------------------------------------+
```

---

## 3. Công nghệ sử dụng

- **Ngôn ngữ & Backend**: Python 3.11+ (`Flask 3`, `Werkzeug` Scrypt password hashing, `Waitress` multi-threaded production WSGI server).
- **Cơ sở dữ liệu**: SQLite ở chế độ **WAL (Write-Ahead Logging)** với khóa giao dịch `BEGIN IMMEDIATE`, ràng buộc `FOREIGN KEY`, `CHECK (balance >= 0)`, `UNIQUE` index từng phần đảm bảo tối đa 1 token QR `ACTIVE` trên mỗi thẻ, lưu trữ toàn bộ tiền tệ dạng **số nguyên 64-bit (`INTEGER`)** cho VNĐ.
- **QR & Thanh toán**:
  - `qrcode` (Vector SVG Data URI sắc nét tuyệt đối trên mọi màn hình).
  - Bộ sinh chuẩn **EMVCo / Napas247 VietQR** kèm mã kiểm tra **CRC-16/CCITT-FALSE** giúp ứng dụng ngân hàng quét tự điền đúng số tài khoản, số tiền và nội dung chuyển khoản riêng.
- **Frontend & PWA**: Vanilla ES6+, CSS Variables Mobile-first Design System, Server-Sent Events (`EventSource`), Web Audio API (âm báo máy quét), `navigator.mediaDevices.getUserMedia` + `BarcodeDetector` + `jsQR` cục bộ, Web App Manifest + Safe Service Worker.
- **Kiểm thử & CI**: `pytest` (31 bài kiểm thử tích hợp bao phủ toàn bộ 30+ kịch bản yêu cầu, bao gồm kiểm thử đa luồng `threading.Barrier` chống trừ tiền hai lần) & GitHub Actions CI.

---

## 4. Cấu trúc thư mục

```text
Design-Thinking-project-th-v-xe/
├── .env.example                    # Mẫu biến môi trường (không chứa khóa bí mật thật)
├── .github/workflows/ci.yml        # GitHub Actions kiểm tra cú pháp, migration & chạy pytest
├── Dockerfile                      # Đóng gói container triển khai thực tế
├── requirements.txt                # Danh sách thư viện Python
├── pyproject.toml                  # Cấu hình dự án & pytest
├── manage.py                       # CLI quản trị: migrate, create-admin, seed, check-health
├── run.py                          # Điểm khởi chạy server (Dev & Waitress Production)
├── backend/
│   ├── __init__.py                 # App Factory, Security Headers, CORS, Error Handlers
│   ├── config.py                   # Quản lý cấu hình từ biến môi trường
│   ├── database.py                 # Quản lý kết nối SQLite WAL, BEGIN IMMEDIATE & Migration runner
│   ├── migrations/
│   │   └── 001_initial_schema.sql  # Lược đồ 16 bảng chuẩn hóa
│   ├── utils/
│   │   ├── formatters.py           # Định dạng VNĐ (100.000 VNĐ), sinh mã thẻ & mã giao dịch
│   │   ├── qr_svg.py               # Render QR SVG & tạo chuỗi chuẩn EMVCo VietQR
│   │   ├── realtime.py             # Bộ điều phối thông báo thời gian thực (SSE Broker)
│   │   └── security.py             # Scrypt hash, QR HMAC, Webhook HMAC, Rate Limiter
│   ├── services/
│   │   ├── auth_service.py         # Đăng ký, đăng nhập, chống brute-force, quên mật khẩu
│   │   ├── card_wallet_service.py  # Đồng bộ trạng thái thẻ, ví và thông báo
│   │   ├── qr_service.py           # Cấp QR động & Transaction 17 bước trừ 4.000 VNĐ
│   │   ├── topup_service.py        # Tạo đơn nạp, xử lý Webhook chống lặp & Admin duyệt sao kê
│   │   └── admin_service.py        # Thống kê, quản lý User, cấu hình Ngân hàng/Giá vé, Audit Log
│   └── routes/                     # Các Blueprint API (/api/auth, /api/user, /api/topups, /api/qr, /api/admin)
├── frontend/
│   ├── templates/                  # Giao diện Auth, User Transit App, Admin Control Center
│   └── static/                     # CSS, JS, PWA Manifest, Service Worker, Icons & jsQR
└── tests/
    ├── conftest.py                 # Fixture môi trường kiểm thử độc lập
    └── test_full_system.py         # Bộ kiểm thử tự động toàn diện (30+ test cases)
```

---

## 5. Hướng dẫn cài đặt dependencies

Yêu cầu: **Python 3.11 trở lên**.

```powershell
# 1. Tạo môi trường ảo Python
python -m venv .venv

# 2. Kích hoạt môi trường ảo
# Trên Windows PowerShell:
.\.venv\Scripts\Activate.ps1
# Trên Linux / macOS:
# source .venv/bin/activate

# 3. Cài đặt các thư viện cần thiết
pip install -r requirements.txt
```

---

## 6. Hướng dẫn cấu hình `.env`

Sao chép file `.env.example` thành `.env` và điền thông số phù hợp:

```powershell
Copy-Item .env.example .env
```

Các biến quan trọng trong `.env`:
- `APP_ENV`: `development` hoặc `production` (khi đặt `production`, hệ thống bắt buộc `SECRET_KEY` và `QR_HMAC_SECRET` phải có độ dài tối thiểu 32 ký tự và khác giá trị mẫu).
- `SECRET_KEY`: Chuỗi ngẫu nhiên bí mật bảo vệ ứng dụng (sinh bằng `python -c "import secrets; print(secrets.token_hex(32))"`).
- `QR_HMAC_SECRET`: Khóa ký HMAC riêng cho Dynamic QR Token.
- `PAYMENT_WEBHOOK_SECRET`: Khóa bí mật xác thực chữ ký HMAC-SHA256 từ cổng webhook ngân hàng (để trống nếu chưa kết nối cổng tự động và sử dụng chế độ Admin duyệt sao kê thủ công).
- `DATABASE_PATH`: Đường dẫn file cơ sở dữ liệu SQLite (mặc định `data/velopass.db`).

---

## 7. Hướng dẫn kết nối Database

Hệ thống sử dụng SQLite ở chế độ **WAL (`Write-Ahead Logging`)** tích hợp sẵn trong Python:
- Tự động tạo thư mục `data/` và file database theo biến `DATABASE_PATH` trong `.env`.
- Bật sẵn `PRAGMA foreign_keys = ON` và `PRAGMA busy_timeout = 30000` trên mọi kết nối.
- Không cần cài đặt database server rời khi phát triển cục bộ hoặc triển khai single-node.

---

## 8. Hướng dẫn chạy Migration

```powershell
python manage.py migrate
python manage.py check-health
```

*(Tùy chọn cho môi trường `development`)* — Khởi tạo nhanh tài khoản mẫu để trải nghiệm:

```powershell
python manage.py seed
```

---

## 9. Hướng dẫn khởi động dự án & chạy kiểm thử

### Chạy bộ kiểm thử tự động (30+ kịch bản)
```powershell
pytest -v
```

### Khởi động máy chủ Development
```powershell
python run.py
```
Truy cập tại: `http://127.0.0.1:5000` (hoặc `http://localhost:5000` để cấp quyền Camera trình duyệt).

### Khởi động máy chủ Production WSGI (Waitress đa luồng)
```powershell
python run.py --prod
```

---

## 10. Hướng dẫn tạo tài khoản Admin ban đầu an toàn

Hệ thống **tuyệt đối không mở form đăng ký Admin công khai** và chặn mọi hành vi tự nâng quyền qua API. Để khởi tạo tài khoản `SUPER_ADMIN` đầu tiên một cách an toàn trên máy chủ, sử dụng CLI:

```powershell
python manage.py create-admin --username admin --email admin@domain.vn --full-name "Quản trị viên Hệ thống"
```
Hệ thống sẽ yêu cầu nhập mật khẩu ẩn trên terminal (tối thiểu 8 ký tự) và băm bằng thuật toán `scrypt`.

---

## 11. Hướng dẫn tích hợp Ngân hàng hoặc xác nhận Top-up thủ công

### Phương án A: Chế độ Admin đối soát sao kê thủ công (Mặc định khi chưa cấu hình Webhook)
1. Admin vào **Admin App -> Ngân Hàng & Cấu Hình** để cập nhật đúng **Tên ngân hàng**, **Mã BIN Napas**, **Số tài khoản** và **Chủ tài khoản**.
2. Khi User tạo đơn nạp tiền, hệ thống sinh mã **VietQR Napas247** kèm **Nội dung chuyển khoản duy nhất** (ví dụ `VP8A3K92M10P`).
3. User chuyển khoản thật trên ứng dụng ngân hàng và có thể bấm *"Tôi đã chuyển khoản"* (chuyển trạng thái sang `PROCESSING` — **tuyệt đối chưa cộng tiền**).
4. Admin kiểm tra sao kê ngân hàng thực tế, vào mục **Duyệt Nạp Tiền**, nhập **Mã giao dịch sao kê (Bank Reference)** và bấm **Xác nhận** -> Hệ thống cộng tiền vào ví, kích hoạt lại thẻ/QR ngay lập tức và lưu vào **Audit Logs**.

### Phương án B: Tích hợp tự động qua Payment Webhook (SePay / Casso / Gateway tương thích)
1. Khai báo `PAYMENT_WEBHOOK_SECRET` trong `.env`.
2. Cấu hình nhà cung cấp gửi `POST /api/payment/webhook` kèm header chữ ký `X-Webhook-Signature: sha256=<hmac_sha256_hex_of_raw_body>` và JSON body:
   ```json
   {
     "event_id": "EVT-20261126-0001",
     "bank_reference": "FT26330998877",
     "amount": 100000,
     "content": "NGUYEN VAN A CHUYEN KHOAN VP8A3K92M10P"
   }
   ```
3. Backend tự động đối chiếu chữ ký HMAC, chống gọi trùng lặp qua bảng `payment_webhook_events`, kiểm tra khớp tuyệt đối số tiền và mã nội dung chuyển khoản trước khi cộng ví.

---

## 12. Hướng dẫn triển khai Production & Tên miền HTTPS thực tế

Xem hướng dẫn chi tiết từng bước tại **[docs/HTTPS_DOMAIN_SETUP.md](docs/HTTPS_DOMAIN_SETUP.md)** (bao gồm cả gắn tên miền riêng qua **Cloudflare Zero Trust Tunnel** lẫn triển khai **VPS + Caddy tự cấp SSL Let's Encrypt** với `deploy/docker-compose.https.yml` và `scripts/start_https_tunnel.ps1`).

1. **Bắt buộc bật HTTPS**: Trình duyệt di động và máy tính chỉ cấp quyền Camera (`navigator.mediaDevices.getUserMedia`) cho trang Admin QR Scanner khi chạy trên **HTTPS** (hoặc `localhost`). Đặt **Caddy**, **Nginx** hoặc **Cloudflare Tunnel** phía trước `Waitress` (`127.0.0.1:5000`).
2. **Cấu hình Nginx hỗ trợ SSE thời gian thực**:
   ```nginx
   location / {
       proxy_pass http://127.0.0.1:5000;
       proxy_http_version 1.1;
       proxy_set_header Host $host;
       proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
       proxy_set_header X-Forwarded-Proto $scheme;
       proxy_buffering off;
       proxy_read_timeout 3600s;
   }
   ```
3. **Chạy qua Docker**:
   ```bash
   docker build -t velopass-app .
   docker run -d -p 5000:5000 --env-file .env -v velopass_data:/app/data velopass-app
   ```
4. **Sao lưu dữ liệu**: Đặt lịch sao lưu định kỳ file database SQLite bằng lệnh an toàn `sqlite3 data/velopass.db ".backup 'backup/velopass.db'"`.
