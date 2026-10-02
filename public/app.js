// ---------------------------------------------------------------------------
// Telegram WebApp bootstrap
// ---------------------------------------------------------------------------
const tg = window.Telegram?.WebApp;
let INIT_DATA = "";

if (tg) {
    tg.expand();
    tg.ready();
    INIT_DATA = tg.initData || "";
}

const API_URL = "/api";

// Server-side authorization is the ONLY real gate; every value below is used
// for display/UX only and is re-checked by the backend on every request.
let MY_PERMISSIONS = [];
let MY_ROLE = "";
let MY_ID = 0;

function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>"']/g, (ch) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
    }[ch]));
}

let _toastTimer = null;
function notify(message, kind = "info") {
    const toast = document.getElementById("toast");
    if (toast) {
        toast.textContent = String(message || "");
        toast.classList.toggle("error", kind === "error");
        toast.classList.add("show");
        if (_toastTimer) clearTimeout(_toastTimer);
        _toastTimer = setTimeout(() => toast.classList.remove("show"), 3200);
        return;
    }
    if (tg && typeof tg.showAlert === "function") {
        try { tg.showAlert(message); return; } catch (e) { /* fallback below */ }
    }
    window.alert(message);
}

async function confirmAction(message) {
    if (tg && typeof tg.showConfirm === "function") {
        try {
            return await new Promise((resolve) => tg.showConfirm(message, resolve));
        } catch (e) {
            // Keep confirmation available on older clients as well.
        }
    }
    return window.confirm(message);
}

let _activeActions = 0;
async function withBusyButton(button, action) {
    if (!button || button.disabled) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    _activeActions += 1;
    try {
        return await action();
    } finally {
        _activeActions -= 1;
        button.disabled = false;
        button.removeAttribute("aria-busy");
    }
}

function haptic(kind) {
    if (!tg) return;
    try {
        if (kind === "success" || kind === "error") {
            tg.HapticFeedback?.notificationOccurred(kind === "error" ? "error" : "success");
        } else {
            tg.HapticFeedback?.impactOccurred(kind || "light");
        }
    } catch (e) {
        /* haptics are best-effort */
    }
}

// Server-side auth: send the REAL initData. Never fabricate a header — a fake
// value ("test") must not authenticate anyone, so we fail closed client-side.
function authHeaders(extra) {
    return Object.assign({ "X-Telegram-Init-Data": INIT_DATA }, extra || {});
}

async function apiFetch(path, options) {
    if (!INIT_DATA) {
        throw new Error("Telegram initData topilmadi. Panelni Telegram ichidan oching.");
    }
    const opts = Object.assign({ headers: authHeaders() }, options || {});
    if (opts.body && !opts.headers["Content-Type"]) {
        opts.headers["Content-Type"] = "application/json";
    }
    const response = await fetch(`${API_URL}${path}`, opts);
    let payload = null;
    try {
        payload = await response.json();
    } catch (e) {
        payload = null;
    }
    if (!response.ok) {
        const error = new Error((payload && payload.error) || `HTTP ${response.status}`);
        error.status = response.status;
        error.payload = payload;
        throw error;
    }
    return payload || {};
}

async function apiFetchBlob(path) {
    if (!INIT_DATA) throw new Error("Telegram initData topilmadi.");
    const response = await fetch(`${API_URL}${path}`, { headers: authHeaders() });
    if (!response.ok) {
        let msg = `HTTP ${response.status}`;
        try { const data = await response.json(); msg = data.error || msg; } catch (e) {}
        throw new Error(msg);
    }
    return await response.blob();
}

function formatMoney(value) {
    return Number(value || 0).toLocaleString("uz-UZ");
}

function can(permission) {
    return MY_PERMISSIONS.includes(permission);
}

// ---------------------------------------------------------------------------
// Header / identity (display only — the server decides permissions)
// ---------------------------------------------------------------------------
const adminUser = tg?.initDataUnsafe?.user;
const nameEl = document.getElementById("admin-name");
if (nameEl) {
    nameEl.textContent = adminUser
        ? `${adminUser.first_name || ""} ${adminUser.last_name || ""}`.trim() || "Admin"
        : "Admin";
}

function applyRoleUi() {
    const badge = document.getElementById("role-badge");
    if (badge) badge.textContent = (MY_ROLE || "-").toUpperCase();

    document.querySelectorAll("[data-requires]").forEach((el) => {
        const permission = el.dataset.requires;
        el.style.display = !permission || can(permission) ? "" : "none";
    });

    const sendBtn = document.getElementById("btn-broadcast");
    const retryBtn = document.getElementById("btn-retry");
    const testBtn = document.getElementById("btn-test");
    const previewBtn = document.getElementById("btn-preview");
    const banBtn = document.getElementById("btn-ban");
    const verifyBtn = document.getElementById("verify-connections-btn");
    const useBroadcast = can("broadcast.use");
    [sendBtn, retryBtn, testBtn, previewBtn].forEach((el) => {
        if (el) el.style.display = useBroadcast ? "" : "none";
    });
    if (banBtn) banBtn.style.display = can("users.moderate") ? "" : "none";
    if (verifyBtn) verifyBtn.style.display = can("connections.manage") ? "" : "none";

    const messageTab = document.querySelector('[data-detail-tab="message"]');
    if (messageTab) messageTab.style.display = can("users.message") ? "" : "none";
    ensureAllowedActiveTab();
}



// ---------------------------------------------------------------------------
// Dashboard + Users
// ---------------------------------------------------------------------------
function renderDashboardChart(series) {
    const chart = document.getElementById("dashboard-chart");
    if (!chart) return;
    if (!Array.isArray(series) || !series.length) {
        chart.innerHTML = '<div class="chart-empty">Faollik ma’lumoti yo‘q.</div>';
        return;
    }
    const max = Math.max(1, ...series.map((p) => Math.max(
        Number(p.messages || 0), Number(p.edited || 0), Number(p.deleted || 0)
    )));
    chart.innerHTML = series.map((point) => {
        const m = Math.max(2, Math.round((Number(point.messages || 0) / max) * 100));
        const e = Math.max(2, Math.round((Number(point.edited || 0) / max) * 100));
        const d = Math.max(2, Math.round((Number(point.deleted || 0) / max) * 100));
        return `<div class="chart-bar-group" title="${escapeHtml(point.bucket || "")}">
            <i class="chart-bar bar-message" style="height:${m}%"></i>
            <i class="chart-bar bar-edit" style="height:${e}%"></i>
            <i class="chart-bar bar-delete" style="height:${d}%"></i>
        </div>`;
    }).join("");
}

async function loadDashboardActivity() {
    const chart = document.getElementById("dashboard-chart");
    if (!chart || !can("analytics.view")) return;
    try {
        const data = await apiFetch("/analytics?range=24h");
        renderDashboardChart(data.series || []);
    } catch (e) {
        chart.innerHTML = '<div class="chart-empty">Grafikni yuklab bo‘lmadi.</div>';
    }
}
async function loadDashboard({ silent = false } = {}) {
    try {
        const data = await apiFetch("/stats");
        MY_ROLE = data.role || MY_ROLE;
        MY_PERMISSIONS = data.permissions || MY_PERMISSIONS;
        applyRoleUi();
        const set = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = Number(value ?? 0).toLocaleString("uz-UZ");
        };
        set("stat-users", data.total_users);
        set("stat-connections", data.active_connections);
        set("stat-deleted", data.deleted_messages);
        set("stat-edited", data.edited_messages);
        const apiStatus = document.getElementById("api-status");
        const dbStatus = document.getElementById("db-status");
        const statusTitle = document.getElementById("dashboard-status-title");
        const statusSub = document.getElementById("dashboard-status-sub");
        if (apiStatus) apiStatus.textContent = "Online";
        if (dbStatus) dbStatus.textContent = "Healthy";
        if (statusTitle) statusTitle.textContent = "Bot ishlayapti";
        if (statusSub) statusSub.textContent = "API va ma’lumotlar bazasi javob bermoqda.";
        loadDashboardActivity();
    } catch (e) {
        console.error(e);
        if (!silent) notify("Umumiy holat yuklanmadi: " + e.message);
    }
}

let _userPage = 1;
const USER_PAGE_SIZE = 50;
let _userSearch = "";
let _userSearchTimer = null;
let _usersRequest = 0;
let _usersController = null;

function invalidateUsersRequest() {
    _usersRequest += 1;
    _usersController?.abort();
}

