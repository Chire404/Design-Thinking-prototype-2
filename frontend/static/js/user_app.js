/* VeloPass Prototype 2.6 User App Controller:
 * - Merged Bottom Nav ("Thẻ của tôi") + preserved top 2-tab switch (Tab Vé Lượt | Tab Vé Tháng)
 * - Vehicle License Plate in Personal Profile & Card display
 * - Priority Passenger Registration with mandatory proof image upload & Student Pass unlock
 */
(() => {
  VP.registerServiceWorker();

  const state = {
    activeView: "home",
    ticketSubTab: "SINGLE", // 'SINGLE' | 'MONTHLY' inside #view-tickets
    homeQrMode: "SINGLE",
    topupChannel: "AUTO",
    snapshot: null,
    qrSingle: null,
    qrMonthly: null,
    qrInFlight: { SINGLE: false, MONTHLY: false },
    activeTopup: null,
    proofImageDataUri: "",
    deferredInstallPrompt: null,
    sse: null,
  };

  const STATUS_LABELS = {
    ACTIVE: { text: "ĐANG HOẠT ĐỘNG", cls: "" },
    INSUFFICIENT_BALANCE: { text: "CHƯA ĐỦ SỐ DƯ", cls: "badge--warn" },
    SUSPENDED: { text: "ĐÃ BỊ KHÓA", cls: "badge--danger" },
    PENDING: { text: "CHỜ XÁC MINH", cls: "badge--warn" },
  };

  // --- Switch Unified Ticket Sub-tab (Blue Box: Tab Vé Lượt | Tab Vé Tháng) ---
  function switchTicketSubTab(mode) {
    state.ticketSubTab = mode === "MONTHLY" ? "MONTHLY" : "SINGLE";
    document.querySelectorAll("[data-ticket-subtab]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.ticketSubtab === state.ticketSubTab);
    });
    document.getElementById("subtab-panel-single").style.display = state.ticketSubTab === "SINGLE" ? "block" : "none";
    document.getElementById("subtab-panel-monthly").style.display = state.ticketSubTab === "MONTHLY" ? "block" : "none";

    if (state.ticketSubTab === "MONTHLY") {
      loadMyPasses();
      if (state.snapshot?.active_pass && !state.qrMonthly) issueModeQR("MONTHLY", false);
    } else {
      syncAllQRScreens();
    }
  }

  document.querySelectorAll("[data-ticket-subtab]").forEach((btn) => {
    btn.addEventListener("click", () => switchTicketSubTab(btn.dataset.ticketSubtab));
  });

  // --- Main View Navigation ---
  function switchView(viewName, optionalTicketTab = null) {
    if (viewName === "card" || viewName === "single-ticket") {
      viewName = "tickets";
      optionalTicketTab = optionalTicketTab || "SINGLE";
    } else if (viewName === "passes" || viewName === "monthly-pass") {
      viewName = "tickets";
      optionalTicketTab = optionalTicketTab || "MONTHLY";
    }
    state.activeView = viewName;

    document.querySelectorAll(".app-view").forEach((v) => {
      v.classList.toggle("active", v.id === `view-${viewName}`);
    });
    document.querySelectorAll("[data-nav]").forEach((btn) => {
      btn.classList.toggle("active", btn.dataset.nav === viewName);
    });

    if (viewName === "tickets") {
      switchTicketSubTab(optionalTicketTab || state.ticketSubTab);
    }
    if (viewName === "history") loadFullHistory();
    if (viewName === "account") {
      loadSupportTickets();
      loadPriorityRegistrations();
    }
  }

  document.querySelectorAll("[data-nav]").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.nav));
  });
  document.querySelectorAll("[data-goto]").forEach((btn) => {
    btn.addEventListener("click", () => switchView(btn.dataset.goto));
  });
  document.querySelectorAll("[data-goto-ticket]").forEach((btn) => {
    btn.addEventListener("click", () => switchView("tickets", btn.dataset.gotoTicket));
  });

  if (location.hash) {
    const h = location.hash.replace("#", "");
    if (["home", "tickets", "card", "passes", "topup", "history", "account"].includes(h)) {
      switchView(h);
    }
  }

  // --- Top-up Channel Tabs (AUTO vs MANUAL) ---
  function switchTopupChannel(chan, presetPurpose = null, presetPlanId = null) {
    state.topupChannel = chan === "MANUAL" ? "MANUAL" : "AUTO";
    document.querySelectorAll("[data-topup-chan]").forEach((b) => {
      b.classList.toggle("active", b.dataset.topupChan === state.topupChannel);
    });
    document.getElementById("panel-topup-auto").style.display = state.topupChannel === "AUTO" ? "block" : "none";
    document.getElementById("panel-topup-manual").style.display = state.topupChannel === "MANUAL" ? "block" : "none";

    if (presetPurpose) {
      if (state.topupChannel === "AUTO") {
        const sel = document.getElementById("auto-topup-purpose");
        sel.value = presetPurpose;
        sel.dispatchEvent(new Event("change"));
        if (presetPlanId) document.getElementById("auto-topup-plan").value = String(presetPlanId);
      } else {
        const sel = document.getElementById("manual-topup-purpose");
        sel.value = presetPurpose;
        sel.dispatchEvent(new Event("change"));
        if (presetPlanId) {
          document.getElementById("manual-topup-plan").value = String(presetPlanId);
          syncManualPlanAmount();
        }
      }
    }
  }

  document.querySelectorAll("[data-topup-chan]").forEach((btn) => {
    btn.addEventListener("click", () => switchTopupChannel(btn.dataset.topupChan));
  });

  // --- Home QR Mode Switcher ---
  document.querySelectorAll("[data-home-qrmode]").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.homeQrMode = btn.dataset.homeQrmode;
      document.querySelectorAll("[data-home-qrmode]").forEach((b) => {
        b.classList.toggle("active", b.dataset.homeQrmode === state.homeQrMode);
      });
      syncAllQRScreens();
    });
  });

  // --- PWA Install ---
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

  // --- Populate Plan Dropdowns (Already filtered by Backend for Priority Eligibility!) ---
  function populatePlanDropdowns(plans) {
    if (!plans || !plans.length) return;
    const optsHtml = plans
      .map(
        (p) =>
          `<option value="${p.id}" data-price="${p.price}">${VP.escapeHtml(p.name)} — ${VP.escapeHtml(p.price_formatted)}</option>`
      )
      .join("");
    ["home-pass-plan-select", "auto-topup-plan", "manual-topup-plan"].forEach((id) => {
      const el = document.getElementById(id);
      if (el) {
        const prev = el.value;
        el.innerHTML = optsHtml;
        if (prev && Array.from(el.options).some((o) => o.value === prev)) {
          el.value = prev;
        }
      }
    });
  }

  function syncManualPlanAmount() {
    const purpose = document.getElementById("manual-topup-purpose").value;
    const planSel = document.getElementById("manual-topup-plan");
    const amtInput = document.getElementById("manual-amount-input");
    if (purpose === "MONTHLY_PASS" && planSel.selectedOptions[0]) {
      amtInput.value = planSel.selectedOptions[0].dataset.price;
      amtInput.readOnly = true;
    } else {
      amtInput.readOnly = false;
    }
  }

  document.getElementById("auto-topup-purpose").addEventListener("change", (e) => {
    const isPass = e.target.value === "MONTHLY_PASS";
    document.getElementById("auto-plan-field").style.display = isPass ? "block" : "none";
    document.getElementById("auto-amount-block").style.display = isPass ? "none" : "block";
  });

  document.getElementById("manual-topup-purpose").addEventListener("change", (e) => {
    const isPass = e.target.value === "MONTHLY_PASS";
    document.getElementById("manual-plan-field").style.display = isPass ? "block" : "none";
    syncManualPlanAmount();
  });
  document.getElementById("manual-topup-plan").addEventListener("change", syncManualPlanAmount);

  // --- Home Page Direct Monthly Pass Top-up ---
  document.getElementById("btn-home-pass-auto-topup").onclick = async () => {
    const planId = Number(document.getElementById("home-pass-plan-select").value);
    if (!planId) return;
    try {
      const order = await VP.api("/api/topups", {
        method: "POST",
        body: { topup_channel: "AUTO", purpose: "MONTHLY_PASS", target_plan_id: planId },
      });
      switchView("topup");
      switchTopupChannel("AUTO", "MONTHLY_PASS", planId);
      showTopupOrder(order);
      VP.toast(`Đã tạo mã VietQR nạp ${order.amount_formatted} cho Vé Tháng!`, "success");
    } catch (err) {
      VP.toast(err.message, "danger");
    }
  };

  document.getElementById("btn-home-pass-manual-topup").onclick = () => {
    const planId = Number(document.getElementById("home-pass-plan-select").value);
    switchView("topup");
    switchTopupChannel("MANUAL", "MONTHLY_PASS", planId);
    VP.toast("Hãy chọn hình thức thanh toán thủ công và gửi Request cho Admin.", "info");
  };

  document.getElementById("btn-home-pass-wallet-buy").onclick = async () => {
    const sel = document.getElementById("home-pass-plan-select");
    const planId = Number(sel.value);
    const label = sel.selectedOptions[0]?.textContent || "Gói Vé Tháng";
    if (!planId) return;
    if (!confirm(`Thanh toán trực tiếp từ Số dư Ví hiện có để kích hoạt "${label}"?`)) return;
    try {
      const res = await VP.api("/api/passes/purchase", {
        method: "POST",
        body: { plan_id: planId, auto_renew: false },
      });
      if (res.next_qr) state.qrMonthly = res.next_qr;
      state.homeQrMode = "MONTHLY";
      document.querySelectorAll("[data-home-qrmode]").forEach((b) => {
        b.classList.toggle("active", b.dataset.homeQrmode === "MONTHLY");
      });
      VP.toast("Đã kích hoạt Vé Tháng từ Số dư Ví thành công!", "success");
      await refreshDashboard();
    } catch (err) {
      VP.toast(err.message, "danger");
    }
  };

  // --- Render Snapshot ---
  function renderSnapshot(snap) {
    state.snapshot = snap;
    const { user, wallet, card, active_pass, pass_plans, system, recent_usage, recent_topups, notifications, unread_notifications } = snap;

    populatePlanDropdowns(pass_plans || []);

    document.getElementById("ui-user-fullname").textContent = user.full_name;
    document.getElementById("ui-card-holder").textContent = user.full_name;
    document.getElementById("ui-card-plate").textContent = user.vehicle_plate || "Chưa ĐK";
    document.getElementById("single-plate").textContent = user.vehicle_plate || "Chưa cập nhật";

    // Header Priority Chip
    const prioChip = document.getElementById("ui-header-prio-chip");
    const promoHint = document.getElementById("ui-priority-promo-hint");
    if (user.priority_status === "APPROVED") {
      prioChip.style.display = "inline-block";
      prioChip.textContent = `Ưu tiên: ${user.priority_label}`;
      if (promoHint) promoHint.style.display = "none";
    } else {
      prioChip.style.display = "none";
      if (promoHint) promoHint.style.display = "block";
    }

    const vCard = document.getElementById("ui-virtual-card");
    vCard.dataset.status = card.status;

    const stMeta = STATUS_LABELS[card.status] || { text: card.status, cls: "badge--warn" };
    const badge1 = document.getElementById("ui-card-status-badge");
    badge1.textContent = stMeta.text;
    badge1.className = `card-status-badge ${stMeta.cls}`;

    const singleEligible = user.status === "ACTIVE" && wallet.status === "ACTIVE" && wallet.balance >= system.fare_amount;
    const singleBadge = document.getElementById("single-status-badge");
    singleBadge.textContent = singleEligible ? "VÉ LƯỢT SẴN SÀNG" : "CHƯA ĐỦ SỐ DƯ";
    singleBadge.className = `card-status-badge ${singleEligible ? "" : "badge--warn"}`;
    document.getElementById("single-card-number").textContent = card.card_number;
    document.getElementById("single-balance").textContent = wallet.balance_formatted;
    document.getElementById("single-fare").textContent = `${system.fare_formatted} / lượt`;

    const monthlyBadge = document.getElementById("monthly-status-badge");
    if (active_pass) {
      monthlyBadge.textContent = `CÒN ${active_pass.remaining_days} NGÀY`;
      monthlyBadge.className = "status-pill status-pill--SUCCESS";
    } else {
      monthlyBadge.textContent = "CHƯA KÍCH HOẠT";
      monthlyBadge.className = "status-pill";
    }

    document.getElementById("ui-card-balance").textContent = wallet.balance_formatted;
    document.getElementById("pass-wallet-balance").textContent = wallet.balance_formatted;
    document.getElementById("fullscreen-balance").textContent = wallet.balance_formatted;

    const passRibbon = document.getElementById("ui-card-pass-ribbon");
    const homePassText = document.getElementById("home-pass-summary-text");
    if (active_pass) {
      passRibbon.style.display = "inline-flex";
      passRibbon.innerHTML = `&#127915; ${VP.escapeHtml(active_pass.plan_name.toUpperCase())} &bull; CÒN ${active_pass.remaining_days} NGÀY`;
      homePassText.innerHTML = `<strong class="text-success">Đang có: ${VP.escapeHtml(active_pass.plan_name)} (còn ${active_pass.remaining_days} ngày)</strong> — Nạp thêm sẽ cộng dồn ngày!`;
    } else {
      passRibbon.style.display = "none";
      homePassText.textContent = "Bạn chưa có Vé Tháng — Nạp ngay bên dưới để quét cổng 0 VNĐ không giới hạn lượt!";
    }

    document.getElementById("ui-card-trips").textContent =
      singleEligible
        ? `Ví Vé Lượt tương đương ~${wallet.remaining_trips} lượt (${system.fare_formatted}/lượt)`
        : `Ví Vé Lượt dưới ${system.fare_formatted}`;

    document.getElementById("ui-card-number").textContent = card.card_number;
    document.getElementById("fullscreen-card-num").textContent = card.card_number;
    document.getElementById("ui-card-issued").textContent = (card.issued_at || "").slice(0, 10);

    document.getElementById("ui-wallet-total-topup").textContent = wallet.total_topup_formatted;
    document.getElementById("ui-wallet-total-spent").textContent = wallet.total_spent_formatted;
    document.getElementById("ui-low-balance-banner").style.display = wallet.low_balance ? "block" : "none";

    // Profile & Vehicle Plate fields
    document.getElementById("acc-username").value = user.username;
    document.getElementById("acc-email").value = user.email;
    document.getElementById("acc-fullname").value = user.full_name;
    document.getElementById("acc-phone").value = user.phone || "";
    document.getElementById("acc-vehicle-plate").value = user.vehicle_plate || "";

    renderPrioritySummaryBanner(user);

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

    syncAllQRScreens();
  }

  function renderPrioritySummaryBanner(user) {
    const badge = document.getElementById("prio-status-badge");
    const banner = document.getElementById("prio-current-banner");
    const form = document.getElementById("form-priority-reg");
    const st = user.priority_status || "NONE";

    if (st === "APPROVED") {
      badge.textContent = "ĐÃ PHÊ DUYỆT";
      badge.className = "status-pill status-pill--SUCCESS";
      banner.style.display = "block";
      banner.className = "vp-alert vp-alert--success";
      banner.innerHTML = `<strong>Đã kích hoạt ưu tiên:</strong> ${VP.escapeHtml(user.priority_label)} &bull; Mã thẻ: <strong>${VP.escapeHtml(user.priority_card_code)}</strong> (${VP.escapeHtml(user.priority_org)}). Bạn đã được mở khóa gói <strong>Vé Tháng Ưu Đãi HSSV</strong>!`;
      form.style.display = "none";
    } else if (st === "PENDING") {
      badge.textContent = "CHỜ XÉT DUYỆT";
      badge.className = "status-pill status-pill--PROCESSING";
      banner.style.display = "block";
      banner.className = "vp-alert vp-alert--warning";
      banner.innerHTML = `Hồ sơ đăng ký <strong>${VP.escapeHtml(user.priority_label)}</strong> (${VP.escapeHtml(user.priority_card_code)}) đang chờ Quản trị viên kiểm tra bằng chứng.`;
      form.style.display = "none";
    } else if (st === "REJECTED") {
      badge.textContent = "CẦN BỔ SUNG";
      badge.className = "status-pill status-pill--FAILED";
      banner.style.display = "block";
      banner.className = "vp-alert vp-alert--danger";
      banner.innerHTML = `Hồ sơ trước đó chưa được duyệt. Vui lòng tải lại ảnh chụp bằng chứng rõ nét để gửi lại.`;
      form.style.display = "block";
    } else {
      badge.textContent = "CHƯA ĐĂNG KÝ";
      badge.className = "status-pill";
      banner.style.display = "none";
      form.style.display = "block";
    }
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
            <div class="tx-item__title">${u.payment_method === "MONTHLY_PASS" ? "Quét cổng Vé Tháng" : "Quét cổng Vé Lượt"}</div>
            <div class="tx-item__sub">${VP.escapeHtml(u.transaction_code)} &bull; ${VP.formatDateTime(u.created_at)}</div>
          </div>
          <div class="tx-item__right">
            <div class="tx-item__amt ${u.payment_method === "MONTHLY_PASS" ? "text-success" : "text-danger"}">${VP.escapeHtml(u.amount_formatted)}</div>
            <div class="tx-item__sub">Ví: ${VP.escapeHtml(u.balance_after_formatted)}</div>
          </div>
        </div>`
      )
      .join("");
  }

  function renderCompactTopups(items) {
    const el = document.getElementById("ui-home-recent-topups");
    if (!items || !items.length) {
      el.innerHTML = `<div class="empty-state">Chưa có yêu cầu nạp tiền nào.</div>`;
      return;
    }
    el.innerHTML = items
      .slice(0, 5)
      .map(
        (t) => `
        <div class="tx-item" style="cursor:pointer;" data-open-topup="${t.id}">
          <div>
            <div class="tx-item__title">${t.topup_channel === "MANUAL" ? "Request Thủ Công" : "Nạp Tự Động"} (${VP.escapeHtml(t.bank_short_name || "Bank")})</div>
            <div class="tx-item__sub">${VP.escapeHtml(t.transaction_code)} &bull; ${VP.formatDateTime(t.created_at)}</div>
          </div>
          <div class="tx-item__right">
            <div class="tx-item__amt text-success">+${VP.escapeHtml(t.amount_formatted)}</div>
            <span class="status-pill status-pill--${VP.escapeHtml(t.status)}">${VP.escapeHtml(t.status)}</span>
          </div>
        </div>`
      )
      .join("");

    el.querySelectorAll("[data-open-topup]").forEach((row) => {
      row.onclick = async () => {
        try {
          const detail = await VP.api(`/api/topups/${Number(row.dataset.openTopup)}`);
          switchView("topup");
          switchTopupChannel(detail.topup_channel || "AUTO");
          showTopupOrder(detail);
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
        </div>`
      )
      .join("");
  }

  // --- Independent Dynamic QR Lifecycle ---
  function isModeEligible(mode) {
    const snap = state.snapshot;
    if (!snap) return false;
    if (snap.user.status !== "ACTIVE" || snap.wallet.status !== "ACTIVE") return false;
    if (mode === "MONTHLY") return Boolean(snap.active_pass);
    return Number(snap.wallet.balance) >= Number(snap.system.fare_amount);
  }

  async function issueModeQR(mode, showToast = false) {
    const m = mode === "MONTHLY" ? "MONTHLY" : "SINGLE";
    if (state.qrInFlight[m] || !isModeEligible(m)) return;
    state.qrInFlight[m] = true;
    try {
      const qrData = await VP.api("/api/qr/issue", { method: "POST", body: { ticket_mode: m } });
      if (m === "MONTHLY") state.qrMonthly = qrData;
      else state.qrSingle = qrData;
      syncAllQRScreens();
      if (showToast) VP.toast(`Đã cấp mã QR ${m === "MONTHLY" ? "Vé Tháng" : "Vé Lượt"} mới.`, "success");
    } catch (_) {
    } finally {
      state.qrInFlight[m] = false;
    }
  }

  function syncAllQRScreens() {
    if (!state.snapshot) return;
    const { user, wallet, system } = state.snapshot;
    const suspended = user.status === "SUSPENDED" || wallet.status === "LOCKED";

    const singleOk = isModeEligible("SINGLE");
    if (!singleOk) {
      state.qrSingle = null;
      document.getElementById("single-qr-active").style.display = "none";
      document.getElementById("single-qr-locked").style.display = "block";
      document.getElementById("single-locked-title").textContent = suspended
        ? "Tài Khoản Đang Tạm Khóa"
        : "Số Dư Ví Chưa Đủ Cho Vé Lượt";
      document.getElementById("single-locked-desc").textContent = suspended
        ? "Quản trị viên đã tạm khóa thẻ."
        : `Số dư hiện tại (${wallet.balance_formatted}) dưới mức ${system.fare_formatted}.`;
    } else {
      document.getElementById("single-qr-locked").style.display = "none";
      if (state.qrSingle?.qr_svg_data_uri) {
        document.getElementById("single-qr-active").style.display = "flex";
        document.getElementById("single-qr-img").src = state.qrSingle.qr_svg_data_uri;
      } else {
        issueModeQR("SINGLE", false);
      }
    }

    const monthlyOk = isModeEligible("MONTHLY");
    if (!monthlyOk) {
      state.qrMonthly = null;
      document.getElementById("monthly-qr-active").style.display = "none";
      document.getElementById("monthly-qr-locked").style.display = "block";
    } else {
      document.getElementById("monthly-qr-locked").style.display = "none";
      if (state.qrMonthly?.qr_svg_data_uri) {
        document.getElementById("monthly-qr-active").style.display = "flex";
        document.getElementById("monthly-qr-img").src = state.qrMonthly.qr_svg_data_uri;
      } else {
        issueModeQR("MONTHLY", false);
      }
    }

    const homeMode = state.homeQrMode;
    document.getElementById("home-qr-mode-heading").textContent =
      homeMode === "MONTHLY" ? "Mã QR Vé Tháng Qua Cổng" : "Mã QR Vé Lượt Qua Cổng";
    document.getElementById("home-qr-mode-sub").textContent =
      homeMode === "MONTHLY"
        ? "Miễn phí 0 VNĐ / lượt quét khi có Vé Tháng hiệu lực"
        : `Trừ ${system.fare_formatted}/lượt từ Ví số dư & tự xoay mã mới`;

    const homeEligible = isModeEligible(homeMode);
    const homeQrObj = homeMode === "MONTHLY" ? state.qrMonthly : state.qrSingle;
    const homeActive = document.getElementById("home-qr-active-box");
    const homeLocked = document.getElementById("home-qr-locked-box");
    const ctaBtn = document.getElementById("btn-home-locked-cta");

    if (!homeEligible) {
      homeActive.style.display = "none";
      homeLocked.style.display = "block";
      if (suspended) {
        document.getElementById("home-qr-locked-title").textContent = "Thẻ Đang Bị Tạm Khóa";
        document.getElementById("home-qr-locked-desc").textContent = "Tài khoản đang bị tạm khóa bởi Quản trị viên.";
        ctaBtn.style.display = "none";
      } else if (homeMode === "MONTHLY") {
        document.getElementById("home-qr-locked-title").textContent = "Chưa Kích Hoạt Vé Tháng";
        document.getElementById("home-qr-locked-desc").textContent =
          "Hãy nạp hoặc mua gói Vé Tháng ở khung phía trên để mở mã QR Vé Tháng!";
        ctaBtn.style.display = "inline-flex";
        ctaBtn.textContent = "Mở Tab Vé Tháng";
        ctaBtn.onclick = () => switchView("tickets", "MONTHLY");
      } else {
        document.getElementById("home-qr-locked-title").textContent = "Ví Vé Lượt Chưa Đủ Số Dư";
        document.getElementById("home-qr-locked-desc").textContent =
          `Số dư Ví Vé Lượt cần tối thiểu ${system.fare_formatted}.`;
        ctaBtn.style.display = "inline-flex";
        ctaBtn.textContent = "Nạp tiền vào Ví ngay";
        ctaBtn.onclick = () => switchView("topup");
      }
    } else {
      homeLocked.style.display = "none";
      if (homeQrObj?.qr_svg_data_uri) {
        homeActive.style.display = "flex";
        document.getElementById("home-qr-img").src = homeQrObj.qr_svg_data_uri;
      } else {
        issueModeQR(homeMode, false);
      }
    }

    updateQRCountdownTick();
  }

  function updateQRCountdownTick() {
    const updateBar = (qrObj, mode, textId, barId) => {
      if (!qrObj || !qrObj.expires_at) return;
      const remainSec = Math.max(0, Math.ceil((new Date(qrObj.expires_at).getTime() - Date.now()) / 1000));
      const ttl = qrObj.ttl_seconds || 90;
      const pct = Math.min(100, Math.max(0, (remainSec / ttl) * 100));
      const tEl = document.getElementById(textId);
      const bEl = document.getElementById(barId);
      if (tEl) tEl.textContent = `Tự làm mới sau ${remainSec}s`;
      if (bEl) bEl.style.width = `${pct}%`;
      if (remainSec <= 2 && isModeEligible(mode)) issueModeQR(mode, false);
    };

    updateBar(state.qrSingle, "SINGLE", "single-qr-countdown", "single-qr-progress");
    updateBar(state.qrMonthly, "MONTHLY", "monthly-qr-countdown", "monthly-qr-progress");
    const homeQr = state.homeQrMode === "MONTHLY" ? state.qrMonthly : state.qrSingle;
    updateBar(homeQr, state.homeQrMode, "home-qr-countdown", "home-qr-progress");
  }
  setInterval(updateQRCountdownTick, 1000);

  document.getElementById("btn-home-refresh-qr").onclick = () => issueModeQR(state.homeQrMode, true);
  document.getElementById("btn-single-refresh").onclick = () => issueModeQR("SINGLE", true);
  document.getElementById("btn-monthly-refresh").onclick = () => issueModeQR("MONTHLY", true);

  document.getElementById("btn-fullscreen-qr").onclick = () => {
    const activeQr = state.homeQrMode === "MONTHLY" ? state.qrMonthly : state.qrSingle;
    if (!activeQr) return VP.toast("Mã QR của chế độ này chưa khả dụng.", "warning");
    document.getElementById("fullscreen-qr-title").textContent =
      state.homeQrMode === "MONTHLY" ? "Trình QR Vé Tháng (0 VNĐ)" : "Trình QR Vé Lượt (4.000 VNĐ)";
    document.getElementById("fullscreen-qr-img").src = activeQr.qr_svg_data_uri;
    document.getElementById("modal-fullscreen-qr").style.display = "flex";
  };
  document.getElementById("btn-close-fullscreen").onclick = () => {
    document.getElementById("modal-fullscreen-qr").style.display = "none";
  };
  document.getElementById("close-fullscreen-backdrop").onclick = () => {
    document.getElementById("modal-fullscreen-qr").style.display = "none";
  };

  // --- Split Top-up Creation ---
  document.querySelectorAll("#auto-amt-chips .amt-chip").forEach((chip) => {
    chip.onclick = () => {
      document.querySelectorAll("#auto-amt-chips .amt-chip").forEach((c) => c.classList.remove("active"));
      chip.classList.add("active");
      document.getElementById("topup-amount-input").value = chip.dataset.amt;
    };
  });

  function showTopupOrder(order) {
    state.activeTopup = order;
    const box = document.getElementById("active-topup-card");
    box.style.display = "block";

    const isManual = order.topup_channel === "MANUAL";
    document.getElementById("topup-detail-channel-chip").textContent = isManual
      ? "REQUEST THỦ CÔNG • CHỜ ADMIN DUYỆT"
      : "NẠP TỰ ĐỘNG • VIETQR 24/7";
    document.getElementById("topup-detail-heading").textContent = isManual
      ? "Phiếu Request Nạp Tiền Thủ Công"
      : "Đơn Chuyển Khoản Nạp Tự Động";

    const stEl = document.getElementById("topup-order-status");
    stEl.textContent = order.status;
    stEl.className = `status-pill status-pill--${order.status}`;

    const bank = order.bank || order;
    document.getElementById("topup-tx-code").textContent = order.transaction_code;
    document.getElementById("topup-channel-label").textContent = order.channel_label || order.topup_channel;
    document.getElementById("topup-purpose-label").textContent =
      order.purpose === "MONTHLY_PASS"
        ? `Kích hoạt ${order.target_plan_name || "Gói Vé Tháng"}`
        : "Ví Vé Lượt (Số dư tiêu chuẩn)";
    document.getElementById("topup-bank-name").textContent = order.payment_method_label || bank.bank_name || "";
    document.getElementById("topup-acc-num").textContent = bank.account_number || "";
    document.getElementById("topup-acc-holder").textContent = bank.account_holder || "";
    document.getElementById("topup-order-amount").textContent = order.amount_formatted || VP.formatVND(order.amount);
    document.getElementById("topup-transfer-content").textContent = order.transfer_content;
    document.getElementById("topup-instructions").textContent = isManual
      ? `Ghi chú của bạn: "${order.note || ""}". Quản trị viên sẽ kiểm tra phiếu thu/giao dịch và duyệt thủ công.`
      : bank.payment_instructions || "";

    document.getElementById("row-acc-num").style.display = isManual ? "none" : "flex";
    document.getElementById("row-acc-holder").style.display = isManual ? "none" : "flex";

    const qrBox = document.getElementById("topup-qr-box");
    const qrSrc = order.qr?.custom_bank_qr_url || order.qr?.qr_svg_data_uri || "";
    if (!isManual && qrSrc && ["PENDING", "PROCESSING"].includes(order.status)) {
      qrBox.style.display = "flex";
      document.getElementById("topup-qr-img").src = qrSrc;
    } else {
      qrBox.style.display = "none";
    }

    const actions = document.getElementById("topup-pending-actions");
    const isOpen = ["PENDING", "PROCESSING"].includes(order.status);
    actions.style.display = isOpen ? "block" : "none";
    document.getElementById("btn-auto-check-topup").style.display = !isManual && isOpen ? "block" : "none";
    document.getElementById("btn-report-transferred").style.display = !isManual && order.status === "PENDING" ? "block" : "none";
    document.getElementById("btn-cancel-topup").style.display = isOpen ? "block" : "none";
  }

  document.getElementById("form-create-topup-auto").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("btn-create-topup-auto");
    btn.disabled = true;
    try {
      const purpose = document.getElementById("auto-topup-purpose").value;
      const payload = { topup_channel: "AUTO", payment_method_detail: "BANK_TRANSFER", purpose };
      if (purpose === "MONTHLY_PASS") {
        payload.target_plan_id = Number(document.getElementById("auto-topup-plan").value);
      } else {
        payload.amount = Number(document.getElementById("topup-amount-input").value);
      }
      const order = await VP.api("/api/topups", { method: "POST", body: payload });
      showTopupOrder(order);
      VP.toast(`Đã tạo đơn Nạp Tự Động ${order.amount_formatted}.`, "success");
      refreshDashboard();
    } catch (err) {
      VP.toast(err.message, "danger");
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("form-create-topup-manual").addEventListener("submit", async (e) => {
    e.preventDefault();
    const btn = document.getElementById("btn-create-topup-manual");
    btn.disabled = true;
    try {
      const purpose = document.getElementById("manual-topup-purpose").value;
      const payload = {
        topup_channel: "MANUAL",
        payment_method_detail: document.getElementById("manual-topup-method").value,
        note: document.getElementById("manual-note-input").value.trim(),
        purpose,
      };
      if (purpose === "MONTHLY_PASS") {
        payload.target_plan_id = Number(document.getElementById("manual-topup-plan").value);
      } else {
        payload.amount = Number(document.getElementById("manual-amount-input").value);
      }
      const order = await VP.api("/api/topups", { method: "POST", body: payload });
      showTopupOrder(order);
      document.getElementById("manual-note-input").value = "";
      VP.toast(`Đã gửi Request Nạp Thủ Công (${order.amount_formatted}) tới Admin!`, "success");
      refreshDashboard();
    } catch (err) {
      VP.toast(err.message, "danger");
    } finally {
      btn.disabled = false;
    }
  });

  document.getElementById("btn-copy-acc").onclick = () => {
    if (state.activeTopup) VP.copyText(state.activeTopup.bank?.account_number || state.activeTopup.account_number, "số tài khoản");
  };
  document.getElementById("btn-copy-amt").onclick = () => {
    if (state.activeTopup) VP.copyText(String(state.activeTopup.amount), "số tiền");
  };
  document.getElementById("btn-copy-content").onclick = () => {
    if (state.activeTopup) VP.copyText(state.activeTopup.transfer_content, "mã đối chiếu");
  };

  document.getElementById("btn-auto-check-topup").onclick = async () => {
    if (!state.activeTopup) return;
    const btn = document.getElementById("btn-auto-check-topup");
    btn.disabled = true;
    try {
      const detail = await VP.api(`/api/topups/${state.activeTopup.id}/auto-check`, { method: "POST" });
      showTopupOrder(detail);
      if (detail.status === "SUCCESS") {
        VP.toast("Đã khớp sao kê ngân hàng — Duyệt TỰ ĐỘNG thành công!", "success");
        refreshDashboard();
      } else {
        VP.toast(
          detail.live_bank_api_checked
            ? "Đã quét API ngân hàng nhưng chưa thấy khoản chuyển khớp nội dung."
            : "Hệ thống đang lắng nghe đối soát tự động (và hàng đợi Admin song song).",
          "info"
        );
      }
    } catch (err) {
      VP.toast(err.message, "danger");
    } finally {
      btn.disabled = false;
    }
  };

  document.getElementById("btn-report-transferred").onclick = async () => {
    if (!state.activeTopup) return;
    try {
      const updated = await VP.api(`/api/topups/${state.activeTopup.id}/report-transferred`, {
        method: "POST",
        body: { note: "Khách xác nhận đã chuyển khoản trên ứng dụng" },
      });
      showTopupOrder(updated);
      VP.toast("Đã báo chuyển khoản thành công.", "info");
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
      VP.toast("Đã hủy yêu cầu nạp tiền.", "warning");
      refreshDashboard();
    } catch (err) {
      VP.toast(err.message, "danger");
    }
  };

  // --- Monthly Pass Sub-tab ---
  async function loadMyPasses() {
    try {
      const data = await VP.api("/api/passes/my");
      const activeBox = document.getElementById("ui-active-pass-body");

      if (data.active_pass) {
        const p = data.active_pass;
        activeBox.innerHTML = `
          <div class="active-pass-hero">
            <div style="font-size:0.75rem;opacity:0.85;letter-spacing:0.06em;">MÃ VÉ THÁNG: ${VP.escapeHtml(p.pass_code)}</div>
            <div style="font-size:1.18rem;font-weight:900;margin:4px 0 10px;">${VP.escapeHtml(p.plan_name)}</div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:0.84rem;background:rgba(0,0,0,0.2);padding:10px;border-radius:12px;">
              <div>Hết hạn: <strong>${VP.formatDateTime(p.expires_at)}</strong></div>
              <div>Còn lại: <strong>${p.remaining_days} ngày</strong></div>
              <div>Đã đi: <strong>${VP.escapeHtml(p.trips_label)}</strong></div>
              <div>Phí qua cổng: <strong>0 VNĐ</strong></div>
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
        activeBox.innerHTML = `<div class="vp-muted" style="text-align:center;">Chưa có gói Vé Tháng nào đang hoạt động.</div>`;
      }

      const plansEl = document.getElementById("ui-pass-plans-list");
      plansEl.innerHTML = data.plans
        .map(
          (pl) => `
          <div class="plan-card">
            <div class="plan-card__top">
              <div>
                <span class="pass-chip ${pl.requires_priority ? "pass-chip--gold" : ""}">${VP.escapeHtml(pl.badge_text)} &bull; ${pl.duration_days} NGÀY</span>
                <div class="plan-card__name">${VP.escapeHtml(pl.name)}</div>
              </div>
              <div class="plan-card__price">${VP.escapeHtml(pl.price_formatted)}</div>
            </div>
            <p class="vp-muted" style="margin:6px 0 12px;">${VP.escapeHtml(pl.description)} (~${VP.escapeHtml(pl.daily_cost_formatted)}/ngày)</p>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;">
              <button type="button" class="vp-btn vp-btn--primary vp-btn--sm" data-buy-plan="${pl.id}" data-plan-name="${VP.escapeHtml(pl.name)}" data-plan-price="${VP.escapeHtml(pl.price_formatted)}">
                ${data.active_pass ? "Gia hạn bằng Ví" : "Mua bằng Số dư Ví"}
              </button>
              <button type="button" class="vp-btn vp-btn--accent vp-btn--sm" data-topup-plan="${pl.id}">
                Nạp riêng gói này
              </button>
            </div>
          </div>`
        )
        .join("");

      plansEl.querySelectorAll("[data-buy-plan]").forEach((btn) => {
        btn.onclick = async () => {
          const planId = Number(btn.dataset.buyPlan);
          if (!confirm(`Xác nhận trừ ${btn.dataset.planPrice} từ Ví VeloPass để đăng ký "${btn.dataset.planName}"?`)) return;
          btn.disabled = true;
          try {
            const res = await VP.api("/api/passes/purchase", {
              method: "POST",
              body: { plan_id: planId, auto_renew: false },
            });
            if (res.next_qr) state.qrMonthly = res.next_qr;
            VP.toast(`Kích hoạt ${btn.dataset.planName} thành công!`, "success");
            await refreshDashboard();
            await loadMyPasses();
          } catch (err) {
            VP.toast(err.message, "danger");
          } finally {
            btn.disabled = false;
          }
        };
      });

      plansEl.querySelectorAll("[data-topup-plan]").forEach((btn) => {
        btn.onclick = () => {
          switchView("topup");
          switchTopupChannel("AUTO", "MONTHLY_PASS", Number(btn.dataset.topupPlan));
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
                  <div class="tx-item__sub">Đã đi: ${VP.escapeHtml(h.trips_label)}</div>
                </div>
                <div class="tx-item__right">
                  <div class="tx-item__amt text-primary">${VP.escapeHtml(h.price_paid_formatted)}</div>
                  <span class="status-pill status-pill--${h.status === "ACTIVE" ? "SUCCESS" : "EXPIRED"}">${VP.escapeHtml(h.status)}</span>
                </div>
              </div>`
            )
            .join("")
        : `<div class="empty-state">Chưa có lịch sử Vé Tháng.</div>`;
    } catch (err) {
      VP.toast(err.message, "danger");
    }
  }

  // --- Full Transaction History ---
  async function loadFullHistory() {
    try {
      const data = await VP.api("/api/user/transactions");
      document.getElementById("htab-usage").innerHTML = data.usages.length
        ? data.usages
            .map(
              (u) => `
            <div class="tx-item">
              <div>
                <div class="tx-item__title">${u.payment_method === "MONTHLY_PASS" ? "Vé Tháng (0 VNĐ)" : "Vé Lượt"} &bull; ${VP.escapeHtml(u.transaction_code)}</div>
                <div class="tx-item__sub">Thời gian: ${VP.formatDateTime(u.created_at)}</div>
                <div class="tx-item__sub">Số dư ví: ${VP.escapeHtml(u.balance_before_formatted)} &rarr; <strong>${VP.escapeHtml(u.balance_after_formatted)}</strong></div>
              </div>
              <div class="tx-item__right">
                <div class="tx-item__amt ${u.payment_method === "MONTHLY_PASS" ? "text-success" : "text-danger"}">${VP.escapeHtml(u.amount_formatted)}</div>
                <span class="status-pill status-pill--SUCCESS">${VP.escapeHtml(u.status)}</span>
              </div>
            </div>`
            )
            .join("")
        : `<div class="empty-state">Chưa có lịch sử quét vé.</div>`;

      document.getElementById("htab-topup").innerHTML = data.topups.length
        ? data.topups
            .map(
              (t) => `
            <div class="tx-item">
              <div>
                <div class="tx-item__title">${VP.escapeHtml(t.transaction_code)} &bull; ${VP.escapeHtml(t.bank_name)}</div>
                <div class="tx-item__sub">Mã đối chiếu: <strong>${VP.escapeHtml(t.transfer_content)}</strong></div>
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

      document.getElementById("htab-ledger").innerHTML = data.ledger.length
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

  // --- Notifications ---
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

  // --- Profile + Vehicle Plate ---
  document.getElementById("form-profile").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await VP.api("/api/user/profile", {
        method: "PUT",
        body: {
          full_name: document.getElementById("acc-fullname").value.trim(),
          phone: document.getElementById("acc-phone").value.trim(),
          vehicle_plate: document.getElementById("acc-vehicle-plate").value.trim().toUpperCase(),
        },
      });
      VP.toast("Đã lưu hồ sơ cá nhân và biển số xe!", "success");
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

  // --- Priority Passenger Registration & Proof Image Compression ---
  function compressImageFileToDataUri(file, maxDim = 960, quality = 0.82) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error("Không thể đọc file ảnh."));
      reader.onload = () => {
        const img = new Image();
        img.onerror = () => reject(new Error("Định dạng file ảnh không hợp lệ."));
        img.onload = () => {
          let w = img.width;
          let h = img.height;
          if (w > maxDim || h > maxDim) {
            if (w >= h) {
              h = Math.round((h * maxDim) / w);
              w = maxDim;
            } else {
              w = Math.round((w * maxDim) / h);
              h = maxDim;
            }
          }
          const canvas = document.createElement("canvas");
          canvas.width = w;
          canvas.height = h;
          const ctx = canvas.getContext("2d");
          ctx.drawImage(img, 0, 0, w, h);
          resolve(canvas.toDataURL("image/jpeg", quality));
        };
        img.src = reader.result;
      };
      reader.readAsDataURL(file);
    });
  }

  document.getElementById("prio-proof-file").addEventListener("change", async (e) => {
    const file = e.target.files && e.target.files[0];
    if (!file) {
      state.proofImageDataUri = "";
      document.getElementById("prio-proof-preview-wrap").style.display = "none";
      return;
    }
    try {
      const dataUri = await compressImageFileToDataUri(file);
      state.proofImageDataUri = dataUri;
      document.getElementById("prio-proof-preview").src = dataUri;
      document.getElementById("prio-proof-preview-wrap").style.display = "block";
    } catch (err) {
      VP.toast(err.message || "Không thể xử lý ảnh minh chứng.", "danger");
    }
  });

  async function loadPriorityRegistrations() {
    try {
      const res = await VP.api("/api/user/priority");
      const el = document.getElementById("ui-priority-req-list");
      if (!res.requests.length) {
        el.innerHTML = "";
        return;
      }
      el.innerHTML = res.requests
        .map(
          (r) => `
          <div class="tx-item">
            <div>
              <div class="tx-item__title">${VP.escapeHtml(r.priority_label)} &bull; ${VP.escapeHtml(r.id_card_code)}</div>
              <div class="tx-item__sub">${VP.escapeHtml(r.organization_name)} &bull; ${VP.formatDateTime(r.created_at)}</div>
              ${r.admin_note ? `<div class="tx-item__sub text-primary">Phản hồi Admin: ${VP.escapeHtml(r.admin_note)}</div>` : ""}
            </div>
            <span class="status-pill status-pill--${r.status === "APPROVED" ? "SUCCESS" : r.status === "REJECTED" ? "FAILED" : "PROCESSING"}">${r.status}</span>
          </div>`
        )
        .join("");
    } catch (_) {}
  }

  document.getElementById("form-priority-reg").addEventListener("submit", async (e) => {
    e.preventDefault();
    if (!state.proofImageDataUri) {
      return VP.toast("Vui lòng chọn ảnh chụp Thẻ sinh viên / Portal trường để làm bằng chứng!", "warning");
    }
    const btn = document.getElementById("btn-submit-priority");
    btn.disabled = true;
    try {
      await VP.api("/api/user/priority", {
        method: "POST",
        body: {
          priority_type: document.getElementById("prio-type").value,
          id_card_code: document.getElementById("prio-card-code").value.trim(),
          organization_name: document.getElementById("prio-org").value.trim(),
          proof_note: document.getElementById("prio-note").value.trim(),
          proof_image_data: state.proofImageDataUri,
        },
      });
      state.proofImageDataUri = "";
      e.target.reset();
      document.getElementById("prio-proof-preview-wrap").style.display = "none";
      VP.toast("Đã gửi hồ sơ Đối tượng Ưu tiên kèm ảnh bằng chứng!", "success");
      await refreshDashboard();
      await loadPriorityRegistrations();
    } catch (err) {
      VP.toast(err.message, "danger");
    } finally {
      btn.disabled = false;
    }
  });

  // --- Support ---
  async function loadSupportTickets() {
    try {
      const res = await VP.api("/api/user/support");
      const el = document.getElementById("ui-support-list");
      el.innerHTML = res.support_requests.length
        ? res.support_requests
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
            .join("")
        : `<div class="empty-state">Chưa có yêu cầu hỗ trợ nào.</div>`;
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

  // --- Real-Time SSE ---
  function connectRealtimeSSE() {
    if (!window.EventSource) return;
    if (state.sse) state.sse.close();

    const es = new EventSource("/api/notifications/stream");
    state.sse = es;

    es.addEventListener("card_scanned", (ev) => {
      const payload = JSON.parse(ev.data);
      const isPass = payload.payment_method === "MONTHLY_PASS";
      VP.toast(
        isPass
          ? `Qua cổng miễn phí 0 VNĐ bằng Vé Tháng! Ví giữ nguyên ${payload.balance_formatted}.`
          : `Đã trừ ${VP.formatVND(payload.amount)} Vé Lượt. Số dư còn ${payload.balance_formatted}.`,
        "success",
        "Quét thẻ thành công!"
      );
      if (isPass) state.qrMonthly = payload.next_qr || null;
      else state.qrSingle = payload.next_qr || null;
      refreshDashboard();
      if (state.activeView === "tickets" && state.ticketSubTab === "MONTHLY") loadMyPasses();
    });

    es.addEventListener("topup_confirmed", (ev) => {
      const payload = JSON.parse(ev.data);
      VP.toast(
        payload.activated_pass_code
          ? `Nạp tiền & kích hoạt Vé Tháng (${payload.activated_pass_code}) thành công!`
          : `Ví vừa được cộng +${VP.formatVND(payload.amount)}. Số dư mới: ${payload.balance_formatted}!`,
        "success",
        "Giao dịch nạp hoàn tất!"
      );
      if (payload.next_qr) {
        if (payload.next_qr.ticket_mode === "MONTHLY") state.qrMonthly = payload.next_qr;
        else state.qrSingle = payload.next_qr;
      }
      if (state.activeTopup && state.activeTopup.id === payload.topup_id) {
        state.activeTopup.status = "SUCCESS";
        showTopupOrder(state.activeTopup);
      }
      refreshDashboard();
      if (state.activeView === "tickets" && state.ticketSubTab === "MONTHLY") loadMyPasses();
    });

    es.addEventListener("pass_updated", (ev) => {
      const payload = JSON.parse(ev.data);
      if (payload.next_qr) state.qrMonthly = payload.next_qr;
      refreshDashboard();
      if (state.activeView === "tickets" && state.ticketSubTab === "MONTHLY") loadMyPasses();
    });

    es.addEventListener("priority_updated", (ev) => {
      const payload = JSON.parse(ev.data);
      VP.toast(
        payload.status === "APPROVED"
          ? "Hồ sơ Đối tượng Ưu tiên của bạn đã được Admin phê duyệt! Đã mở khóa gói Vé Tháng HSSV."
          : "Hồ sơ Đối tượng Ưu tiên đã có kết quả xét duyệt mới.",
        payload.status === "APPROVED" ? "success" : "warning"
      );
      refreshDashboard();
      if (state.activeView === "account") loadPriorityRegistrations();
      if (state.activeView === "tickets") loadMyPasses();
    });

    es.addEventListener("account_status_changed", (ev) => {
      const payload = JSON.parse(ev.data);
      if (payload.user_status === "SUSPENDED") {
        state.qrSingle = null;
        state.qrMonthly = null;
        VP.toast(`Tài khoản đã bị tạm khóa: ${payload.reason}`, "danger", "Thông báo bảo mật");
      } else {
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

  refreshDashboard().then(() => connectRealtimeSSE());
})();
