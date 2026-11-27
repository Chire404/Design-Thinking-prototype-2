/* VeloPass User Transit App Controller: Dynamic QR Lifecycle, Wallet Top-up & Real-time SSE */
(() => {
  VP.registerServiceWorker();

  const state = {
    activeView: "home",
    snapshot: null,
    qr: null, // { qr_token, qr_svg_data_uri, expires_at, ttl_seconds }
    qrTimerId: null,
    qrRequestInFlight: false,
    activeTopup: null,
    deferredInstallPrompt: null,
    sse: null,
  };

  const STATUS_LABELS = {
    ACTIVE: { text: "ĐANG HOẠT ĐỘNG", cls: "" },
    INSUFFICIENT_BALANCE: { text: "KHÔNG ĐỦ SỐ DƯ", cls: "badge--warn" },
    SUSPENDED: { text: "ĐÃ BỊ KHÓA", cls: "badge--danger" },
    PENDING: { text: "CHỜ XÁC MINH", cls: "badge--warn" },
  };

  // --- Navigation ---
  function switchView(viewName) {
    state.activeView = viewName;
    document.querySelectorAll(".app-view").forEach((v) => {
      v.classList.toggle("active", v.id === `view-${viewName}`);
    });
    document.querySelectorAll("[data-nav]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.nav === viewName);
    });
    if (viewName === "history") loadFullHistory();
    if (viewName === "account") loadSupportTickets();
    if (viewName === "passes") loadMyPasses();
    if (viewName === "card" && state.snapshot?.card?.qr_eligible && !state.qr) {
      issueDynamicQR(false);
    }
  }

  document.querySelectorAll("[data-nav]").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.nav));
  });
  document.querySelectorAll("[data-goto]").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.goto));
  });

  if (location.hash) {
    const target = location.hash.replace("#", "");
    if (["home", "card", "passes", "topup", "history", "account"].includes(target)) {
      switchView(target);
    }
  }

  // --- PWA Install Prompt ---
  window.addEventListener("beforeinstallprompt", (e) => {
    e.preventDefault();
    state.deferredInstallPrompt = e;
    document.getElementById("btn-install-pwa").style.display = "inline-flex";
    document.getElementById("btn-install-pwa-desktop").style.display = "inline-flex";
  });

  async function triggerInstallPWA() {
    if (!state.deferredInstallPrompt) return;
    state.deferredInstallPrompt.prompt();
    await state.deferredInstallPrompt.userChoice;
    state.deferredInstallPrompt = null;
    document.getElementById("btn-install-pwa").style.display = "none";
    document.getElementById("btn-install-pwa-desktop").style.display = "none";
  }
  document.getElementById("btn-install-pwa").onclick = triggerInstallPWA;
  document.getElementById("btn-install-pwa-desktop").onclick = triggerInstallPWA;

  // --- Render Dashboard Snapshot ---
  function renderSnapshot(snap) {
    state.snapshot = snap;
    const { user, wallet, card, system, recent_usage, recent_topups, notifications, unread_notifications } = snap;

    document.getElementById("ui-user-fullname").textContent = user.full_name;
    document.getElementById("ui-card-holder").textContent = user.full_name;
    document.getElementById("pass-holder").textContent = user.full_name;

    const vCard = document.getElementById("ui-virtual-card");
    vCard.dataset.status = card.status;

    const stMeta = STATUS_LABELS[card.status] || { text: card.status, cls: "badge--warn" };
    const badge1 = document.getElementById("ui-card-status-badge");
    const badge2 = document.getElementById("pass-status-badge");
    badge1.textContent = stMeta.text;
    badge1.className = `card-status-badge ${stMeta.cls}`;
    badge2.textContent = stMeta.text;
    badge2.className = `card-status-badge ${stMeta.cls}`;

    document.getElementById("ui-card-balance").textContent = wallet.balance_formatted;
    document.getElementById("pass-balance").textContent = wallet.balance_formatted;
    document.getElementById("topup-current-balance").textContent = wallet.balance_formatted;
    document.getElementById("fullscreen-balance").textContent = wallet.balance_formatted;
    const passBalEl = document.getElementById("pass-wallet-balance");
    if (passBalEl) passBalEl.textContent = wallet.balance_formatted;

    const passRibbon = document.getElementById("ui-card-pass-ribbon");
    if (snap.active_pass) {
      passRibbon.style.display = "inline-flex";
      passRibbon.innerHTML = `&#127915; ${VP.escapeHtml(snap.active_pass.plan_name.toUpperCase())} &bull; CÒN ${snap.active_pass.remaining_days} NGÀY (0 VNĐ/LƯỢT)`;
      document.getElementById("ui-card-trips").textContent =
        `Đang ưu tiên Vé Tháng (Đã đi ${snap.active_pass.trips_used} lượt • Miễn phí qua cổng)`;
      document.getElementById("pass-fare").textContent = "0 VNĐ (Vé Tháng)";
    } else {
      passRibbon.style.display = "none";
      document.getElementById("ui-card-trips").textContent =
        card.status === "ACTIVE"
          ? `Tương đương ~${wallet.remaining_trips} lượt đi (${system.fare_formatted}/lượt)`
          : `Cần tối thiểu ${system.fare_formatted} hoặc mua Vé Tháng để mở cổng`;
      document.getElementById("pass-fare").textContent = system.fare_formatted;
    }

    document.getElementById("ui-card-number").textContent = card.card_number;
    document.getElementById("pass-card-number").textContent = card.card_number;
    document.getElementById("fullscreen-card-num").textContent = card.card_number;
    document.getElementById("ui-card-issued").textContent = (card.issued_at || "").slice(0, 10);

    document.getElementById("ui-wallet-total-topup").textContent = wallet.total_topup_formatted;
    document.getElementById("ui-wallet-total-spent").textContent = wallet.total_spent_formatted;
    document.getElementById("ui-low-balance-banner").style.display = wallet.low_balance ? "block" : "none";

    // Profile inputs
    document.getElementById("acc-username").value = user.username;
    document.getElementById("acc-email").value = user.email;
    document.getElementById("acc-fullname").value = user.full_name;
    document.getElementById("acc-phone").value = user.phone || "";

    // Notification badge
    const nb = document.getElementById("ui-notif-badge");
    if (unread_notifications > 0) {
      nb.style.display = "inline-flex";
      nb.textContent = unread_notifications > 99 ? "99+" : String(unread_notifications);
    } else {
      nb.style.display = "none";
    }

    renderCompactUsage(recent_usage);
    renderCompactTopups(recent_topups);
    renderCompactNotifications(notifications, "ui-home-notifications", 5);
    renderCompactNotifications(notifications, "modal-notif-list", 30);

    syncQRDisplayWithCardState();
  }

  function renderCompactUsage(items) {
    const el = document.getElementById("ui-home-recent-usage");
    if (!items || !items.length) {
      el.innerHTML = `<div class="empty-state">Chưa có lượt quét thẻ nào.</div>`;
      return;
    }
    el.innerHTML = items
      .slice(0, 5)
      .map(
        (u) => `
        <div class="tx-item">
          <div>
            <div class="tx-item__title">Quét vé qua cổng</div>
            <div class="tx-item__sub">${VP.escapeHtml(u.transaction_code)} &bull; ${VP.formatDateTime(u.created_at)}</div>
          </div>
          <div class="tx-item__right">
            <div class="tx-item__amt text-danger">${VP.escapeHtml(u.amount_formatted)}</div>
            <div class="tx-item__sub">Còn ${VP.escapeHtml(u.balance_after_formatted)}</div>
          </div>
        </div>
      `
      )
      .join("");
  }

  function renderCompactTopups(items) {
    const el = document.getElementById("ui-home-recent-topups");
    if (!items || !items.length) {
      el.innerHTML = `<div class="empty-state">Chưa có giao dịch nạp tiền nào.</div>`;
      return;
    }
    el.innerHTML = items
      .slice(0, 5)
      .map(
        (t) => `
        <div class="tx-item" style="cursor:pointer;" data-open-topup="${t.id}">
          <div>
            <div class="tx-item__title">Nạp ví (${VP.escapeHtml(t.bank_short_name || "Bank")})</div>
            <div class="tx-item__sub">${VP.escapeHtml(t.transaction_code)} &bull; ${VP.formatDateTime(t.created_at)}</div>
          </div>
          <div class="tx-item__right">
            <div class="tx-item__amt text-success">+${VP.escapeHtml(t.amount_formatted)}</div>
            <span class="status-pill status-pill--${VP.escapeHtml(t.status)}">${VP.escapeHtml(t.status)}</span>
          </div>
        </div>
      `
      )
      .join("");

    el.querySelectorAll("[data-open-topup]").forEach((row) => {
      row.onclick = async () => {
        const id = Number(row.dataset.openTopup);
        try {
          const detail = await VP.api(`/api/topups/${id}`);
          showTopupOrder(detail);
          switchView("topup");
        } catch (err) {
          VP.toast(err.message, "danger");
        }
      };
    });
  }

  function renderCompactNotifications(items, containerId, limit) {
    const el = document.getElementById(containerId);
    if (!el) return;
    if (!items || !items.length) {
      el.innerHTML = `<div class="empty-state">Không có thông báo mới.</div>`;
      return;
    }
    el.innerHTML = items
      .slice(0, limit)
      .map(
        (n) => `
        <div class="tx-item" style="${n.is_read ? "" : "border-left:3px solid #0D9488;background:#F0FDFA;"}">
          <div>
            <div class="tx-item__title">${VP.escapeHtml(n.title)}</div>
            <div class="tx-item__sub">${VP.escapeHtml(n.message)}</div>
            <div class="tx-item__sub">${VP.formatDateTime(n.created_at)}</div>
          </div>
        </div>
      `
      )
      .join("");
  }

  // --- Dynamic QR State Machine ---
  function syncQRDisplayWithCardState() {
    const card = state.snapshot?.card;
    if (!card) return;

    const homeActive = document.getElementById("home-qr-active-box");
    const homeLocked = document.getElementById("home-qr-locked-box");
    const passActive = document.getElementById("pass-qr-active");
    const passLocked = document.getElementById("pass-qr-locked");

    if (card.status !== "ACTIVE") {
      // Immediately disable QR and clear any token in memory
      state.qr = null;
      homeActive.style.display = "none";
      passActive.style.display = "none";
      homeLocked.style.display = "block";
      passLocked.style.display = "block";
      document.getElementById("modal-fullscreen-qr").style.display = "none";

      let title = "Mã QR Đang Vô Hiệu Hóa";
      let desc = `Số dư hiện tại dưới ${card.fare_formatted}. Vui lòng nạp thêm tiền để kích hoạt thẻ.`;
      if (card.status === "SUSPENDED") {
        title = "Thẻ Đang Bị Tạm Khóa";
        desc = "Tài khoản đã bị Quản trị viên tạm khóa. Không thể sử dụng mã QR.";
      } else if (card.status === "PENDING") {
        title = "Thẻ Đang Chờ Xác Minh";
        desc = "Tài khoản đang chờ xác minh trước khi kích hoạt mã QR.";
      }

      document.getElementById("home-qr-locked-title").textContent = title;
      document.getElementById("home-qr-locked-desc").textContent = desc;
      document.getElementById("pass-locked-title").textContent = title;
      document.getElementById("pass-locked-desc").textContent = desc;
      return;
    }

    // Card is ACTIVE!
    homeLocked.style.display = "none";
    passLocked.style.display = "none";

    if (state.qr && state.qr.qr_svg_data_uri) {
      renderQRVisual(state.qr);
    } else {
      issueDynamicQR(false);
    }
  }

  function renderQRVisual(qrBundle) {
    state.qr = qrBundle;
    document.getElementById("home-qr-active-box").style.display = "flex";
    document.getElementById("pass-qr-active").style.display = "flex";
    document.getElementById("home-qr-img").src = qrBundle.qr_svg_data_uri;
    document.getElementById("pass-qr-img").src = qrBundle.qr_svg_data_uri;
    document.getElementById("fullscreen-qr-img").src = qrBundle.qr_svg_data_uri;
    updateQRCountdownTick();
  }

  async function issueDynamicQR(showToastOnSuccess = false) {
    if (state.qrRequestInFlight) return;
    if (!state.snapshot?.card?.qr_eligible) return;
    state.qrRequestInFlight = true;
    try {
      const qrData = await VP.api("/api/qr/issue", { method: "POST" });
      renderQRVisual(qrData);
      if (showToastOnSuccess) {
        VP.toast("Đã cấp mã QR động mới.", "success");
      }
    } catch (err) {
      if (err.code === "INSUFFICIENT_BALANCE" || err.code === "SUSPENDED_ACCOUNT") {
        await refreshDashboard();
      }
    } finally {
      state.qrRequestInFlight = false;
    }
  }

  function updateQRCountdownTick() {
    if (!state.qr || !state.qr.expires_at) return;
    const expiresMs = new Date(state.qr.expires_at).getTime();
    const remainSec = Math.max(0, Math.ceil((expiresMs - Date.now()) / 1000));
    const ttl = state.qr.ttl_seconds || 90;
    const pct = Math.min(100, Math.max(0, (remainSec / ttl) * 100));

    document.getElementById("home-qr-countdown").textContent = `Tự làm mới sau ${remainSec}s`;
    document.getElementById("pass-qr-countdown").textContent = `Tự động đổi mã sau ${remainSec}s`;
    document.getElementById("home-qr-progress").style.width = `${pct}%`;
    document.getElementById("pass-qr-progress").style.width = `${pct}%`;

    // Automatically rotate 2 seconds before expiration so User never holds an expired QR at the gate
    if (remainSec <= 2 && state.snapshot?.card?.qr_eligible) {
      issueDynamicQR(false);
    }
  }

  setInterval(updateQRCountdownTick, 1000);

  document.getElementById("btn-home-refresh-qr").onclick = () => issueDynamicQR(true);
  document.getElementById("btn-pass-refresh").onclick = () => issueDynamicQR(true);

  document.getElementById("btn-fullscreen-qr").onclick = () => {
    if (!state.qr) return VP.toast("Mã QR chưa khả dụng. Vui lòng nạp đủ tối thiểu 4.000 VNĐ.", "warning");
    document.getElementById("modal-fullscreen-qr").style.display = "flex";
  };
  document.getElementById("btn-close-fullscreen").onclick = () => {
    document.getElementById("modal-fullscreen-qr").style.display = "none";
  };
  document.getElementById("close-fullscreen-backdrop").onclick = () => {
    document.getElementById("modal-fullscreen-qr").style.display = "none";
  };

  // --- Top-up Flow ---
  document.querySelectorAll(".amt-chip").forEach((chip) => {
    chip.onclick = () => {
      document.querySelectorAll(".amt-chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      document.getElementById("topup-amount-input").value = chip.dataset.amt;
    };
  });

  function showTopupOrder(order) {
    state.activeTopup = order;
    const box = document.getElementById("active-topup-card");
    box.style.display = "block";

    const stEl = document.getElementById("topup-order-status");
    stEl.textContent = order.status;
    stEl.className = `status-pill status-pill--${order.status}`;

    const bank = order.bank || order;
    document.getElementById("topup-tx-code").textContent = order.transaction_code;
    document.getElementById("topup-bank-name").textContent = bank.bank_name || "";
    document.getElementById("topup-acc-num").textContent = bank.account_number || "";
    document.getElementById("topup-acc-holder").textContent = bank.account_holder || "";
    document.getElementById("topup-order-amount").textContent = order.amount_formatted || VP.formatVND(order.amount);
    document.getElementById("topup-transfer-content").textContent = order.transfer_content;
    const hintEl = document.getElementById("topup-auto-code-hint");
    if (hintEl) hintEl.textContent = order.transfer_content;
    document.getElementById("topup-instructions").textContent = bank.payment_instructions || "";

    const qrBox = box.querySelector(".topup-qr-box");
    const qrSrc = order.qr?.custom_bank_qr_url || order.qr?.qr_svg_data_uri || "";
    if (qrSrc && ["PENDING", "PROCESSING"].includes(order.status)) {
      qrBox.style.display = "flex";
      document.getElementById("topup-qr-img").src = qrSrc;
    } else {
      qrBox.style.display = "none";
    }

    const actions = document.getElementById("topup-pending-actions");
    actions.style.display = ["PENDING", "PROCESSING"].includes(order.status) ? "block" : "none";
    const repBtn = document.getElementById("btn-report-transferred");
    if (repBtn) repBtn.style.display = order.status === "PENDING" ? "block" : "none";
    const cancelBtn = document.getElementById("btn-cancel-topup");
    if (cancelBtn) cancelBtn.style.display = order.status === "PENDING" ? "block" : "none";
  }

  document.getElementById("form-create-topup").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("btn-create-topup");
    btn.disabled = true;
    try {
      const amount = Number(document.getElementById("topup-amount-input").value);
      const order = await VP.api("/api/topups", { method: "POST", body: { amount } });
      showTopupOrder(order);
      VP.toast(`Đã tạo đơn nạp ${order.amount_formatted}. Hãy quét mã VietQR để chuyển khoản.`, "success");
      refreshDashboard();
    } catch (err) {
      VP.toast(err.message, "danger");
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("btn-copy-acc").onclick = () => {
    if (state.activeTopup) {
      const acc = state.activeTopup.bank?.account_number || state.activeTopup.account_number;
      VP.copyText(acc, "số tài khoản");
    }
  };
  document.getElementById("btn-copy-amt").onclick = () => {
    if (state.activeTopup) VP.copyText(String(state.activeTopup.amount), "số tiền");
  };
  document.getElementById("btn-copy-content").onclick = () => {
    if (state.activeTopup) VP.copyText(state.activeTopup.transfer_content, "nội dung chuyển khoản");
  };

  document.getElementById("btn-report-transferred").onclick = async () => {
    if (!state.activeTopup) return;
    try {
      const updated = await VP.api(`/api/topups/${state.activeTopup.id}/report-transferred`, {
        method: "POST",
        body: { note: "Khách hàng xác nhận đã chuyển khoản trên ứng dụng" },
      });
      showTopupOrder(updated);
      VP.toast("Đã gửi yêu cầu đối soát. Ví sẽ tự cộng tiền ngay khi ngân hàng/Admin xác nhận.", "info");
      refreshDashboard();
    } catch (err) {
      VP.toast(err.message, "danger");
    }
  };

  document.getElementById("btn-cancel-topup").onclick = async () => {
    if (!state.activeTopup) return;
    try {
      const updated = await VP.api(`/api/topups/${state.activeTopup.id}/cancel`, { method: "POST" });
      showTopupOrder(updated);
      VP.toast("Đã hủy đơn nạp tiền.", "warning");
      refreshDashboard();
    } catch (err) {
      VP.toast(err.message, "danger");
    }
  };

  const btnAutoCheck = document.getElementById("btn-auto-check-topup");
  if (btnAutoCheck) {
    btnAutoCheck.onclick = async () => {
      if (!state.activeTopup) return;
      btnAutoCheck.disabled = true;
      try {
        const detail = await VP.api(`/api/topups/${state.activeTopup.id}/auto-check`, { method: "POST" });
        showTopupOrder(detail);
        if (detail.status === "SUCCESS") {
          VP.toast("Đã đối chiếu khớp giao dịch ngân hàng — Cộng tiền tự động thành công!", "success");
          refreshDashboard();
        } else {
          VP.toast(
            detail.live_bank_api_checked
              ? "Đã quét tài khoản ngân hàng nhưng chưa thấy giao dịch khớp mã nội dung."
              : "Hệ thống đang lắng nghe đối soát tự động (và hàng đợi Admin song song).",
            "info"
          );
        }
      } catch (err) {
        VP.toast(err.message, "danger");
      } finally {
        btnAutoCheck.disabled = false;
      }
    };
  }

  // --- Monthly Pass Management (Prototype 2) ---
  async function loadMyPasses() {
    try {
      const data = await VP.api("/api/passes/my");
      const activeBox = document.getElementById("ui-active-pass-body");
      const pill = document.getElementById("my-pass-status-pill");

      if (data.active_pass) {
        const p = data.active_pass;
        pill.style.display = "inline-block";
        activeBox.innerHTML = `
          <div class="active-pass-hero">
            <div style="font-size:0.75rem;opacity:0.85;letter-spacing:0.06em;">MÃ VÉ THÁNG: ${VP.escapeHtml(p.pass_code)}</div>
            <div style="font-size:1.22rem;font-weight:900;margin:4px 0 10px;">${VP.escapeHtml(p.plan_name)}</div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:0.84rem;background:rgba(0,0,0,0.2);padding:10px;border-radius:12px;">
              <div>Hết hạn: <strong>${VP.formatDateTime(p.expires_at)}</strong></div>
              <div>Thời gian còn: <strong>${p.remaining_days} ngày</strong></div>
              <div>Lượt đã đi: <strong>${VP.escapeHtml(p.trips_label)}</strong></div>
              <div>Ưu đãi cổng: <strong>0 VNĐ / lượt</strong></div>
            </div>
            <label style="display:flex;align-items:center;gap:8px;margin-top:12px;font-size:0.84rem;cursor:pointer;">
              <input type="checkbox" id="chk-pass-autorenew" ${p.auto_renew ? "checked" : ""} />
              <span>Tự động gia hạn bằng số dư ví khi hết hạn</span>
            </label>
          </div>
        `;
        document.getElementById("chk-pass-autorenew").onchange = async (ev) => {
          try {
            await VP.api(`/api/passes/${p.id}/auto-renew`, {
              method: "POST",
              body: { auto_renew: ev.target.checked },
            });
            VP.toast("Đã lưu cài đặt tự động gia hạn Vé Tháng.", "success");
          } catch (err) {
            VP.toast(err.message, "danger");
          }
        };
      } else {
        pill.style.display = "none";
        activeBox.innerHTML = `
          <div class="empty-state">
            Bạn chưa có gói Vé Tháng đang hiệu lực. Đăng ký ngay bên cạnh để đi lại không giới hạn (0 VNĐ/lượt quét)!
          </div>
        `;
      }

      const plansEl = document.getElementById("ui-pass-plans-list");
      plansEl.innerHTML = data.plans
        .map(
          (pl) => `
          <div class="plan-card">
            <div class="plan-card__top">
              <div>
                <span class="pass-chip">${VP.escapeHtml(pl.badge_text)} &bull; ${pl.duration_days} NGÀY</span>
                <div class="plan-card__name">${VP.escapeHtml(pl.name)}</div>
              </div>
              <div class="plan-card__price">${VP.escapeHtml(pl.price_formatted)}</div>
            </div>
            <p class="vp-muted" style="margin:6px 0 12px;">${VP.escapeHtml(pl.description)} (~${VP.escapeHtml(pl.daily_cost_formatted)}/ngày)</p>
            <button type="button" class="vp-btn vp-btn--primary vp-btn--block vp-btn--sm" data-buy-plan="${pl.id}" data-plan-name="${VP.escapeHtml(pl.name)}" data-plan-price="${VP.escapeHtml(pl.price_formatted)}">
              ${data.active_pass ? "Gia hạn cộng dồn thêm " + pl.duration_days + " ngày" : "Đăng ký ngay bằng Số dư Ví"}
            </button>
          </div>
        `
        )
        .join("");

      plansEl.querySelectorAll("[data-buy-plan]").forEach((btn) => {
        btn.onclick = async () => {
          const planId = Number(btn.dataset.buyPlan);
          if (!confirm(`Xác nhận thanh toán ${btn.dataset.planPrice} từ Ví VeloPass để đăng ký "${btn.dataset.planName}"?`)) {
            return;
          }
          btn.disabled = true;
          try {
            const res = await VP.api("/api/passes/purchase", {
              method: "POST",
              body: { plan_id: planId, auto_renew: false },
            });
            if (res.next_qr) state.qr = res.next_qr;
            VP.toast(`Kích hoạt ${btn.dataset.planName} thành công!`, "success");
            await refreshDashboard();
            await loadMyPasses();
          } catch (err) {
            VP.toast(err.message, "danger");
            if (err.code === "INSUFFICIENT_BALANCE") {
              switchView("topup");
            }
          } finally {
            btn.disabled = false;
          }
        };
      });

      const histEl = document.getElementById("ui-pass-history-list");
      histEl.innerHTML = data.passes.length
        ? data.passes
            .map(
              (h) => `
              <div class="tx-item">
                <div>
                  <div class="tx-item__title">${VP.escapeHtml(h.plan_name)}</div>
                  <div class="tx-item__sub">${VP.escapeHtml(h.pass_code)} &bull; Hạn: ${VP.formatDateTime(h.expires_at)}</div>
                  <div class="tx-item__sub">Đã sử dụng: ${VP.escapeHtml(h.trips_label)}</div>
                </div>
                <div class="tx-item__right">
                  <div class="tx-item__amt text-primary">${VP.escapeHtml(h.price_paid_formatted)}</div>
                  <span class="status-pill status-pill--${h.status === "ACTIVE" ? "SUCCESS" : "EXPIRED"}">${VP.escapeHtml(h.status)}</span>
                </div>
              </div>`
            )
            .join("")
        : `<div class="empty-state">Chưa có lịch sử mua Vé Tháng.</div>`;
    } catch (err) {
      VP.toast(err.message, "danger");
    }
  }

  // --- Full Transaction History ---
  async function loadFullHistory() {
    try {
      const data = await VP.api("/api/user/transactions");
      const uEl = document.getElementById("htab-usage");
      const tEl = document.getElementById("htab-topup");
      const lEl = document.getElementById("htab-ledger");

      uEl.innerHTML = data.usages.length
        ? data.usages
            .map(
              (u) => `
            <div class="tx-item">
              <div>
                <div class="tx-item__title">Quét QR qua cổng &bull; ${VP.escapeHtml(u.transaction_code)}</div>
                <div class="tx-item__sub">Thời gian: ${VP.formatDateTime(u.created_at)}</div>
                <div class="tx-item__sub">Số dư: ${VP.escapeHtml(u.balance_before_formatted)} &rarr; <strong>${VP.escapeHtml(u.balance_after_formatted)}</strong></div>
              </div>
              <div class="tx-item__right">
                <div class="tx-item__amt text-danger">${VP.escapeHtml(u.amount_formatted)}</div>
                <span class="status-pill status-pill--SUCCESS">${VP.escapeHtml(u.status)}</span>
              </div>
            </div>`
            )
            .join("")
        : `<div class="empty-state">Chưa có lịch sử sử dụng thẻ.</div>`;

      tEl.innerHTML = data.topups.length
        ? data.topups
            .map(
              (t) => `
            <div class="tx-item">
              <div>
                <div class="tx-item__title">${VP.escapeHtml(t.transaction_code)} &bull; ${VP.escapeHtml(t.bank_name)}</div>
                <div class="tx-item__sub">Nội dung CK: <strong>${VP.escapeHtml(t.transfer_content)}</strong></div>
                <div class="tx-item__sub">Tạo lúc: ${VP.formatDateTime(t.created_at)}</div>
              </div>
              <div class="tx-item__right">
                <div class="tx-item__amt text-success">+${VP.escapeHtml(t.amount_formatted)}</div>
                <span class="status-pill status-pill--${VP.escapeHtml(t.status)}">${VP.escapeHtml(t.status)}</span>
              </div>
            </div>`
            )
            .join("")
        : `<div class="empty-state">Chưa có lịch sử nạp tiền.</div>`;

      lEl.innerHTML = data.ledger.length
        ? data.ledger
            .map(
              (l) => `
            <div class="tx-item">
              <div>
                <div class="tx-item__title">${VP.escapeHtml(l.description)}</div>
                <div class="tx-item__sub">${VP.escapeHtml(l.transaction_code)} &bull; ${VP.formatDateTime(l.created_at)}</div>
                <div class="tx-item__sub">${VP.escapeHtml(l.balance_before_formatted)} &rarr; <strong>${VP.escapeHtml(l.balance_after_formatted)}</strong></div>
              </div>
              <div class="tx-item__right">
                <div class="tx-item__amt ${l.signed_amount_formatted.startsWith("-") ? "text-danger" : "text-success"}">
                  ${VP.escapeHtml(l.signed_amount_formatted)}
                </div>
              </div>
            </div>`
            )
            .join("")
        : `<div class="empty-state">Sổ cái chưa có biến động.</div>`;
    } catch (err) {
      VP.toast(err.message, "danger");
    }
  }

  document.querySelectorAll(".h-tab").forEach((tab) => {
    tab.onclick = () => {
      document.querySelectorAll(".h-tab").forEach((t) => t.classList.remove("active"));
      tab.classList.add("active");
      ["usage", "topup", "ledger"].forEach((name) => {
        document.getElementById(`htab-${name}`).style.display = tab.dataset.htab === name ? "block" : "none";
      });
    };
  });

  // --- Notifications Modal ---
  document.getElementById("btn-open-notifications").onclick = () => {
    document.getElementById("modal-notifications").style.display = "flex";
  };
  document.getElementById("btn-close-notif").onclick = () => {
    document.getElementById("modal-notifications").style.display = "none";
  };
  document.getElementById("close-notif-backdrop").onclick = () => {
    document.getElementById("modal-notifications").style.display = "none";
  };

  async function markAllRead() {
    await VP.api("/api/user/notifications/read-all", { method: "POST" });
    refreshDashboard();
  }
  document.getElementById("btn-mark-all-read").onclick = markAllRead;
  document.getElementById("btn-mark-read-inline").onclick = markAllRead;

  // --- Account, Password & Support ---
  document.getElementById("form-profile").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await VP.api("/api/user/profile", {
        method: "PUT",
        body: {
          full_name: document.getElementById("acc-fullname").value.trim(),
          phone: document.getElementById("acc-phone").value.trim(),
        },
      });
      VP.toast("Đã lưu thông tin cá nhân.", "success");
      refreshDashboard();
    } catch (err) {
      VP.toast(err.message, "danger");
    }
  });

  document.getElementById("form-change-pw").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await VP.api("/api/auth/change-password", {
        method: "POST",
        body: {
          current_password: document.getElementById("pw-current").value,
          new_password: document.getElementById("pw-new").value,
        },
      });
      e.target.reset();
      VP.toast("Đổi mật khẩu thành công.", "success");
    } catch (err) {
      VP.toast(err.message, "danger");
    }
  });

  async function loadSupportTickets() {
    try {
      const res = await VP.api("/api/user/support");
      const el = document.getElementById("ui-support-list");
      if (!res.support_requests.length) {
        el.innerHTML = `<div class="empty-state">Chưa có yêu cầu hỗ trợ nào.</div>`;
        return;
      }
      el.innerHTML = res.support_requests
        .map(
          (s) => `
          <div class="tx-item">
            <div>
              <div class="tx-item__title">#${s.id} &bull; ${VP.escapeHtml(s.subject)}</div>
              <div class="tx-item__sub">${VP.escapeHtml(s.message)}</div>
              ${s.admin_note ? `<div class="tx-item__sub text-primary">Phản hồi Admin: ${VP.escapeHtml(s.admin_note)}</div>` : ""}
            </div>
            <span class="status-pill">${VP.escapeHtml(s.status)}</span>
          </div>`
        )
        .join("");
    } catch (_) {}
  }

  document.getElementById("form-support").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await VP.api("/api/user/support", {
        method: "POST",
        body: {
          subject: document.getElementById("sup-subject").value.trim(),
          message: document.getElementById("sup-message").value.trim(),
        },
      });
      e.target.reset();
      VP.toast("Đã gửi yêu cầu hỗ trợ tới Quản trị viên.", "success");
      loadSupportTickets();
    } catch (err) {
      VP.toast(err.message, "danger");
    }
  });

  async function doLogout() {
    await VP.api("/api/auth/logout", { method: "POST" }).catch(() => {});
    window.location.href = "/login";
  }
  document.getElementById("btn-logout-rail").onclick = doLogout;
  document.getElementById("btn-logout-mobile").onclick = doLogout;

  // --- Real-Time SSE Connection (Instant Scan & Topup Updates!) ---
  function connectRealtimeSSE() {
    if (!window.EventSource) return;
    if (state.sse) state.sse.close();

    const es = new EventSource("/api/notifications/stream");
    state.sse = es;

    es.addEventListener("card_scanned", (ev) => {
      const payload = JSON.parse(ev.data);
      const scanText =
        payload.payment_method === "MONTHLY_PASS"
          ? `Qua cổng miễn phí 0 VNĐ bằng Vé Tháng! Số dư ví giữ nguyên ${payload.balance_formatted}.`
          : `Đã trừ ${VP.formatVND(payload.amount)} qua cổng. Số dư còn ${payload.balance_formatted}.`;
      VP.toast(scanText, "success", "Quét thẻ thành công!");
      if (payload.next_qr) {
        state.qr = payload.next_qr;
      } else {
        state.qr = null;
      }
      refreshDashboard();
      if (state.activeView === "passes") loadMyPasses();
    });

    es.addEventListener("pass_updated", (ev) => {
      const payload = JSON.parse(ev.data);
      if (payload.next_qr) state.qr = payload.next_qr;
      refreshDashboard();
      if (state.activeView === "passes") loadMyPasses();
    });

    es.addEventListener("topup_confirmed", (ev) => {
      const payload = JSON.parse(ev.data);
      VP.toast(
        `Ví vừa được cộng +${VP.formatVND(payload.amount)}. Số dư mới: ${payload.balance_formatted}!`,
        "success",
        "Nạp tiền thành công!"
      );
      if (payload.next_qr) {
        state.qr = payload.next_qr;
      }
      if (state.activeTopup && state.activeTopup.id === payload.topup_id) {
        state.activeTopup.status = "SUCCESS";
        showTopupOrder(state.activeTopup);
      }
      refreshDashboard();
    });

    es.addEventListener("account_status_changed", (ev) => {
      const payload = JSON.parse(ev.data);
      if (payload.user_status === "SUSPENDED") {
        state.qr = null;
        VP.toast(`Tài khoản đã bị tạm khóa: ${payload.reason}`, "danger", "Thông báo bảo mật");
      } else {
        if (payload.next_qr) state.qr = payload.next_qr;
        VP.toast("Tài khoản & thẻ điện tử đã được mở khóa trở lại!", "success");
      }
      refreshDashboard();
    });

    es.onerror = () => {
      document.getElementById("ui-sse-status").style.opacity = "0.6";
    };
    es.onopen = () => {
      document.getElementById("ui-sse-status").style.opacity = "1";
    };
  }

  async function refreshDashboard() {
    try {
      const snap = await VP.api("/api/user/dashboard");
      renderSnapshot(snap);
    } catch (err) {
      VP.toast(err.message, "danger");
    }
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refreshDashboard();
  });

  // Boot User App
  refreshDashboard().then(() => connectRealtimeSSE());
})();