async function loadUsers(resetPage = false, { silent = false } = {}) {
    if (!can("users.view")) return;
    if (resetPage) _userPage = 1;
    invalidateUsersRequest();
    const requestId = _usersRequest;
    const page = _userPage;
    const controller = new AbortController();
    _usersController = controller;
    const list = document.getElementById("users-list");
    if (list && !list.children.length) list.innerHTML = '<div class="loading-state">Foydalanuvchilar yuklanmoqda...</div>';
    if (list) list.setAttribute("aria-busy", "true");
    try {
        const offset = (page - 1) * USER_PAGE_SIZE;
        const params = new URLSearchParams({ search: _userSearch, limit: String(USER_PAGE_SIZE), offset: String(offset) });
        const data = await apiFetch(`/users?${params.toString()}`, { signal: controller.signal });
        if (!list || requestId !== _usersRequest) return;
        const items = data.items || [];
        if (!items.length && Number(data.total || 0) > 0 && _userPage > 1) {
            _userPage -= 1;
            return loadUsers(false);
        }
        const empty = document.getElementById("users-empty");
        if (!items.length) {
            if (empty) empty.style.display = "block";
        } else {
            if (empty) empty.style.display = "none";
        }
        // Keep existing cards (and their focus) when nothing visible changed.
        const existing = new Map(Array.from(list.children)
            .filter((item) => item.dataset.userId)
            .map((item) => [item.dataset.userId, item]));
        const rendered = [];
        items.forEach((user) => {
            const safeId = String(user.user_id);
            const name = `${user.first_name || ""} ${user.last_name || ""}`.trim() || "Noma'lum";
            const role = user.role ? ` · ${escapeHtml(user.role.toUpperCase())}` : "";
            const status = user.banned
                ? '<span class="status-badge status-disabled">🚫 BLOKLANGAN</span>'
                : (user.connected
                    ? '<span class="status-badge status-enabled">🟢 ULANGAN</span>'
                    : '<span class="status-badge status-neutral">⚪ ULANMAGAN</span>');
            const markup = `
                <div class="conn-header">
                    <div class="conn-user">
                        <div class="avatar" aria-hidden="true">${escapeHtml((Array.from(name)[0] || "?").toUpperCase())}</div>
                        <div class="user-info">
                            <h4>${escapeHtml(name)}${role}</h4>
                            <span>ID: ${escapeHtml(safeId)}${user.username ? " · @" + escapeHtml(user.username) : ""}</span>
                            <span>Ulanishlar: ${escapeHtml(user.connections_count || 0)} · Faol: ${escapeHtml(user.active_connections || 0)}</span>
                        </div>
                    </div>
                    ${status}
                </div>
                <div class="conn-actions" style="display:flex;gap:8px;flex-wrap:wrap;">
                    <button class="btn btn-secondary open-user" data-id="${escapeHtml(safeId)}">Profil</button>
                    <button class="btn btn-secondary copy-id" data-id="${escapeHtml(safeId)}">📋 ID nusxalash</button>
                    ${can("users.moderate") && !user.protected && !user.banned ? '<button class="btn btn-danger ban-user">🚫 Bloklash</button>' : ""}
                    ${can("users.moderate") && user.banned && !user.protected ? '<button class="btn btn-secondary unban-user">♻️ Blokdan olish</button>' : ""}
                    ${can("users.delete") && !user.protected ? '<button class="btn btn-danger delete-user">🗑 O‘chirish</button>' : ""}
                </div>
            `;
            let item = existing.get(safeId);
            if (item && item._markup === markup) {
                rendered.push(item);
                return;
            }
            item = document.createElement("div");
            item.className = "connection-item";
            item.dataset.userId = safeId;
            item._markup = markup;
            item.innerHTML = markup;
            item.querySelector(".open-user")?.addEventListener("click", () => openUserDetail(safeId));
            item.querySelector(".copy-id")?.addEventListener("click", async () => {
                try {
                    await navigator.clipboard.writeText(String(safeId));
                    haptic("success");
                    notify(`ID nusxalandi: ${safeId}`);
                } catch (e) {
                    haptic("error");
                    notify(`ID: ${safeId}`);
                }
            });
            item.querySelector(".ban-user")?.addEventListener("click", (event) => withBusyButton(event.currentTarget, async () => {
                const reason = window.prompt("Bloklash sababi (ixtiyoriy):", "");
                if (reason === null) return;
                invalidateUsersRequest();
                const ok = await moderate("ban", safeId, reason);
                if (ok) {
                    await loadUsers(false);
                    await loadDashboard();
                }
            }));
            item.querySelector(".unban-user")?.addEventListener("click", (event) => withBusyButton(event.currentTarget, async () => {
                invalidateUsersRequest();
                const ok = await moderate("unban", safeId, "");
                if (ok) {
                    await loadUsers(false);
                    await loadDashboard();
                }
            }));
            item.querySelector(".delete-user")?.addEventListener("click", (event) => withBusyButton(event.currentTarget, async () => {
                const confirmed = await confirmAction(`${name} (ID: ${safeId}) va unga tegishli yozuvlar butunlay o‘chiriladi. Davom etasizmi?`);
                if (!confirmed) return;
                invalidateUsersRequest();
                try {
                    const res = await apiFetch(`/users/${safeId}`, { method: "DELETE" });
                    haptic("success");
                    const warning = (res.cleanup_warnings || []).length
                        ? `\nOgohlantirish: ${(res.cleanup_warnings || []).join(", ")}`
                        : "";
                    notify(`Foydalanuvchi ${safeId} o'chirildi.${warning}`);
                    await loadUsers(false);
                    if (can("dashboard.view")) await loadDashboard();
                } catch (e) {
                    haptic("error");
                    notify("Foydalanuvchi o'chirilmadi: " + e.message);
                }
            }));
            rendered.push(item);
        });
        const keep = new Set(rendered);
        Array.from(list.children).forEach((item) => { if (!keep.has(item)) item.remove(); });
        rendered.forEach((item, index) => {
            if (list.children[index] !== item) list.insertBefore(item, list.children[index] || null);
        });
        const totalPages = Math.max(1, Math.ceil(Number(data.total || 0) / USER_PAGE_SIZE));
        const info = document.getElementById("users-page-info");
        if (info) info.textContent = `${page} / ${totalPages} · ${Number(data.total || 0).toLocaleString("uz-UZ")} foydalanuvchi`;
        const prev = document.getElementById("users-prev");
        const next = document.getElementById("users-next");
        if (prev) prev.disabled = _userPage <= 1;
        if (next) next.disabled = !data.has_next;
        const status = document.getElementById("users-status");
        if (status) status.textContent = "";
    } catch (e) {
        if (e.name === "AbortError" || requestId !== _usersRequest) return;
        console.error(e);
        const status = document.getElementById("users-status");
        if (status) status.textContent = "Yangilanmadi. Ko‘rsatilgan ma’lumot eskirgan bo‘lishi mumkin. Qayta yangilang.";
        if (list && !list.querySelector(".connection-item")) {
            list.innerHTML = `<div class="empty-state">Yuklab bo‘lmadi: ${escapeHtml(e.message)}</div>`;
        }
        if (!silent) notify("Foydalanuvchilar yangilanmadi: " + e.message);
    } finally {
        if (list && requestId === _usersRequest) list.removeAttribute("aria-busy");
    }
}

async function verifyConnections() {
    if (!can("connections.manage")) return;
    const btn = document.getElementById("verify-connections-btn");
    if (btn?.disabled) return;
    if (btn) btn.disabled = true;
    try {
        const result = await apiFetch("/connections/verify", { method: "POST" });
        haptic("success");
        notify(`Tekshirildi: ${result.checked}
O'zgargan: ${result.changed}
OK: ${result.ok}
Noma'lum: ${result.unknown}
Xato: ${result.errors}`);
        await loadUsers(false);
        await loadDashboard();
    } catch (e) {
        haptic("error");
        notify("Connection verify xatosi: " + e.message);
    } finally {
        if (btn) btn.disabled = false;
    }
}

// ---------------------------------------------------------------------------
// Tabs
// ---------------------------------------------------------------------------
const TAB_PERMISSIONS = {
    "tab-dashboard": "dashboard.view",
    "tab-users": "users.view",
    "tab-analytics": "analytics.view",
    "tab-broadcast": "broadcast.view",
    "tab-moderation": "users.view",
    "tab-settings": "settings.view",
    "tab-subscriptions": "settings.view",
    "tab-admins": "admins.view",
};

function tabAllowed(tabId) {
    const permission = TAB_PERMISSIONS[tabId];
    return !permission || can(permission);
}

function loadTab(tabId, options = {}) {
    if (tabId === "tab-dashboard" && can("dashboard.view")) return loadDashboard(options);
    if (tabId === "tab-users" && can("users.view")) return loadUsers(false, options);
    if (tabId === "tab-settings" && can("settings.view")) return loadSettings();
    if (tabId === "tab-subscriptions" && can("settings.view")) return loadSubscriptions();
    if (tabId === "tab-analytics" && can("analytics.view")) return loadAnalytics(options);
    if (tabId === "tab-broadcast" && can("broadcast.view")) return loadBroadcastStatus(options);
    if (tabId === "tab-moderation" && can("users.view")) return loadBanned();
    if (tabId === "tab-admins" && can("admins.view")) return loadAdmins();
    return Promise.resolve();
}

