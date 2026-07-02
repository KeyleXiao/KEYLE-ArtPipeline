/**
 * 运行日志悬浮按钮 + 抽屉（主界面与后处理共用）
 */
import { API } from "./api.js";
import { t } from "./i18n.js";

const $ = (s) => document.querySelector(s);

const LOG_FAB_POS_KEY = "artApp.logFabPos";
const LOG_FAB_DRAG_THRESHOLD = 5;

let logFilter = "全部";
let logEs = null;

function logEntryTime(entry) {
  return entry?.ts ?? entry?.time ?? "—";
}

function logEntryText(entry) {
  return entry?.msg ?? entry?.message ?? "";
}

function formatLogLine(entry) {
  return `[${logEntryTime(entry)}] [${entry?.kind ?? "?"}] ${logEntryText(entry)}`;
}

export function appendLog(entry) {
  const tab = logFilter;
  if (tab !== "全部" && entry.kind !== tab) return;
  const pre = $("#log-body");
  if (!pre) return;
  pre.textContent += `${formatLogLine(entry)}\n`;
  if (pre.textContent.length > 120000) {
    pre.textContent = pre.textContent.slice(-80000);
  }
  pre.scrollTop = pre.scrollHeight;
}

export function openLogDrawer() {
  const drawer = $("#log-drawer");
  const fab = $("#log-fab");
  drawer?.classList.add("open");
  drawer?.setAttribute("aria-hidden", "false");
  fab?.classList.add("active");
  fab?.setAttribute("aria-expanded", "true");
  const pre = $("#log-body");
  if (pre) pre.scrollTop = pre.scrollHeight;
}

export function closeLogDrawer() {
  const drawer = $("#log-drawer");
  const fab = $("#log-fab");
  drawer?.classList.remove("open");
  drawer?.setAttribute("aria-hidden", "true");
  fab?.classList.remove("active");
  fab?.setAttribute("aria-expanded", "false");
}

function toggleLogDrawer() {
  if ($("#log-drawer")?.classList.contains("open")) closeLogDrawer();
  else openLogDrawer();
}

function clampLogFabPosition(x, y, fab) {
  const el = fab || $("#log-fab");
  if (!el) return { x, y };
  const pad = 8;
  const w = el.offsetWidth || 48;
  const h = el.offsetHeight || 28;
  return {
    x: Math.max(pad, Math.min(x, window.innerWidth - w - pad)),
    y: Math.max(pad, Math.min(y, window.innerHeight - h - pad)),
  };
}

function applyLogFabPosition(x, y, fab, persist = true) {
  const el = fab || $("#log-fab");
  if (!el) return;
  const p = clampLogFabPosition(x, y, el);
  el.style.left = `${p.x}px`;
  el.style.top = `${p.y}px`;
  el.style.right = "auto";
  el.style.bottom = "auto";
  if (persist) {
    try {
      localStorage.setItem(LOG_FAB_POS_KEY, JSON.stringify(p));
    } catch {
      /* ignore */
    }
  }
  return p;
}

function initLogFabPosition() {
  const fab = $("#log-fab");
  if (!fab) return;
  try {
    const raw = localStorage.getItem(LOG_FAB_POS_KEY);
    if (raw) {
      const p = JSON.parse(raw);
      if (Number.isFinite(p.x) && Number.isFinite(p.y)) {
        applyLogFabPosition(p.x, p.y, fab);
        return;
      }
    }
  } catch {
    /* ignore */
  }
  fab.style.left = "12px";
  fab.style.bottom = "12px";
  fab.style.top = "auto";
  fab.style.right = "auto";
}

function bindLogFabDrag() {
  const fab = $("#log-fab");
  if (!fab) return;
  initLogFabPosition();

  let drag = null;

  fab.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    const rect = fab.getBoundingClientRect();
    fab.style.left = `${rect.left}px`;
    fab.style.top = `${rect.top}px`;
    fab.style.right = "auto";
    fab.style.bottom = "auto";
    drag = {
      pointerId: e.pointerId,
      startX: e.clientX,
      startY: e.clientY,
      origX: rect.left,
      origY: rect.top,
      moved: false,
    };
    fab.setPointerCapture(e.pointerId);
    e.preventDefault();
  });

  fab.addEventListener("pointermove", (e) => {
    if (!drag || e.pointerId !== drag.pointerId) return;
    const dx = e.clientX - drag.startX;
    const dy = e.clientY - drag.startY;
    if (!drag.moved && Math.abs(dx) + Math.abs(dy) < LOG_FAB_DRAG_THRESHOLD) return;
    drag.moved = true;
    fab.classList.add("is-dragging");
    applyLogFabPosition(drag.origX + dx, drag.origY + dy, fab, false);
  });

  const endDrag = (e) => {
    if (!drag || e.pointerId !== drag.pointerId) return;
    const wasDrag = drag.moved;
    drag = null;
    fab.classList.remove("is-dragging");
    try {
      fab.releasePointerCapture(e.pointerId);
    } catch {
      /* ignore */
    }
    if (wasDrag) {
      const x = parseInt(fab.style.left, 10);
      const y = parseInt(fab.style.top, 10);
      if (Number.isFinite(x) && Number.isFinite(y)) applyLogFabPosition(x, y, fab, true);
    } else {
      toggleLogDrawer();
    }
  };

  fab.addEventListener("pointerup", endDrag);
  fab.addEventListener("pointercancel", endDrag);

  fab.addEventListener(
    "click",
    (e) => {
      e.preventDefault();
    },
    true,
  );

  window.addEventListener("resize", () => {
    const x = parseInt(fab.style.left, 10);
    const y = parseInt(fab.style.top, 10);
    if (Number.isFinite(x) && Number.isFinite(y)) applyLogFabPosition(x, y, fab);
  });
}

function connectLogs() {
  if (logEs) logEs.close();
  const pre = $("#log-body");
  if (pre) pre.textContent = "";
  logEs = new EventSource("/api/logs/stream");
  logEs.onmessage = (ev) => {
    try {
      appendLog(JSON.parse(ev.data));
    } catch {
      /* ignore */
    }
  };
  logEs.onerror = () => {
    logEs?.close();
    setTimeout(connectLogs, 3000);
  };
}

async function reloadLogsHistory() {
  const data = await API.get(`/api/logs?tab=${encodeURIComponent(logFilter)}`);
  const pre = $("#log-body");
  if (!pre) return;
  pre.textContent = (data.entries || []).map(formatLogLine).join("\n");
  pre.scrollTop = pre.scrollHeight;
}

function bindLogDrawerActions() {
  $("#log-filter")?.addEventListener("change", (e) => {
    logFilter = e.target.value;
    reloadLogsHistory().catch(() => {});
  });

  document.querySelector('[data-action="clear-logs"]')?.addEventListener("click", async () => {
    await API.del("/api/logs");
    const pre = $("#log-body");
    if (pre) pre.textContent = "";
  });

  document.querySelector('[data-action="close-logs"]')?.addEventListener("click", () => {
    closeLogDrawer();
  });

  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && $("#log-drawer")?.classList.contains("open")) {
      closeLogDrawer();
    }
  });
}

/** 挂载日志 FAB + 抽屉；需在 DOM 就绪且 i18n 初始化后调用 */
export function initLogPanel() {
  if (!$("#log-fab") || !$("#log-drawer")) return;
  bindLogFabDrag();
  bindLogDrawerActions();
  connectLogs();
  reloadLogsHistory().catch(() => {});
}
