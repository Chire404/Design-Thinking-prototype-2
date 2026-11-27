/* Shared API Client, CSRF helper, VND formatting & Toast system for VeloPass */
window.VP = (() => {
  function getCookie(name) {
    const match = document.cookie.match(new RegExp("(^| )" + name + "=([^;]+)"));
    return match ? decodeURIComponent(match[2]) : "";
  }

  function formatVND(amount) {
    if (amount === null || amount === undefined || Number.isNaN(Number(amount))) return "0 VNĐ";
    const n = Math.trunc(Number(amount));
    const sign = n < 0 ? "-" : "";
    const grouped = Math.abs(n).toString().replace(/\B(?=(\d{3})+(?!\d))/g, ".");
    return `${sign}${grouped} VNĐ`;
  }

  function formatDateTime(isoStr) {
    if (!isoStr) return "—";
    try {
      const d = new Date(isoStr);
      if (Number.isNaN(d.getTime())) return isoStr;
      return d.toLocaleString("vi-VN", {
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit",
        day: "2-digit",
        month: "2-digit",
        year: "numeric",
      });
    } catch (_) {
      return isoStr;
    }
  }

  function escapeHtml(str) {
    if (str === null || str === undefined) return "";
    return String(str)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#039;");
  }

  async function api(path, options = {}) {
    const method = (options.method || "GET").toUpperCase();
    const headers = Object.assign({ Accept: "application/json" }, options.headers || {});

    if (options.body && typeof options.body === "object" && !(options.body instanceof FormData)) {
      headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(options.body);
    }

    if (["POST", "PUT", "PATCH", "DELETE"].includes(method)) {
      const csrf = window.__VP_CSRF__ || getCookie("vp_csrf");
      if (csrf) headers["X-CSRF-Token"] = csrf;
    }

    const res = await fetch(path, {
      method,
      headers,
      body: options.body,
      credentials: "same-origin",
    });

    let json = null;
    try {
      json = await res.json();
    } catch (_) {
      throw { code: "NETWORK_PARSE_ERROR", message: "Không thể đọc phản hồi từ máy chủ.", status: res.status };
    }

    if (!res.ok || !json.ok) {
      const err = (json && json.error) || {};
      if (res.status === 401 && !path.startsWith("/api/auth/login")) {
        window.location.href = "/login";
      }
      throw {
        code: err.code || `HTTP_${res.status}`,
        message: err.message || "Yêu cầu không thành công.",
        details: err.details || {},
        status: res.status,
      };
    }

    return json.data;
  }

  function toast(message, type = "info", title = "") {
    let container = document.getElementById("vp-toast-container");
    if (!container) {
      container = document.createElement("div");
      container.id = "vp-toast-container";
      container.className = "vp-toast-container";
      document.body.appendChild(container);
    }
    const el = document.createElement("div");
    el.className = `vp-toast vp-toast--${type}`;
    el.innerHTML = `
      <div class="vp-toast__body">
        ${title ? `<div class="vp-toast__title">${escapeHtml(title)}</div>` : ""}
        <div class="vp-toast__msg">${escapeHtml(message)}</div>
      </div>
      <button class="vp-toast__close" aria-label="Đóng">&times;</button>
    `;
    el.querySelector(".vp-toast__close").onclick = () => el.remove();
    container.appendChild(el);
    setTimeout(() => {
      el.classList.add("vp-toast--hide");
      setTimeout(() => el.remove(), 260);
    }, 4500);
  }

  async function copyText(text, label = "nội dung") {
    try {
      await navigator.clipboard.writeText(text);
      toast(`Đã sao chép ${label}: ${text}`, "success");
    } catch (_) {
      const ta = document.createElement("textarea");
      ta.value = text;
      document.body.appendChild(ta);
      ta.select();
      document.execCommand("copy");
      ta.remove();
      toast(`Đã sao chép ${label}`, "success");
    }
  }

  function registerServiceWorker() {
    if ("serviceWorker" in navigator) {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    }
  }

  return {
    api,
    getCookie,
    formatVND,
    formatDateTime,
    escapeHtml,
    toast,
    copyText,
    registerServiceWorker,
  };
})();