function activateTab(tabId, { load = true } = {}) {
    const target = document.getElementById(tabId);
    if (!target || !tabAllowed(tabId)) return false;

    document.querySelectorAll(".tab-content").forEach((c) => c.classList.remove("active"));
    target.classList.add("active");

    const extras = new Set(["tab-broadcast", "tab-moderation", "tab-settings", "tab-subscriptions", "tab-admins", "tab-profile"]);
    const navTab = extras.has(tabId) ? "tab-menu" : tabId;
    document.querySelectorAll(".tab-btn").forEach((b) => {
        const active = b.dataset.tab === navTab;
        b.classList.toggle("active", active);
        b.setAttribute("aria-pressed", String(active));
    });
    window.scrollTo({ top: 0, behavior: "smooth" });
    if (load) Promise.resolve(loadTab(tabId)).catch((e) => console.debug("tab load failed", e));
    return true;
}

function ensureAllowedActiveTab() {
    const activeId = activeTabId();
    if (activeId && tabAllowed(activeId)) return;
    const first = ["tab-dashboard", "tab-users", "tab-analytics", "tab-menu"]
        .find((id) => tabAllowed(id));
    if (first) activateTab(first, { load: false });
}

function activeTabId() {
    return document.querySelector(".tab-content.active")?.id || "";
}

document.querySelectorAll(".tab-btn").forEach((button) => {
    button.addEventListener("click", () => {
        haptic("light");
        activateTab(button.dataset.tab);
    });
});

document.querySelectorAll("[data-open-tab]").forEach((button) => {
    button.addEventListener("click", () => {
        const target = button.dataset.openTab;
        if (!target || !tabAllowed(target)) return;
        haptic("light");
        activateTab(target);
    });
});

const profileOpen = document.getElementById("profile-open");
if (profileOpen) profileOpen.addEventListener("click", () => activateTab("tab-profile", { load: false }));
const dashboardRefresh = document.getElementById("dashboard-refresh");
if (dashboardRefresh) dashboardRefresh.addEventListener("click", () => loadDashboard());

// Users search/pagination/realtime
const usersRefresh = document.getElementById("users-refresh");
if (usersRefresh) usersRefresh.addEventListener("click", () => loadUsers(false));
const verifyBtn = document.getElementById("verify-connections-btn");
if (verifyBtn) verifyBtn.addEventListener("click", verifyConnections);
const usersPrev = document.getElementById("users-prev");
if (usersPrev) usersPrev.addEventListener("click", () => { if (_userPage > 1) { _userPage -= 1; loadUsers(false); } });
const usersNext = document.getElementById("users-next");
if (usersNext) usersNext.addEventListener("click", () => { _userPage += 1; loadUsers(false); });
const usersSearch = document.getElementById("search-users");
if (usersSearch) usersSearch.addEventListener("input", (e) => {
    invalidateUsersRequest();
    _userSearch = e.target.value.trim();
    if (_userSearchTimer) clearTimeout(_userSearchTimer);
    _userSearchTimer = setTimeout(() => {
        _userSearchTimer = null;
        loadUsers(true);
    }, 250);
});

// ---------------------------------------------------------------------------
// Admin Management — backend-enforced OWNER permission
// ---------------------------------------------------------------------------
async function loadAdmins() {
    if (!can("admins.view")) return;
    const list = document.getElementById("admins-list");
    if (list) list.innerHTML = '<div class="loading-state">Adminlar yuklanmoqda...</div>';
    try {
        const data = await apiFetch("/admins");
        if (list) list.innerHTML = "";
        const admins = data.admins || [];
        if (!admins.length) {
            if (list) list.innerHTML = '<div class="empty-state">Adminlar topilmadi.</div>';
            return;
        }
        admins.forEach((admin) => {
            const roleUpper = String(admin.role || "").toUpperCase();
            const item = document.createElement("div");
            item.className = "connection-item";
            const name = `${admin.first_name || ""} ${admin.last_name || ""}`.trim() || "Noma'lum";
            const identity = `${name}${admin.username ? " · @" + admin.username : ""}`;
            const editable = !!data.can_manage && !!admin.role_editable;
            item.innerHTML = `
                <div class="conn-header">
                    <div class="conn-user">
                        <div class="avatar">${escapeHtml((name.charAt(0) || "?").toUpperCase())}</div>
                        <div class="user-info">
                            <h4>${escapeHtml(identity)}</h4>
                            <span>ID: ${escapeHtml(admin.user_id)}</span>
                            <span>Rol: ${escapeHtml(admin.role)}</span>
                        </div>
                    </div>
                    <span class="status-badge ${admin.is_owner ? "status-enabled" : "status-disabled"}">${admin.is_owner ? "👑 OWNER" : escapeHtml(admin.role)}</span>
                </div>
                <div class="conn-actions" style="display:flex;gap:8px;flex-wrap:wrap;">
                    ${editable ? `
                        <select class="glass-input admin-role-select" data-id="${escapeHtml(admin.user_id)}">
                            <option value="ADMIN" ${roleUpper === "ADMIN" ? "selected" : ""}>ADMIN</option>
                            <option value="MODERATOR" ${roleUpper === "MODERATOR" ? "selected" : ""}>MODERATOR</option>
                            <option value="VIEWER" ${roleUpper === "VIEWER" ? "selected" : ""}>VIEWER</option>
                        </select>
                        <button class="btn btn-secondary admin-save-role" data-id="${escapeHtml(admin.user_id)}">💾 Saqlash</button>
                        <button class="btn btn-danger admin-remove" data-id="${escapeHtml(admin.user_id)}">🗑 Olib tashlash</button>
                    ` : ""}
                </div>
            `;
            item.querySelector(".admin-save-role")?.addEventListener("click", async (event) => {
                const button = event.currentTarget;
                if (button.disabled) return;
                const select = item.querySelector(".admin-role-select");
                button.disabled = true;
                if (select) select.disabled = true;
                try {
                    const res = await apiFetch(`/admins/${encodeURIComponent(admin.user_id)}`, {
                        method: "PATCH",
                        body: JSON.stringify({ role: select?.value || "ADMIN" }),
                    });
                    haptic("success");
                    if (!res.changed) notify(`Admin ${admin.user_id} roli allaqachon ${String(res.role || "").toUpperCase()}.`);
                    else if (res.notification_sent) notify(`Admin ${admin.user_id} roli yangilandi va xabar yuborildi.`);
                    else notify(`Admin ${admin.user_id} roli yangilandi, lekin Telegram xabari yetkazilmadi.`);
                    await loadAdmins();
                    await loadDashboard();
                } catch (e) {
                    haptic("error");
                    notify("Rol o'zgartirilmadi: " + e.message);
                } finally {
                    button.disabled = false;
                    if (select) select.disabled = false;
                }
            });
            item.querySelector(".admin-remove")?.addEventListener("click", (event) => withBusyButton(event.currentTarget, async () => {
                if (!await confirmAction(`${admin.user_id} admin huquqini olib tashlashni tasdiqlaysizmi?`)) return;
                try {
                    const res = await apiFetch(`/admins/${encodeURIComponent(admin.user_id)}`, { method: "DELETE" });
                    haptic("success");
                    notify(res.notification_sent
                        ? `Admin ${admin.user_id} olib tashlandi va xabar yuborildi.`
                        : `Admin ${admin.user_id} olib tashlandi, lekin Telegram xabari yetkazilmadi.`);
                    await loadAdmins();
                } catch (e) {
                    haptic("error");
                    notify("Admin olib tashlanmadi: " + e.message);
                }
            }));
            list?.appendChild(item);
        });
    } catch (e) {
        console.error(e);
        if (list) list.innerHTML = `<div class="empty-state">Yuklab bo'lmadi: ${escapeHtml(e.message)}</div>`;
    }
}

