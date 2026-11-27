# Hướng Dẫn Cấu Hình Tên Miền HTTPS Thực Tế Cho VeloPass

Trình duyệt trên điện thoại (iOS Safari, Android Chrome) và máy tính **bắt buộc sử dụng giao thức `HTTPS`** (khi truy cập qua tên miền hoặc IP ngoài `localhost`) để:
1. Cấp quyền truy cập **Camera thật** (`navigator.mediaDevices.getUserMedia`) trên màn hình **Admin QR Scanner**.
2. Cài đặt ứng dụng **Progressive Web App (PWA)** lên màn hình chính điện thoại.
3. Bảo mật cookie phiên đăng nhập (`Secure`, `HttpOnly`) và kết nối thời gian thực **SSE**.

Hệ thống **VeloPass** đã tích hợp sẵn `Werkzeug ProxyFix` để nhận diện chính xác `X-Forwarded-Proto: https` và IP thực của người dùng sau mọi Reverse Proxy / Tunnel.

---

## Phương Án 1: Chạy HTTPS Ngay Trên Máy Hiện Tại Bằng Cloudflare Tunnel (Không Cần Mở Port Router)

### 1A. Đường dẫn HTTPS miễn phí tức thì (`https://*.trycloudflare.com`)
Dùng để kiểm thử ngay trên nhiều điện thoại thật (cả 4G/5G lẫn Wi-Fi) chỉ với 1 lệnh:

```powershell
# Cửa sổ 1: Chạy VeloPass WSGI Server
.\.venv\Scripts\python.exe run.py --prod

# Cửa sổ 2: Mở đường hầm HTTPS trực tiếp
powershell -ExecutionPolicy Bypass -File .\scripts\start_https_tunnel.ps1
```
Màn hình sẽ xuất hiện đường dẫn dạng `https://xxxx-xxxx.trycloudflare.com` có chứng chỉ TLS hợp lệ của Cloudflare.

### 1B. Gắn Tên Miền Riêng Vĩnh Viễn (VD: `https://velopass.tenmiencuaban.vn`) Qua Cloudflare Zero Trust
Nếu bạn đã sở hữu tên miền riêng và trỏ Nameserver về **Cloudflare** (gói Free hoàn toàn miễn phí):

1. Truy cập bảng điều khiển **Cloudflare Zero Trust** (`https://one.dash.cloudflare.com/`) -> mục **Networks** -> **Tunnels**.
2. Bấm **Create a tunnel** -> chọn **Cloudflared** -> đặt tên `velopass-prod`.
3. Sao chép chuỗi **Tunnel Token** (`eyJhIjoi...`).
4. Ở bước **Public Hostname**:
   - **Subdomain**: `velopass` (hoặc để trống nếu dùng domain gốc)
   - **Domain**: chọn tên miền của bạn (ví dụ `tenmiencuaban.vn`)
   - **Service Type**: `HTTP`
   - **URL**: `127.0.0.1:5000`
5. Bấm **Save hostname** — Cloudflare tự động tạo bản ghi DNS CNAME và cấp chứng chỉ SSL `HTTPS` cho `https://velopass.tenmiencuaban.vn`.
6. Trên máy chủ Windows của bạn, chạy:
   ```powershell
   $env:CLOUDFLARE_TUNNEL_TOKEN="eyJhIjoi..."
   powershell -ExecutionPolicy Bypass -File .\scripts\start_https_tunnel.ps1
   ```
   *(Hoặc cài đặt thành Windows Service tự chạy cùng máy bằng lệnh quản trị: `.\tools\cloudflared.exe service install <TOKEN>`).*

---

## Phương Án 2: Triển Khai Tên Miền Riêng Trên VPS Linux Với Caddy (Tự Cấp & Gia Hạn Let's Encrypt SSL)

Nếu bạn thuê VPS có IP tĩnh (Viettel IDC, Vietnix, Vultr, DigitalOcean, AWS EC2...):

1. Trỏ bản ghi DNS **A** của tên miền (ví dụ `velopass.tenmiencuaban.vn`) về địa chỉ IPv4 của VPS và mở cổng `80`, `443`.
2. Trên VPS, clone repository và tạo file `.env`:
   ```bash
   git clone https://github.com/Chire404/Design-Thinking-project-th-v-xe.git
   cd Design-Thinking-project-th-v-xe
   cp .env.example .env
   # Chỉnh sửa SECRET_KEY và QR_HMAC_SECRET ngẫu nhiên >= 32 ký tự trong .env
   ```
3. Khởi chạy toàn bộ ứng dụng kèm **Caddy HTTPS Reverse Proxy** (đã cấu hình sẵn tắt bộ đệm `flush_interval -1` cho luồng Realtime SSE trong `deploy/Caddyfile`):
   ```bash
   VELOPASS_DOMAIN=velopass.tenmiencuaban.vn docker compose -f deploy/docker-compose.https.yml up -d --build
   ```
4. Khởi tạo tài khoản `SUPER_ADMIN` đầu tiên an toàn:
   ```bash
   docker exec -it velopass-app python manage.py create-admin --username admin --email admin@tenmiencuaban.vn
   ```
5. Truy cập ngay `https://velopass.tenmiencuaban.vn` — Caddy tự động xin và gia hạn chứng chỉ SSL Let's Encrypt hoàn toàn tự động!