const adminsRefresh = document.getElementById("admins-refresh");
if (adminsRefresh) adminsRefresh.addEventListener("click", loadAdmins);
const adminAdd = document.getElementById("admin-add");
if (adminAdd) adminAdd.addEventListener("click", async () => {
    if (adminAdd.disabled) return;
    const raw = document.getElementById("admin-user-id")?.value.trim();
    const role = document.getElementById("admin-role")?.value || "ADMIN";
    const userId = Number(raw);
    if (!Number.isInteger(userId) || userId <= 0) {
        notify("To'g'ri Telegram ID kiriting.");
        return;
    }
    adminAdd.disabled = true;
    try {
        const res = await apiFetch("/admins", {
            method: "POST",
            body: JSON.stringify({ user_id: userId, role }),
        });
        haptic("success");
        const cleanupWarning = (res.cleanup_warnings || []).length
            ? `\nOgohlantirish: ${(res.cleanup_warnings || []).join(", ")}`
            : "";
        notify((res.notification_sent
            ? `Admin ${userId} qo'shildi va xabar yuborildi.`
            : `Admin ${userId} qo'shildi, lekin Telegram xabari yetkazilmadi.`) + cleanupWarning);
        document.getElementById("admin-user-id").value = "";
        await loadAdmins();
        await loadDashboard();
    } catch (e) {
        haptic("error");
        notify("Admin qo'shilmadi: " + e.message);
    } finally {
        adminAdd.disabled = false;
    }
});

// ---------------------------------------------------------------------------
// Analytics — REAL database data (server aggregates; nothing is hardcoded)
// ---------------------------------------------------------------------------
let _analyticsRange = "24h";
let _analyticsRequest = 0;

async function loadAnalytics({ silent = false } = {}) {
    const requestId = ++_analyticsRequest;
    const range = _analyticsRange;
    const params = new URLSearchParams({ range });
    if (range === "custom") {
        const start = document.getElementById("range-start").value;
        const end = document.getElementById("range-end").value;
        if (!start || !end || start > end) {
            if (!silent) notify("Boshlanish va tugash sanasini to‘g‘ri tanlang.");
            return;
        }
        params.set("start", start);
        params.set("end", end);
    }
    try {
        const data = await apiFetch(`/analytics?${params.toString()}`);
        if (requestId !== _analyticsRequest) return;
        const m = data.metrics || {};
        const set = (id, value) => {
            const el = document.getElementById(id);
            if (el) el.textContent = Number(value ?? 0).toLocaleString("uz-UZ");
        };
        set("m-users-active", m.users_active);
        set("m-users-banned", m.users_banned);
        set("m-conns-active", m.connections_active);
        set("m-conns-total", m.connections_total);
        set("m-messages", m.messages);
        set("m-edited", m.edited);
        set("m-deleted", m.deleted);
        set("m-media", m.media);
        const win = document.getElementById("analytics-window");
        if (win) {
            win.textContent = `${String(data.since).slice(0, 10)} → ${String(data.until).slice(0, 10)} · Jami foydalanuvchilar: ${m.users_total ?? 0}`;
        }
        renderChart(data.series || [], data.bucket);
        const custom = document.getElementById("custom-range");
        if (custom) custom.style.display = _analyticsRange === "custom" ? "grid" : "none";
    } catch (e) {
        if (requestId !== _analyticsRequest) return;
        console.error(e);
        if (!silent) notify("Tahlil yuklanmadi: " + e.message);
    }
}

function renderChart(series, bucket = "day") {
    const chart = document.getElementById("analytics-chart");
    if (!chart) return;
    if (!series.length) {
        chart.innerHTML = '<div class="empty-state">Ma\'lumot yo\'q.</div>';
        return;
    }
    const max = Math.max(1, ...series.map((p) => Number(p.messages || 0)));
    chart.innerHTML = series.map((point) => {
        const value = Number(point.messages || 0);
        const height = Math.max(2, Math.round((value / max) * 100));
        const label = String(point.bucket || "").replace("T", " ");
        const shortLabel = bucket === "hour" ? label.slice(11, 13) + ":00" : label.slice(5, 10);
        return `<div class="bar-wrap" title="${escapeHtml(label)}: ${value}">
            <div class="bar" style="height:${height}%"></div>
            <span>${escapeHtml(shortLabel)}</span>
        </div>`;
    }).join("");
}

document.querySelectorAll(".range-btn").forEach((button) => {
    button.addEventListener("click", () => {
        document.querySelectorAll(".range-btn").forEach((b) => b.classList.remove("active"));
        button.classList.add("active");
        _analyticsRange = button.dataset.range;
        _analyticsRequest += 1;
        if (_analyticsRange !== "custom") loadAnalytics();
        else {
            const custom = document.getElementById("custom-range");
            if (custom) custom.style.display = "grid";
        }
    });
});

const rangeApply = document.getElementById("range-apply");
if (rangeApply) {
    rangeApply.addEventListener("click", () => loadAnalytics());
}
const analyticsRefresh = document.getElementById("analytics-refresh");
if (analyticsRefresh) {
    analyticsRefresh.addEventListener("click", () => loadAnalytics());
}

// ---------------------------------------------------------------------------
// Ad Builder — media + caption + up to 10 URL buttons (text/url/style)
// ---------------------------------------------------------------------------
const MAX_BUTTONS = 10;
const BUTTON_STYLES = ["", "primary", "success", "danger"];
const buttonsEditor = document.getElementById("ad-buttons");

function addButtonRow(text = "", url = "", style = "") {
    if (!buttonsEditor || buttonsEditor.children.length >= MAX_BUTTONS) {
        if (buttonsEditor) notify(`Maksimal ${MAX_BUTTONS} tugma.`);
        return;
    }
    const row = document.createElement("div");
    row.className = "button-row";
    row.innerHTML = `
        <input type="text" class="glass-input btn-text" aria-label="Tugma matni" placeholder="Tugma matni" value="${escapeHtml(text)}">
        <input type="url" class="glass-input btn-url" aria-label="Tugma havolasi" placeholder="https://..." value="${escapeHtml(url)}">
        <select class="glass-input btn-style" aria-label="Tugma rangi">
            ${BUTTON_STYLES.map((s) => `<option value="${s}"${s === style ? " selected" : ""}>${({ "": "Oddiy", primary: "Ko‘k", success: "Yashil", danger: "Qizil" })[s]}</option>`).join("")}
        </select>
        <button class="icon-btn btn-remove" title="O'chirish" aria-label="Tugmani o‘chirish">✖️</button>
    `;
    row.querySelector(".btn-remove").addEventListener("click", () => row.remove());
    buttonsEditor.appendChild(row);
}

function collectButtons() {
    if (!buttonsEditor) return [];
    return Array.from(buttonsEditor.querySelectorAll(".button-row")).map((row) => ({
        text: row.querySelector(".btn-text").value.trim(),
        url: row.querySelector(".btn-url").value.trim(),
        style: row.querySelector(".btn-style").value,
    })).filter((b) => b.text || b.url);
}

function adPayload() {
    return {
        caption: document.getElementById("broadcast-text")?.value.trim() || "",
        media_url: document.getElementById("broadcast-media")?.value.trim() || "",
        media_type: document.getElementById("broadcast-media-type")?.value || "",
        buttons: collectButtons(),
    };
}

function renderStatus(status) {
    _broadcastRunning = !!status.running;
    updateBroadcastButtons();
    const set = (id, value) => {
        const el = document.getElementById(id);
        if (el) el.textContent = Number(value || 0).toLocaleString("uz-UZ");
    };
    set("bc-total", status.total);
    set("bc-sent", status.sent);
    set("bc-failed", status.failed);
    set("bc-pending", status.pending);
    set("bc-unknown", status.unknown);
    const line = document.getElementById("bc-status");
    if (line) {
        if (status.running) {
            line.textContent = `⏳ Yuborilmoqda... (${status.sent}/${status.total})`;
        } else if (status.broadcast_id) {
            line.textContent = `Holat: ${status.interrupted ? "uzilgan (restart)" : "yakunlangan"} · ${status.broadcast_id}` +
                (status.last_error ? ` · oxirgi xato: ${status.last_error}` : "");
        } else {
            line.textContent = "Hali ommaviy xabar yuborilmagan.";
        }
    }
    const failedBox = document.getElementById("bc-failed-list");
    if (failedBox) {
        const sample = status.failed_sample || [];
        failedBox.style.display = sample.length ? "block" : "none";
        failedBox.textContent = sample.length
            ? "Xato olgan oluvchilar (namuna): " + sample.join(", ")
            : "";
    }
}

let _broadcastStatusRequest = 0;
async function fetchBroadcastStatus() {
    const requestId = ++_broadcastStatusRequest;
    const status = await apiFetch("/broadcast/status");
    if (requestId !== _broadcastStatusRequest) return null;
    renderStatus(status);
    return status;
}

function acceptBroadcastStatus(status) {
    // A POST response is newer than any status request already in flight.
    _broadcastStatusRequest += 1;
    renderStatus(status);
}

async function loadBroadcastStatus({ silent = false } = {}) {
    try {
        await fetchBroadcastStatus();
    } catch (e) {
        console.error(e);
        if (!silent) notify("Yuborish holati yangilanmadi: " + e.message);
    }
}

async function pollBroadcast() {
    const started = Date.now();
    while (Date.now() - started < 5 * 60 * 1000) {
        // eslint-disable-next-line no-await-in-loop
        const status = await fetchBroadcastStatus();
        if (status && !status.running) return status;
        // eslint-disable-next-line no-await-in-loop
        await new Promise((resolve) => setTimeout(resolve, 1500));
    }
    return null;
}

const addButtonBtn = document.getElementById("btn-add-button");
if (addButtonBtn) addButtonBtn.addEventListener("click", () => addButtonRow());
if (buttonsEditor && buttonsEditor.children.length === 0) addButtonRow();

const previewBtn = document.getElementById("btn-preview");
if (previewBtn) {
    previewBtn.addEventListener("click", () => withBusyButton(previewBtn, async () => {
        try {
            const data = await apiFetch("/broadcast/preview", {
                method: "POST",
                body: JSON.stringify(adPayload()),
            });
            const box = document.getElementById("preview-box");
            if (box) box.textContent = data.preview_text || "";
            notify(`Ko‘rib chiqish tayyor (${data.recipients} oluvchi).`);
        } catch (e) {
            haptic("error");
            notify("Ko‘rib chiqish xatosi: " + e.message);
        }
    }));
}

const testBtn = document.getElementById("btn-test");
if (testBtn) {
    testBtn.addEventListener("click", async () => {
        if (testBtn.disabled) return;
        const recipient = document.getElementById("test-recipient")?.value.trim();
        const payload = adPayload();
        if (recipient) {
            const targetId = Number(recipient);
            if (!Number.isInteger(targetId) || targetId <= 0) {
                notify("Test oluvchi ID musbat butun son bo'lishi kerak.");
                return;
            }
            payload.test_recipient = targetId;
        }
        testBtn.disabled = true;
        try {
            const data = await apiFetch("/broadcast/test", {
                method: "POST",
                body: JSON.stringify(payload),
            });
            haptic("success");
            notify(`✅ Test yuborildi (ID ${data.target}). Broadcast boshlanmadi.`);
        } catch (e) {
            haptic("error");
            notify("Test yuborilmadi: " + e.message);
        } finally {
            testBtn.disabled = false;
        }
    });
}

const broadcastBtn = document.getElementById("btn-broadcast");
let _broadcastActionPending = false;
let _broadcastRunning = false;
function updateBroadcastButtons() {
    const disabled = _broadcastActionPending || _broadcastRunning;
    ["btn-broadcast", "btn-retry"].forEach((id) => {
        const button = document.getElementById(id);
        if (button) button.disabled = disabled;
    });
}

if (broadcastBtn) {
    broadcastBtn.addEventListener("click", async () => {
        if (_broadcastActionPending || _broadcastRunning || broadcastBtn.disabled) return;
        const payload = adPayload();
        if (!payload.caption && !payload.media_url) {
            notify("Matn yoki media havolasi bo'lishi shart.");
            return;
        }
        haptic("medium");
        _broadcastActionPending = true;
        updateBroadcastButtons();
        try {
            // Validate and count without sending; send exactly the reviewed snapshot.
            const preview = await apiFetch("/broadcast/preview", {
                method: "POST",
                body: JSON.stringify(payload),
            });
            const box = document.getElementById("preview-box");
            if (box) box.textContent = preview.preview_text || "";
            const recipients = Number(preview.recipients);
            if (!Number.isInteger(recipients) || recipients <= 0) {
                notify("Hozir yuborish uchun oluvchilar yo‘q.");
                return;
            }
            if (!await confirmAction(`Ushbu xabar taxminan ${recipients} ta foydalanuvchiga yuboriladi. Yuborishni tasdiqlaysizmi?`)) return;
            const res = await apiFetch("/broadcast", {
                method: "POST",
                body: JSON.stringify(payload),
            });
            acceptBroadcastStatus(res.status || { running: true, total: res.total });
            notify(`✅ Yuborish qabul qilindi (${res.total || 0} oluvchi).`);
            const status = await pollBroadcast();
            if (status) {
                haptic("success");
                notify(
                    `Yakunlandi.\n\nYuborildi: ${status.sent}\nXatolik: ${status.failed}\nNoaniq: ${status.unknown || 0}` +
                    (status.last_error ? `\n\nOxirgi xato: ${status.last_error}` : "")
                );
            } else {
                notify("Holatni olish uchun vaqt tugadi — keyinroq qayta tekshiring.");
            }
        } catch (e) {
            haptic("error");
            if (e.status === 409) {
                acceptBroadcastStatus(e.payload?.status || { running: true });
                notify("Broadcast allaqachon ishlayapti — tugashini kuting.");
            } else if (e.status === 503 && e.payload?.hold_mode) {
                notify("🔴 Hold Mode yoqilgan — broadcast boshlanmaydi.");
            } else {
                notify("Xato: " + e.message);
            }
        } finally {
            _broadcastActionPending = false;
            updateBroadcastButtons();
        }
    });
}

const retryBtn = document.getElementById("btn-retry");
if (retryBtn) {
    retryBtn.addEventListener("click", async () => {
        if (_broadcastActionPending || _broadcastRunning || retryBtn.disabled) return;
        _broadcastActionPending = true;
        updateBroadcastButtons();
        try {
            if (!await confirmAction("Oxirgi yuborishda xato olgan foydalanuvchilarga qayta yuborilsinmi?")) return;
            const res = await apiFetch("/broadcast/retry", { method: "POST" });
            acceptBroadcastStatus(res.status || { running: true, total: res.total });
            notify(`♻️ Faqat xato olganlarga qayta yuborilmoqda (${res.total}).`);
            const status = await pollBroadcast();
            if (status) notify(`Retry yakunlandi. Yuborildi: ${status.sent}, xato: ${status.failed}, noaniq: ${status.unknown || 0}`);
        } catch (e) {
            haptic("error");
            if (e.status === 409) acceptBroadcastStatus(e.payload?.status || { running: true });
            notify(e.status === 409 ? "Broadcast allaqachon ishlayapti." : "Retry: " + e.message);
        } finally {
            _broadcastActionPending = false;
            updateBroadcastButtons();
        }
    });
}

const broadcastRefresh = document.getElementById("broadcast-refresh");
if (broadcastRefresh) broadcastRefresh.addEventListener("click", () => loadBroadcastStatus());

// ---------------------------------------------------------------------------
// Moderation (ban / unban) — server enforces users.moderate
// ---------------------------------------------------------------------------
async function loadBanned() {
    if (!can("users.view")) return;
    const list = document.getElementById("banned-list");
    try {
        const data = await apiFetch("/moderation");
        if (!list) return;
        list.innerHTML = "";
        if (!data.banned || !data.banned.length) {
            list.innerHTML = '<div class="empty-state">Bloklangan foydalanuvchilar yo\'q.</div>';
            return;
        }
        data.banned.forEach((row) => {
            const item = document.createElement("div");
            item.className = "connection-item";
            item.innerHTML = `
                <div class="conn-header">
                    <div class="user-info">
                        <h4>🚫 ${escapeHtml(row.user_id)}</h4>
                        <span>${escapeHtml(row.at || "")} · Admin: ${escapeHtml(row.by)}</span>
                        <span>${escapeHtml(row.reason || "")}</span>
                    </div>
                    ${can("users.moderate") ? '<button class="btn btn-secondary unban-btn">♻️ Blokdan olish</button>' : ''}
                </div>
            `;
            item.querySelector(".unban-btn")?.addEventListener("click", (event) => withBusyButton(event.currentTarget, async () => {
                const ok = await moderate("unban", row.user_id);
                if (ok) await loadUsers(false);
            }));
            list.appendChild(item);
        });
    } catch (e) {
        console.error(e);
        if (list) list.innerHTML = `<div class="empty-state">Ban ro'yxati yuklanmadi: ${escapeHtml(e.message)}</div>`;
    }
}

async function moderate(action, userId, reason) {
    try {
        const res = await apiFetch("/moderation", {
            method: "POST",
            body: JSON.stringify({ action, user_id: userId, reason: reason || "" }),
        });
        haptic("success");
        if (!res.changed) {
            notify(action === "ban"
                ? `Foydalanuvchi ${res.user_id} allaqachon bloklangan.`
                : `Foydalanuvchi ${res.user_id} blokda emas.`);
        } else {
            notify(`${action === "ban" ? "Bloklandi" : "Blokdan olindi"}: ${res.user_id} (jami: ${res.count})`);
        }
        await loadBanned();
        return true;
    } catch (e) {
        haptic("error");
        notify("Xato: " + e.message);
        return false;
    }
}

const banBtn = document.getElementById("btn-ban");
if (banBtn) {
    banBtn.addEventListener("click", async () => {
        if (banBtn.disabled) return;
        const raw = document.getElementById("ban-user-id")?.value.trim();
        const userId = Number(raw);
        if (!raw || !Number.isInteger(userId) || userId <= 0) {
            notify("Foydalanuvchi ID musbat butun son bo'lishi kerak.");
            return;
        }
        banBtn.disabled = true;
        try {
            const ok = await moderate("ban", userId, document.getElementById("ban-reason")?.value.trim());
            if (ok) {
                const idInput = document.getElementById("ban-user-id");
                const reasonInput = document.getElementById("ban-reason");
                if (idInput) idInput.value = "";
                if (reasonInput) reasonInput.value = "";
                await loadUsers(false);
                await loadDashboard();
            }
        } finally {
            banBtn.disabled = false;
        }
    });
}
const moderationRefresh = document.getElementById("moderation-refresh");
if (moderationRefresh) moderationRefresh.addEventListener("click", () => loadBanned());

// ---------------------------------------------------------------------------
// Premium subscriptions / manual card payments
// ---------------------------------------------------------------------------
const subEnabled = document.getElementById("sub-enabled");
const subPrice = document.getElementById("sub-price");
const subDays = document.getElementById("sub-days");
const subCard = document.getElementById("sub-card");
const subHolder = document.getElementById("sub-holder");
const subSave = document.getElementById("sub-save");
const subFilter = document.getElementById("sub-payment-filter");
const subRefresh = document.getElementById("subscriptions-refresh");

function renderSubscriptionMode(enabled) {
    const help = document.getElementById("sub-mode-help");
    if (!help) return;
    help.textContent = enabled
        ? "ON: obuna tugmasi ko‘rinadi, View Once faqat ACTIVE Premium bilan ishlaydi."
        : "OFF: obuna tugmasi yashirin, View Once bepul ishlaydi.";
    help.classList.toggle("status-enabled", !enabled);
}

async function openReceipt(paymentId) {
    try {
        const blob = await apiFetchBlob(`/subscriptions/payments/${encodeURIComponent(paymentId)}/receipt`);
        const url = URL.createObjectURL(blob);
        const a = document.createElement("a");
        a.href = url;
        a.target = "_blank";
        a.rel = "noopener";
        document.body.appendChild(a);
        a.click();
        a.remove();
        setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (e) {
        notify("Chek ochilmadi: " + e.message, "error");
    }
}

function renderPayments(items, canWrite) {
    const list = document.getElementById("subscription-payments");
    if (!list) return;
    list.innerHTML = "";
    if (!items?.length) {
        list.innerHTML = '<div class="empty-state">Bu holatda to‘lov arizasi yo‘q.</div>';
        return;
    }
    items.forEach((p) => {
        const name = `${p.first_name || ""} ${p.last_name || ""}`.trim() || (p.username ? `@${p.username}` : `ID ${p.user_id}`);
        const status = String(p.status || "PENDING").toUpperCase();
        const statusClass = status === "APPROVED" ? "status-enabled" : status === "REJECTED" ? "status-disabled" : "status-neutral";
        const el = document.createElement("div");
        el.className = "panel-card payment-card";
        el.innerHTML = `
            <div class="payment-head"><div><b>${escapeHtml(name)}</b><span>ID: ${escapeHtml(p.user_id)}</span></div><span class="status-badge ${statusClass}">${escapeHtml(status)}</span></div>
            <div class="payment-meta"><span>💎 ${escapeHtml(p.plan_days)} kun</span><span>💰 ${formatMoney(p.amount)} so‘m</span><span>🕐 ${escapeHtml(formatDateTime(p.created_at))}</span></div>
            <div class="payment-actions">
                <button class="btn secondary compact receipt-open" type="button">🧾 Chekni ko‘rish</button>
                ${canWrite && status === "PENDING" ? '<button class="btn primary compact payment-approve" type="button">✅ Tasdiqlash</button><button class="btn danger compact payment-reject" type="button">❌ Rad etish</button>' : ""}
            </div>`;
        el.querySelector(".receipt-open")?.addEventListener("click", () => openReceipt(p.id));
        el.querySelector(".payment-approve")?.addEventListener("click", async (ev) => {
            const btn = ev.currentTarget;
            await withBusyButton(btn, async () => {
                if (!await confirmAction(`${name} uchun ${p.plan_days} kun Premiumni tasdiqlaysizmi?`)) return;
                try {
                    await apiFetch(`/subscriptions/payments/${encodeURIComponent(p.id)}`, { method: "POST", body: JSON.stringify({ action: "approve" }) });
                    haptic("success"); notify("Premium tasdiqlandi."); await loadSubscriptions();
                } catch (e) { haptic("error"); notify("Tasdiqlanmadi: " + e.message, "error"); }
            });
        });
        el.querySelector(".payment-reject")?.addEventListener("click", async (ev) => {
            const reason = window.prompt("Rad etish sababi (ixtiyoriy):", "");
            if (reason === null) return;
            await withBusyButton(ev.currentTarget, async () => {
                try {
                    await apiFetch(`/subscriptions/payments/${encodeURIComponent(p.id)}`, { method: "POST", body: JSON.stringify({ action: "reject", reason }) });
                    haptic("success"); notify("To‘lov rad etildi."); await loadSubscriptions();
                } catch (e) { haptic("error"); notify("Rad etilmadi: " + e.message, "error"); }
            });
        });
        list.appendChild(el);
    });
}

async function loadSubscriptions() {
    if (!can("settings.view")) return;
    try {
        const filter = subFilter?.value || "PENDING";
        const data = await apiFetch(`/subscriptions?status=${encodeURIComponent(filter)}`);
        const cfg = data.config || {};
        const canWrite = !!data.can_write;
        if (subEnabled) { subEnabled.checked = !!cfg.enabled; subEnabled.disabled = !canWrite; }
        if (subPrice) { subPrice.value = cfg.price || 25000; subPrice.disabled = !canWrite; }
        if (subDays) { subDays.value = cfg.days || 30; subDays.disabled = !canWrite; }
        if (subCard) { subCard.value = cfg.card_number || ""; subCard.disabled = !canWrite; }
        if (subHolder) { subHolder.value = cfg.card_holder || ""; subHolder.disabled = !canWrite; }
        if (subSave) subSave.disabled = !canWrite;
        renderSubscriptionMode(!!cfg.enabled);
        setText("sub-active-count", data.summary?.active || 0);
        setText("sub-pending-count", data.summary?.pending || 0);
        setText("sub-approved-count", data.summary?.approved || 0);
        setText("sub-rejected-count", data.summary?.rejected || 0);
        renderPayments(data.payments || [], canWrite);
    } catch (e) {
        console.error(e); notify("Obuna ma’lumotlari yuklanmadi: " + e.message, "error");
    }
}

if (subEnabled) subEnabled.addEventListener("change", () => renderSubscriptionMode(subEnabled.checked));
if (subFilter) subFilter.addEventListener("change", loadSubscriptions);
if (subRefresh) subRefresh.addEventListener("click", loadSubscriptions);
if (subSave) subSave.addEventListener("click", () => withBusyButton(subSave, async () => {
    if (!can("settings.write")) return;
    try {
        const payload = {
            enabled: !!subEnabled?.checked,
            price: Number(subPrice?.value || 0),
            days: Number(subDays?.value || 0),
            card_number: subCard?.value || "",
            card_holder: subHolder?.value || "",
        };
        await apiFetch("/subscriptions/config", { method: "POST", body: JSON.stringify(payload) });
        haptic("success");
        notify(payload.enabled ? "💎 Premium rejim YOQILDI." : "✅ Premium rejim o‘chirildi — View Once bepul.");
        await loadSubscriptions();
    } catch (e) { haptic("error"); notify("Sozlama saqlanmadi: " + e.message, "error"); }
}));

// ---------------------------------------------------------------------------
// Settings (Hold Mode / maintenance notification / retention).
// Server enforces owner-only writes — the checkbox is never trusted.
// The Telegram admin panel toggles the SAME persistent state.
// ---------------------------------------------------------------------------
const maintenanceToggle = document.getElementById("toggle-maintenance");
const holdStatus = document.getElementById("hold-status");
const retentionSelect = document.getElementById("retention-days");
const retentionSave = document.getElementById("retention-save");

function renderHoldStatus(active) {
    if (!holdStatus) return;
    holdStatus.textContent = active ? "🔴 Texnik tanaffus" : "🟢 Bot ishlayapti";
    holdStatus.classList.toggle("status-enabled", !active);
    holdStatus.classList.toggle("status-disabled", !!active);
}

async function loadSettings() {
    if (!can("settings.view")) return;
    try {
        const data = await apiFetch("/settings");
        const canWrite = !!data.can_write;
        if (maintenanceToggle) {
            maintenanceToggle.checked = !!data.maintenance_mode;
            maintenanceToggle.disabled = !canWrite;
            maintenanceToggle.title = canWrite ? "" : "Faqat OWNER";
        }
        if (retentionSelect) {
            retentionSelect.value = String(data.retention_days || 90);
            retentionSelect.disabled = !canWrite;
        }
        if (retentionSave) retentionSave.disabled = !canWrite;
        renderHoldStatus(!!data.maintenance_mode);
    } catch (e) {
        console.error(e);
        if (maintenanceToggle) maintenanceToggle.disabled = true;
        if (retentionSelect) retentionSelect.disabled = true;
        if (retentionSave) retentionSave.disabled = true;
    }
}

if (maintenanceToggle) {
    maintenanceToggle.addEventListener("change", async () => {
        const desired = maintenanceToggle.checked;
        maintenanceToggle.disabled = true;
        try {
            const res = await apiFetch("/settings", {
                method: "POST",
                body: JSON.stringify({ maintenance_mode: desired }),
            });
            maintenanceToggle.checked = !!res.settings?.maintenance_mode;
            renderHoldStatus(maintenanceToggle.checked);
            haptic("success");
            notify(
                maintenanceToggle.checked
                    ? "🔴 Hold Mode YOQILDI — bot texnik xizmatda."
                    : "🟢 Hold Mode O'CHIRILDI — bot normal ishga qaytdi."
            );
        } catch (e) {
            haptic("error");
            maintenanceToggle.checked = !desired;
            notify("O'zgartirib bo'lmadi: " + e.message);
        } finally {
            maintenanceToggle.disabled = false;
        }
    });
}

if (retentionSave) {
    retentionSave.addEventListener("click", async () => {
        if (!can("settings.write")) return;
        const days = Number(retentionSelect?.value || 90);
        retentionSave.disabled = true;
        try {
            const res = await apiFetch("/settings", {
                method: "POST",
                body: JSON.stringify({ retention_days: days }),
            });
            if (retentionSelect && res.settings?.retention_days) {
                retentionSelect.value = String(res.settings.retention_days);
            }
            haptic("success");
            notify(`🗂 Ma'lumot saqlash muddati ${days} kun qilib saqlandi.`);
        } catch (e) {
            haptic("error");
            notify("Retention saqlanmadi: " + e.message);
        } finally {
            retentionSave.disabled = !can("settings.write");
        }
    });
}

// ---------------------------------------------------------------------------
// Event rendering helpers
// ---------------------------------------------------------------------------

function eventVisualType(type) {
    const value = String(type || "").toLowerCase();
    if (value === "delete" || value === "delete_media") return "delete";
    if (value === "edit") return "edit";
    if (["sticker", "photo", "video", "animation", "voice", "video_note", "audio", "document"].includes(value)) return "media";
    return "message";
}

function eventLabel(type) {
    const labels = {
        text: "Matn", edit: "Tahrirlangan", delete: "O‘chirilgan",
        delete_media: "O‘chirilgan media", photo: "Rasm", video: "Video",
        animation: "GIF", sticker: "Stiker", voice: "Ovozli xabar",
        video_note: "Video xabar", audio: "Audio", document: "Fayl",
        connection: "Ulanish",
    };
    return labels[String(type || "")] || String(type || "Hodisa");
}

function formatDateTime(value) {
    if (!value) return "-";
    const raw = String(value);
    const normalized = raw.includes("T") ? raw : raw.replace(" ", "T");
    const date = new Date(normalized);
    if (Number.isNaN(date.getTime())) return raw.replace("T", " ").slice(0, 19);
    try {
        return new Intl.DateTimeFormat("uz-UZ", {
            year: "numeric", month: "2-digit", day: "2-digit",
            hour: "2-digit", minute: "2-digit",
        }).format(date);
    } catch (e) {
        return raw.replace("T", " ").slice(0, 19);
    }
}

function renderEventElement(row) {
    const card = document.createElement("article");
    card.className = "message-card";
    const visual = eventVisualType(row.event_type);
    const icon = visual === "delete" ? "DEL" : visual === "edit" ? "EDIT" : visual === "media" ? "MED" : "MSG";
    const details = String(row.details || "").trim() || "Tafsilot yo‘q";
    card.innerHTML = `
        <div class="event-icon ${visual}">${icon}</div>
        <div class="event-body">
            <div class="event-head">
                <b>${escapeHtml(eventLabel(row.event_type))}</b>
                <time>${escapeHtml(formatDateTime(row.occurred_at))}</time>
            </div>
            <div class="event-meta">User: ${escapeHtml(row.user_id || "-")}${row.message_id ? ` · Msg: ${escapeHtml(row.message_id)}` : ""}${row.chat_title ? ` · ${escapeHtml(row.chat_title)}` : ""}</div>
            <div class="event-details">${escapeHtml(details)}</div>
        </div>`;
    return card;
}

// ---------------------------------------------------------------------------
// User detail sheet + direct message
// ---------------------------------------------------------------------------
let _sheetUserId = 0;
let _sheetUserProtected = false;
let _sheetUserBanned = false;

function closeUserSheet() {
    const overlay = document.getElementById("user-detail-sheet");
    if (!overlay) return;
    overlay.classList.remove("open");
    overlay.setAttribute("aria-hidden", "true");
    document.body.style.overflow = "";
}

document.querySelectorAll("[data-close-sheet]").forEach((el) => el.addEventListener("click", closeUserSheet));

document.querySelectorAll(".detail-tab").forEach((button) => {
    button.addEventListener("click", () => {
        const key = button.dataset.detailTab;
        document.querySelectorAll(".detail-tab").forEach((b) => b.classList.toggle("active", b === button));
        document.querySelectorAll(".detail-pane").forEach((pane) => pane.classList.toggle("active", pane.dataset.detailPane === key));
    });
});

function setText(id, value) {
    const el = document.getElementById(id);
    if (el) el.textContent = value == null || value === "" ? "-" : String(value);
}

function renderSheetEvents(events) {
    const list = document.getElementById("sheet-events");
    if (!list) return;
    list.innerHTML = "";
    if (!events || !events.length) {
        list.innerHTML = '<div class="empty-state">Faollik topilmadi.</div>';
        return;
    }
    events.forEach((row) => list.appendChild(renderEventElement(row)));
}

async function openUserDetail(userId) {
    if (!can("users.view")) return;
    const id = Number(userId);
    if (!Number.isInteger(id) || id <= 0) return;
    const overlay = document.getElementById("user-detail-sheet");
    if (!overlay) return;
    _sheetUserId = id;
    setText("sheet-user-name", "Yuklanmoqda...");
    overlay.classList.add("open");
    overlay.setAttribute("aria-hidden", "false");
    document.body.style.overflow = "hidden";
    try {
        const user = await apiFetch(`/users/${id}`);
        const name = `${user.first_name || ""} ${user.last_name || ""}`.trim() || "Noma’lum";
        _sheetUserProtected = !!user.protected;
        _sheetUserBanned = !!user.banned;
        setText("sheet-user-name", name);
        setText("sheet-avatar", (Array.from(name)[0] || "?").toUpperCase());
        setText("sheet-username", user.username ? `@${user.username}` : "Username yo‘q");
        setText("sheet-user-id", `ID: ${user.user_id}`);
        setText("sheet-full-name", name);
        setText("sheet-username-line", user.username ? `@${user.username}` : "-");
        setText("sheet-created", formatDateTime(user.created_at));
        setText("sheet-last-activity", formatDateTime(user.last_activity));
        setText("sheet-connections", `${user.active_connections || 0} faol / ${user.connections_count || 0} jami`);
        const stats = user.stats || {};
        setText("sheet-stat-events", Number(stats.events_total || 0).toLocaleString("uz-UZ"));
        setText("sheet-stat-edits", Number(stats.edits || 0).toLocaleString("uz-UZ"));
        setText("sheet-stat-deletes", Number((stats.deletes || 0) + (stats.deletes_media || 0)).toLocaleString("uz-UZ"));
        setText("sheet-stat-active", Number(stats.active_connections || 0).toLocaleString("uz-UZ"));
        const sub = user.subscription || {};
        const subStatus = document.getElementById("sheet-sub-status");
        if (subStatus) {
            subStatus.className = `status-badge ${sub.active ? "status-enabled" : "status-neutral"}`;
            subStatus.textContent = sub.active ? "ACTIVE" : "YO‘Q";
        }
        setText("sheet-sub-expiry", sub.active ? (sub.is_lifetime ? "Cheksiz" : formatDateTime(sub.expires_at)) : "-");
        setText("sheet-sub-remaining", sub.active ? (sub.is_lifetime ? "Cheksiz" : `${sub.remaining_days || 0} kun`) : "-");
        const subControls = document.getElementById("sheet-sub-controls");
        if (subControls) subControls.style.display = can("settings.write") ? "grid" : "none";
        const status = document.getElementById("sheet-user-status");
        if (status) {
            status.className = `status-badge ${user.banned ? "status-disabled" : user.connected ? "status-enabled" : "status-neutral"}`;
            status.textContent = user.banned ? "BLOKLANGAN" : user.connected ? "ULANGAN" : "ULANMAGAN";
        }
        renderSheetEvents(user.events || []);
        const ban = document.getElementById("sheet-ban");
        const del = document.getElementById("sheet-delete");
        if (ban) {
            ban.style.display = can("users.moderate") && !user.protected ? "" : "none";
            ban.textContent = user.banned ? "Blokdan olish" : "Bloklash";
            ban.className = user.banned ? "btn secondary" : "btn warning";
        }
        if (del) del.style.display = can("users.delete") && !user.protected ? "" : "none";
        const messageTab = document.querySelector('[data-detail-tab="message"]');
        if (messageTab) messageTab.style.display = can("users.message") ? "" : "none";
        const firstTab = document.querySelector('.detail-tab[data-detail-tab="info"]');
        firstTab?.click();
    } catch (e) {
        notify("Foydalanuvchi profili yuklanmadi: " + e.message, "error");
        closeUserSheet();
    }
}

async function manageSheetSubscription(payload) {
    if (!_sheetUserId || !can("settings.write")) return;
    try {
        await apiFetch(`/subscriptions/users/${_sheetUserId}`, { method: "POST", body: JSON.stringify(payload) });
        haptic("success"); notify("Obuna yangilandi."); await openUserDetail(_sheetUserId);
        if (document.getElementById("tab-subscriptions")?.classList.contains("active")) await loadSubscriptions();
    } catch (e) { haptic("error"); notify("Obuna o‘zgarmadi: " + e.message, "error"); }
}

document.querySelectorAll("[data-sub-days]").forEach((btn) => btn.addEventListener("click", () => {
    manageSheetSubscription({ action: "add_days", days: Number(btn.dataset.subDays || 0) });
}));
document.getElementById("sheet-sub-lifetime")?.addEventListener("click", () => manageSheetSubscription({ action: "lifetime" }));
document.getElementById("sheet-sub-cancel")?.addEventListener("click", async () => {
    if (await confirmAction("Bu user Premium obunasini bekor qilasizmi?")) manageSheetSubscription({ action: "cancel" });
});
document.getElementById("sheet-sub-set-date")?.addEventListener("click", () => {
    const value = document.getElementById("sheet-sub-date")?.value || "";
    if (!value) { notify("Tugash sanasini tanlang."); return; }
    manageSheetSubscription({ action: "set_expiry", expires_at: value });
});

const sheetCopy = document.getElementById("sheet-copy-id");
if (sheetCopy) sheetCopy.addEventListener("click", async () => {
    if (!_sheetUserId) return;
    try {
        await navigator.clipboard.writeText(String(_sheetUserId));
        haptic("success");
        notify(`ID nusxalandi: ${_sheetUserId}`);
    } catch (e) {
        notify(`ID: ${_sheetUserId}`);
    }
});

const sheetSend = document.getElementById("sheet-send-message");
if (sheetSend) sheetSend.addEventListener("click", () => withBusyButton(sheetSend, async () => {
    if (!_sheetUserId || !can("users.message")) return;
    const input = document.getElementById("sheet-message-text");
    const text = input?.value.trim() || "";
    if (!text) { notify("Xabar matnini kiriting."); return; }
    try {
        await apiFetch(`/users/${_sheetUserId}/message`, { method: "POST", body: JSON.stringify({ text }) });
        if (input) input.value = "";
        haptic("success");
        notify("Xabar foydalanuvchiga yuborildi.");
    } catch (e) {
        haptic("error");
        notify("Xabar yuborilmadi: " + e.message, "error");
    }
}));

const sheetBan = document.getElementById("sheet-ban");
if (sheetBan) sheetBan.addEventListener("click", () => withBusyButton(sheetBan, async () => {
    if (!_sheetUserId || _sheetUserProtected || !can("users.moderate")) return;
    if (_sheetUserBanned) {
        if (await moderate("unban", _sheetUserId, "")) {
            await openUserDetail(_sheetUserId);
            await loadUsers(false, { silent: true });
        }
        return;
    }
    const reason = window.prompt("Bloklash sababi (ixtiyoriy):", "");
    if (reason === null) return;
    if (await moderate("ban", _sheetUserId, reason)) {
        await openUserDetail(_sheetUserId);
        await loadUsers(false, { silent: true });
    }
}));

const sheetDelete = document.getElementById("sheet-delete");
if (sheetDelete) sheetDelete.addEventListener("click", () => withBusyButton(sheetDelete, async () => {
    if (!_sheetUserId || _sheetUserProtected || !can("users.delete")) return;
    if (!await confirmAction(`ID ${_sheetUserId} va unga tegishli operational ma’lumotlar butunlay o‘chiriladi. Davom etasizmi?`)) return;
    try {
        await apiFetch(`/users/${_sheetUserId}`, { method: "DELETE" });
        haptic("success");
        notify(`Foydalanuvchi ${_sheetUserId} o‘chirildi.`);
        closeUserSheet();
        await loadUsers(false);
        if (can("dashboard.view")) await loadDashboard({ silent: true });
    } catch (e) {
        haptic("error");
        notify("Foydalanuvchi o‘chirilmadi: " + e.message, "error");
    }
}));

// ---------------------------------------------------------------------------
// Current admin profile
// ---------------------------------------------------------------------------
function renderProfile() {
    const name = adminUser
        ? `${adminUser.first_name || ""} ${adminUser.last_name || ""}`.trim() || "Admin"
        : "Admin";
    const initial = (Array.from(name)[0] || "A").toUpperCase();
    setText("admin-name", name);
    setText("profile-initial", initial);
    setText("profile-avatar-large", initial);
    setText("profile-role", (MY_ROLE || "-").toUpperCase());
    setText("profile-user-id", MY_ID || adminUser?.id || "-");
    setText("profile-role-line", (MY_ROLE || "-").toUpperCase());
    setText("profile-permission-count", MY_PERMISSIONS.length);
    const box = document.getElementById("profile-permissions");
    if (box) {
        box.innerHTML = "";
        MY_PERMISSIONS.slice().sort().forEach((permission) => {
            const chip = document.createElement("span");
            chip.textContent = permission;
            box.appendChild(chip);
        });
        if (!MY_PERMISSIONS.length) box.innerHTML = '<span>Ruxsatlar yo‘q</span>';
    }
}

const profileReload = document.getElementById("profile-reload");
if (profileReload) profileReload.addEventListener("click", () => window.location.reload());
const profileClose = document.getElementById("profile-close");
if (profileClose) profileClose.addEventListener("click", () => {
    if (tg && typeof tg.close === "function") tg.close();
    else notify("Mini App’ni Telegram oynasidan yopishingiz mumkin.");
});

// ---------------------------------------------------------------------------
// Realtime refresh + initial load
// ---------------------------------------------------------------------------
let _realtimeTimer = null;
function startRealtimeRefresh() {
    if (_realtimeTimer) clearTimeout(_realtimeTimer);
    const tick = async () => {
        try {
            if (document.hidden || _activeActions || _userSearchTimer || _broadcastActionPending) return;
            const tab = activeTabId();
            // Avoid replacing controls while the user is interacting with them.
            const section = document.getElementById(tab);
            if (tab !== "tab-broadcast" && section?.contains(document.activeElement) && document.activeElement.matches("input, textarea, select")) return;
            if (["tab-dashboard", "tab-users", "tab-analytics", "tab-broadcast", "tab-moderation"].includes(tab)) {
                await loadTab(tab, { silent: true });
            }
        } catch (e) {
            console.debug("realtime refresh failed", e);
        } finally {
            // Wait until this request settles before scheduling another one.
            _realtimeTimer = setTimeout(tick, 7000);
        }
    };
    _realtimeTimer = setTimeout(tick, 7000);
}

async function bootstrapPanel() {
    try {
        // Minimal identity endpoint: analytics buzilsa ham panel auth/bootstrap ishlaydi.
        const identity = await apiFetch("/me");
        MY_ID = Number(identity.user_id || 0);
        MY_ROLE = identity.role || "";
        MY_PERMISSIONS = identity.permissions || [];
        applyRoleUi();
        renderProfile();
        ensureAllowedActiveTab();
        const tab = activeTabId();
        if (tab) await loadTab(tab);
        startRealtimeRefresh();
    } catch (e) {
        console.error(e);
        notify("Admin panel yuklanmadi: " + e.message);
    }
}

if (!INIT_DATA) {
    notify("Telegram initData topilmadi — panelni Telegram ichidan oching.");
} else {
    bootstrapPanel();
}
