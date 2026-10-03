"use strict";
/* ==========================================================================
   Fengcode Web UI —— 零构建单页应用
   全部逻辑集中在此文件，便于打包分发与离线使用。
   ========================================================================== */

/* ---------------- 基础工具 ---------------- */
const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => Array.from((r || document).querySelectorAll(s));
/** 安全写文本：元素不存在就跳过（设置中心搬运面板后元素可能已不在原位）。 */
function setText(sel, text) {
  const el = $(sel);
  if (el) el.textContent = text;
  return el;
}
/** 安全绑事件：元素不存在就跳过。 */
function onClick(sel, fn) {
  const el = $(sel);
  if (el) el.onclick = fn;
  return el;
}
/** 安全读输入框的值。
 *
 * 设置中心一次只显示一个设置面板，保存时其余面板的元素并不存在，
 * 直接读 .value 会抛 `Cannot read properties of null`。
 * 不存在时返回空字符串（后端会保留原值，因为 patch 里空值会被忽略）。
 */
function val(sel) {
  const el = $(sel);
  return el ? el.value : "";
}
/** 安全读复选框状态；元素不存在时返回 undefined（调用方跳过该字段）。 */
function chk(sel) {
  const el = $(sel);
  return el ? el.checked : undefined;
}
const esc = (s) => String(s == null ? "" : s)
  .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
  .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
const fmtBytes = (n) => {
  n = Number(n) || 0;
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
};
// 生成 <option> 列表：value 用 ref，第一项是"跟随默认"之类的空值项。
function modelOpts(models, current, placeholder) {
  const opts = [`<option value=""${!current ? " selected" : ""}>${esc(placeholder || "自动")}</option>`];
  (models || []).forEach((m) => {
    opts.push(`<option value="${esc(m.ref)}"${m.ref === current ? " selected" : ""}>` +
      `${esc(m.provider_display)} / ${esc(m.model)}${m.has_key ? "" : "（无密钥）"}</option>`);
  });
  return opts.join("");
}
/* ★ 统一的数字显示（K / M 缩写）——**全站唯一入口**。
   为什么要收口：此前同一屏里既出现 `44800` 又出现 `44.8K`（状态栏用一套、
   信息面板用另一套，各自还带一个本地 kw 函数四舍五入），用户看着乱。
   规则（与上游后台口径一致，按 1000 进位）：
     < 1000        → 原样（如 512）
     < 100000      → 一位小数的 K（如 4.5K、44.8K）
     < 1000000     → 整数 K（如 250K，避免三位有效数字过于啰嗦）
     >= 1000000    → 两位小数的 M（如 1.05M）
   注意：整数 K 那档是刻意的 —— 25 万显示成「250K」比「250.0K」干净。 */
const fmtNum = (n) => {
  n = Number(n) || 0;
  if (n < 1000) return String(n);
  if (n < 100000) return (n / 1000).toFixed(1) + "K";
  if (n < 1e6) return Math.round(n / 1000) + "K";
  return (n / 1e6).toFixed(2) + "M";
};
/* ★ 1-N：费用统一走这里，**不硬编码 ¥**。
   为什么必须收口：USD 账户下硬编 ¥ 会把金额显示成错的。
   currency 可能是符号（¥/$）或代码（CNY/USD/""），两种都要认。 */
const CUR_SYMBOL = { CNY: "¥", RMB: "¥", USD: "$", EUR: "€", GBP: "£", JPY: "¥", HKD: "HK$" };
const fmtCost = (amount, currency, digits) => {
  const n = Number(amount) || 0;
  let c = String(currency == null ? "" : currency).trim();
  let sym;
  if (!c) {
    // 后端没给币种时，跟随用户的「费用显示币种」设置（默认 CNY）
    try { c = (localStorage.getItem("fengcode_currency") || "CNY"); } catch (e) { c = "CNY"; }
  }
  if (CUR_SYMBOL[c.toUpperCase()]) sym = CUR_SYMBOL[c.toUpperCase()];
  else if (/^[^\w\s]{1,3}$/.test(c)) sym = c;   // 本身就是符号：¥ / $ / € …
  else sym = "";
  const d = digits == null ? 4 : digits;
  return sym ? `${sym}${n.toFixed(d)}` : `${n.toFixed(d)} ${c}`.trim();
};
const fmtTime = (ts) => {
  if (!ts) return "—";
  const d = new Date(ts * 1000);
  const p = (x) => String(x).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}`;
};
const fmtDur = (s) => {
  s = Number(s) || 0;
  if (s < 1) return (s * 1000).toFixed(0) + " ms";
  if (s < 60) return s.toFixed(1) + " s";
  const m = Math.floor(s / 60), r = Math.floor(s % 60);
  if (m < 60) return `${m} 分 ${r} 秒`;
  return `${Math.floor(m / 60)} 时 ${m % 60} 分`;
};
const ago = (ts) => {
  if (!ts) return "—";
  const d = Date.now() / 1000 - ts;
  if (d < 60) return "刚刚";
  if (d < 3600) return Math.floor(d / 60) + " 分钟前";
  if (d < 86400) return Math.floor(d / 3600) + " 小时前";
  if (d < 2592000) return Math.floor(d / 86400) + " 天前";
  return fmtTime(ts).slice(0, 10);
};

/* ==========================================================================
   图标系统：内联 SVG，线性抽象风
   ---------------------------------------------------------------------
   为什么不用 emoji：
     · 各平台渲染差异大（Windows/macOS/Linux 长得不一样）
     · 彩色卡通感强，不适合工具类界面
     · 无法跟随主题色，深色下对比度差
   这里统一用 1.7px 描边的线性图标，颜色走 currentColor。
   ========================================================================== */
const ICONS = {
  // —— 导航 ——
  chat: '<path d="M4 5.5h16v11H8.5L4 20.2V5.5z"/>',
  folder: '<path d="M3 6.5h5.2l1.8 2H21v9.5H3z"/>',
  folderOpen: '<path d="M3 6.5h5.2l1.8 2H21"/><path d="M3 18.5l1.5-7h17l-1.6 7z"/>',
  brain: '<path d="M9.5 4.5a3 3 0 0 0-3 3 2.6 2.6 0 0 0-1 5 3 3 0 0 0 2.6 4.4V19a1.5 1.5 0 0 0 3 0v-9a2.5 2.5 0 0 0-1.6-5.5z"/><path d="M14.5 4.5a3 3 0 0 1 3 3 2.6 2.6 0 0 1 1 5 3 3 0 0 1-2.6 4.4V19a1.5 1.5 0 0 1-3 0"/>',
  bolt: '<path d="M13.5 3.5 6 13h5l-1 7.5L18 11h-5z"/>',
  wrench: '<path d="M15.5 4.5a4.5 4.5 0 0 0-4 6.7L4.8 18a1.7 1.7 0 0 0 2.4 2.4l6.8-6.8a4.5 4.5 0 0 0 5.5-5.8l-2.6 2.6-2.2-.6-.6-2.2 2.6-2.6a4.5 4.5 0 0 0-1.2-.5z"/>',
  plug: '<path d="M9 3.5v5M15 3.5v5"/><path d="M6.5 8.5h11v3a5.5 5.5 0 0 1-5.5 5.5 5.5 5.5 0 0 1-5.5-5.5z"/><path d="M12 17v3.5"/>',
  puzzle: '<path d="M9.5 4.5h5v2.2a1.8 1.8 0 1 0 0 3.6V12h2.3a1.8 1.8 0 1 1 0 3.6H14.5v3.9h-5v-2.2a1.8 1.8 0 0 1-2.4-1.7 1.8 1.8 0 0 1 2.4-1.7V12H7.2a1.8 1.8 0 1 1 0-3.6h2.3z"/>',
  flow: '<circle cx="6" cy="6" r="2.2"/><circle cx="18" cy="12" r="2.2"/><circle cx="6" cy="18" r="2.2"/><path d="M8.2 6h4.3a3 3 0 0 1 3 3v.8M8.2 18h4.3a3 3 0 0 0 3-3v-.8"/>',
  clock: '<circle cx="12" cy="12" r="8"/><path d="M12 7.5V12l3 2"/>',
  server: '<rect x="3.5" y="4.5" width="17" height="6" rx="1.5"/><rect x="3.5" y="13.5" width="17" height="6" rx="1.5"/><path d="M7 7.5h.01M7 16.5h.01"/>',
  chart: '<path d="M4 19.5h16"/><rect x="6" y="11" width="3" height="6"/><rect x="11" y="7" width="3" height="10"/><rect x="16" y="13.5" width="3" height="3.5"/>',
  clipboard: '<rect x="5.5" y="4.5" width="13" height="15" rx="1.5"/><path d="M9 4.5V3.4A.9.9 0 0 1 9.9 2.5h4.2a.9.9 0 0 1 .9.9v1.1z"/><path d="M9 10h6M9 13.5h6M9 17h3.5"/>',
  gear: '<path d="M12 15.4a3.4 3.4 0 1 0 0-6.8 3.4 3.4 0 0 0 0 6.8z"/><path d="M19.4 15a1.7 1.7 0 0 0 .34 1.88l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.7 1.7 0 0 0-1.88-.34 1.7 1.7 0 0 0-1.03 1.55V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.55 1.7 1.7 0 0 0-1.88.34l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06a1.7 1.7 0 0 0 .34-1.88 1.7 1.7 0 0 0-1.55-1.03H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.55-1.1 1.7 1.7 0 0 0-.34-1.88l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06a1.7 1.7 0 0 0 1.88.34H9a1.7 1.7 0 0 0 1.03-1.55V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1.03 1.55 1.7 1.7 0 0 0 1.88-.34l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06a1.7 1.7 0 0 0-.34 1.88V9a1.7 1.7 0 0 0 1.55 1.03H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
  users: '<circle cx="9" cy="8" r="3"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><path d="M16 5.6a3 3 0 0 1 0 5.4M17 13.7a5.5 5.5 0 0 1 3.5 5.3"/>',
  shield: '<path d="M12 3.2 19 6v6c0 4.2-3 7.2-7 8.8-4-1.6-7-4.6-7-8.8V6z"/><path d="M9.2 12.2l2 2 3.6-3.8"/>',
  flask: '<path d="M9.5 3.5h5v4l3.8 9.4a2 2 0 0 1-1.8 2.8H7.5a2 2 0 0 1-1.8-2.8L9.5 7.5z"/><path d="M7 14.5h10"/>',
  robot: '<rect x="4.5" y="7.5" width="15" height="11" rx="2.5"/><path d="M12 4v3.5"/><circle cx="9" cy="12.5" r="1"/><circle cx="15" cy="12.5" r="1"/><path d="M9.5 16h5"/><path d="M2.5 11.5v3M21.5 11.5v3"/>',
  // —— 操作 ——
  plus: '<path d="M12 5v14M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="6.2"/><path d="M15.6 15.6 20 20"/>',
  trash: '<path d="M4.5 7h15"/><path d="M9.5 7V5.2a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1V7"/><path d="M6.5 7l.9 12.1a1.4 1.4 0 0 0 1.4 1.3h6.4a1.4 1.4 0 0 0 1.4-1.3L17.5 7"/><path d="M10.5 11v5.5M13.5 11v5.5"/>',
  more: '<circle cx="5.5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="18.5" cy="12" r="1.4"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7"/>',
  chevronLeft: '<path d="M14.5 6.5L9 12l5.5 5.5"/>',
  moreHorizontal: '<circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/>',
  clock: '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>',
  box: '<path d="M4 8.5L12 4l8 4.5v7L12 20l-8-4.5z"/><path d="M4 8.5L12 13l8-4.5"/><path d="M12 13v7"/>',
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5"/><circle cx="12" cy="7.8" r="0.9" fill="currentColor" stroke="none"/>',
  sparkles: '<path d="M12 3l1.8 4.6L18 9.4l-4.2 1.8L12 15.8l-1.8-4.6L6 9.4l4.2-1.8z"/><path d="M18.5 15.5l.8 2 2 .8-2 .8-.8 2-.8-2-2-.8 2-.8z"/>',
  wand: '<path d="M4 20L16 8"/><path d="M14 4l1 2.2L17.2 7 15 8l-1 2.2L13 8 10.8 7 13 6.2z"/><path d="M19 14l.7 1.6 1.6.7-1.6.7-.7 1.6-.7-1.6-1.6-.7 1.6-.7z"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 4v4h-4"/>',
  download: '<path d="M12 4v11"/><path d="M7.5 11 12 15.5 16.5 11"/><path d="M5 19.5h14"/>',
  upload: '<path d="M12 16V5"/><path d="M7.5 9.5 12 5l4.5 4.5"/><path d="M5 19.5h14"/>',
  edit: '<path d="M4.5 19.5h4l10-10a2.1 2.1 0 0 0-3-3l-10 10z"/><path d="M14.5 7.5l2 2"/>',
  star: '<path d="m12 4 2.4 5.1 5.6.7-4.1 3.9 1 5.6L12 16.6 7.1 19.3l1-5.6L4 9.8l5.6-.7z"/>',
  send: '<path d="M4.5 12 20 5l-6.5 15-2.2-6.3z"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="1.5"/>',
  file: '<path d="M6.5 3.5h7l4.5 4.5v12.5h-11.5z"/><path d="M13.5 3.5V8h4.5"/>',
  image: '<rect x="3.5" y="5" width="17" height="14" rx="2"/><circle cx="8.5" cy="10" r="1.6"/><path d="M4.5 17l5-5 3.5 3.5 3-2.5 4 4"/>',
  link: '<path d="M9.5 14.5 14.5 9.5"/><path d="M11 6.5l1.5-1.5a3.5 3.5 0 0 1 5 5L16 11.5"/><path d="M13 17.5 11.5 19a3.5 3.5 0 0 1-5-5L8 12.5"/>',
  history: '<path d="M3.8 12a8.2 8.2 0 1 0 2.6-6"/><path d="M3.5 4v4h4"/><path d="M12 8v4.5l3 1.8"/>',
  // —— 状态 ——
  info: '<circle cx="12" cy="12" r="8.5"/><path d="M12 11v5.5"/><path d="M12 7.8h.01"/>',
  warning: '<path d="M12 4 21 19.5H3z"/><path d="M12 9.5V14"/><path d="M12 16.8h.01"/>',
  error: '<circle cx="12" cy="12" r="8.5"/><path d="M9 9l6 6M15 9l-6 6"/>',
  lock: '<rect x="5" y="10.5" width="14" height="9.5" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 7 0v2.5"/>',
  unlock: '<rect x="5" y="10.5" width="14" height="9.5" rx="2"/><path d="M8.5 10.5V8a3.5 3.5 0 0 1 6.7-1.4"/>',
  eye: '<path d="M2.5 12S6 6.5 12 6.5 21.5 12 21.5 12 18 17.5 12 17.5 2.5 12 2.5 12z"/><circle cx="12" cy="12" r="2.8"/>',
  chevronRight: '<path d="M9.5 6 15.5 12l-6 6"/>',
  chevronDown: '<path d="M6 9.5 12 15.5l6-6"/>',
  arrowUp: '<path d="M12 19V5"/><path d="M6.5 10.5 12 5l5.5 5.5"/>',
  layers: '<path d="m12 3.5 8.5 4.5L12 12.5 3.5 8z"/><path d="m3.5 12.5 8.5 4.5 8.5-4.5"/><path d="m3.5 16.5 8.5 4.5 8.5-4.5"/>',
  target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1"/>',
  listChecks: '<path d="M4 6.5l1.8 1.8L9 5"/><path d="M4 12.5l1.8 1.8L9 11"/><path d="M4 18.5l1.8 1.8L9 17"/><path d="M12 6.5h8M12 12.5h8M12 18.5h8"/>',
  sparkle: '<path d="m12 4 1.8 5.2L19 11l-5.2 1.8L12 18l-1.8-5.2L5 11l5.2-1.8z"/>',
  paperclip: '<path d="M17 8.5 9.8 15.7a2.5 2.5 0 0 1-3.5-3.5l8-8a3.5 3.5 0 0 1 5 5l-8 8a4.5 4.5 0 0 1-6.4-6.4L12 4.5"/>',
  terminal: '<path d="M5 7l4 4-4 4"/><path d="M11.5 17h8"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.2 2.4 3.3 5.3 3.3 8.5s-1.1 6.1-3.3 8.5c-2.2-2.4-3.3-5.3-3.3-8.5S9.8 5.9 12 3.5z"/>',
  database: '<ellipse cx="12" cy="6.5" rx="7.5" ry="3"/><path d="M4.5 6.5v11c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-11"/><path d="M4.5 12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3"/>',
  key: '<circle cx="8" cy="15.5" r="3.5"/><path d="M10.5 13 19 4.5"/><path d="M16.5 7l2.5 2.5"/>',
  palette: '<path d="M12 3.5a8.5 8.5 0 0 0 0 17c1 0 1.7-.8 1.7-1.7 0-.5-.2-.9-.5-1.2-.3-.3-.5-.7-.5-1.2 0-1 .8-1.7 1.7-1.7h1.9a4.2 4.2 0 0 0 4.2-4.2c0-3.9-3.8-7-8.5-7z"/><circle cx="7.8" cy="10" r="1.1"/><circle cx="11.5" cy="7.5" r="1.1"/><circle cx="15.8" cy="9.5" r="1.1"/>',
  code: '<path d="M8.5 8 4.5 12l4 4"/><path d="M15.5 8l4 4-4 4"/><path d="M13.5 5.5l-3 13"/>',
  copy: '<rect x="8" y="8" width="11" height="11" rx="1.5"/><path d="M5 15V5a2 2 0 0 1 2-2h10"/>',
  quote: '<path d="M5 5.5h14v10H9l-4 3v-3H5z"/><path d="M8 9h8M8 12h5"/>',
  package: '<path d="m12 3.5 8 4.3v8.4l-8 4.3-8-4.3V7.8z"/><path d="m4 7.8 8 4.3 8-4.3"/><path d="M12 12.1v8.4"/>',
  cpu: '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3.5V7M14 3.5V7M10 17v3.5M14 17v3.5M3.5 10H7M3.5 14H7M17 10h3.5M17 14h3.5"/>',
  home: '<path d="M4 10.5 12 4l8 6.5V20H4z"/><path d="M9.5 20v-6h5v6"/>',
  compass: '<circle cx="12" cy="12" r="8.5"/><path d="m15 9-2 5-5 2 2-5z"/>',
  play: '<path d="M7 4.5 19 12 7 19.5z"/>',
};

/** 生成一个图标的 HTML。size 默认 16px */
function icon(name, size, cls) {
  const body = ICONS[name] || ICONS.info;
  const s = size || 16;
  return `<svg class="ic${cls ? " " + cls : ""}" width="${s}" height="${s}" viewBox="0 0 24 24" ` +
    `fill="none" stroke="currentColor" stroke-width="1.7" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}

/* 极简 Markdown 渲染（转义优先，支持代码块/行内码/标题/列表/粗斜体/链接/表格/引用） */
function md(text) {
  if (!text) return "";
  const blocks = [];
  let s = String(text);
  // 先取出代码块，避免被后续规则污染
  s = s.replace(/```([a-zA-Z0-9_+-]*)\n?([\s\S]*?)```/g, (m, lang, code) => {
    const l = (lang || "").toLowerCase();
    const body = code.replace(/\n$/, "");
    // ★ 2-M：mermaid 代码块先落成占位，等这段内容**写完**后再交给 Mermaid 画图
    //   （绝不能跟着流式每 50ms 重画一次 —— 图还没写完就画会报错、还会把界面拖卡）
    if (l === "mermaid") {
      // 落成占位：未渲染时**显示原始代码**（可读、可复制），写完后再换成图
      blocks.push(`<div class="mermaid-src" data-src="${esc(body)}"><pre class="rich-fallback"><code>${esc(body)}</code></pre></div>`);
    } else {
      blocks.push(`<pre><code data-lang="${esc(lang)}">${esc(body)}</code></pre>`);
    }
    return `\u0000B${blocks.length - 1}\u0000`;
  });
  // ★ 2-Q：数学公式（先 $$ 块级，再 $ 行内）。必须在 esc 之前提取 ——
  //   否则公式里的 "<" "&" 会被转义成 &lt; / &amp;，KaTeX 就渲染错了。
  //   行内规则要求开、闭 $ 紧邻处不是空格，这样 "$5 and $10" 这类价格不会被误判。
  s = s.replace(/\$\$([\s\S]+?)\$\$/g, (m, tex) => {
    blocks.push(`<span class="math-src" data-display="1" data-tex="${esc(tex.trim())}">${esc(tex.trim())}</span>`);
    return `\u0000B${blocks.length - 1}\u0000`;
  });
  s = s.replace(/\$(?!\s)([^\$\n]*?[^\s\$])\$/g, (m, tex) => {
    blocks.push(`<span class="math-src" data-display="0" data-tex="${esc(tex)}">${esc(tex)}</span>`);
    return `\u0000B${blocks.length - 1}\u0000`;
  });
  s = esc(s);
  // 表格
  s = s.replace(/(^\|.+\|\s*$\n^\|[\s:|-]+\|\s*$\n(?:^\|.*\|\s*$\n?)*)/gm, (tbl) => {
    const lines = tbl.trim().split("\n");
    if (lines.length < 2) return tbl;
    const cells = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => c.trim());
    const head = cells(lines[0]);
    let html = '<table><thead><tr>' + head.map((c) => `<th>${c}</th>`).join("") + "</tr></thead><tbody>";
    for (let i = 2; i < lines.length; i++) {
      html += "<tr>" + cells(lines[i]).map((c) => `<td>${c}</td>`).join("") + "</tr>";
    }
    return html + "</tbody></table>\n";
  });
  // 标题
  s = s.replace(/^######\s+(.+)$/gm, "<h4>$1</h4>")
       .replace(/^#####\s+(.+)$/gm, "<h4>$1</h4>")
       .replace(/^####\s+(.+)$/gm, "<h4>$1</h4>")
       .replace(/^###\s+(.+)$/gm, "<h3>$1</h3>")
       .replace(/^##\s+(.+)$/gm, "<h2>$1</h2>")
       .replace(/^#\s+(.+)$/gm, "<h1>$1</h1>");
  // 引用
  s = s.replace(/^&gt;\s?(.*)$/gm, "<blockquote>$1</blockquote>");
  s = s.replace(/<\/blockquote>\n<blockquote>/g, "\n");
  // 分隔线
  s = s.replace(/^\s*(?:---|\*\*\*|___)\s*$/gm, "<hr>");
  // 列表
  s = s.replace(/^\s*[-*+]\s+(.+)$/gm, "<li>$1</li>");
  s = s.replace(/^\s*\d+\.\s+(.+)$/gm, "<li>$1</li>");
  s = s.replace(/(<li>[\s\S]*?<\/li>)(?!\s*<li>)/g, (m) => `<ul>${m}</ul>`);
  s = s.replace(/<\/ul>\s*<ul>/g, "");
  // 行内
  s = s.replace(/`([^`\n]+)`/g, "<code>$1</code>");
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>");
  s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  s = s.replace(/~~([^~\n]+)~~/g, "<del>$1</del>");
  s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
    '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g,
    '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  // 段落：按空行分段（避免 CJK 断裂问题沿用旧行为，只是注释更新）
  s = s.split(/\n{2,}/).map((p) => {
    const t = p.trim();
    if (!t) return "";
    if (/^<(h[1-4]|ul|ol|pre|blockquote|table|hr|li|div)/.test(t)) return t;
    return `<p>${t.replace(/\n/g, "<br>")}</p>`;
  }).join("\n");
  // 还原代码块
  s = s.replace(/\u0000B(\d+)\u0000/g, (m, i) => blocks[Number(i)]);
  return s;
}

/* ==========================================================================
   富内容渲染（★ 2-M 流程图 / ★ 2-Q 数学公式）
   ---------------------------------------------------------------------
   为什么必须「写完再画」而不是跟着流式重画：
     · 流式期间 markdown 每 50ms 重绘一次。mermaid 要解析语法→算布局→出 SVG，
       图还没写完就画必然报错，而且每秒画二十次会把界面拖卡。
     · 所以：md() 只落**占位**（并显示原始代码，可读可复制），
       等内容定稿后再统一交给下面的渲染器。
   两个库都**懒加载**：平时不加载脚本，第一次真出现图/公式时才去取，
   启动速度不受影响。任何一个库加载失败或语法有错，都**保留原始文本**，不弹红框。
   ========================================================================== */
const RICH_LIB = {};
function loadScriptOnce(src) {
  if (RICH_LIB[src]) return RICH_LIB[src];
  RICH_LIB[src] = new Promise((resolve, reject) => {
    const s = document.createElement("script");
    s.src = src;
    s.onload = () => resolve(true);
    s.onerror = () => reject(new Error("加载失败：" + src));
    document.head.appendChild(s);
  });
  return RICH_LIB[src];
}
let MERMAID_READY = null;
let MERMAID_SEQ = 0;

/* ★ anime.js：本地 vendor + **懒加载**（与 mermaid/katex 一致）。
   为什么懒加载而不是在 index.html 里挂 <script>：它是纯装饰，不该拖慢启动；
   只有真正要用动效时才加载一次，失败就当没有 —— 界面功能不受影响。 */
let ANIME_READY = null;
function ensureAnime() {
  if (ANIME_READY) return ANIME_READY;
  ANIME_READY = (async () => {
    try {
      await loadScriptOnce("/static/vendor/anime.iife.min.js");
      return window.anime || null;
    } catch (e) {
      return null;   // 加载失败 → 所有动效静默跳过
    }
  })();
  return ANIME_READY;
}

/* 动效总闸：尊重 --zh-weight 同级的「界面动画」偏好。
   注意这里是**读**而不是写：设置里的开关已按移除，
   但保留 html.no-anim 这条通道，便于以后需要时一键关掉。 */
function animOn() {
  try {
    if (document.documentElement.classList.contains("no-anim")) return false;
  } catch (e) {}
  return S.anim !== false;
}

/** 面板/设置页打开：淡入 + 轻微上移（幅度很小，避免晃眼）。 */
async function animEnter(el) {
  if (!el || !animOn()) return;
  const anime = await ensureAnime();
  if (!anime) return;
  try {
    anime.animate(el, { opacity: [0, 1], translateY: [4, 0], duration: 180, ease: "out(2)" });
  } catch (e) {}
}

/** 新消息气泡出现：渐显（只做透明度，不动位置，免得正文跳动）。 */
async function animMessage(el) {
  if (!el || !animOn()) return;
  const anime = await ensureAnime();
  if (!anime) return;
  try {
    anime.animate(el, { opacity: [0, 1], duration: 150, ease: "out(2)" });
  } catch (e) {}
}

/** 数值变化时平滑过渡（待办进度条等）。 */
async function animNumber(el, from, to) {
  if (!el || !animOn()) return;
  const anime = await ensureAnime();
  if (!anime) return;
  try {
    const obj = { v: from };
    anime.animate(obj, {
      v: to, duration: 320, ease: "out(2)",
      onUpdate: () => { el.textContent = String(Math.round(obj.v)); },
    });
  } catch (e) {}
}

function currentRichTheme() {
  try {
    return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "default";
  } catch (e) { return "default"; }
}

async function ensureMermaid() {
  if (MERMAID_READY) return MERMAID_READY;
  MERMAID_READY = (async () => {
    await loadScriptOnce("/static/vendor/mermaid.min.js");
    if (!window.mermaid) throw new Error("mermaid 未就绪");
    window.mermaid.initialize({
      startOnLoad: false,
      securityLevel: "strict",     // 不允许图里塞脚本
      theme: currentRichTheme(),
      fontFamily: "inherit",
    });
    return window.mermaid;
  })();
  return MERMAID_READY;
}

/** 渲染 root 内所有尚未处理过的图/公式。可以安全重复调用（已渲染的会跳过）。 */
async function renderRich(root) {
  const box = root || document;
  // ---- 数学公式（KaTeX，轻量，先做） ----
  const maths = box.querySelectorAll(".math-src:not([data-done])");
  if (maths.length) {
    let katex = null;
    try {
      await loadScriptOnce("/static/vendor/katex.min.js");
      katex = window.katex;
    } catch (e) { katex = null; }
    maths.forEach((el) => {
      el.setAttribute("data-done", "1");
      if (!katex) return;                      // 加载失败 → 保留原始 $...$
      try {
        katex.render(el.dataset.tex || "", el, {
          displayMode: el.dataset.display === "1",
          throwOnError: false,                 // 语法错就渲染成红字，不抛异常打断整页
          strict: false,
        });
      } catch (e) { el.textContent = el.dataset.tex || ""; }
    });
  }
  // ---- 流程图 / 时序图（Mermaid，较重，单独 try） ----
  const diagrams = box.querySelectorAll(".mermaid-src:not([data-done])");
  if (diagrams.length) {
    let mermaid = null;
    try { mermaid = await ensureMermaid(); } catch (e) { mermaid = null; }
    for (const el of diagrams) {
      el.setAttribute("data-done", "1");
      if (!mermaid) continue;                    // 加载失败 → 保留原始代码块
      const src = el.dataset.src || "";
      try {
        const id = "mmd-" + (++MERMAID_SEQ);
        const out = await mermaid.render(id, src);
        el.innerHTML = (out && out.svg) ? out.svg : "";
        el.classList.add("mermaid-done");
      } catch (e) {
        // 语法错：保留原始代码，附一行提示（不遮挡内容）
        el.classList.remove("mermaid-done");
        el.innerHTML = `<pre class="rich-fallback"><code>${esc(src)}</code></pre>`
          + `<div class="rich-hint">图语法有误，未渲染（已按原文显示）</div>`;
      }
    }
  }
  // ---- 正文里的文件路径变成可点（输出里的路径要能点开） ----
  try { linkifyPaths(box); } catch (e) {}
}

/* ★ 把正文里的文件路径渲染成可点链接（实测：回答里的路径只能看不能点）。
   识别两种形式：Windows 盘符路径（X:\... 或 X:/...）与 Unix 绝对路径（/xxx/...）。
   已有的工具卡「打开位置」是针对已知产出文件的；正文里的路径是模型自由写出来的，
   两条路径互不覆盖。
   实现要点：
     · 只处理**文本节点**，不碰 <code> 里已经是链接的部分、也不碰已有按钮；
     · 用 TreeWalker 遍历，避免把标签属性里的内容也改掉（那是 innerHTML 正则的通病）；
     · 点一下调 Electron 的 showInFolder；网页版退化为「复制路径」。 */
const PATH_RE = /(?:[A-Za-z]:[\\/][^\s"'<>|]+|\/(?:[\w.@+-]+\/)*[\w.@+-]+)/g;
function linkifyPaths(root) {
  const box = root || document;
  const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT, {
    acceptNode(node) {
      if (!node.nodeValue || node.nodeValue.length < 3) return NodeFilter.FILTER_REJECT;
      const p = node.parentElement;
      if (!p) return NodeFilter.FILTER_REJECT;
      // 跳过代码块、已有链接/按钮、表格路径列（那些已自带按钮）
      if (p.closest("pre, code, a, button, .rc-row, .fm-row, .tf-row, .memo-item")) {
        return NodeFilter.FILTER_REJECT;
      }
      return PATH_RE.test(node.nodeValue) ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT;
    },
  });
  const targets = [];
  let n;
  while ((n = walker.nextNode())) targets.push(n);
  for (const node of targets) {
    const text = node.nodeValue;
    PATH_RE.lastIndex = 0;
    if (!PATH_RE.test(text)) continue;
    PATH_RE.lastIndex = 0;
    const frag = document.createDocumentFragment();
    let last = 0;
    let m;
    while ((m = PATH_RE.exec(text))) {
      const hit = m[0];
      // 太短的（如 /v1）不算路径，避免把普通斜杠文本变成链接
      if (hit.length < 5) continue;
      if (m.index > last) frag.appendChild(document.createTextNode(text.slice(last, m.index)));
      const a = document.createElement("span");
      a.className = "path-link";
      a.dataset.path = hit;
      a.title = "点击在文件管理器中定位";
      a.textContent = hit;
      frag.appendChild(a);
      last = m.index + hit.length;
    }
    if (last === 0) continue;
    if (last < text.length) frag.appendChild(document.createTextNode(text.slice(last)));
    node.parentNode.replaceChild(frag, node);
  }
  // 事件用委托，避免逐节点绑定（长回答里路径可能很多）
  box.querySelectorAll(".path-link:not([data-bound])").forEach((el) => {
    el.setAttribute("data-bound", "1");
    el.onclick = async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const p = el.dataset.path || "";
      if (window.fengcode && typeof window.fengcode.showInFolder === "function") {
        await window.fengcode.showInFolder(p);
      } else {
        try { await navigator.clipboard.writeText(p); toast("网页版无法打开文件夹，路径已复制", ""); }
        catch (err) { toast(p, ""); }
      }
    };
  });
}

/* ---------------- Toast / Modal ---------------- */
function toast(msg, kind) {
  const el = document.createElement("div");
  el.className = "toast" + (kind ? " " + kind : "");
  el.textContent = msg;
  $("#toasts").appendChild(el);
  setTimeout(() => { el.style.opacity = "0"; el.style.transition = "opacity .3s"; }, 2600);
  setTimeout(() => el.remove(), 3000);
}
function modal(title, bodyHTML, footHTML) {
  $("#modal-head").textContent = title;
  $("#modal-body").innerHTML = bodyHTML || "";
  $("#modal-foot").innerHTML = footHTML || '<button class="btn" data-close>关闭</button>';
  $("#modal-bg").classList.add("show");
  $$("#modal [data-close]").forEach((b) => b.onclick = closeModal);
  return $("#modal-body");
}
function closeModal() { $("#modal-bg").classList.remove("show"); }
$("#modal-bg").onclick = (e) => { if (e.target.id === "modal-bg") closeModal(); };
document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeModal(); });

/* ---------------- API ---------------- */
let TOKEN = "";
try { TOKEN = localStorage.getItem("fengcode_token") || ""; } catch (e) {}
const api = async (path, opts) => {
  opts = opts || {};
  const headers = Object.assign({ "Content-Type": "application/json" }, opts.headers || {});
  if (TOKEN) headers["X-Fengcode-Token"] = TOKEN;
  const res = await fetch(path, Object.assign({}, opts, {
    headers,
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  }));
  const ct = res.headers.get("content-type") || "";
  let data;
  if (ct.includes("application/json")) {
    try { data = await res.json(); } catch (e) { data = null; }
  } else {
    data = await res.text();
  }
  if (!res.ok) {
    const msg = (data && data.error) || (typeof data === "string" ? data.slice(0, 300) : "") || res.statusText;
    const err = new Error(msg);
    err.status = res.status; err.data = data;
    throw err;
  }
  return data;
};

/* ---------------- 全局状态 ---------------- */
const S = {
  boot: null,
  sessionId: null,
  model: "",
  streaming: false,
  abort: null,
  attachments: [],
  page: "chat",
  theme: "light",
  reasoningOpen: {},
  toolNodes: new Map(),
  pendingApproval: [],
  workspace: "",            // 当前工作区名
  workspaces: [],           // 项目（工作区）列表
  infoOpen: true,           // 右侧信息面板默认展开，可随时收起
  turnUsage: null,          // 本轮用量（右侧面板 + 状态栏用）
  msgSeq: 0,                // 用户消息序号（对话导航锚点用）
  queue: [],                // ★ 排队待发的指令（执行中继续发的消息，本轮结束后按序发出）
  // ★ 队列持久化：内存数组刷新即丢，所以每次增删改都同步落盘（见 syncQueue）。
  //   启动时按会话读回来，用户排的队不会因为一次刷新就没了。
  _queueLoaded: false,
};

/* 设置中心的当前分类 */
const SS = { current: "general" };

/* ---------------- 主题 ---------------- */
function applyTheme(t) {
  S.theme = t;
  const sysDark = window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches;
  const actual = t === "auto" ? (sysDark ? "dark" : "light") : t;
  document.documentElement.setAttribute("data-theme", actual);
  try { localStorage.setItem("fengcode_theme", t); } catch (e) {}
}

/**
 * 应用「基础配色」。
 * 只切颜色变量，不影响明暗模式。
 */
const SKINS = [
  { id: "graphite", name: "石墨", color: "#4f46e5" },
  { id: "aurora", name: "极光", color: "#7c3aed" },
  { id: "slate", name: "板岩", color: "#2563eb" },
  { id: "forest", name: "松林", color: "#15803d" },
  { id: "amber", name: "琥珀", color: "#c2410c" },
  { id: "rose", name: "玫瑰", color: "#be123c" },
];
function applySkin(id) {
  S.skin = id || "graphite";
  document.documentElement.setAttribute("data-skin", S.skin);
  try { localStorage.setItem("fengcode_skin", S.skin); } catch (e) {}
}

/**
 * 图片主题：全屏背景图。
 * - 空对话时整张图清晰可见
 * - 有对话内容后自动变淡（body.theme-dimmed），保证文字可读
 * 图片可以是内置预设，也可以是用户自己上传的（存 data URL）。
 */
const THEME_IMAGES = [];   // 由 /api/themes 或内置清单填充（preset 图在 static/themes/）
function applyImageTheme(spec) {
  S.imageTheme = spec || "";
  const bg = document.getElementById("theme-bg");
  if (!bg) return;
  const body = document.body;
  if (!spec) {
    bg.style.backgroundImage = "";
    body.classList.remove("theme-has-image", "theme-dimmed");
    try { localStorage.removeItem("fengcode_image_theme"); } catch (e) {}
    return;
  }
  // spec 支持三种形式：内置名、"data:image/..."（用户上传）、http(s) 链接
  const url = /^(data:|https?:|blob:)/.test(spec)
    ? spec
    : `/static/themes/${spec}.webp`;
  bg.style.backgroundImage = `url("${url}")`;
  body.classList.add("theme-has-image");
  syncImageThemeDim();
  try { localStorage.setItem("fengcode_image_theme", spec); } catch (e) {}
}

/** 根据"是否已有对话内容"决定背景图是清晰还是变淡 */
/** 根据"是否已有对话内容 / 是否正在生成"决定背景图是清晰还是变淡。
 *  需求：不发消息时高清显示；发消息开始工作后就模糊一点。 */
function syncImageThemeDim() {
  // 打开设置：背景图糊成色斑（只留主题色调），这时不需要「变淡」遮罩
  document.body.classList.toggle("settings-open", S.page === "settings");
  if (!document.body.classList.contains("theme-has-image")) {
    document.body.classList.remove("theme-dimmed");
    return;
  }
  const has = (S.messages && S.messages.length > 0)
    || !!document.querySelector("#messages .msg")
    || !!S.streaming;   // 正在生成也算「开始工作」
  document.body.classList.toggle("theme-dimmed", !!has);
}

/** 应用界面字号（13 小 / 15 标准 / 17 大 / 19 特大 / 22 超大） */
function applyFontSize(px) {
  const n = Number(px) || 17;
  S.fontSize = n;
  const r = document.documentElement;
  r.style.setProperty("--app-font", n + "px");
  // data-fs 驱动 --ui-scale：让设置侧栏、正文、输入框等 px 硬编码也一起放大
  r.setAttribute("data-fs", String(n));
  try { localStorage.setItem("fengcode_fontsize", String(n)); } catch (e) {}
}
if (window.matchMedia) {
  try {
    window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
      if (S.theme === "auto") applyTheme("auto");
    });
  } catch (e) {}
}
applyTheme((() => { try { return localStorage.getItem("fengcode_theme") || "light"; } catch (e) { return "light"; } })());
// 恢复上次的配色与图片主题（图片主题在 DOM 就绪后应用）
applySkin((() => { try { return localStorage.getItem("fengcode_skin") || "graphite"; } catch (e) { return "graphite"; } })());
document.addEventListener("DOMContentLoaded", () => {
  try {
    const img = localStorage.getItem("fengcode_image_theme");
    if (img) applyImageTheme(img);
    const fs = localStorage.getItem("fengcode_fontsize");
    if (fs) applyFontSize(fs);
    const ff = localStorage.getItem("fengcode_font");
    if (ff) applyFontStack(ff);
    const cw = localStorage.getItem("fengcode_chat_width");
    if (cw) applyChatWidth(cw);
  } catch (e) { /* ignore */ }
});

/* ---------------- 设置中心的分类定义 ---------------- */
const PAGES = {};
/* 右侧设置页的分组；`page` 指明这一项对应哪个已有页面渲染函数
   分组顺序偏好设置 / 模型 / 集成与连接 / 能力扩展 /
   记忆与上下文 / 自动化与开发者 / 安全与控制 / 应用 */
const SETTINGS_NAV = [
  { group: "偏好设置" },
  { id: "general", label: "通用", icon: "palette", desc: "语言、币种、会话体验" },
  { id: "ui", label: "外观", icon: "eye", desc: "主题、字号、界面元素", page: "settings", tab: "ui" },
  { group: "模型" },
  { id: "providers", label: "模型服务", icon: "key", desc: "供应商、密钥、默认模型", page: "settings", tab: "model" },
  { id: "agent", label: "模型偏好", icon: "compass", desc: "步数、温度、思考语言", page: "settings", tab: "agent" },
  { id: "usage", label: "用量统计", icon: "chart", desc: "调用次数、tokens、费用", page: "stats" },
  { group: "集成与连接" },
  { id: "mcp", label: "MCP 与工具", icon: "plug", desc: "MCP 服务器与内置工具", page: "mcp" },
  { id: "remote", label: "远程 SSH", icon: "server", desc: "远程主机与命令执行", page: "remote" },
  { group: "能力扩展" },
  { id: "skills", label: "技能", icon: "bolt", desc: "SKILL.md 与 Python 技能", page: "skills" },
  { id: "subagents", label: "子智能体", icon: "users", desc: "内置预设、并行派生与预算", page: "subagents" },
  { id: "plugins", label: "插件", icon: "puzzle", desc: "扩展工具与钩子", page: "plugins" },
  { group: "记忆与上下文" },
  { id: "memory", label: "记忆", icon: "brain", desc: "记忆条目、召回设置、指令文件",
    page: "settings", tab: "memory" },
  { group: "自动化与开发者" },
  { id: "jobs", label: "定时任务", icon: "clock", desc: "cron 定时执行", page: "jobs" },
  { id: "audit", label: "审计日志", icon: "clipboard", desc: "操作留痕，密钥已脱敏", page: "audit" },
  { group: "安全与控制" },
  { id: "safety", label: "权限", icon: "shield", desc: "权限等级与细粒度规则", page: "settings", tab: "safety" },
  { id: "sandbox", label: "沙箱", icon: "box", desc: "Shell 解释器、写入范围", page: "settings", tab: "safety" },
  { group: "应用" },
  { id: "advanced", label: "高级", icon: "flask", desc: "服务端口、数据目录、维护", page: "settings", tab: "adv" },
  { id: "about", label: "关于", icon: "info", desc: "版本与更新", page: "about" },
];

/* 页面标题映射（顶部标题栏用） */
const PAGE_TITLES = {
  chat: "对话", sessions: "会话记录", settings: "设置",
  memory: "记忆", skills: "技能", tools: "工具", mcp: "MCP 服务",
  plugins: "插件", workflows: "工作流", jobs: "定时任务", remote: "远程主机",
  stats: "用量统计", audit: "审计日志", about: "关于",
};
/* 兼容旧代码里对 NAV 的引用 */
const NAV = Object.entries(PAGE_TITLES).map(([id, label]) => ({ id, label }));

/* ---------------- 侧边栏：项目（工作区）> 会话 ---------------- */
let WS_OPEN = {};          // 项目展开状态
// ★ 2-G：每个项目默认只列 5 条会话，其余折叠成「还有 N 个…」。
//   为什么：会话攒到几十上百条时，侧边栏一屏塞不下、滚动很长，
//   找最近那个会话反而变难。点一次展开就多显示一批。
let WS_SHOW = {};          // { 项目key: 已显示条数 }
const NAV_PAGE = 5;

async function renderNav() {
  const nav = $("#nav");
  let sessions = [], workspaces = [];
  try {
    const d = await api("/api/sessions?limit=200");
    sessions = d.sessions || [];
  } catch (e) { /* 后端没起来时静默 */ }
  // 注意：工作区列表走 /api/workspaces（复数），不是 /api/workspace（那是文件浏览）
  try {
    const w = await api("/api/workspaces");
    if (w && w.workspaces) workspaces = w.workspaces;
  } catch (e) {}

  const curPath = (S.boot && S.boot.config && S.boot.config.agent && S.boot.config.agent.workspace_override) || S.workspace || "";
  const defWs = workspaces.find((x) => x.is_default);

  // 会话按工作区路径归组
  const groups = new Map();
  for (const w of workspaces) {
    groups.set(w.path || w.name, { ws: w, list: [] });
  }
  const fallbackKey = (defWs && (defWs.path || defWs.name)) || curPath || "默认工作区";
  if (!groups.size) groups.set(fallbackKey, { ws: { name: "默认工作区", path: curPath, is_default: true }, list: [] });

  for (const s of sessions) {
    const key = s.workspace || fallbackKey;
    if (!groups.has(key)) groups.set(key, { ws: { name: wsLabel(key) || key, path: key }, list: [] });
    groups.get(key).list.push(s);
  }

  let html = `<div class="nav-head">
    <span class="nh-title">项目</span>
    <button class="nh-add" id="new-ws-btn" title="新建空白项目">${icon("plus", 14)}</button>
  </div>`;

  for (const [key, g] of groups) {
    const wsName = g.ws.name || wsLabel(key) || key;
    const open = WS_OPEN[key] !== false;      // 默认展开
    html += `<div class="ws-group">
      <div class="ws-head" data-ws="${esc(key)}">
        <span class="ws-caret">${icon(open ? "chevronDown" : "chevronRight", 13)}</span>
        <span class="ws-name" title="${esc(g.ws.path || key)}">${esc(wsName)}</span>
        <span class="ws-more" data-wsmenu="${esc(g.ws.id || "")}" title="项目设置">${icon("moreHorizontal", 13)}</span>
        <span class="ws-add" data-add="${esc(key)}" title="在此项目新建会话">${icon("plus", 13)}</span>
      </div>`;
    if (open) {
      if (!g.list.length) {
        html += `<div class="sess-item" style="color:var(--text-faint);padding-left:30px">（暂无会话）</div>`;
      }
      // ★ 2-G：先是 5 条，剩下的折叠；当前正在看的那个会话一定可见（别把它藏起来）
      const shown = Math.max(NAV_PAGE, WS_SHOW[key] || 0);
      let head = g.list.slice(0, shown);
      if (!head.some((s) => s.id === S.sessionId)) {
        const cur = g.list.find((s) => s.id === S.sessionId);
        if (cur) head = [...head, cur];
      }
      const rest = g.list.length - head.length;
      for (const s of head) {
        const active = s.id === S.sessionId ? " active" : "";
        html += `<button class="sess-item${active}" data-sid="${esc(s.id)}">
          <span class="s-title" title="${esc(s.title || "未命名")}">${esc(s.title || "未命名")}</span>
          <span class="s-time">${relTime(s.updated_at || s.created_at)}</span>
          <span class="s-del" data-del="${esc(s.id)}" title="删除">${icon("close", 12)}</span>
        </button>`;
      }
      if (rest > 0) {
        html += `<button class="sess-more" data-more="${esc(key)}">
          还有 ${rest} 个会话…（点击展开）</button>`;
      }
    }
    html += `</div>`;
  }
  nav.innerHTML = html;

  // ★ 重绘后重新套用会话搜索词：会话列表每次刷新（新建/删除/切换）都会重建 DOM，
  //   不在这里补一次，搜索框里的词就会被「重置成显示全部」，看起来像搜索失效。
  try { applySessionFilter(); } catch (e) {}

  // 绑定：新建空白项目
  const nw = $("#new-ws-btn", nav);
  if (nw) nw.onclick = () => openNewProjectDialog();

  // 绑定：项目菜单（改名 / 换目录 / 删除）
  $$(".ws-more", nav).forEach((b) => b.onclick = (e) => {
    e.stopPropagation();
    const id = b.dataset.wsmenu;
    if (id) openProjectMenu(b, id);
  });

  // 绑定：展开/折叠项目
  $$(".ws-head", nav).forEach((h) => h.onclick = (e) => {
    if (e.target.closest(".ws-add") || e.target.closest(".ws-more")) return;
    const ws = h.dataset.ws;
    WS_OPEN[ws] = WS_OPEN[ws] === false;
    renderNav();
  });
  // 绑定：在项目里新建会话
  $$(".ws-add", nav).forEach((b) => b.onclick = async (e) => {
    e.stopPropagation();
    S.workspace = b.dataset.add;
    await newSession();
    renderNav();
  });
  // 绑定：切换会话
  $$(".sess-item[data-sid]", nav).forEach((b) => b.onclick = async (e) => {
    if (e.target.dataset.del) return;
    await openSession(b.dataset.sid);
  });
  // ★ 2-G：展开更多会话（每次多显示一批）
  $$(".sess-more", nav).forEach((b) => b.onclick = (e) => {
    e.preventDefault();
    e.stopPropagation();
    const k = b.dataset.more;
    WS_SHOW[k] = (WS_SHOW[k] || NAV_PAGE) + NAV_PAGE * 2;   // 一次多放 10 条
    renderNav();
  });
  // 绑定：删除会话（就地二次确认归档，不使用中央弹窗）
  $$(".sess-item[data-sid]", nav).forEach((row) => {
    const b = $("[data-del]", row);
    if (!b) return;
    b.onclick = async (e) => {
      e.preventDefault();
      e.stopPropagation();
      const sid = row.dataset.sid;
      if (row.dataset.archiving === "1") {
        row.dataset.archiving = "0";
        b.textContent = "归档";
        b.title = "归档会话";
        try {
          await api(`/api/sessions/${encodeURIComponent(sid)}`, { method: "PATCH", body: { archived: 1 } });
          // ★ 归档「当前会话」时不要无脑新建：先看还有没有别的会话，有就切过去，
          //   一个都没有才建新的。
          //   旧写法每次都 newSession()，用户连着归档几个会话就攒出一堆空白「新对话」。
          if (sid === S.sessionId) {
            let rest = [];
            try {
              const d2 = await api("/api/sessions?limit=200");
              rest = (d2.sessions || []).filter((s) => !s.archived && s.id !== sid);
            } catch (e) {}
            if (rest.length) await openSession(rest[0].id);
            else await newSession();
          }
          const input = $("#input");
          if (input) { input.disabled = false; input.readOnly = false; input.focus(); }
          toast("已归档，可在回收站恢复");
          renderNav();
        } catch (err) { toast("归档失败：" + err.message, "err"); }
        return;
      }
      row.dataset.archiving = "1";
      b.textContent = "确认归档";
      b.title = "再次点击确认归档";
    };
  });
}

/** 新建空白项目：填名字 + 选目录（留空则自动创建） */
function openNewProjectDialog() {
  const dlg = document.createElement("div");
  dlg.className = "modal-mask show";
  dlg.innerHTML = `
    <div class="modal">
      <h3>新建空白项目</h3>
      <div class="sec-desc">项目绑定一个工作目录，会话和文件都在这个目录里。</div>
      <div class="field"><label>项目名称</label>
        <input id="np-name" placeholder="例如：我的网站" /></div>
      <div class="field"><label>工作目录（可留空自动创建）</label>
        <input id="np-path" class="mono" placeholder="D:\\projects\\my-site" /></div>
      <div class="modal-foot">
        <button class="btn ghost" id="np-cancel">取消</button>
        <button class="btn primary" id="np-ok">创建</button>
      </div>
    </div>`;
  document.body.appendChild(dlg);
  const close = () => dlg.remove();
  dlg.onclick = (e) => { if (e.target === dlg) close(); };
  $("#np-cancel", dlg).onclick = close;
  $("#np-ok", dlg).onclick = async () => {
    const name = ($("#np-name", dlg).value || "").trim();
    const path = ($("#np-path", dlg).value || "").trim();
    if (!name) { toast("请填项目名称", "err"); return; }
    try {
      const r = await api("/api/workspaces", {
        method: "POST",
        body: JSON.stringify({ action: "create", name, path }),
      });
      close();
      toast(`已创建项目「${r.workspace.name}」`);
      await refreshWorkspaces();
      renderNav();
    } catch (e) { toast("创建失败：" + e.message, "err"); }
  };
  setTimeout(() => { const i = $("#np-name", dlg); if (i) i.focus(); }, 30);
}

/** 项目菜单：改名 / 换目录 / 删除 */
function openProjectMenu(anchor, wsId) {
  const ws = (S.workspaces || []).find((w) => w.id === wsId);
  if (!ws) return;
  const items = [
    { id: "rename", label: "重命名项目", icon: "edit" },
    { id: "repath", label: "更改工作目录", icon: "folder" },
  ];
  if (!ws.is_default) items.push({ id: "delete", label: "删除项目", icon: "trash", danger: true });
  openChoiceMenu(anchor, items, "", async (act) => {
    if (act === "rename") {
      const name = prompt("新的项目名称：", ws.name);
      if (!name) return;
      await api("/api/workspaces", { method: "POST", body: JSON.stringify({ action: "rename", id: wsId, name }) });
      toast("已重命名");
    } else if (act === "repath") {
      const p = prompt("新的工作目录（绝对路径）：", ws.path || "");
      if (p === null) return;
      await api("/api/workspaces", { method: "POST", body: JSON.stringify({ action: "update", id: wsId, path: p }) });
      toast("已更新目录");
    } else if (act === "delete") {
      if (!confirm(`删除项目「${ws.name}」？磁盘上的文件不会被删除。`)) return;
      await api("/api/workspaces", { method: "POST", body: JSON.stringify({ action: "delete", id: wsId }) });
      toast("已删除项目");
    }
    await refreshWorkspaces();
    renderNav();
  });
}

/** 拉取工作区列表并缓存（供项目菜单等使用） */
async function refreshWorkspaces() {
  try {
    const w = await api("/api/workspaces");
    if (w && w.workspaces) S.workspaces = w.workspaces;
  } catch (e) {}
}

/** 把工作区路径变成好读的名字：取最后一段；空则返回空串 */
function wsLabel(p) {
  if (!p) return "";
  const s = String(p).replace(/[\\/]+$/, "");
  if (!s) return "";
  const parts = s.split(/[\\/]/).filter(Boolean);
  let last = parts[parts.length - 1] || s;
  // .fengcode\workspace 这类默认目录显示成「默认工作区」
  if (last.toLowerCase() === "workspace" && (s.includes(".fengcode") || s.includes("fengcode-data"))) {
    return "默认工作区";
  }
  // D:\Fengcode\命令端 这种：取最后一段
  if (/^[A-Za-z]:$/.test(last)) return last;      // 盘符根目录
  return last;
}

/** 相对时间：刚刚 / 12 分钟 / 3 小时 / 昨天 / 9-28 */
function relTime(ts) {
  if (!ts) return "";
  const t = typeof ts === "number" ? ts * 1000 : Date.parse(ts);
  if (!t || isNaN(t)) return "";
  const diff = Date.now() - t;
  const m = Math.floor(diff / 60000);
  if (m < 1) return "刚刚";
  if (m < 60) return m + " 分钟";
  const h = Math.floor(m / 60);
  if (h < 24) return h + " 小时";
  if (h < 48) return "昨天";
  const d = new Date(t);
  return `${d.getMonth() + 1}-${d.getDate()}`;
}

function setBadge(id, n) {
  const el = $(`[data-badge="${id}"]`);
  if (!el) return;
  if (n) { el.style.display = ""; el.textContent = n; } else { el.style.display = "none"; }
}

/* ---------------- 页面切换 ---------------- */

/** 应用标记（**唯一来源**）。
    左栏 #app-logo 与顶栏 #top-logo 都用它渲染，保证「打开设置后左上角的 logo」
    与主界面**一模一样**（之前两处各写一份 SVG：一个是 18px 白笔画配渐变方块，
    另一个是 15px 灰笔画配透明底，看起来当然不像）。
    形状：折线 + 收尾圆点。 */
function logoSvg(size, mono) {
  const s = size || 18;
  // mono=true 时用 currentColor 描边（用在输入区状态行，跟着文字颜色走；
  // 渐变方块里的白笔画在浅色背景上会看不见）。
  const c = mono ? "currentColor" : "#fff";
  return `<svg width="${s}" height="${s}" viewBox="0 0 256 256" fill="none"
    stroke="${c}" stroke-linecap="round" stroke-linejoin="round">
    <path d="M72 178 L72 82" stroke-width="20"/>
    <path d="M72 82 L172 82" stroke-width="20"/>
    <path d="M72 128 L138 128" stroke-width="20" opacity="0.75"/>
    <circle cx="184" cy="178" r="20" fill="${c}" stroke="none" opacity="0.92"/>
  </svg>`;
}

async function go(page, opt) {
  S.page = page;
  $$(".page").forEach((p) => p.classList.remove("active"));
  const el = $("#page-" + page);
  if (el) el.classList.add("active");

  // 设置中心：切换分类
  const inSettings = page === "settings" || (opt && opt.fromSettings);
  $("#back-btn").style.display = inSettings ? "" : "none";
  SS.current = (opt && opt.tab) ? (opt.item || SS.current) : SS.current;

  const title = PAGE_TITLES[page] || page;
  $("#page-title").textContent = inSettings ? "设置" : title;
  $("#page-sub").textContent = "";
  $("#app").classList.remove("side-open");
  // 设置模式：整页覆盖（隐藏左右侧边栏与顶栏）
  $("#app").classList.toggle("settings-mode", page === "settings");
  // 背景态跟着页面走：设置页要把背景图糊成一块色斑（只留主题色调，看不到画面）
  syncImageThemeDim();
  // 设置模式下顶栏左侧显示 logo（可点回对话）。
  // ★ 为什么补这个：设置模式隐藏了左栏，原来左上角就只有「设置」两个字、
  //   少了品牌标记；用户希望 logo 一直在（原来打开设置就整个消失了）。
  const tl = $("#top-logo");
  if (tl) {
    tl.style.display = inSettings ? "" : "none";
    tl.innerHTML = logoSvg(18);
    tl.onclick = () => go("chat");
  }
  renderTopActions(page);

  if (page === "settings") {
    renderSettingsSide();
    await openSetting(SS.current || "general");
  } else if (PAGES[page]) {
    try { await PAGES[page](); } catch (e) { toast("加载失败：" + e.message, "err"); }
  }
  if (page === "chat") setTimeout(() => $("#input").focus(), 50);
  // 切回对话页后重新同步一次输入区留白：setupResizers() 在初始化时跑，
  // 那时 #chat-page 还没 .active（display:none），量不到 #composer 的真实高度，
  // 只能用推算值（偏小），会导致文字从卡片下方露出来。
  if (page === "chat" && typeof window.__syncComposerBlock === "function") {
    setTimeout(() => window.__syncComposerBlock(), 0);
  }
}

/* ---------------- 设置中心 ---------------- */
function renderSettingsSide() {
  const el = $("#settings-side");
  const kw = (SS.filter || "").trim().toLowerCase();
  let html = `<button class="ss-back" id="ss-back">${icon("chevronLeft", 14)} 返回工作区</button>
    <div class="ss-search"><input id="ss-filter" placeholder="搜索设置…" value="${esc(SS.filter || "")}"></div>`;
  for (const n of SETTINGS_NAV) {
    if (n.group) {
      const items = SETTINGS_NAV.filter((x) => x.id && x.desc !== undefined);
      // 分组标题在筛选时按需显示，见下
      html += `<div class="ss-group" data-group="${esc(n.group)}">${esc(n.group)}</div>`;
      continue;
    }
    const hay = (n.label + " " + (n.desc || "")).toLowerCase();
    const hit = !kw || hay.includes(kw);
    html += `<button class="ss-item${n.id === SS.current ? " active" : ""}"
      data-set="${esc(n.id)}" data-hit="${hit ? 1 : 0}"${hit ? "" : ' style="display:none"'}>
      <span class="ico">${icon(n.icon || "info", 15)}</span><span>${esc(n.label)}</span></button>`;
  }
  el.innerHTML = html;

  // 搜索：隐藏不匹配项，并隐藏空分组
  const input = $("#ss-filter");
  if (input) {
    input.oninput = () => {
      SS.filter = input.value;
      const q = input.value.trim().toLowerCase();
      let shown = 0;
      $$(".ss-item", el).forEach((b) => {
        const n = SETTINGS_NAV.find((x) => x.id === b.dataset.set) || {};
        const hay = ((n.label || "") + " " + (n.desc || "")).toLowerCase();
        const ok = !q || hay.includes(q);
        b.style.display = ok ? "" : "none";
        if (ok) shown++;
      });
      // 分组标题：该组下没有任何可见项就藏起来
      let groupEls = $$(".ss-group", el);
      for (let i = groupEls.length - 1; i >= 0; i--) {
        const gEl = groupEls[i];
        let vis = 0;
        let sib = gEl.nextElementSibling;
        while (sib && !sib.classList.contains("ss-group")) {
          if (sib.classList.contains("ss-item") && sib.style.display !== "none") vis++;
          sib = sib.nextElementSibling;
        }
        gEl.style.display = vis ? "" : "none";
      }
    };
  }
  $("#ss-back").onclick = () => go("chat");
  $$(".ss-item", el).forEach((b) => b.onclick = () => openSetting(b.dataset.set));
}

/** 打开设置中心的某个分类 */
async function openSetting(id) {
  SS.current = id;
  $$(".ss-item").forEach((b) => b.classList.toggle("active", b.dataset.set === id));
  const meta = SETTINGS_NAV.find((n) => n.id === id) || {};
  const body = $("#settings-body");
  if (!body) return;

  // 顶部工具栏按「这个分类对应的页面」渲染 ——
  // 否则在设置中心里看不到「添加服务」「新建插件」这类页面级按钮。
  try { renderTopActions(meta.page || "settings"); } catch (e) {}

  // 把上次搬进设置中心的内容放回原位，
  // 否则原页面会留个空位，来回切换就再也找不到了。
  const prevHost = $("#ss-host");
  if (prevHost) {
    try {
      if (prevHost._movedPane && prevHost._movedHome && prevHost._movedHome.isConnected) {
        prevHost._movedHome.appendChild(prevHost._movedPane);
      } else if (prevHost._movedKids && prevHost._movedHome && prevHost._movedHome.isConnected) {
        prevHost._movedKids.forEach((n) => prevHost._movedHome.appendChild(n));
      }
    } catch (e) {
      // 原容器已被销毁（例如页面重渲染过），节点丢了也不影响本次渲染
    }
    prevHost._movedPane = null;
    prevHost._movedHome = null;
    prevHost._movedKids = null;
  }

  // 通用（界面外观等）由这里自己渲染
  if (id === "general") {
    await renderGeneralSettings(body);
    return;
  }

  // 其余分类：先在 body 里放标题与承接容器，再让对应页面渲染进 #ss-host
  body.innerHTML = `<h2>${esc(meta.label || id)}</h2>
    <div class="sec-desc">${esc(meta.desc || "")}</div>
    <div id="ss-host"></div>`;
  const ssHost = $("#ss-host");
  if (!ssHost) return;

  const target = meta.page || "settings";

  // 其它页面：渲染后只把「目标面板」搬进 ssHost，避免把整页（含其它分类的
  // tabs 与面板）都塞进来——这正是之前「点模型服务却显示全部设置」的根因。
  if (PAGES[target]) {
    // settings 页要先渲染到 #page-settings，才能按分类取到对应面板搬走
    if (target === "settings") PAGES.settings._toPage = true;
    try { await PAGES[target](); } catch (e) { toast("加载失败：" + e.message, "err"); }
    if (target === "settings") PAGES.settings._toPage = false;
  }
  const host = $("#page-" + target);
  if (!host) {
    ssHost.innerHTML = `<div class="empty">该设置项暂不可用。</div>`;
    return;
  }
  if (target === "settings") {
    // 面板渲染在 #settings-render 里（与设置中心容器平级），从那里取。
    const paneHost = $("#settings-render") || host;
    paneHost.querySelectorAll("#set-tabs").forEach((el) => el.remove());
    const want = meta.tab || "general";
    const pane = paneHost.querySelector("#set-" + want);
    if (pane) {
      // 关键：搬运「真实节点」而不是复制 innerHTML。
      // 复制会让所有直接绑在节点上的 onclick 失效（点按钮没反应），
      // 搬真节点则事件绑定原样保留。
      pane.style.display = "";     // 干掉面板自带的 display:none，否则搬过去是隐藏的
      ssHost.replaceChildren(pane);
      ssHost._movedPane = pane;
      // 保存条在 settings 页各面板之外，搬面板时不会被带上，补一个。
      // ⚠️ 必须挂在「滚动容器」#settings-body 上，不能挂在 #ss-host：
      //    sticky 的活动范围受父元素边界限制，挂在 #ss-host 里会卡在内容中间浮起来。
      const needsSave = ["model", "agent", "safety", "memory", "adv"].includes(want);
      if (needsSave && pane.querySelector("input, select, textarea")) {
        body.querySelectorAll(".save-bar").forEach((el) => el.remove());
        const bar = document.createElement("div");
        bar.className = "save-bar";
        bar.innerHTML = `<button class="btn primary" id="set-save">保存设置</button>
          <span id="set-msg" style="font-size:12.5px;color:var(--text-dim)"></span>`;
        body.appendChild(bar);
      } else {
        body.querySelectorAll(".save-bar").forEach((el) => el.remove());
      }
      // 异步补全：环境探测 / 规则列表这类内容是在 PAGES.settings 里异步取的
      if (want === "safety" && typeof fillSandboxProbe === "function") {
        try { await fillSandboxProbe(); } catch (e) {}
      }
      if (want === "safety" && typeof paintRulesGlobal === "function") {
        try { paintRulesGlobal(); } catch (e) {}
      }
      if (want === "safety" && typeof fillPermWarnings === "function") {
        try { await fillPermWarnings(); } catch (e) {}
      }
      if (want === "adv" && typeof PAGES.settings._loadRaw === "function") {
        try { await PAGES.settings._loadRaw(); } catch (e) {}
      }
      if (want === "memory" && typeof loadMemoryPanel === "function") {
        try { await loadMemoryPanel(); } catch (e) {}
      }
    } else {
      ssHost.innerHTML = `<div class="empty">未找到「${esc(meta.label || want)}」面板。</div>`;
    }
    return;
  }
  // 非 settings 页（技能 / MCP / 插件等）：同样搬「真实节点」，
  // 否则复制出来的副本上事件全失效，按钮点了没反应。
  ssHost.replaceChildren();
  const kids = Array.from(host.childNodes);
  kids.forEach((n) => {
    if (n.nodeType === 1) n.style.display = "";
    ssHost.appendChild(n);
  });
  ssHost._movedPane = null;
  ssHost._movedHome = host;
  ssHost._movedKids = kids;
}

/** 通用设置（左标题右控件」的排布） */
async function renderGeneralSettings(body) {
  let boot = S.boot;
  if (!boot) { try { boot = S.boot = await api("/api/bootstrap"); } catch (e) {} }
  const ui = (boot && boot.config && boot.config.ui) || {};
  const cfg = (boot && boot.config) || {};

  body.innerHTML = `
    <h2>通用</h2>
    <div class="sec-desc">界面外观、语言与费用显示。</div>

    <div class="sec-title">桌面与语言</div>
    <div class="set-row">
      <div class="sr-main">
        <div class="sr-title">明暗模式</div>
        <div class="sr-desc">浅色为主；深色用的是护眼暗色（非纯黑）</div>
      </div>
      <div class="sr-ctl">
        <select id="g-theme">
          <option value="light">浅色</option>
          <option value="dark">深色</option>
          <option value="auto">跟随系统</option>
        </select>
      </div>
    </div>
    <div class="set-row">
      <div class="sr-main">
        <div class="sr-title">基础配色</div>
        <div class="sr-desc">换一套主色与背景色，立即生效</div>
      </div>
      <div class="sr-ctl"><select id="g-skin">
        ${SKINS.map((s) => `<option value="${s.id}">${esc(s.name)}</option>`).join("")}
      </select></div>
    </div>
    <div class="set-row">
      <div class="sr-main">
        <div class="sr-title">界面语言</div>
        <div class="sr-desc">本应用界面为中文</div>
      </div>
      <div class="sr-ctl"><select id="g-lang"><option value="zh">简体中文</option></select></div>
    </div>
    <div class="set-row">
      <div class="sr-main">
        <div class="sr-title">费用显示币种</div>
        <div class="sr-desc">只影响费用展示，不改变结算</div>
      </div>
      <div class="sr-ctl"><select id="g-currency">
        <option value="CNY">人民币 ¥</option><option value="USD">美元 $</option></select></div>
    </div>

    <div class="sec-title">外观细节</div>
    <div class="set-row">
      <div class="sr-main">
        <div class="sr-title">界面字号</div>
        <div class="sr-desc">影响正文与列表；档位之间差别明显</div>
      </div>
      <div class="sr-ctl"><select id="g-fontsize">
        <option value="13">很小（13）</option>
        <option value="15">小（15）</option>
        <option value="17" selected>标准（17，默认）</option>
        <option value="19">大（19）</option>
        <option value="22">超大（22）</option>
      </select></div>
    </div>
    <div class="set-row">
      <div class="sr-main">
        <div class="sr-title">界面字体</div>
        <div class="sr-desc">中文字体（黑体为主，笔画更均匀）</div>
      </div>
      <div class="sr-ctl"><select id="g-font">
        <option value="yahei">微软雅黑（推荐）</option>
        <option value="system">系统默认</option>
        <option value="serif">宋体</option>
      </select></div>
    </div>
    <div class="set-row">
      <div class="sr-main">
        <div class="sr-title">会话区域宽度</div>
        <div class="sr-desc">居中阅读更舒适，全宽能看到更多代码</div>
      </div>
      <div class="sr-ctl">
        <div class="seg" id="g-width-seg">
          <button class="seg-btn" data-v="standard">标准</button>
          <button class="seg-btn" data-v="wide">全宽</button>
        </div>
      </div>
    </div>

    <div class="sec-title">会话体验</div>
    <div class="set-row">
      <div class="sr-main">
        <div class="sr-title">新会话默认权限</div>
        <div class="sr-desc">新建会话时默认采用哪种权限；对话中仍可随时切换</div>
      </div>
      <div class="sr-ctl"><select id="g-default-perm">
        <option value="deny"${(ui.default_permission || (ui.default_readonly ? "deny" : "ask")) === "deny" ? " selected" : ""}>只读</option>
        <option value="ask"${(ui.default_permission || "ask") === "ask" ? " selected" : ""}>工作区可改（推荐）</option>
        <option value="allow"${(ui.default_permission || "ask") === "allow" ? " selected" : ""}>完全权限</option>
      </select></div>
    </div>

    <div class="sec-title">系统行为</div>
    <div class="set-row">
      <div class="sr-main">
        <div class="sr-title">关闭窗口时</div>
        <div class="sr-desc">桌面端关闭主窗口后的行为</div>
      </div>
      <div class="sr-ctl"><select id="g-close">
        <option value="tray">最小化到托盘（继续跑）</option>
        <option value="quit">直接退出</option></select></div>
    </div>

    <div class="sec-title">数据</div>
    <div class="set-row">
      <div class="sr-main">
        <div class="sr-title">工作区目录</div>
        <div class="sr-desc" id="g-ws-path">—</div>
      </div>
      <div class="sr-ctl"><button class="btn sm" id="g-open-ws">打开</button></div>
    </div>
    <div class="set-row">
      <div class="sr-main">
        <div class="sr-title">数据目录</div>
        <div class="sr-desc" id="g-data-path">—</div>
      </div>
      <div class="sr-ctl"><button class="btn sm" id="g-open-data">打开</button></div>
    </div>
  `;

  const th = $("#g-theme");
  if (th) { th.value = S.theme; th.onchange = () => applyTheme(th.value); }
  const cur = $("#g-currency"); if (cur) cur.value = ui.currency || "CNY";
  bindGeneralExtras();

  // 立刻生效的本地项
  // ★ 「显示本轮用量 / 信息面板 / 显示思考过程 / 界面动画」这类开关已从设置里移除：
  //   它们不是用户的「选择」，而是界面的固有部分（要求「默认强制开启，
  //   不要放在设置里让选，太臃肿」）。行为固定为：全都开。
  const gdp = $("#g-default-perm");
  if (gdp) gdp.onchange = () => { ui.default_permission = gdp.value; ui.default_readonly = gdp.value === "deny"; try { localStorage.setItem("fengcode_default_permission", gdp.value); } catch (e) {} saveUiQuiet(ui); };

  // 路径信息
  try {
    const st = await api("/api/status");
    const paths = st.paths || {};
    const wsPath = st.workspace || paths.workspace || "";
    const dataDir = paths.data || st.data_dir || st.home || "";
    if ($("#g-ws-path")) $("#g-ws-path").textContent = wsPath || "（未设置）";
    if ($("#g-open-ws")) $("#g-open-ws").onclick = async () => {
      try { await api("/api/open", { method: "POST", body: { path: wsPath } }); }
      catch (e) { toast("打开失败：" + e.message, "err"); }
    };
    if ($("#g-data-path")) $("#g-data-path").textContent = dataDir || "（未知）";
    if ($("#g-open-data")) $("#g-open-data").onclick = async () => {
      try { await api("/api/open", { method: "POST", body: { path: dataDir } }); }
      catch (e) { toast("打开失败：" + e.message, "err"); }
    };
  } catch (e) {}
}

/* 通用页新增控件的绑定（配色 / 字号 / 字体 / 宽度 / 动画） */
function bindGeneralExtras() {
  // 基础配色：立即生效
  const sk = $("#g-skin");
  if (sk) {
    sk.value = S.skin || "graphite";
    sk.onchange = () => { applySkin(sk.value); saveUiQuiet({ skin: sk.value }); };
  }
  // 界面字号：立即生效（档位差距明显）
  const fs = $("#g-fontsize");
  if (fs) {
    fs.value = String(S.fontSize || 17);
    fs.onchange = () => { applyFontSize(fs.value); saveUiQuiet({ font_size: Number(fs.value) }); };
  }
  // 界面字体：通过 CSS 变量切换字体栈
  const ff = $("#g-font");
  if (ff) {
    ff.value = S.font || "yahei";
    ff.onchange = () => applyFontStack(ff.value);
  }
  // 会话区域宽度
  const wseg = $("#g-width-seg");
  if (wseg) {
    const cur = S.chatWidth || "standard";
    wseg.querySelectorAll(".seg-btn").forEach((b) => {
      b.classList.toggle("active", b.dataset.v === cur);
      b.onclick = () => {
        wseg.querySelectorAll(".seg-btn").forEach((x) => x.classList.toggle("active", x === b));
        applyChatWidth(b.dataset.v);
      };
    });
  }
  // 界面动画开关
  // ★ 按钮已从设置里移除（动画属于界面固有行为，不给用户关）。默认开。
  S.anim = true;
  document.documentElement.classList.remove("no-anim");
  // 显示思考过程：★ 固定开启（思考过程是核心信息，不给关）。
  // 这里没有 ui 变量（那是 renderGeneralSettings 的局部量），直接写进引导数据，
  // 渲染层统一读 S.boot.ui.show_reasoning。
  if (S.boot && S.boot.ui) S.boot.ui.show_reasoning = true;
}

/** 切换正文字体栈（不改字号） */
function applyFontStack(kind) {
  S.font = kind;
  const stacks = {
    yahei: '"Microsoft YaHei UI", "Microsoft YaHei", "Segoe UI", "PingFang SC", sans-serif',
    system: '-apple-system, BlinkMacSystemFont, "Segoe UI", "PingFang SC", sans-serif',
    serif: '"SimSun", "Songti SC", "Noto Serif SC", serif',
  };
  document.documentElement.style.setProperty("--app-font-family", stacks[kind] || stacks.yahei);
  try { localStorage.setItem("fengcode_font", kind); } catch (e) {}
}

/** 会话区域宽度：标准（居中限宽）或全宽 */
function applyChatWidth(v) {
  S.chatWidth = v;
  document.documentElement.style.setProperty("--chat-max-w", v === "wide" ? "100%" : "900px");
  try { localStorage.setItem("fengcode_chat_width", v); } catch (e) {}
}

/** 静默保存 UI 配置（不弹提示） */
async function saveUiQuiet(ui) {
  try {
    // ★ 必须用 PATCH：/api/config 只接受 GET/PATCH/POST，旧写法发 PUT 会 405。
    //   而这里的 catch 是「忽略错误」—— 结果就是**设置界面里改什么都保存不上**，
    //   用户看到「改了字号、刷新又变回去」（实测反馈）。不能静默吞，要提示。
    const r = await api("/api/config", { method: "PATCH", body: { ui } });
    return r;
  } catch (e) {
    toast("保存设置失败：" + e.message, "err");
  }
}

/* ==========================================================================
   右侧信息面板 + 底部状态栏
   ========================================================================== */
function toggleInfoPanel(open) {
  S.infoOpen = open === undefined ? !S.infoOpen : !!open;
  $("#infopanel").classList.toggle("open", S.infoOpen);
  $("#panel-toggle").classList.toggle("on", S.infoOpen);
  if (S.infoOpen) {
    if (typeof BrowserTab !== "undefined" && IP_TAB === "browser") BrowserTab.enter();
    renderInfoPanel();
  } else if (typeof BrowserTab !== "undefined") {
    // ★ 侧栏收起时必须把网页视图藏起来：它是独立图层，不会跟着 DOM 隐掉，
    //   不收就会「侧栏关了、网页还浮在界面上」。
    BrowserTab.leave();
  }
}

async function openSessionTrash() {
  modal("回收站", `<div class="sec-desc">归档的会话会保留在这里，可以恢复到项目列表。
    「恢复副本」是从某个对话派生出来的分支，已折进它的来源对话下面。</div><div id="trash-list" style="max-height:48vh;overflow:auto"></div>`, `<button class="btn" data-close>关闭</button>`);
  const list = $("#trash-list");
  try {
    const d = await api("/api/sessions?limit=200&archived=1");
    const archived = (d.sessions || []).filter((s) => s.archived);
    if (!archived.length) {
      list.innerHTML = `<div class="empty">回收站是空的</div>`;
      return;
    }
    // ★ 恢复副本折叠：parent_id 指向另一个会话的条目是「恢复副本」，
    //   直接平铺会让回收站里出现一堆同名条目、看不出它们其实同源。
    //   这里按来源对话分组：来源在前，它的副本折在下面并标注。
    const byId = new Map(archived.map((s) => [s.id, s]));
    const children = new Map();
    const roots = [];
    for (const s of archived) {
      const pid = s.parent_id || "";
      if (pid && byId.has(pid)) {
        if (!children.has(pid)) children.set(pid, []);
        children.get(pid).push(s);
      } else {
        roots.push(s);   // 没有来源（或来源不在回收站里）→ 自己当根
      }
    }
    const rowOf = (s, isCopy) => `<div class="trash-row${isCopy ? " trash-copy" : ""}">
        <span class="trash-name">${isCopy ? '<span class="trash-branch">└</span> ' : ""}${esc(s.title || "未命名")}</span>
        ${isCopy ? '<span class="tag">恢复副本</span>' : ""}
        <span class="spacer"></span>
        <button class="btn sm ghost" data-restore="${esc(s.id)}">恢复</button>
      </div>`;
    const html = [];
    for (const s of roots) {
      html.push(rowOf(s, false));
      for (const c of (children.get(s.id) || [])) html.push(rowOf(c, true));
    }
    // 兜底：来源不在回收站里的副本（避免因为分组而漏显示）
    const shown = new Set();
    for (const s of roots) { shown.add(s.id); (children.get(s.id) || []).forEach((c) => shown.add(c.id)); }
    for (const s of archived) if (!shown.has(s.id)) html.push(rowOf(s, !!s.parent_id));
    list.innerHTML = html.join("");
    $$(`[data-restore]`, list).forEach((b) => b.onclick = async () => {
      try {
        await api(`/api/sessions/${encodeURIComponent(b.dataset.restore)}`, { method: "PATCH", body: { archived: 0 } });
        b.closest(".trash-row").remove();
        toast("会话已恢复", "ok");
        renderNav();
      } catch (e) { toast("恢复失败：" + e.message, "err"); }
    });
  } catch (e) { list.innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

/* 命中率显示：**不要四舍五入成 100%**。
   实测用户实例上游返回 cached=34816 / prompt=34904（99.7%），前缀缓存确实生效；
   但显示成「100%」会让人以为数字是编的（实测「命中率变成百分百，这不可能」）。
   规则：保留一位小数；只有当它**真的是 100** 时才显示 100。 */
function hitRatePct(hit, tot) {
  const t = Number(tot) || 0;
  if (t <= 0) return 0;
  const raw = (Number(hit) || 0) / t * 100;
  let r = Math.round(raw * 10) / 10;
  if (r >= 100 && raw < 100) r = 99.9;
  return r;
}

async function renderInfoPanel() {
  if (!S.infoOpen) return;
  const body = $("#ip-body");
  if (!body) return;
  // ★ 右侧面板刷新时轻微淡入（幅度很小；纯装饰，失败也不影响内容）
  try { animEnter(body); } catch (e) {}

  let stats = null;
  try {
    const q = S.sessionId ? `?session_id=${encodeURIComponent(S.sessionId)}` : "";
    stats = await api("/api/stats" + q);
  } catch (e) {}

  const u = S.turnUsage || {};
  const all = (stats && stats.all) || {};
  // 「累计 tokens / 请求数 / 费用」按当前口径应该是本会话的：优先用后端返回的
  // session 维度，没有再退回全库。避免容易以为那是这个会话的数字。
  const sess = (stats && stats.session) || all;

  // 上下文档位（本轮最后一次调用的 prompt 占模型上限的比例）
  // ★ 上限按「用户设置 / 模型能力 / 默认 1M」照实显示；只有真取不到数值才显示
  //   「未限制」。旧实现把兜底值整体判成未限制，用户看不出自己设的窗口生效没有
  //   （实测反馈「各模型的上下文用户设置的多少他就显示多少」）。
  const limit = S.contextLimit || 0;
  const used = u.prompt_tokens || 0;   // 最后一次调用的输入（真实上下文占用）
  const unlimited = !!S.contextLimitUnlimited || limit <= 0;
  const pct = limit > 0 ? Math.min(100, Math.round((used / limit) * 100)) : 0;
  const barCls = limit > 0 ? (pct >= 85 ? "danger" : (pct >= 70 ? "warn" : "")) : "";
  const limitText = unlimited ? "未限制" : fmtNum(limit);
  const hintText = unlimited ? "交给上游限制" : (pct >= 85 ? "接近上限" : "尚有余量");

  body.innerHTML = `
    <div class="ip-card">
      <h4>上下文窗口<span class="hint">${hintText}</span></h4>
      <div class="ip-big">${fmtNum(used)}<span class="ip-sub"> / ${limitText}</span></div>
      ${unlimited ? "" : `
      <div class="ip-bar">
        <i class="${barCls}" style="width:${pct}%"></i>
        <span class="ip-bar-mark" style="left:${Math.round((S.compactPct || 0.8) * 100)}%"></span>
      </div>
      <div class="ip-legend"><span>已用 ${pct}%</span><span class="spacer"></span>
        <span>压缩阈值 ${Math.round((S.compactPct || 0.8) * 100)}%</span></div>`}
    </div>

    <div class="ip-card">
      <h4>命中率<span class="hint">越高越省钱</span></h4>
      ${(() => {
        // ★ 命中率口径必须跟着会话走（实测「上下文卡 0 的时候命中率显示
        //   4.77M，AI 输出之后才变回正常」）。
        //   根因：本会话没有数据时**回退到全库聚合** —— 那个 4.77M 是所有历史
        //   会话混在一起的累计值，跟当前会话毫无关系；上下文明明是 0，旁边却
        //   挂着一个巨大的命中量，看起来就是数字错乱。
        //   现在：本会话没数据就显示「暂无」，不再拿全库数字冒充本会话读数。
        const hasLocal = (u.prompt_tokens || 0) > 0;
        if (!hasLocal) {
          return `<div class="ip-big">—<span class="ip-sub"></span></div>
            <div class="ip-legend"><span>本会话还没有调用记录</span></div>`;
        }
        const tot = u.prompt_tokens || 0;
        const hit = u.cached_tokens || 0;
        const rate = hitRatePct(hit, tot);
        const cls = rate >= 70 ? "" : (rate >= 40 ? "warn" : "danger");
        return `<div class="ip-big">${rate}<span class="ip-sub">%</span></div>
          <div class="ip-bar"><i class="${cls}" style="width:${rate}%"></i></div>
          <div class="ip-legend"><span>命中 ${fmtNum(hit)}</span><span class="spacer"></span>
          <span>共 ${fmtNum(tot)}</span></div>
          <div class="ip-legend" style="margin-top:4px"><span>口径：本会话最后一次调用</span></div>`;
      })()}
    </div>

    <div class="ip-card">
      <h4>会话指标<span class="hint">${(stats && stats.session) ? "本会话" : "全库累计"}</span></h4>
      <div class="ip-row"><span class="k">累计 tokens</span><span class="v">${fmtNum(sess.total_tokens || 0)}</span></div>
      <div class="ip-row"><span class="k">请求数</span><span class="v">${fmtNum(sess.calls || 0)}</span></div>
      <div class="ip-row"><span class="k">会话费用</span><span class="v">${(sess.cost || 0).toFixed(4)}</span></div>
      <div class="ip-row"><span class="k">运行时间</span><span class="v">${fmtDuration(sess.duration || 0)}</span></div>
      <div class="ip-row"><span class="k">平均每轮</span><span class="v">${sess.calls ? fmtDuration((sess.duration || 0) / sess.calls) : "—"}</span></div>
    </div>

    <div class="ip-card">
      <h4>本轮用量<span class="hint">本回合累计</span></h4>
      <div class="ip-row"><span class="k">输入</span><span class="v">${fmtNum(u.acc_prompt_tokens || 0)}</span></div>
      <div class="ip-row"><span class="k">输出</span><span class="v">${fmtNum(u.completion_tokens || 0)}</span></div>
      <div class="ip-row"><span class="k">推理</span><span class="v">${fmtNum(u.reasoning_tokens || 0)}</span></div>
      <div class="ip-row"><span class="k">缓存命中</span><span class="v">${fmtNum(u.acc_cached_tokens || 0)}</span></div>
      <div class="ip-row"><span class="k">费用</span><span class="v">${fmtCost(u.cost || 0, u.currency)}</span></div>
    </div>

    ${renderUsageBreakdown(stats)}
    ${renderToolStats(stats)}
  `;
}

/** 把秒数格式化成人话（如 1m23s / 12.4s） */
function fmtDuration(sec) {
  const n = Number(sec) || 0;
  if (n <= 0) return "—";
  if (n < 60) return n.toFixed(1) + "s";
  const m = Math.floor(n / 60), s = Math.round(n % 60);
  if (m < 60) return `${m}m${String(s).padStart(2, "0")}s`;
  return `${Math.floor(m / 60)}h${String(m % 60).padStart(2, "0")}m`;
}

/** 工具调用统计（次数 / 成功率） */
function renderToolStats(stats) {
  const tools = (stats && stats.tools) || [];
  if (!Array.isArray(tools) || !tools.length) {
    return `<div class="ip-card"><h4>工具调用</h4>
      <div class="ip-sub">还没有工具调用记录</div></div>`;
  }
  const rows = tools.slice(0, 8).map((t) => {
    const calls = t.calls || 0, ok = t.ok != null ? t.ok : calls;
    const rate = calls > 0 ? Math.round((ok / calls) * 100) : 0;
    return `<div class="ip-row"><span class="k">${esc(t.name || t.tool || "未知")}</span>
      <span class="v">${calls} 次 · ${rate}%</span></div>`;
  }).join("");
  return `<div class="ip-card"><h4>工具调用<span class="hint">次数 · 成功率</span></h4>${rows}</div>`;
}

/** 来源占比 + 明细 */
function renderUsageBreakdown(stats) {
  if (!stats) return "";
  const byModel = stats.by_model || stats.models || [];
  if (!Array.isArray(byModel) || !byModel.length) {
    return `<div class="ip-card"><h4>用量分析</h4>
      <div class="ip-sub">还没有调用记录</div></div>`;
  }
  const total = byModel.reduce((s, m) => s + (m.total_tokens || 0), 0) || 1;
  const palette = ["#4f46e5", "#0ea5e9", "#10b981", "#f59e0b", "#ef4444", "#8b5cf6"];
  let bars = "", legend = "";
  byModel.slice(0, 6).forEach((m, i) => {
    const p = Math.round(((m.total_tokens || 0) / total) * 100);
    bars += `<i style="width:${p}%;background:${palette[i % palette.length]}"></i>`;
    legend += `<div class="ratio-legend" style="margin-top:4px">
      <span class="ratio-dot" style="background:${palette[i % palette.length]}"></span>
      <span>${esc(m.model || m.name || "未知")}</span>
      <span class="spacer" style="flex:1"></span>
      <span>${p}%</span></div>`;
  });
  return `<div class="ip-card">
    <h4>用量分析<span class="hint">共 ${byModel.length} 个模型</span></h4>
    <div class="ratio-bar">${bars}</div>
    ${legend}
  </div>`;
}

function renderStatusBar() {
  const u = S.turnUsage || {};
  const left = $("#sb-left"), right = $("#sb-right");
  if (!left || !right) return;
  // 上下文占用 = 最后一次调用的 prompt_tokens（不是本回合累加值）
  const prompt = u.prompt_tokens || 0;
  const cached = u.cached_tokens || 0;
  const hit = hitRatePct(cached, prompt);
  const elapsed = S.turnElapsed || 0;
  const speed = S.turnSpeed || 0;
  const compact = Math.round((S.compactPct || 0.8) * 100);
  const state = S.streaming ? "运行中" : "就绪";
  // 上下文读数统一走 fmtNum（全站唯一的 K/M 缩写入口）。
  // ★ 旧写法这里另有一个本地 kw 函数做四舍五入，与信息面板的 fmtNum 不一致 ——
  //   同一屏里会出现「44800」和「44.8K」两种写法（实测反馈「数字看着乱」）。
  const pctUsed = S.contextLimit > 0 ? Math.round((prompt / S.contextLimit) * 100) : 0;
  // 未设置上限（无限上下文）时不显示百分比，只显示绝对值
  const ctxText = S.contextLimit > 0
    ? `${fmtNum(prompt)}/${fmtNum(S.contextLimit)}（${pctUsed}%）`
    : (prompt ? fmtNum(prompt) : "0");
  left.innerHTML =
    `<span class="sb-item">${esc(S.workspace || "默认工作区")}</span>` +
    `<span class="sb-item">本次命中 <b>${hit}%</b></span>` +
    `<span class="sb-item">本次输出 <b>${fmtNum(u.completion_tokens || 0)}</b></span>` +
    `<span class="sb-item">本次费用 <b>${fmtCost(u.cost || 0, u.currency)}</b></span>`;
  right.innerHTML =
    `<span class="sb-item">${state}</span>` +
    `<span class="sb-item">${elapsed ? fmtDuration(elapsed) : "—"}</span>` +
    `<span class="sb-item">${speed ? `${speed.toFixed(1)} t/s` : "吞吐 —"}</span>` +
    `<span class="sb-item">上下文 ${ctxText}</span>` +
    `<span class="sb-item">压缩阈值 ${compact}%</span>`;
  paintComposerStatus(speed);
}

/* 运行中的状态短词：每 10 秒换一个。
   ★ 用「时间片 + 哈希」而不是 Math.random()：同一 10 秒片内必须稳定 ——
   如果真用随机数，状态行每秒重绘都会跳一个新词，看起来像在闪。 */
const BUSY_WORDS = [
  "运行中", "工作中…", "文火慢炖中…", "憋词中…", "摸鱼中…",
  "奋笔疾书中…", "绞尽脑汁中…", "运筹帷幄中…", "头脑风暴中…", "字斟句酌中…",
  "一挥而就中…", "冥思苦想中…", "妙笔生花中…", "打磨细节中…", "搬砖中…",
  "肝代码中…", "冲浪查资料中…", "正在使劲…", "酝酿中…", "推敲中…",
];
function busyWord() {
  const slot = Math.floor(Date.now() / 10000);
  let h = slot | 0;
  h = Math.imul(h ^ (h >>> 16), 0x45d9f3b);
  h = Math.imul(h ^ (h >>> 16), 0x45d9f3b);
  h = h ^ (h >>> 16);
  return BUSY_WORDS[Math.abs(h) % BUSY_WORDS.length];
}

/* 输入卡片**上方**的状态行：logo + 状态词 ······ 吞吐。
   空闲＝「空闲」；运行中＝轮换短词 + logo 书写动画；刚结束＝「已完成」。
   与底部状态栏共用同一个 S.turnSpeed。 */
function paintComposerStatus(speed) {
  const logo = $("#cs-logo"), text = $("#cs-text"), sp = $("#cs-speed");
  if (!logo || !text || !sp) return;
  if (!logo.dataset.inited) { logo.innerHTML = logoSvg(18, true); logo.dataset.inited = "1"; }
  const phase = S.streaming ? "busy" : (S._phase || "idle");
  logo.classList.toggle("busy", phase === "busy");
  // ★ 静默过久时不再只写「运行中」——实测过「看起来一直没做完」，
  //   分不清到底是在跑还是断了。这里把「已多少秒没新输出」直接写出来。
  const silent = phase === "busy" && S.lastEventAt ? (Date.now() - S.lastEventAt) / 1000 : 0;
  if (phase === "busy" && silent > 45) {
    text.textContent = silent > 300
      ? `上游长时间无响应（${Math.round(silent)}s）`
      : `仍在运行…（${Math.round(silent)}s 无新输出）`;
  } else {
    text.textContent = phase === "busy" ? busyWord() : (phase === "done" ? "已完成" : "空闲");
  }
  sp.textContent = speed > 0 ? `${speed.toFixed(1)} t/s` : "";
}

/* 计速：滑动窗口 + 按墙钟过期。
   ★ 前两版都算错了：
     · 累计口径把**输入** token 也算成"输出"，读数两千多；
     · 分段口径的分母被整段生成时间主导，读数又偏低（3.7 t/s）。
   现在的规则：只在「确有新内容产出」的时刻记录一个样本（时间 + 已产出 token），
   吞吐 = 窗口内新增产出 / 窗口时间跨度；**没有新内容时读数保持不变**，
   不会随时间往下滑。 */
function pushSpeedSample(now) {
  now = now || Date.now();
  const out = S.streamOutEst || 0;
  const q = S.speedQ || (S.speedQ = []);
  const last = q.length ? q[q.length - 1].out : 0;
  if (out <= last) return;              // 没有新内容：不产生新读数
  q.push({ t: now, out });
  const WINDOW = 12000;                 // 只保留最近 12 秒的样本
  while (q.length > 2 && now - q[0].t > WINDOW) q.shift();
  if (q.length >= 2) {
    const dt = (now - q[0].t) / 1000;
    const dout = out - q[0].out;
    if (dt >= 0.6 && dout > 0) S.turnSpeed = dout / dt;
  }
}

function openExportMenu(anchor) {
  const items = [
    { id: "md", label: "导出 Markdown", icon: "file" },
    { id: "json", label: "导出 JSON", icon: "code" },
    { id: "pdf", label: "打印 / 导出 PDF", icon: "file" },
    { id: "image", label: "导出图片（PNG）", icon: "image" },
    { id: "diag", label: "导出会话诊断", icon: "chart" },
  ];
  openChoiceMenu(anchor, items, "", async (kind) => {
    if (!S.sessionId) return toast("当前没有可导出的会话", "err");
    try {
      if (kind === "md" || kind === "json") {
        const a = document.createElement("a");
        a.href = `/api/sessions/${encodeURIComponent(S.sessionId)}/export?format=${kind}`;
        a.download = `fengcode-${S.sessionId}.${kind}`;
        a.click();
        return;
      }
      const d = await api(`/api/sessions/${encodeURIComponent(S.sessionId)}`);
      const messages = d.messages || [];
      if (kind === "pdf") {
        const win = window.open("", "_blank");
        if (!win) return toast("浏览器阻止了新窗口，请允许弹窗", "err");
        win.document.write(`<html><head><title>${esc(d.session?.title || "Fengcode 会话")}</title><style>body{font-family:Microsoft YaHei,Arial,sans-serif;font-size:17px;max-width:900px;margin:40px auto;line-height:1.7;color:#222}pre{white-space:pre-wrap;background:#f5f5f5;padding:12px;border-radius:8px}</style></head><body><h1>${esc(d.session?.title || "Fengcode 会话")}</h1>${messages.map((m) => `<h3>${esc(m.role)}</h3><pre>${esc(m.content || m.reasoning || "")}</pre>`).join("")}</body></html>`);
        win.document.close(); win.focus(); win.print();
        return;
      }
      if (kind === "image") {
        const text = messages.map((m) => `${m.role}: ${m.content || m.reasoning || ""}`).join("\n\n");
        // ★ 直接用 canvas 逐行绘制文本，不再走「SVG + foreignObject → Image → canvas」。
        //   旧做法在 Chromium 里 foreignObject 转 canvas 常被安全策略挡下，
        //   于是每次都落到 SVG 兜底——用户拿到的就永远是 .svg 而不是 PNG。
        //   canvas 自绘不依赖任何外部资源，稳定产出真 PNG。
        const ok = drawTextToPng(text, d.session?.title || "Fengcode 会话", `fengcode-${S.sessionId}.png`);
        if (!ok) {
          // 极端情况（canvas 不可用）才退回 SVG，并明确告知用户。
          const width = 1400, height = Math.max(800, Math.min(12000, 180 + text.length * 0.55));
          const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}"><rect width="100%" height="100%" fill="#fff"/><foreignObject x="50" y="40" width="1300" height="95%"><div xmlns="http://www.w3.org/1999/xhtml" style="font:17px Microsoft YaHei,Arial,sans-serif;white-space:pre-wrap;line-height:1.6;color:#222">${esc(text)}</div></foreignObject></svg>`;
          downloadText(`fengcode-${S.sessionId}.svg`, svg, "image/svg+xml;charset=utf-8");
          toast("当前环境无法生成 PNG，已导出 SVG", "");
        }
        return;
      }
      const stats = await api("/api/stats").catch(() => ({}));
      downloadText(`fengcode-${S.sessionId}-diagnostic.json`, JSON.stringify({ exported_at: new Date().toISOString(), session: d.session, message_count: messages.length, stats }, null, 2), "application/json");
    } catch (e) { toast("导出失败：" + e.message, "err"); }
  });
}
function downloadBlob(name, blob) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}
/* 用 canvas 逐行绘制文本并导出真 PNG。
   为什么不用 SVG + foreignObject 转 canvas：Chromium 对该路径有安全限制，
   多数情况下 onerror/抛错，导出会落到 SVG 兜底，用户拿到的是 .svg 而非 .png。
   自绘完全不依赖外部资源，稳定产出 PNG。返回 true 表示已成功导出。 */
function drawTextToPng(text, title, filename) {
  try {
    const DPR = 2;                    // 2 倍像素密度，导出图更清晰
    const W = 1240, PAD = 56, LINE = 30, FS = 17;
    const src = String(text || "");
    const maxChars = Math.floor((W - PAD * 2) / (FS * 0.62));  // 中文近似字宽
    const lines = [];
    for (const raw of src.split(/\r?\n/)) {
      if (!raw) { lines.push(""); continue; }
      for (let i = 0; i < raw.length; i += maxChars) lines.push(raw.slice(i, i + maxChars));
    }
    const headerH = 92;
    const H = Math.max(400, headerH + lines.length * LINE + PAD * 2);

    const canvas = document.createElement("canvas");
    canvas.width = W * DPR; canvas.height = H * DPR;
    const ctx = canvas.getContext("2d");
    if (!ctx) return false;
    ctx.scale(DPR, DPR);
    ctx.fillStyle = "#ffffff"; ctx.fillRect(0, 0, W, H);

    // 标题
    ctx.fillStyle = "#111111";
    ctx.font = `600 24px "Microsoft YaHei", Arial, sans-serif`;
    ctx.fillText(String(title || "Fengcode 会话").slice(0, 40), PAD, PAD + 18);
    ctx.fillStyle = "#bbbbbb";
    ctx.fillRect(PAD, PAD + 42, W - PAD * 2, 1);

    // 正文
    ctx.font = `${FS}px "Microsoft YaHei", Arial, sans-serif`;
    ctx.fillStyle = "#222222";
    let y = PAD + headerH;
    for (const ln of lines) {
      ctx.fillText(ln, PAD, y);
      y += LINE;
    }
    canvas.toBlob((png) => {
      if (png) downloadBlob(filename, png);
      else downloadText(filename.replace(/\.png$/, ".svg"),
        `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}"><rect width="100%" height="100%" fill="#fff"/></svg>`,
        "image/svg+xml;charset=utf-8");
    }, "image/png");
    return true;
  } catch (e) { return false; }
}
function downloadText(name, text, type) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([text], { type: type || "text/plain;charset=utf-8" }));
  a.download = name; a.click(); setTimeout(() => URL.revokeObjectURL(a.href), 1000);
}

function renderTopActions(page) {
  const bar = $("#top-actions");
  const btn = (label, fn, cls) => {
    const b = document.createElement("button");
    b.className = "btn sm " + (cls || "");
    b.textContent = label;
    b.onclick = fn;
    return b;
  };
  bar.innerHTML = "";
  if (page === "chat") {
    bar.appendChild(btn("导出", () => openExportMenu($("#top-actions button")), ""));
    // 顶部不放「新对话」（侧边栏已有），只保留轻量导出和侧栏开关。
    bar.classList.add("has-chat-actions");
  } else if (page === "sessions") {
    bar.appendChild(btn("新建会话", newSession, "primary"));
    bar.appendChild(btn("刷新", () => PAGES.sessions()));
  } else if (page === "memory") {
    bar.appendChild(btn("新增记忆", () => memoryEdit(null), "primary"));
    bar.appendChild(btn("重建索引", async () => {
      try { const r = await api("/api/memories", { method: "POST", body: { action: "reindex" } });
        toast(`已重建 ${r.count} 条索引`, "ok"); PAGES.memory(); } catch (e) { toast(e.message, "err"); }
    }));
    bar.appendChild(btn("刷新", () => PAGES.memory()));
  } else if (page === "skills") {
    bar.appendChild(btn("新建技能", () => skillCreate(), "primary"));
    bar.appendChild(btn("重新扫描", async () => {
      try { const r = await api("/api/skills", { method: "POST", body: { action: "reload" } });
        toast(`发现 ${r.count} 个技能`, "ok"); PAGES.skills(); } catch (e) { toast(e.message, "err"); }
    }));
  } else if (page === "tools") {
    bar.appendChild(btn("刷新", () => PAGES.tools()));
  } else if (page === "mcp") {
    bar.appendChild(btn("添加服务", () => mcpEdit(null), "primary"));
    bar.appendChild(btn("从配置导入", importMcp, ""));
    bar.appendChild(btn("全部启动", async () => {
      try {
        const d = await api("/api/mcp");
        for (const s of (d.configured || [])) {
          if (s.enabled) { try { await api("/api/mcp", { method: "POST", body: { action: "start", name: s.name } }); } catch (e) {} }
        }
        toast("已启动全部服务", "ok"); PAGES.mcp();
      } catch (e) { toast(e.message, "err"); }
    }));
    bar.appendChild(btn("刷新", () => PAGES.mcp()));
  } else if (page === "plugins") {
    bar.appendChild(btn("重新加载", async () => {
      try { await api("/api/plugins", { method: "POST", body: { action: "reload" } });
        toast("已重载", "ok"); PAGES.plugins(); } catch (e) { toast(e.message, "err"); }
    }));
  } else if (page === "workflows") {
    bar.appendChild(btn("新建流程", () => workflowEdit(null), "primary"));
    bar.appendChild(btn("示例", async () => {
      try { const d = await api("/api/workflows?sample=1"); workflowEdit({ dag: d.sample, name: d.sample.name, description: d.sample.description }); }
      catch (e) { toast(e.message, "err"); }
    }));
  } else if (page === "jobs") {
    bar.appendChild(btn("新建任务", () => jobEdit(null), "primary"));
    bar.appendChild(btn("刷新", () => PAGES.jobs()));
  } else if (page === "remote") {
    bar.appendChild(btn("添加主机", () => remoteEdit(null), "primary"));
    bar.appendChild(btn("刷新", () => PAGES.remote()));
  } else if (page === "stats") {
    bar.appendChild(btn("查看审计", () => go("audit")));
    bar.appendChild(btn("刷新", () => PAGES.stats()));
  } else if (page === "audit") {
    bar.appendChild(btn("刷新", () => PAGES.audit()));
  }
}

/* ==========================================================================
   对话页
   ========================================================================== */
const msgBox = () => $("#messages");

function addMessage(role, html, opts) {
  opts = opts || {};
  const wrap = document.createElement("div");
  wrap.className = "msg-wrap";
  // 给用户消息一个锚点 id，供右侧「对话导航」跳转
  if (role === "user") {
    S.msgSeq = (S.msgSeq || 0) + 1;
    wrap.id = "msg-" + S.msgSeq;
    wrap.dataset.userMsg = "1";
  }
  wrap.innerHTML = `<div class="msg ${role}${opts.pending ? " pending" : ""}">
    <div class="body">
      <div class="who">
        ${role === "user" ? `<button class="turn-toggle" type="button" title="折叠 / 展开这一轮">收起</button>` : ""}
        <span class="meta">${esc(opts.meta || "")}</span>
      </div>
      <div class="content"></div>
    </div></div>`;
  const c = $(".content", wrap);
  if (opts.raw) c.innerHTML = html; else c.className = "md", c.innerHTML = md(html);
  msgBox().appendChild(wrap);
  // ★ 新气泡渐显（只动透明度，不动位置 —— 动位置会让正文在流式中反复跳动）
  try { animMessage(wrap); } catch (e) {}
  if (role === "user") {
    const tg = $(".turn-toggle", wrap);
    if (tg) tg.onclick = (e) => { e.preventDefault(); toggleTurn(wrap); };
    refreshMsgNav();
  }
  scrollDown();
  return c;
}

/* ★ 1-D：按轮次折叠。
   以前一轮里堆了「思考 → 工具 → 正文 → 工具…」几十个块，长会话几乎没法读，
   而且**被中断/出错的轮次连个折叠入口都没有**。
   规则：
     · 每个用户消息 = 一轮；点「收起」把这一轮后续的所有块折叠起来；
     · 折叠状态**按轮次**记进 localStorage（不落服务端）；
     · **中断/出错的轮次默认保持展开**，让异常一眼可见。 */

/** 找某个 wrap 所属轮次的锚点（最近的 user 消息 wrap）。 */
function turnAnchorOf(wrap) {
  let cur = wrap;
  while (cur) {
    if (cur.dataset && cur.dataset.userMsg === "1") return cur;
    cur = cur.previousElementSibling;
  }
  return null;
}

/** 找**本轮**已产生的最后一个 wrap（同一轮锚点之后、下一个 user 消息之前）。
    ★ 用途：result 到达时若 c.assist 已被 tool.start 封存，需要新建一个气泡承载
      定稿文字 —— 它必须落回本轮时间线的收尾位置，而不是被 append 到整段对话末尾。
      （实测「最终文字跑到最上面 / 掉到工具卡后面，重启才回下面」。）
    轮次锚点从「对话里最后一个 user 消息」倒推：本轮就是它之后的所有节点。
    找不到 user 消息时退回「对话里最后一个 wrap」。 */
function lastTurnWrap(c) {
  const box = msgBox();
  const all = box.querySelectorAll(".msg-wrap");
  if (!all.length) return null;
  // 兜底：c 上记过本轮锚点就直接用（tool.start 封存 assist 时会留下）
  let anchor = (c && c._turnAnchor && c._turnAnchor.isConnected) ? c._turnAnchor : null;
  if (!anchor) {
    for (let i = all.length - 1; i >= 0; i--) {
      if (all[i].dataset && all[i].dataset.userMsg === "1") { anchor = all[i]; break; }
    }
  }
  if (!anchor) return all[all.length - 1];
  let last = null;
  let n = anchor.nextElementSibling;
  while (n && !(n.dataset && n.dataset.userMsg === "1")) { last = n; n = n.nextElementSibling; }
  return last || all[all.length - 1];
}

/** 这一轮里是否有失败/中断的痕迹（有就默认展开，别把异常藏起来）。 */
function turnHasProblem(userWrap) {
  let n = userWrap.nextElementSibling;
  while (n && !(n.dataset && n.dataset.userMsg === "1")) {
    if (n.querySelector && n.querySelector(".msg.failed, .msg.interrupted")) return true;
    n = n.nextElementSibling;
  }
  return false;
}

function turnKey(userWrap) {
  return "fg_turn_collapsed_" + (S.sessionId || "") + "_" + (userWrap.id || "");
}

function setTurnCollapsed(userWrap, collapsed) {
  let n = userWrap.nextElementSibling;
  while (n && !(n.dataset && n.dataset.userMsg === "1")) {
    n.classList.toggle("turn-hidden", collapsed);
    n = n.nextElementSibling;
  }
  userWrap.classList.toggle("turn-collapsed", collapsed);
  const tg = $(".turn-toggle", userWrap);
  if (tg) tg.textContent = collapsed ? "展开" : "收起";
  try {
    if (collapsed) localStorage.setItem(turnKey(userWrap), "1");
    else localStorage.removeItem(turnKey(userWrap));
  } catch (e) {}
}

function toggleTurn(userWrap) {
  const want = !userWrap.classList.contains("turn-collapsed");
  // 这一轮有问题（失败/中断）时不允许收起 —— 用户要能一眼看到异常
  if (want && turnHasProblem(userWrap)) {
    toast("这一轮有失败或中断内容，已保持展开", "");
    return;
  }
  setTurnCollapsed(userWrap, want);
}

/** 新会话/切换会话后：按 localStorage 还原折叠状态；有问题的轮次强制展开。 */
function restoreTurnCollapse() {
  const users = $$("#messages .msg-wrap[data-user-msg]");
  for (const u of users) {
    const problem = turnHasProblem(u);
    let collapsed = false;
    try { collapsed = !problem && localStorage.getItem(turnKey(u)) === "1"; } catch (e) {}
    setTurnCollapsed(u, collapsed);
  }
}
/* 是否跟随最新内容滚动。
   为什么不直接用「距底 < 180px」判断：思考框展开或一次性追加内容时，距底会
   瞬间冲到 180px 以上，判据立刻失效，后续所有自动滚动都不再贴底——用户看到的
   就是「输出被顶上去、看不到新文字」。
   改成显式的跟随标志：只有在用户主动向上滚动时才停止跟随。 */
let FOLLOW_TAIL = true;

function scrollDown(force) {
  const m = msgBox();
  if (force) FOLLOW_TAIL = true;
  if (force || FOLLOW_TAIL) m.scrollTop = m.scrollHeight;
  updateJumpBottom();
}
/** ★ 由「用户明确操作」维护是否跟随，而不是靠「内容增长后的距底距离」反推。
    为什么必须这样：流式期间内容每 50ms 就增长一次，每次增长都会把视图拉到底。
    用户想上滑看早前内容时，刚滚上去一点（还没超过 180px 的判定阈值）就被下一次
    增长拽回底部 —— 表现就是「滚轮抽搐、怎么滚都滚不上去」（实测）。
    现在：一旦检测到用户主动向上滚/按上方向键，立刻停止跟随；滚回底部才恢复。 */
function watchUserScroll() {
  const m = msgBox();
  if (!m || m._watchScrollBound) return;
  m._watchScrollBound = true;
  m.addEventListener("wheel", (e) => {
    if (e.deltaY < 0) FOLLOW_TAIL = false;                 // 向上滚 = 我要看上面的
    else if (m.scrollHeight - m.scrollTop - m.clientHeight < 24) FOLLOW_TAIL = true;
    updateJumpBottom();
  }, { passive: true });
  m.addEventListener("keydown", (e) => {
    if (e.key === "PageUp" || e.key === "ArrowUp" || e.key === "Home") FOLLOW_TAIL = false;
  }, true);
}
function updateJumpBottom() {
  const m = msgBox(), b = $("#jump-bottom");
  if (!m || !b) return;
  const away = m.scrollHeight - m.scrollTop - m.clientHeight > 180;
  // 离开底部一段距离就认为用户主动脱离了跟随；回到阈值内则恢复跟随。
  if (away) FOLLOW_TAIL = false;
  else FOLLOW_TAIL = true;
  // ★ 「回到底部」也要在**思考区**里出现：
  //   用户展开思考、上滑看早前内容时，外层消息区可能仍在底部，
  //   只按外层判断按钮就不会出现（实测「回到底部在思考页面用不了」）。
  //   只要任一个已展开的思考块离开了底部，就显示按钮。
  let rcAway = false;
  try {
    const list = document.querySelectorAll("#messages .reasoning[open] .rc");
    for (const rc of list) {
      if (rc.scrollHeight - rc.scrollTop - rc.clientHeight > 20) { rcAway = true; break; }
    }
  } catch (e) {}
  b.classList.toggle("show", away || rcAway);
}
function jumpToBottom() {
  const m = msgBox();
  if (m) m.scrollTo({ top: m.scrollHeight, behavior: "smooth" });
  FOLLOW_TAIL = true;
  // ★ 一并把展开的思考块滚到底 —— 否则按钮在思考区里点了没反应（只有外层动了）。
  try {
    document.querySelectorAll("#messages .reasoning[open] .rc").forEach((rc) => {
      rc._followTail = true;
      rc.scrollTop = rc.scrollHeight;
    });
  } catch (e) {}
  updateJumpBottom();
}
function clearMessages() {
  msgBox().innerHTML = "";
  S.msgSeq = 0;
  refreshMsgNav();
}

/* ==========================================================================
   对话内导航（右侧小横杠）
   ---------------------------------------------------------------------
   每条用户消息对应一根横杠；点一下滚动到那条消息。
   超过 30 条时按比例抽样，避免横杠太密。
   ========================================================================== */
function refreshMsgNav() {
  const nav = $("#msg-nav");
  if (!nav) return;
  const wraps = $$("#messages .msg-wrap[data-user-msg]");
  if (!wraps.length) {
    nav.innerHTML = "";
    nav.classList.remove("has-items");
    return;
  }
  const MAX_BARS = 30;
  let picked = wraps;
  if (wraps.length > MAX_BARS) {
    const step = wraps.length / MAX_BARS;
    picked = [];
    for (let i = 0; i < MAX_BARS; i++) picked.push(wraps[Math.floor(i * step)]);
  }
  nav.innerHTML = picked.map((w) => {
    const c = w.querySelector(".content");
    const t = (c ? c.innerText : "").replace(/\s+/g, " ").trim().slice(0, 90);
    return `<button class="mn-bar" data-goto="${esc(w.id)}" title="${esc(t || "（空消息）")}"></button>`;
  }).join("");
  nav.classList.add("has-items");
  nav.classList.toggle("dense", wraps.length > 20);
  $$(".mn-bar", nav).forEach((b) => b.onclick = () => {
    const el = document.getElementById(b.dataset.goto);
    if (!el) return;
    el.scrollIntoView({ behavior: "smooth", block: "start" });
    el.style.transition = "background .5s";
    const prev = el.style.background;
    el.style.background = "var(--accent-soft)";
    setTimeout(() => { el.style.background = prev || ""; }, 700);
  });
}

/** 滚动时高亮当前所在的横杠 */
function syncMsgNavActive() {
  const nav = $("#msg-nav");
  if (!nav || !nav.classList.contains("has-items")) return;
  const wraps = $$("#messages .msg-wrap[data-user-msg]");
  if (!wraps.length) return;
  const box = msgBox();
  const top = box.scrollTop + 60;
  let cur = 0;
  wraps.forEach((w, i) => { if (w.offsetTop <= top) cur = i; });
  const curId = wraps[cur].id;
  $$(".mn-bar", nav).forEach((b) => b.classList.toggle("active", b.dataset.goto === curId));
}

async function newSession() {
  try {
    saveDraft(S.sessionId);        // 新建前先存下旧会话的草稿
    const r = await api("/api/sessions", {
      method: "POST",
      body: { title: "新对话", workspace: S.workspace || "" },
    });
    S.sessionId = r.session.session_id || r.session.id;
    const defaultPerm = (() => { try { return localStorage.getItem("fengcode_default_permission") || "ask"; } catch (e) { return "ask"; } })();
    CUR_PERM = ["deny", "ask", "allow"].includes(defaultPerm) ? defaultPerm : "ask";
    try { localStorage.setItem("fengcode_perm", CUR_PERM); } catch (e) {}
    paintPermChip();
    clearMessages();
    emptyState();
    // ★ 新会话的上下文占用必须归零（实测「说完话切换新对话，上下文占用
    //   还是老对话的几十 K，重启才变成 0K」）。新建的会话没有任何历史，
    //   用量就该是 0；不显式复位就会沿用上一个会话的读数。
    applySessionUsage({ input_tokens: 0, output_tokens: 0, cost: 0 });
    restoreDraft(S.sessionId);     // 新会话是空白草稿（通常为空）
    go("chat");
    renderNav();
    try { refreshTodos(); } catch (e) {}   // 新会话：待办面板应为空
    const chatInput = $("#input");
    if (chatInput) { chatInput.disabled = false; chatInput.focus(); }
  } catch (e) { toast("新建失败：" + e.message, "err"); }
}

/** 从侧边栏点开一个会话 */
async function openSession(sid) {
  if (!sid) return;
  if (sid === S.sessionId && S.page === "chat") return;
  saveDraft(S.sessionId);          // 离开前把当前输入存进它自己的草稿位
  await loadSession(sid);
  // 待发队列按会话隔离：切过来先清掉上一会话的，再读本会话的（别串队）
  S.queue = [];
  try { await loadQueue(); } catch (e) {}
  restoreDraft(sid);               // 切回来时恢复该会话没发出去的草稿
  go("chat");
  renderNav();
  try { refreshTodos(); } catch (e) {}   // 切会话：待办面板跟着换（别串上一会话的清单）
}

/* ---------------- 输入草稿：每个会话各留一份 ----------------
   为什么按会话存：「切换对话时按会话分别保留草稿」——
   在 A 会话打了一半的字，切到 B 再切回 A 时应当还在，
   而不是被清空、也不是串到 B 的输入框里。
   存在内存 Map 里（会话切换是高频操作，不必落盘）；
   localStorage 只用来跨刷新保留当前会话那一份，避免刷新丢字。 */
const DRAFTS = new Map();

function saveDraft(sid) {
  if (!sid) return;
  const inp = $("#input");
  if (!inp) return;
  const v = inp.value;
  if (v && v.trim()) DRAFTS.set(sid, v);
  else DRAFTS.delete(sid);
}

function restoreDraft(sid) {
  const inp = $("#input");
  if (!inp) return;
  const v = (sid && DRAFTS.get(sid)) || "";
  inp.value = v;
  // 高度跟着内容走，否则多行草稿恢复后输入框还是单行高
  inp.style.height = "auto";
  inp.style.height = Math.min(260, inp.scrollHeight) + "px";
}
/* ==========================================================================
   首次启动向导
   只问三件真正影响可用性的事，随时可跳过；完成后不再打扰。
   ========================================================================== */
function openFirstRunWizard() {
  const models = ((S.boot && S.boot.models) || []);
  const hasKey = models.some((m) => m.has_key);
  let step = 0;

  const mask = document.createElement("div");
  mask.className = "modal-mask show";
  mask.id = "frw";
  document.body.appendChild(mask);

  const finish = async (skipped) => {
    try {
      await api("/api/config", { method: "PATCH", body: { first_run_done: true } });
    } catch (e) {}
    mask.remove();
    if (!skipped) toast("设置完成，可以开始了", "ok");
  };

  const render = () => {
    const dots = [0, 1, 2].map((i) =>
      `<span style="width:6px;height:6px;border-radius:50%;display:inline-block;margin:0 3px;background:${i === step ? "var(--accent)" : "var(--border-strong)"}"></span>`).join("");
    const foot = (extra) => `
      <div class="row" style="justify-content:space-between;margin-top:14px">
        <span>${dots}</span>
        <span class="row">
          <button class="btn ghost" id="frw-skip">跳过</button>${extra || ""}
        </span>
      </div>`;

    if (step === 0) {
      mask.innerHTML = `<div class="modal" style="max-width:520px">
        <h3>欢迎使用 Fengcode</h3>
        <div class="sec-desc">它会读写文件、执行命令、访问网络——真的动手，不只是聊天。</div>
        <div style="border:1px solid var(--border);border-radius:9px;padding:12px 14px;margin:12px 0;font-size:12.5px;line-height:1.9">
          <div>• 所有数据存在本机，模型供应商由你自己配</div>
          <div>• 有审批门、路径白名单、沙箱三道防护</div>
          <div>• 授予最小权限，不要指向不可信的数据</div>
        </div>
        ${foot(`<button class="btn primary" id="frw-next">继续</button>`)}
      </div>`;
      $("#frw-skip").onclick = () => finish(true);
      $("#frw-next").onclick = () => { step = 1; render(); };
      return;
    }

    if (step === 1) {
      const ready = models.filter((m) => m.has_key).length;
      mask.innerHTML = `<div class="modal" style="max-width:520px">
        <h3>配置模型</h3>
        <div class="sec-desc">${hasKey
          ? "检测到已有可用模型，可以直接开始。"
          : "还没有可用的模型密钥，先填一个才能对话。"}</div>
        <div style="border:1px solid var(--border);border-radius:9px;padding:12px 14px;margin:12px 0">
          ${hasKey
            ? `<span class="tag ok">已就绪</span><span style="font-size:12.5px;margin-left:6px">共 ${ready} 个模型可用</span>`
            : `<div style="font-size:12.5px">到「设置 → 模型服务」填供应商地址与密钥，支持任何 OpenAI 兼容接口。</div>`}
        </div>
        ${foot((hasKey ? "" : `<button class="btn" id="frw-goconf">去配置</button>`)
               + `<button class="btn primary" id="frw-next">${hasKey ? "继续" : "我已配好"}</button>`)}
      </div>`;
      $("#frw-skip").onclick = () => finish(true);
      $("#frw-next").onclick = () => { step = 2; render(); };
      const gc = $("#frw-goconf");
      if (gc) gc.onclick = async () => { await finish(true); go("settings"); openSetting("providers"); };
      return;
    }

    mask.innerHTML = `<div class="modal" style="max-width:520px">
      <h3>权限等级</h3>
      <div class="sec-desc">控制它能自己改哪些地方，之后可在设置里随时改。</div>
      <div style="margin:12px 0">
        ${[["deny", "只看不改", "只读取查看，任何写操作都被拒绝"],
           ["ask", "工作区可改", "工作区内直接改，越界会先问你（推荐）"],
           ["allow", "完全权限", "全部放行，不再逐条确认"]].map(([v, t, ds]) => `
          <label style="display:flex;gap:9px;align-items:flex-start;padding:9px 11px;border:1px solid var(--border);border-radius:9px;margin-bottom:7px;cursor:pointer">
            <input type="radio" name="frw-perm" value="${v}"${v === "ask" ? " checked" : ""} style="width:auto;margin-top:3px">
            <span><b style="font-size:13px">${t}</b>
              <div class="help" style="margin-top:2px">${ds}</div></span>
          </label>`).join("")}
      </div>
      ${foot(`<button class="btn primary" id="frw-done">开始使用</button>`)}
    </div>`;
    $("#frw-skip").onclick = () => finish(true);
    $("#frw-done").onclick = async () => {
      const picked = (document.querySelector('input[name="frw-perm"]:checked') || {}).value || "ask";
      try {
        await api("/api/config", { method: "PATCH", body: { permissions: { mode: picked } } });
        CUR_PERM = picked;
        if (typeof paintPermChip === "function") paintPermChip();
      } catch (e) {}
      await finish(false);
    };
  };
  render();
}

function emptyState() {
  const b = msgBox();
  b.innerHTML = `<div class="empty welcome-empty" style="max-width:700px;margin:72px auto">
    <div class="big">${icon("sparkle", 36)}</div>
    <div style="font-size:26px;font-weight:650;color:var(--text);margin-bottom:14px">有什么可以帮你？</div>
    <div style="font-size:17px;line-height:1.9">
      我能读写文件、执行命令、联网搜索、操作数据库、管理长期记忆，忙不过来时还会自己开小号并行干活。<br>
      试试：<span class="tag">看一下 D 盘有哪些工程</span>
      <span class="tag">帮我写个脚本统计日志</span>
      <span class="tag">搜一下最新的 xxx 方案</span>
    </div></div>`;
}
async function loadSession(sid) {
  try {
    const d = await api("/api/sessions/" + encodeURIComponent(sid));
    S.sessionId = sid;
    if (d.session && d.session.model) S.model = d.session.model;
    if (d.session && d.session.workspace) S.workspace = d.session.workspace;
    // ★ 上下文占用必须跟着会话走（实测「切新对话还显示旧对话的几十 K、
    //   重启才归零；换回来又没效果」）。
    //   旧实现只换 sessionId 与消息，从不碰 S.turnUsage —— 而状态行读数就取自它，
    //   于是新会话沿用上一个会话的占用、切回来也不会恢复本会话的占用。
    //   这里按该会话记录的用量恢复占用（后端按会话独立存 input_tokens）。
    applySessionUsage(d.session);
    clearMessages();
    if (!d.messages.length) emptyState();
    for (const m of d.messages) renderStored(m);
    syncModelSelect();
    // ★ 1-D：历史加载完，按 localStorage 还原每轮折叠状态（有问题的轮次强制展开）
    try { restoreTurnCollapse(); } catch (e) {}
  } catch (e) { toast("加载会话失败：" + describeLoadError(e), "err"); }
}

/** 按会话记录设置「上下文占用」读数。
    ★ 为什么要单独一个函数：新建会话、载入会话、换模型三条路径都要刷新它，
      散着写必然漏（实测的「不独立 / 换了没效果 / 重启才对」就是这么来的）。
    后端按会话独立存 input_tokens：有就按它显示，没有就归零。 */
function applySessionUsage(sess) {
  const used = Number((sess && sess.input_tokens) || 0);
  const out = Number((sess && sess.output_tokens) || 0);
  // ★ 上下文占用 / 命中率必须取「最后一次调用」的快照（后端存在 meta.last_usage）：
  //   sessions.input_tokens 是**累计值**（一场对话总共消耗），一轮工具循环会把同一份
  //   上下文向上游重发十几次，拿累计值当占用会显示成远大于真实上下文的天文数字。
  //   没有快照（旧数据）时才退回累计值，保证不显示成空白。
  const lu = (sess && sess.meta && sess.meta.last_usage) || null;
  const ctxPrompt = lu ? Number(lu.prompt_tokens || 0) : used;
  const ctxCached = lu ? Number(lu.cached_tokens || 0) : 0;
  S.turnUsage = Object.assign({}, S.turnUsage || {}, {
    // prompt_tokens = 上下文占用读数（取该会话最近一次调用的输入量）
    prompt_tokens: ctxPrompt,
    cached_tokens: ctxCached,
    // 累计口径：会话指标里的「累计 tokens」用它
    acc_prompt_tokens: used,
    completion_tokens: out,
    total_tokens: used + out,
    cost: Number((sess && sess.cost) || 0),
  });
  try { renderStatusBar(); } catch (e) {}
  try { renderInfoPanel(); } catch (e) {}
}
/* ★ 1-H：把「会话加载失败」翻译成用户能懂的说明，而不是把原始报错甩给他。
   后端在失败时返回 {error, code}，这里按 code 给具体原因与下一步建议。 */
function describeLoadError(e) {
  const msg = (e && e.message) || String(e || "");
  const code = (e && e.code) || "";
  const map = {
    corrupted: "会话数据损坏（文件内容不完整）。可在「清理与归档」里导出备份后删除该会话。",
    too_large: "会话过大，读取超出可用内存。建议新开一个会话继续。",
    io_error: "读取会话文件失败：可能被其它程序占用或权限不足，请重试。",
    load_failed: "读取会话历史时出错，请重试或换个会话打开。",
    not_found: "会话不存在或已被删除。",
  };
  if (code && map[code]) return map[code] + "（" + msg + "）";
  if (/404/.test(msg)) return map.not_found;
  return msg;
}
function renderStored(m) {
  if (m.role === "system") {
    if ((m.content || "").startsWith("<compacted_history>")) {
      addMessage("system", `【历史已压缩】${esc((m.content || "").replace(/<\/?compacted_history>/g, "").slice(0, 600))}`, { raw: true });
    }
    return;
  }
  if (m.role === "tool") {
    const ok = !String(m.content || "").startsWith("【错误】");
    renderToolCard({ name: m.tool_name || "tool", ok, preview: m.content || "", historical: true });
    return;
  }
  const el = addMessage(m.role, m.role === "assistant" ? md(m.content) : esc(m.content).replace(/\n/g, "<br>"),
    { raw: true, meta: m.created_at ? ago(m.created_at) : "" });
  // ★ 2-M/2-Q：历史消息里的流程图与公式在这里补渲染
  if (m.role === "assistant") { try { renderRich(el); } catch (e) {} }
  // ★ 回执卡回放：回合收尾时那张卡随消息落库在 meta.receipt 里，
  //   重开会话要能重新看到 —— 否则用户回头查「那轮到底改了哪些文件」就没了
  //   （流式期间的卡片只是即时插入，不落库的话重开即消失）。
  if (m.role === "assistant" && m.meta && m.meta.receipt) {
    try { renderReceipt(m.meta.receipt, el.closest(".msg-wrap")); } catch (e) {}
  }
  if (m.reasoning) {
    // 历史消息：思考过程同样默认折叠。
    // ★ 不再判断「显示思考过程」开关 —— 该开关已移除，思考一律显示。
    const rwrap = document.createElement("div");
    rwrap.className = "msg-wrap reasoning-wrap";
    rwrap.innerHTML = `<details class="reasoning"><summary>思考过程<span class="rhint">${fmtNum(String(m.reasoning).length)} 字</span></summary><div class="rc">${esc(m.reasoning)}</div></details>`;
    // ★ 必须插到 `.msg-wrap` 的**兄弟位**（与流式路径 app.js 的 result 分支一致）。
    //   旧写法用 `el.parentElement.parentElement.insertBefore(rwrap, el.parentElement)`，
    //   而 el(.content) → parentElement=.body → parentElement=.msg，等于把思考块插进
    //   `.msg` 里 —— 而 .msg 是 flex 行容器（app.css `.msg { display:flex; gap:12px }`），
    //   于是思考块和正文并排：思考在左、正文被挤到右侧、展开后直接被顶出对话框。
    //   症状只在「大退重进」（走 renderStored）后出现，流式期间反而正常，正是这个原因。
    const owner = el.closest(".msg-wrap");
    if (owner && owner.parentNode) owner.parentNode.insertBefore(rwrap, owner);
  }
}
function renderToolCard(t) {
  const id = "tool-" + Math.random().toString(36).slice(2, 9);
  const running = t.running;
  const cls = running ? "run" : (t.ok === false ? "err" : "");
  const status = running ? '<span class="loading"></span>' : (t.ok === false ? '<span class="tag err">失败</span>' : '<span class="tag ok">成功</span>');
  const dur = t.duration ? `<span class="tag">${fmtDur(t.duration)}</span>` : "";
  const args = t.arguments ? Object.entries(t.arguments).slice(0, 6).map(([k, v]) =>
    `<div><span style="color:var(--text-dim)">${esc(k)}</span> = <span class="mono">${esc(String(v).slice(0, 300))}</span></div>`).join("") : "";
  const html = `<details class="tool ${cls}" id="${id}">
    <summary><span class="tname">${esc(t.name)}</span> ${status} ${dur} ${t.display ? `<span class="tinfo">${esc(t.display)}</span>` : ""}</summary>
    <div class="tbody">
      ${args ? `<div style="font-size:11.5px;line-height:1.7;margin-bottom:8px">${args}</div>` : ""}
      <pre>${esc(t.preview || "（等待结果…）")}</pre>
    </div></details>`;
  const holder = document.createElement("div");
  holder.className = "msg-wrap";
  holder.innerHTML = html;
  msgBox().appendChild(holder);
  scrollDown();
  return $(".tool", holder);
}
function updateToolCard(name, data, done) {
  let node = S.toolNodes.get(name + "|" + (data.index == null ? "" : data.index));
  if (!node) {
    // 找最后一个同名且仍在运行的卡片
    const all = $$(".tool.run");
    for (let i = all.length - 1; i >= 0; i--) {
      if ($(".tname", all[i]).textContent === name) { node = all[i]; break; }
    }
  }
  if (!node) return;
  node.classList.remove("run");
  node.classList.toggle("err", data.ok === false);
  const sum = $("summary", node);
  const spin = $(".loading", sum);
  if (spin) {
    const tag = document.createElement("span");
    tag.className = "tag " + (data.ok === false ? "err" : "ok");
    tag.textContent = data.ok === false ? "失败" : "成功";
    spin.replaceWith(tag);
  }
  if (data.duration != null) {
    const t = document.createElement("span");
    t.className = "tag"; t.textContent = fmtDur(data.duration);
    sum.appendChild(t);
  }
  const pre = $(".tbody pre", node);
  if (pre && data.preview != null) pre.textContent = data.preview;
  // ★ 真实绝对路径：把「AI 说写好了」变成「你能找到的文件」。
  //   旧界面只给 `~/xxx`（其实是 fengcode-data\workspace），用户去桌面白找一场。
  if (data.files && data.files.length) {
    const body = $(".tbody", node);
    if (body && !$(".tool-files", body)) {
      const wrap = document.createElement("div");
      wrap.className = "tool-files";
      wrap.innerHTML = data.files.slice(0, 12).map((f) =>
        `<div class="tool-file" title="${esc(f)}">
           <span class="tf-path">${esc(f)}</span>
           <button class="tf-btn" data-open="${esc(f)}">打开位置</button>
           <button class="tf-btn" data-ref="${esc(f)}">插入路径</button>
         </div>`).join("");
      body.appendChild(wrap);
      $$("[data-open]", wrap).forEach((b) => b.onclick = async (e) => {
        e.preventDefault();
        const p = b.dataset.open;
        if (window.fengcode && typeof window.fengcode.showInFolder === "function") {
          await window.fengcode.showInFolder(p);
        } else {
          try { await navigator.clipboard.writeText(p); toast("网页版无法打开文件夹，路径已复制", ""); }
          catch (err) { toast(p, ""); }
        }
      });
      $$("[data-ref]", wrap).forEach((b) => b.onclick = (e) => {
        e.preventDefault();
        insertRefs([b.dataset.ref]);
        toast("路径已插入输入框", "ok");
      });
    }
  }
  S.toolNodes.delete(name);
}

/** 回合结束时把「本回合生成的文件」明确列出来（★ 实测：AI 说写好了，
    用户去桌面找不到 —— 因为文件其实在工作区 `fengcode-data\workspace`）。
    给真实完整路径 + 「打开位置」按钮，让文件变得可找。 */
function renderTurnFiles(files) {  const list = (files || []).filter(Boolean);
  if (!list.length) return;
  const wrap = document.createElement("div");
  wrap.className = "msg-wrap";
  const rows = list.slice(0, 12).map((f) =>
    `<div class="fm-row">
       <span class="fm-path" title="${esc(f)}">${esc(f)}</span>
       <button class="fm-btn" data-fm-open="${esc(f)}">打开位置</button>
       <button class="fm-btn" data-fm-ref="${esc(f)}">插入路径</button>
     </div>`).join("");
  wrap.innerHTML = `<div class="files-made">
      <div class="fm-head">本回合生成/修改的文件（共 ${list.length} 个）：</div>
      ${rows}
    </div>`;
  msgBox().appendChild(wrap);
  $$("[data-fm-open]", wrap).forEach((b) => b.onclick = async () => {
    const p = b.dataset.fmOpen;
    if (window.fengcode && typeof window.fengcode.showInFolder === "function") {
      await window.fengcode.showInFolder(p);
    } else {
      try { await navigator.clipboard.writeText(p); toast("网页版无法打开文件夹，路径已复制", ""); }
      catch (e) { toast(p, ""); }
    }
  });
  $$("[data-fm-ref]", wrap).forEach((b) => b.onclick = () => {
    insertRefs([b.dataset.fmRef]);
    toast("路径已插入输入框", "ok");
  });
  scrollDown();
}

/** 回合结束时渲染「完成回执卡」。
    ★ 为什么要它（实测痛点）：一轮做完，用户看不出「到底改了哪些文件、
      验证没验证、还有没有没做完的」，只能从正文里那句「我做好了」去猜。
    数据全部来自后端本回合的真实执行记录（TurnResult 的 changed_files /
    verify_commands / gaps），**不是模型自述** —— 所以它比正文可信。
    三行分别是：改了哪些文件（可点「打开位置」）／跑了什么验证／还差什么。
    afterWrap：历史回放时插到该消息之后（不传则追加到对话末尾）。 */
function renderReceipt(data, afterWrap) {
  const files = (data && data.changed_files) || [];
  const verifies = (data && data.verify_commands) || [];
  const gaps = (data && data.gaps) || [];
  const err = data && data.error;
  // 三样都空且没出错 → 没什么可汇报的，不占版面
  if (!files.length && !verifies.length && !gaps.length && !err) return;
  const wrap = document.createElement("div");
  wrap.className = "msg-wrap";
  // 结论：有 gaps 或出错 → 未完成；跑过验证 → 已自检；否则只算「已改动」
  let verdict = "已完成", vcls = "ok";
  if (err) { verdict = "未完成（出错）"; vcls = "err"; }
  else if (gaps.length) { verdict = "未完成"; vcls = "warn"; }
  else if (verifies.length) { verdict = "已完成 · 已自检"; vcls = "ok"; }
  const fileRows = files.slice(0, 12).map((f) =>
    `<div class="rc-row"><span class="rc-path" title="${esc(f)}">${esc(f)}</span>
       <button class="rc-btn" data-rc-open="${esc(f)}">打开位置</button></div>`).join("");
  const vRows = verifies.slice(0, 8).map((c) =>
    `<div class="rc-row"><span class="rc-cmd" title="${esc(c)}">${esc(c)}</span></div>`).join("");
  const gRows = gaps.slice(0, 8).map((g) =>
    `<div class="rc-row"><span class="rc-gap">${esc(g)}</span></div>`).join("");
  wrap.innerHTML = `<div class="receipt">
      <div class="rc-head"><span class="rc-title">本回合回执</span>
        <span class="tag ${vcls}">${verdict}</span></div>
      <div class="rc-sec"><div class="rc-label">改动文件</div>
        ${files.length ? fileRows : `<div class="rc-row"><span class="rc-none">本回合未修改文件</span></div>`}</div>
      <div class="rc-sec"><div class="rc-label">验证</div>
        ${verifies.length ? vRows : `<div class="rc-row"><span class="rc-none">本回合未跑验证</span></div>`}</div>
      ${gaps.length ? `<div class="rc-sec"><div class="rc-label">未完成</div>${gRows}</div>` : ""}
    </div>`;
  // 历史回放：插到该回合消息之后（保持时间顺序）；流式结束：追加到末尾。
  if (afterWrap && afterWrap.parentNode === msgBox() && afterWrap.nextSibling !== wrap) {
    afterWrap.parentNode.insertBefore(wrap, afterWrap.nextSibling);
  } else {
    msgBox().appendChild(wrap);
  }
  $$("[data-rc-open]", wrap).forEach((b) => b.onclick = async () => {
    const p = b.dataset.rcOpen;
    if (window.fengcode && typeof window.fengcode.showInFolder === "function") {
      await window.fengcode.showInFolder(p);
    } else {
      try { await navigator.clipboard.writeText(p); toast("网页版无法打开文件夹，路径已复制", ""); }
      catch (e) { toast(p, ""); }
    }
  });
  scrollDown();
}

/* ---- 待办清单面板（的「拉待办任务」能力）----
   AI 用 `todo` 工具排计划时，后端每题都会 emit `task.update`。
   旧版本前端只在 WebSocket 分支里顺手点个徽标就扔了，**清单本身从不显示**；
   SSE 的 handleEvent 里连 case 都没有 —— 用户完全看不见 AI 的计划。
   这里把当前会话的任务渲染成一个小面板（对话区底部、输入框上方）。 */
const TODO_MARK = {
  pending: "○", in_progress: "◐", completed: "●",
  blocked: "!", cancelled: "×",
};

async function refreshTodos() {
  if (!S.sessionId) return;
  try {
    const d = await api("/api/tasks?session_id=" + encodeURIComponent(S.sessionId));
    renderTodoPanel(d.tasks || [], d.summary || {}, d.goals || []);
  } catch (e) { /* 面板是辅助信息，拉不到就不显示 */ }
}
/* ★ 待办刷新要「跟着回合走」，不能只在 task.update 事件里刷一次。
   实测「待办进度卡在第一步不刷新」的成因有两半：
     后端：只有动过文件才算实质进展，只更新计划时不注入收尾自检；
     前端：事件丢了/晚到时面板就停住。
   这里补一条兜底：回合结束（result）后再拉一次，保证面板与库一致。 */
function refreshTodosSoon() {
  setTimeout(() => { try { refreshTodos(); } catch (e) {} }, 120);
}

function renderTodoPanel(tasks, summary, goals) {
  const box = $("#todo-panel");
  if (!box) return;
  const list = tasks || [];
  const goal = (goals || [])[0] || null;
  if (!list.length && !goal) { box.hidden = true; box.innerHTML = ""; return; }
  const done = (summary && summary.completed) || 0;
  const total = (summary && summary.total) || list.length;
  const pct = total ? Math.round((done / total) * 100) : 0;
  const rows = list.map((t) => {
    const st = t.status || "pending";
    return `<div class="todo-item ${esc(st)}">
      <span class="todo-mark">${TODO_MARK[st] || "○"}</span>
      <span class="todo-title">${esc(t.title || "")}</span>
      ${t.detail ? `<span class="todo-detail" title="${esc(t.detail)}">${esc(t.detail)}</span>` : ""}
    </div>`;
  }).join("");
  box.hidden = false;
  box.innerHTML = `
    <div class="todo-head" data-toggle-todo>
      <span class="todo-ic">${icon("listChecks", 14)}</span>
      <span class="todo-h-title">待办</span>
      <span class="todo-count">${done}/${total}</span>
      <span class="todo-bar"><i style="width:${pct}%"></i></span>
      ${goal ? `<span class="todo-goal" title="${esc(goal.objective || "")}">目标 · ${esc((goal.phase || ""))}</span>` : ""}
      <span class="todo-caret">收起</span>
    </div>
    <div class="todo-body">${rows}</div>`;
  const head = $("[data-toggle-todo]", box);
  const body = $(".todo-body", box);
  const caret = $(".todo-caret", box);
  if (head && body) head.onclick = () => {
    const hid = body.hidden = !body.hidden;
    if (caret) caret.textContent = hid ? "展开" : "收起";
  };
}

/* ---- 发送 ---- */
async function send() {
  const input = $("#input");
  const text = input.value.trim();
  if (!text && !S.attachments.length) return;
  // ★ 执行中再发指令 → 进排队，而不是被丢弃。
  //   旧写法这里直接 `if (S.streaming) return;`，用户敲了回车却什么都没发生。
  if (S.streaming) {
    S.queue.push({
      id: "q" + Date.now().toString(36) + Math.random().toString(36).slice(2, 6),
      text,
      atts: (S.attachments || []).slice(),
      created_at: Date.now() / 1000,
    });
    S.attachments = [];
    renderAtts();
    input.value = "";
    input.style.height = "auto";
    renderQueue();
    syncQueue();   // ★ 先落盘再发：排队指令不能因为一次刷新就消失
    toast(`已加入排队（第 ${S.queue.length} 条），本轮结束后自动发送`, "");
    return;
  }
  input.value = "";
  input.style.height = "auto";
  if (S.sessionId) DRAFTS.delete(S.sessionId);   // 已发出，草稿位清掉
  const welcome = msgBox().querySelector(".welcome-empty");
  if (welcome) welcome.remove();
  addMessage("user", esc(text).replace(/\n/g, "<br>") + attHtml(), { raw: true });
  S.attachments = [];
  renderAtts();
  S.streaming = true;
  S._cancelled = false;   // 新一轮开始：清掉上一轮的「用户已停止」标记
  S._phase = "busy";      // 状态行：运行中
  $("#send-btn").disabled = true;
  $("#stop-btn").style.display = "";
  setStatus("busy", "生成中…");
  syncImageThemeDim();   // 开始工作：图片主题转为模糊态
  const t0 = Date.now();
  let assistEl = null, reasoningEl = null, reasoningText = "";
  S.turnStarted = t0;
  S.turnElapsed = 0;
  S.turnSpeed = 0;
  S.streamOutEst = 0;   // 本轮已产出内容估算出的 token 数（按流式字符数换算）
  S.speedQ = [];        // 计速滑动窗口样本：[{t, out}]
  S.lastEventAt = t0;   // ★ 看门狗基准：必须每轮重置，否则会继承上一轮的时间戳误报「无响应」
  const turnTicker = setInterval(() => {
    if (!S.streaming) return;
    S.turnElapsed = (Date.now() - t0) / 1000;
    // 每秒推一次样本；没有新内容时不产生新读数（见 pushSpeedSample）
    pushSpeedSample();
    // 思考时长：实时刷新当前思考块的秒数（思考过程 3.2s 451字」里的那个 3.2s）
    try {
      const rb = streamContext.reasoningBox;
      // ★ 只在「这段思考尚未结束」时刷新。rtStopped 由 done 事件（一段输出结束）
      //   与 text 事件（已开始输出正文）置位 —— 否则正文都在打字了、思考时长还在
      //   往上跑，用户会觉得计时是错的（实测反馈）。
      if (rb && rb.isConnected && streamContext.rt0 && !streamContext.rtStopped) {
        const dEl = rb.querySelector(".rdur");
        if (dEl) dEl.textContent = fmtDur((Date.now() - streamContext.rt0) / 1000);
      }
    } catch (e) {}
    // ★ 看门狗：区分「仍在跑」和「真卡死」，别让界面骗人。
    //   实测过「看起来一直没做完」：上游思考很久、一个事件都不发，
    //   界面却仍写着「生成中」，无法判断到底是在跑还是断了。
    const silent = (Date.now() - (S.lastEventAt || t0)) / 1000;
    if (silent > 45 && silent <= 300) {
      setStatus("busy", `仍在运行…（已 ${Math.round(silent)} 秒没有新输出，可继续等待或点停止）`);
    } else if (silent > 300) {
      setStatus("busy", `上游长时间无响应（已 ${Math.round(silent)} 秒）`);
    }
    renderStatusBar();
  }, 1000);
  // ★ 流式渲染改为「累积原文 + rAF 节流」：
  //   旧实现每帧 innerText 回读（把 Markdown 源读成渲染后文本再 re-parse），
  //   代码块、表格、粗体在流式中途全部错乱。现在只在内存里拼原文，渲染层
  //   每 50ms 最多重绘一次，最终 result 再用完整文本精确重绘一遍。
  let streamBuf = "";
  let rafPending = false;
  const paintStream = () => {
    if (rafPending) return;
    rafPending = true;
    requestAnimationFrame(() => {
      rafPending = false;
      if (assistEl) { assistEl.innerHTML = md(streamBuf); scrollDown(); }
    });
  };
  // ★ 立即出现「正在回复」占位气泡：用户一发消息就能看到 AI 侧在动，而不是空白等待。
  let pendingEl = addMessage("assistant", "", { meta: "正在回复…", pending: true });
  const toolTimes = {};
  // 事件共享同一对象，避免结束事件丢失思考框引用。
  const streamContext = {
    get assist() { return assistEl; }, set assist(v) { assistEl = v; },
    get pending() { return pendingEl; }, set pending(v) { pendingEl = v; },
    get reasoning() { return reasoningEl; }, set reasoning(v) { reasoningEl = v; },
    get rtext() { return reasoningText; }, set rtext(v) { reasoningText = v; },
    get buf() { return streamBuf; }, set buf(v) { streamBuf = v; },
    finish: null, paint: paintStream, reasoningBox: null, toolTimes, t0,
    turnFiles: [],   // 本回合工具产出的真实文件路径（收尾时提示「文件已生成在 X」）
  };
  // ★ 这条流「声明归属」的会话：本轮请求发出的那个会话 id。
  //   必须在发请求时冻结下来，之后**不随 S.sessionId 变化** —— 实测
  //   「老对话跑任务时打开新对话，老对话的输出与实时思考串进新对话」，
  //   根因就是事件处理只看当前 DOM、不看归属。冻结后即可逐条比对丢弃。
  const streamOwnerSid = S.sessionId || "";
  let streamDone = false;   // 是否收到完整的 result（用于判断是否需要自动重试）
  // ★ 停止要「立刻」断开连接，不能只发一个 API 请求然后干等。
  //   旧写法点停止只调 /api/chat/stop + toast，前端这条 fetch 仍在读流，
  //   后端也要等这一轮流式结束才收手 —— 表现就是「点了停止没反应」。
  const ac = new AbortController();
  S.abort = ac;
  const runStream = async () => {
    const res = await fetch("/api/chat", {
      method: "POST",
      signal: ac.signal,
      headers: Object.assign({ "Content-Type": "application/json" }, TOKEN ? { "X-Fengcode-Token": TOKEN } : {}),
      body: JSON.stringify({
        message: text,
        session_id: S.sessionId,
        model: S.model || undefined,
        // 工作模式："" = 不指定（由 AI 自主判断）
        mode: CUR_MODE || "",
        stream: true,
        // ★ 附件（含图片真实路径 + 原图 base64）随请求发给后端 → 进 Message.attachments
        //   → openai/anthropic/gemini 客户端转成多模态 content 数组发给模型。
        //   旧写法这里完全没有 attachments 字段，用户选的文件永远到不了模型。
        attachments: (S.attachments || []).map((a) => ({
          kind: a.kind || "file",
          name: a.name,
          path: a.path || "",
          mime: a.mime || "",
          data: a.dataUrl || "",
        })),
      }),
    });
    if (!res.ok) {
      const t = await res.text();
      let msg = t.slice(0, 300);
      try { msg = JSON.parse(t).error || msg; } catch (e) {}
      throw new Error(msg);
    }
    const reader = res.body.getReader();
    const dec = new TextDecoder();
    let buf = "";
    const processPart = (part) => {
      if (!part.trim() || part.startsWith(":")) return;
      const line = part.replace(/^data:\s?/, "");
      if (!line.trim()) return;
      try {
        const ev = JSON.parse(line);
        // ★ 会话归属校验（实测「老对话跑任务时打开新对话，老对话的输出与
        //   实时思考串进新对话」）。
        //   根因：切会话时这条流并没有被中止，而事件处理只看 DOM、不看归属 ——
        //   于是老会话的事件继续往**新会话的界面**里写。
        //   这里逐条比对事件里的 session_id：不是本流要写的那个会话就丢弃。
        //   后端每个事件都带 session_id（见 server/app.py 的 _sse 封装），
        //   缺失时才放行（避免个别事件没带就被误丢）。
        const evSid = ev.session_id || (ev.data && ev.data.session_id) || "";
        if (evSid && streamOwnerSid && evSid !== streamOwnerSid) return;
        if (ev.type === "result") streamDone = true;
        S.lastEventAt = Date.now();   // 看门狗用：最近一次收到后端事件的时刻
        handleEvent(ev, streamContext);
      } catch (e) { /* 忽略不完整事件，交给断流收尾 */ }
    };
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      const parts = buf.split("\n\n");
      buf = parts.pop();
      for (const part of parts) processPart(part);
    }
    // 最后一个事件可能没有以空行结束，不能静默丢掉 result/error。
    buf += dec.decode();
    if (buf.trim()) processPart(buf);
  };
  try {
    // ★ 断线自动重试。
    //   中转/网关会掐掉长连接，报「网络错误：terminated」；此时多半**没**收到 result，
    //   自动重连一次通常就能跑完，不必让用户手点。收到过 result 就不再重试，
    //   避免把已经完成的回合又跑一遍。
    let lastErr = null;
    for (let attempt = 0; attempt < 2; attempt++) {
      try {
        await runStream();
        lastErr = null;
        break;
      } catch (e) {
        lastErr = e;
        // 用户主动停止 / 已收到完整结果：不再重试
        if (streamDone || S._cancelled) { lastErr = null; break; }
        if (attempt === 0) {
          setStatus("busy", "连接中断，正在自动重试…");
          addMessage("system", '<span class="tag warn">连接中断</span> 正在自动重试…', { raw: true });
          await new Promise((r) => setTimeout(r, 1200));
          assistEl = null;      // 重试会重发完整回答，丢掉半截气泡避免重复渲染
          reasoningEl = null;
          streamBuf = "";
          reasoningText = "";
        }
      }
    }
    if (lastErr) throw lastErr;
  } catch (e) {
    // 失败：先在「正在回复」占位气泡上标出失败，再补一条错误详情。
    if (pendingEl && pendingEl.isConnected) {
      const msgEl = pendingEl.closest(".msg");
      if (msgEl) {
        msgEl.classList.remove("pending");
        msgEl.classList.add("failed");
        const meta = $(".meta", msgEl);
        if (meta) meta.textContent = "回复失败";
        pendingEl.innerHTML = `<span class="tag err">失败</span>`;
      }
    }
    addMessage("system", `<span class="err-text">出错：${esc(e.message)}</span>`, { raw: true });
  } finally {
    clearInterval(turnTicker);
    S.turnElapsed = (Date.now() - t0) / 1000;
    pushSpeedSample();   // 回合结束：再推一次样本，让最终读数落定
    S.streaming = false;
    S.abort = null;              // 本轮结束，清掉中止句柄
    $("#send-btn").disabled = false;
    $("#stop-btn").style.display = "none";
    setStatus("ok", "就绪");
    if (assistEl && !assistEl.textContent.trim()) {
      assistEl.closest(".msg-wrap").remove();
    }
    // 占位气泡收尾：成功（或正常结束）就移除；已标失败则保留「失败」让用户看得见。
    if (pendingEl && pendingEl.isConnected) {
      const msgEl = pendingEl.closest(".msg");
      if (!(msgEl && msgEl.classList.contains("failed"))) {
        pendingEl.closest(".msg-wrap").remove();
      }
    }
    scrollDown(true);
    refreshFooter();
    syncImageThemeDim();   // 回合结束：若已有对话内容则保持模糊态
      // ★ 收尾兜底：把最终交付文字从会话记录追回来，并把仍写着「生成中」的气泡复位。
      //   触发条件：**定稿文字确实没渲染到界面上** 才补。
      //   历史坑（实测「发你好输出两次、重启才变一次」）：
      //   旧条件写成 `!streamDone || !_finalRendered`，是「或」——result 已到、文字已渲染时，
      //   只要 _finalRendered 因任何原因没置位（自动重试重建气泡、tool.start 封存 assist、
      //   result 的 content 为空但界面已由 text 事件写出正文）就会再补一遍，
      //   于是同一段文字在同一屏出现两次，第二条还挂着「补充（连接中断后从会话记录恢复）」。
      //   现在改成：先看界面上有没有**本轮**的定稿文字，有就绝不补。
      if (!streamContext._finalRendered && streamContext._sawAssistantText !== true) {
        try { await recoverFinalText(streamContext); } catch (e) {}
      }
    finalizeTurnBubbles({ gotResult: streamDone, cancelled: S._cancelled });
    // ★ 明确告诉用户「文件生成在哪」：AI 写的文件默认落在工作区
    //   （fengcode-data\workspace），不是桌面。旧界面只显示 `~/xxx`，
    //   容易以为在用户目录，去桌面白找一场。
    try { renderTurnFiles(streamContext.turnFiles); } catch (e) {}
    // 状态行显示「已完成」（空闲时仍是「空闲」）
    S._phase = "done";
    renderStatusBar();
    // ★ 本轮结束：队列里还有指令就自动发下一条（排队 / 立即强制发送都走这条路径）。
    //   延迟一点让本轮的状态与 DOM 收尾落定，避免两轮状态交叉。
    if ((S.queue || []).length) setTimeout(() => dispatchNextQueued(), 150);
  }
}
/* ---- 回合收尾的统一兜底（★ 实测「已完成但左边还写着生成中、且没有收尾文字」）----
   背景：AI 气泡的 meta 只在 handleEvent 的 result 分支里被改写。而 result 事件
   会因为①用户点停止（前端 abort，连接已断）②中转/代理掐掉长连接 —— 而**没有送达**。
   这时状态行（S._phase="done"）显示「已完成」，但气泡还停在「生成中」，看起来像没结束。 */

/** 把仍挂着「生成中」的流式气泡复位；已写好「N 步 · …」的结果信息不动。 */
function finalizeTurnBubbles(ctx) {
  ctx = ctx || {};
  msgBox().querySelectorAll(".msg.streaming").forEach((m) => {
    m.classList.remove("streaming");
    const meta = $(".meta", m);
    if (!meta) return;
    const cur = (meta.textContent || "").trim();
    // 只在「还是占位文案」时才改写，避免覆盖「3 步 · 12K tokens · 8s」
    if (cur && cur !== "生成中" && cur !== "正在回复…") return;
    if (ctx.gotResult) { meta.textContent = "已完成"; return; }
    if (m.classList.contains("failed")) { meta.textContent = "回复失败"; return; }
    if (ctx.cancelled) {
      // ★ 1-D：中断的轮次要被识别出来 → 默认保持展开、绝不自动折叠藏起来
      m.classList.add("interrupted");
      meta.textContent = "已中断";
      return;
    }
    const txt = ((m.querySelector(".content") || {}).textContent || "").trim();
    meta.textContent = txt ? "已完成" : "未收到返回";
  });
}

/** 取 DOM 中最后一条 AI 正文节点（用于判断「这段文字是否已经在界面上」）。 */
function lastAssistNode() {
  const all = msgBox().querySelectorAll(".msg.assistant .content");
  return all.length ? all[all.length - 1] : null;
}

/** 断流兜底：没收到 result 时，从服务端把本回合的最终交付文字追回来。
    ★ 后端 agent 跑完一定会把最终 assistant 消息写库（core/agent.py 收尾段）。
      前端缺 result 时界面就只有半截、甚至空白 —— 实测「他做完了却没有输出
      收尾文字，我还以为一直没做完」。这里按会话记录补上。
    ★ 去重（实测「发你好输出两次、重启才变一次」三道闸）：
      ①本轮只要出现过任何正文（c._sawAssistantText）→ 不补；
      ②本轮已补过（c._recovered）→ 不补；
      ③界面上已有这段文字（textAlreadyShown）→ 不补。 */
async function recoverFinalText(c) {
  if (!S.sessionId) return false;
  if (c && (c._recovered || c._sawAssistantText)) return false;
  try {
    const d = await api("/api/sessions/" + encodeURIComponent(S.sessionId) + "?limit=40");
    const msgs = d.messages || [];
    let last = "";
    for (let i = msgs.length - 1; i >= 0; i--) {
      if (msgs[i].role === "assistant" && (msgs[i].content || "").trim()) { last = msgs[i].content; break; }
    }
    if (!last.trim()) return false;
    if (textAlreadyShown(last)) return false;   // 界面已有这段文字 → 不补
    if (c) c._recovered = true;
    addMessage("assistant", md(last), { raw: true, meta: "补充（连接中断后从会话记录恢复）" });
    scrollDown(true);
    return true;
  } catch (e) {
    return false;
  }
}

/** 这段文字是否已经显示在界面上？
    ★ 判据（实测「同一段话出现两三遍」踩出来的，改了四轮才定位到真因）：
      真因是**拿库里的原始 Markdown 去比对 DOM 渲染后的纯文本**：
      `md()` 会把 `**粗体**`、`\\`code\\``、表格管道符、`#` 标题、`- ` 列表标记
      全部转换成标签或删掉，于是「原文前 12 字符」在 textContent 里根本不存在，
      带格式的回复**必然**被判成「未显示」→ 重复补写。
      正确做法：把待判文字也**先过一遍 md()**，再取其纯文本参与比对。
      另外 segment 过滤阈值从 8 降到 4，并用「片段命中数 ≥2 或覆盖过半」放宽短回复。 */
function textAlreadyShown(text) {
  const t = String(text || "").trim();
  if (!t) return false;
  const nodes = msgBox().querySelectorAll(".msg.assistant .content");
  const shown = Array.from(nodes).map((n) => (n.textContent || "").trim()).join("\n");
  if (!shown) return false;
  // ★ 关键：把 Markdown 原文渲染一遍再取纯文本，才能与 DOM 里的 textContent 同口径比对。
  let plain = t;
  try {
    const box = document.createElement("div");
    box.innerHTML = md(t);
    plain = (box.textContent || "").trim();
  } catch (e) { plain = t; }
  // 逐行切片（阈值 4 字符，短回复也能切出片段）
  const pieces = plain.split(/\n+/).map((s) => s.trim()).filter((s) => s.length >= 4);
  const probe = pieces.length ? pieces : [plain];
  const hit = probe.filter((p) => shown.includes(p.slice(0, 12))).length;
  // 命中过半即认为已显示；单片段时命中即算（避免短回复永远 0/1）
  if (probe.length === 1) return hit === 1;
  return hit / probe.length >= 0.5;
}

function attHtml() {
  // ★ 发出的用户消息里也要显示附件（旧写法恒返回 ""，发完图气泡里只剩文字，
  //   用户看不出自己到底带没带上）。这里用小缩略图 + 文件名。
  const list = S.attachments || [];
  if (!list.length) return "";
  const items = list.map((a) => (a.thumb
    ? `<span class="u-att u-att-img"><img src="${a.thumb}" alt=""><span class="u-att-n">${esc(a.name)}</span></span>`
    : `<span class="u-att"><span class="u-att-ic">${icon(a.kind === "image" ? "image" : "file", 12)}</span><span class="u-att-n">${esc(a.name)}</span></span>`
  )).join("");
  return `<div class="u-atts">${items}</div>`;
}
/* 给当前这段思考「停表」并把标题从「思考中…」改成「思考过程」。
   ★ 什么时候才能停表（实测「思考时间一直是 0 秒」后重新定的规则）：
     只有**这一轮思考真的结束**才停，即出现下面任一件事：
       · 开始输出正文（text）
       · 开始生成工具参数（tool_delta / tool.start）
       · 整轮收尾（result）
     而**单段 `done` 绝不能停表** —— `done` 是每段输出结束都发的事件，
     模型分多段吐思考时，第一段一结束就停表的话，秒数会永远停在 0.x 上。
   幂等：重复调用只做一次，避免每段都重设标题。 */
function markReasoningDone(c) {
  if (!c || !c.reasoningBox || c.rtStopped) return;
  const dEl = c.reasoningBox.querySelector(".rdur");
  if (dEl && c.rt0) dEl.textContent = fmtDur((Date.now() - c.rt0) / 1000);
  c.rtStopped = true;
  const det = c.reasoningBox.querySelector("details");
  if (det) det.classList.remove("reasoning-live");
  const ttl = c.reasoningBox.querySelector(".rtitle");
  if (ttl && ttl.textContent === "思考中…") {
    ttl.textContent = c.finish === "length" || c.finish === "max_tokens"
      ? "思考已达到输出上限" : "思考过程";
  }
}

function handleEvent(ev, c) {
  const d = ev.data || {};
  switch (ev.type) {
    case "text": {
      // ★ 已开始输出正文 → 这段思考结束，计时停表（不然正文都在打字了，
      //   上面的「思考 3.2s」还在往上跳 —— 实测反馈）。
      markReasoningDone(c);
      // ★ 记下「本轮已出现过助手正文」：收尾兜底据此判断要不要从会话记录补写。
      //   只要流式期间写过正文，就绝不补 —— 否则同一段文字会在同一屏出现两次
      //   （实测「发你好输出两次、重启才变一次」）。
      if ((d.text || "").trim()) c._sawAssistantText = true;
      if (!c.assist) {
        // 复用占位气泡（"正在回复…"），避免出现两条空 AI 气泡
        if (c.pending && c.pending.isConnected) {
          c.assist = c.pending;
          c.pending = null;
          const m = c.assist.closest(".msg");
          if (m) { m.classList.remove("pending"); m.classList.add("streaming"); }
          const meta = m && $(".meta", m);
          if (meta) meta.textContent = "生成中";
        } else {
          c.assist = addMessage("assistant", "", { meta: "生成中" });
        }
      }
      c.buf += d.text || "";
      // 计速用：把流式收到的字符数折算成"已产出 token"（中文约 2.5 字符/token）
      S.streamOutEst = (S.streamOutEst || 0) + (d.text || "").length / 2.5;
      c.paint();
      break;
    }
    case "reasoning": {
      // ★ 思考过程固定显示（显示思考过程」开关已从设置里移除）
      // 判断依据必须是「DOM 里是否还有这个框」，不能只看 c.reasoning 是否为 null。
      // 旧写法只在 !c.reasoning 时新建，而回合中工具调用会把 reasoningBox 挪走并把
      // 引用置空、却漏了 c.reasoning —— 于是后续思考继续写进那个已被挪走的旧框，
      // 一个回合就碎成十几个「思考过程」块（实测见 17 个）。
      if (!c.reasoning || !c.reasoning.isConnected) {
        c.rtext = d.text || "";   // 新一段思考：文本从头累积，不接上一段的尾巴
        c.rt0 = Date.now();       // 这一块思考的开始时间（用于实时显示思考时长）
        const wrap = document.createElement("div");
        wrap.className = "msg-wrap reasoning-wrap";
        // 流式期间默认折叠：思考是大段英文/长文时不再把正文挤到看不见
        wrap.innerHTML = `<details class="reasoning reasoning-live"><summary><span class="rtitle">思考中…</span><span class="rdur">0.0s</span><span class="rlive-preview"></span><span class="rhint rhint-toggle">点击展开</span></summary><div class="rc"></div></details>`;
        msgBox().appendChild(wrap);
        c.reasoning = wrap.querySelector(".rc");
        c.reasoningBox = wrap;
      } else {
        c.rtext += d.text || "";
      }
      c.reasoning.textContent = c.rtext;
      // 计速用：思考内容也算"产出"（否则思考阶段读数一直是 0）
      S.streamOutEst = (S.streamOutEst || 0) + (d.text || "").length / 2.5;
      const preview = c.reasoningBox && c.reasoningBox.querySelector(".rlive-preview");
      if (preview) preview.textContent = c.rtext.split(/\r?\n/).filter(Boolean).pop() || "";
      // ★ 跟随逻辑必须由「用户是否滚动过」决定，不能用「距底距离」即时判断。
      //   旧写法每次内容增长后算 scrollHeight - scrollTop - clientHeight < 48：
      //   用户明明停在底部，但新内容一加进来距底就超过 48px，于是被判成
      //   「已上滑」，再也不跟随 —— 正是反馈的「滑到最底还是不跟」。
      //   现在由 scroll 事件维护 rc._followTail（见下方全局委托），
      //   内容增长时只看这个标志。
      const det0 = c.reasoningBox && c.reasoningBox.querySelector("details");
      if (det0 && det0.open) {
        const rc = c.reasoning;
        if (rc._followTail === undefined) rc._followTail = true;
        if (rc._followTail) rc.scrollTop = rc.scrollHeight;
        updateJumpBottom();
      }
      // 外层对话区：用户未主动上滑时才跟随（上滑后由 jump-bottom 提供回到底部入口）
      scrollDown();
      break;
    }
    case "done": {
      c.finish = ev.finish_reason || d.finish_reason || "stop";
      // 一段输出（思考或正文）结束：推一次计速样本
      pushSpeedSample();
      // ★ 不再在这里给思考计时「定格」（实测「思考时间一直是 0 秒」）。
      //   为什么：`done` 是**每段输出结束**都会发的事件 —— 模型分多段吐思考时，
      //   第一段一结束就把计时停住，秒数永远停在 0.x 上，看起来像没在计时。
      //   现在只在**整轮思考真正结束**时定格，判据见 markReasoningDone()：
      //   要么开始出正文（text），要么开始调工具（tool.start/tool_delta），
      //   要么这一轮彻底收尾（result）。单段 done 只记一下时长，不停表。
      if (c.reasoningBox) {
        const dEl = c.reasoningBox.querySelector(".rdur");
        if (dEl && c.rt0) dEl.textContent = fmtDur((Date.now() - c.rt0) / 1000);
      }
      break;
    }
    // ★ 工具参数增量：模型在流式生成 write_file 的长参数（文件内容）时，
    //   后端持续发 tool_delta，但前端此前**没有这个 case**，直接丢弃 ——
    //   于是这十几秒界面毫无动静，直到 tool.start 才突然冒出卡片。
    //   实测：「思考完成后等了 10 几秒才出现 write_file 提示」。
    //   这里给一个就地占位，让用户看到「正在生成参数」。
    case "tool_delta": {
      // ★ 工具参数一开始生成，本段思考就已经结束了 —— 立刻停表。
      //   否则「思考完成」到 write_file 卡片弹出这段（十几秒）里，
      //   思考时长还在每秒往上跳，看起来像还在思考（实测）。
      markReasoningDone(c);
      const tname = (ev.meta && ev.meta.name) || d.name || "工具";
      let ph = $("#tooldelta");
      if (!ph) {
        ph = document.createElement("div");
        ph.className = "msg-wrap";
        ph.id = "tooldelta";
        ph.innerHTML = `<div class="tool run" style="pointer-events:none">
          <div class="td-head"><span class="loading"></span>
          <span class="tname"></span>
          <span class="info">正在生成参数…</span>
          <span class="rhint td-len"></span></div></div>`;
        msgBox().appendChild(ph);
        c._deltaPh = ph;
      }
      const nm = $(".tname", ph);
      if (nm) nm.textContent = tname;
      c._deltaLen = (c._deltaLen || 0) + ((d.text || "").length);
      const ln = $(".td-len", ph);
      if (ln) ln.textContent = c._deltaLen > 200 ? `已 ${fmtNum(c._deltaLen)} 字符` : "";
      scrollDown();
      break;
    }
    case "tool.start": {
      // 参数生成结束 → 撤掉占位，换成真正的工具卡片
      if (c._deltaPh && c._deltaPh.isConnected) { c._deltaPh.remove(); c._deltaPh = null; }
      c._deltaLen = 0;
      c.toolTimes[d.name + d.id] = Date.now();
      // ★ 就地插入：工具卡片要落在「它被调用的那个位置」，而不是全部堆在最后。
      //   做法是先封存当前文字气泡（c.assist=null、c.buf 清空），再插入工具卡片；
      //   之后模型继续输出时，text 事件会发现 assist 为空而新开一个气泡，
      //   渲染顺序自然成为「文字 → 工具卡片 → 后续文字」。
      //
      //   ★ 这段被封存的文字要标记成「过程说明」（data-tool-preamble）：
      //   它只是「我准备调用工具」的前言，真正的交付内容是工具链**之后**那一轮正文。
      //   旧实现不标记，收尾兜底与重开会话时它会被当成交付内容，导致
      //   ①同一轮界面上出现两段（前言 + 结论）；②重启后只剩库里的结论那段
      //   ——正是实测「发你好输出两次、重启才变一次」的机理。
      if (c.assist || (c.buf && c.buf.trim())) {
        c.finishTextSegment = true;
        try {
          const wrap = c.assist && c.assist.closest(".msg-wrap");
          if (wrap) wrap.dataset.toolPreamble = "1";
        } catch (e) {}
        c.assist = null;
        c.buf = "";
      }
      // ★ 思考段也要一并封存：否则下一次 reasoning 事件会继续往这一段的老框里追加，
      //   一个回合的思考就全糊进同一个折叠块、且位置停在最早发生的地方。
      //   封存后 reasoning 事件会新开一块，形成「思考→工具→思考」的时间线。
      if (c.reasoningBox) {
        const det = c.reasoningBox.querySelector("details");
        if (det) det.classList.remove("reasoning-live");
        const ttl = c.reasoningBox.querySelector(".rtitle");
        if (ttl && ttl.textContent === "思考中…") ttl.textContent = "思考过程";
      }
      c.reasoning = null;
      c.reasoningBox = null;
      c.rtext = "";
      const node = renderToolCard({ name: d.name, arguments: d.arguments, running: true });
      S.toolNodes.set(d.name, node);
      break;
    }
    case "tool.end": {
      const started = c.toolTimes[d.name];
      updateToolCard(d.name, {
        ok: d.ok, duration: d.duration || (started ? (Date.now() - started) / 1000 : 0),
        preview: d.preview, display: d.display, files: d.files,
      }, true);
      // 记下本回合产出的文件（收尾时统一提示「文件已生成在 X」）
      if (d.ok !== false && Array.isArray(d.files)) {
        c.turnFiles = c.turnFiles || [];
        for (const f of d.files) if (f && !c.turnFiles.includes(f)) c.turnFiles.push(f);
      }
      // ★ 写文件类工具执行后刷新右侧文件树：否则删/改完文件，列表还是旧的，
      //   必须手动点「刷新」才更新（实测）。
      //   只在文件树**当前可见**时刷新，避免白拉接口。
      try {
        if (IP_TAB === "files" && $("#infopanel") && $("#infopanel").classList.contains("open")) {
          loadFileTree();
        }
      } catch (e) {}
      break;
    }
    case "approval.request": {
      showApproval(ev.data);
      break;
    }
    case "approval.done": {
      const el = $("#ap-" + d.id);
      if (el) el.remove();
      if (!d.allowed) toast("已拒绝该操作", "");
      break;
    }
    // ★ ask_user 工具：AI 主动提问 + 2~4 个候选，用户点一下即答。
    //   旧版本后端发了 ask.user 事件，但前端**没有对应 case**，
    //   界面一片安静 —— 实测「fengcode 好像没有 ask 功能」。
    case "ask.user": {
      showAsk(d);
      break;
    }
    case "ask.done": {
      const el = $("#ask-" + d.id);
      if (el) el.remove();
      break;
    }
    case "subagent.start": {
      addMessage("system", `<span class="tag accent">子智能体</span> ${esc(d.name || d.type)} 开始工作：${esc((d.task || "").slice(0, 160))}`, { raw: true });
      break;
    }
    case "subagent.end": {
      const flag = d.ok ? "完成" : `失败（${d.error || ""}）`;
      addMessage("system", `<span class="tag ${d.ok ? "ok" : "err"}">子智能体</span> ${esc(d.name || "")} ${flag}，用时 ${fmtDur(d.duration)}`, { raw: true });
      break;
    }
    // ★ 待办更新：AI 用 todo 工具排计划 → 刷新右下角待办面板。
    //   旧版本 SSE 里连这个 case 都没有，清单永远不显示。
    case "task.update":
    case "goal.update": {
      try { refreshTodos(); } catch (e) {}
      break;
    }
    case "usage": {
      // ★ 会话归属校验（与 processPart 同一道理）：老会话的用量不得覆盖新会话读数。
      const evSid = ev.session_id || d.session_id || "";
      if (evSid && streamOwnerSid && evSid !== streamOwnerSid) break;
      // 若用户已切走（当前会话 ≠ 本流归属），也不要把读数写进新会话的界面。
      if (streamOwnerSid && S.sessionId && S.sessionId !== streamOwnerSid) break;
      const u = d.usage || {};
      // last_usage = 最后一次上游调用的用量（上下文占用 / 命中率用它）；
      // usage = 本回合累加（费用、「本次 tokens」用它）。
      // ★ 为什么必须分开：一轮工具循环会把同一份上下文向上游重发十几次，
      //   累加 prompt_tokens 就是把它重复计数 —— 实测界面 250K、上游 20K。
      const lu = d.last_usage || u;
      S.turnUsage = {
        total_tokens: u.total_tokens || 0,
        acc_prompt_tokens: u.prompt_tokens || 0,
        acc_cached_tokens: u.cached_tokens || 0,
        prompt_tokens: lu.prompt_tokens || 0,
        cached_tokens: lu.cached_tokens || 0,
        completion_tokens: u.completion_tokens || 0,
        reasoning_tokens: u.reasoning_tokens || 0,
        cost: d.cost || 0,
        currency: d.currency || "",
      };
      $("#usage-hint").textContent =
        `${fmtNum(S.turnUsage.total_tokens)} tokens · ${fmtCost(S.turnUsage.cost || 0, S.turnUsage.currency)}`;
      renderInfoPanel();
      renderStatusBar();
      break;
    }
    case "error": {
      // 出错：把「正在回复…」占位气泡标成失败，再补一条错误详情
      if (c.pending && c.pending.isConnected) {
        const m = c.pending.closest(".msg");
        if (m) {
          m.classList.remove("pending");
          m.classList.add("failed");
          const meta = $(".meta", m);
          if (meta) meta.textContent = "回复失败";
        }
        c.pending.innerHTML = `<span class="tag err">失败</span>`;
        c.pending = null;
      }
      addMessage("system", `<span class="err-text">错误：${esc(d.error || "未知错误")}</span>`, { raw: true });
      break;
    }
    case "result": {
      // 回合结束：把「思考中…」的折叠块收尾成「思考过程」，并搬进 AI 消息内部
      if (c.reasoningBox && c.reasoningBox.isConnected && c.rtext) {
        const det = c.reasoningBox.querySelector("details");
        if (det) {
          const sum = det.querySelector("summary");
          if (sum) {
            const dur = c.rt0 ? fmtDur((Date.now() - c.rt0) / 1000) : "";
            sum.innerHTML = `<span class="rtitle">思考过程</span>`
              + `<span class="rdur">${dur}</span>`
              + `<span class="rhint">${fmtNum(c.rtext.length)} 字</span>`
              + `<span class="rhint rhint-toggle">点击展开</span>`;
          }
          det.classList.remove("reasoning-live");
        }
        // 挪到 assistant 消息前面（同一条时间线里），而不是独立占一屏宽度
        const target = c.assist ? c.assist.closest(".msg-wrap") : null;
        if (target && target.parentNode) {
          target.parentNode.insertBefore(c.reasoningBox, target);
        }
      }
      // 思考块引用的对象在 send() 局部，回合结束顺手置空防复用。
      // ★ c.reasoning 必须一起置空：只清 reasoningBox 会让下一段思考误判为
      //   「框还在」，继续写进已被挪走的旧节点，导致一回合碎成多个思考块。
      c.reasoningBox = null;
      c.reasoning = null;
      c.rtext = "";
      // ★ 最终交付文字必须**无条件**渲染出来。
      //   旧写法只在 c.assist 非空时回刷，而工具调用（tool.start）会把 c.assist
      //   置空并封存文字气泡 —— 如果模型最后一段是工具调用、之后不再产生 text 事件，
      //   交付文字就没有宿主可写，界面上直接消失（重启后从库里读才能看到）。
      //   实测反馈的正是这个现象。
      //
      //   ★ 顺位（实测「最终文字跑到最上面 / 掉到工具卡后面，重启才回下面」）：
      //   旧写法直接 addMessage(...) —— 那是无条件 append 到对话**最末尾**，
      //   于是定稿文字被排到本轮所有工具卡之后；而重开会话时 renderStored 按库序
      //   渲染，位置又变回正文该在的地方，表现为「重启才回下面」。
      //   这里改为：插入到**本轮最后一条消息之后**，保证它落回本轮时间线的收尾位置。
      //
      //   ★ 重复（实测「同一段话出现两三遍」）：本条文字在流式期间已由 text 事件
      //   逐段写出，若这里再整段重绘、finally 再补一次，就会同屏三份。
      //   故先判断「界面是否已显示过这段文字」：已显示则只做收尾（改 meta、清 streaming），
      //   不再重绘正文。
      const finalText = (d.data && d.data.content) ? String(d.data.content) : "";
      // ★ 后端标记：这段正文在流式阶段已经逐字发给前端并渲染完成（agent.py 收尾段
      //   的 content_streamed）。此时**绝不能**整段重绘 —— 否则同一段回答出现两遍，
      //   还常被 finally 的兜底再补一遍（实测「发你好输出两次、重启才变一次」）。
      //   但若界面上确实没有（例如自动重试重建了气泡），仍要补画，故两者取「且」。
      const streamedOk = !!(d.data && d.data.content_streamed) && textAlreadyShown(finalText);
      if (finalText && !streamedOk) {
        const already = textAlreadyShown(finalText);
        if (!already && (!c.assist || !c.assist.isConnected)) {
          const prev = lastTurnWrap(c);
          const host = addMessage("assistant", "", { meta: "完成" });
          const wrap = host.closest(".msg-wrap");
          if (prev && wrap && prev.parentNode === wrap.parentNode && prev.nextSibling !== wrap) {
            prev.parentNode.insertBefore(wrap, prev.nextSibling);
          }
          c.assist = host;
          host.closest(".msg").classList.remove("streaming", "pending");
        }
        if (!already && c.assist) {
          c.assist.innerHTML = md(finalText);
          // ★ 2-M/2-Q：内容已定稿（不是流式中途）→ 这时才画图 / 渲染公式。
          //   流式期间每 50ms 重绘一次，绝不能在那时跑 mermaid（没写完会报错、还会卡）。
          try { renderRich(c.assist); } catch (e) {}
        }
        c._finalRendered = true;   // 定稿文字已在界面（无论本次绘制还是此前流式写出）→ 不再补
      }
      if (c.assist) {
        const m = c.assist.closest(".msg");
        if (m) m.classList.remove("streaming", "pending");
      }
      // ★ 完成回执卡：把「这轮改了什么、验证没验证、还差什么」摆给用户看。
      //   放在结果分支里（result 一定带着本回合的完整执行记录）。
      try { if (d.data) renderReceipt(d.data); } catch (e) {}
      // 成功：移除「正在回复…」占位气泡（若还没被内容接管）
      if (c.pending && c.pending.isConnected) {
        c.pending.closest(".msg-wrap").remove();
        c.pending = null;
      }
      // ★ 回合结束兜底刷新待办面板：task.update 事件可能丢失/晚到，
      //   不补这一次的话面板会停在上一步（实测「进度卡住不刷新」）。
      try { refreshTodosSoon(); } catch (e) {}
      // ★ 回合结束兜底刷新用量读数：usage 事件可能被会话归属校验丢弃、
      //   也可能在收尾时晚到。不补这一次，状态栏就会停在「上下文 0 / 命中 0%」，
      //   即使这轮明明已经消耗了 token。
      try {
        const du = (d.data && d.data.last_usage) || (d.data && d.data.usage) || null;
        if (du && (du.prompt_tokens || du.completion_tokens)) {
          S.turnUsage = Object.assign({}, S.turnUsage || {}, {
            prompt_tokens: du.prompt_tokens || 0,
            cached_tokens: du.cached_tokens || 0,
            completion_tokens: (d.data.usage || {}).completion_tokens || du.completion_tokens || 0,
            total_tokens: (d.data.usage || {}).total_tokens || du.total_tokens || 0,
            acc_prompt_tokens: (d.data.usage || {}).prompt_tokens || 0,
            acc_cached_tokens: (d.data.usage || {}).cached_tokens || 0,
            cost: (d.data && d.data.cost) || 0,
            currency: (d.data && d.data.currency) || "",
          });
          renderStatusBar();
          renderInfoPanel();
        }
      } catch (e) {}
      const meta = c.assist && c.assist.closest(".msg").querySelector(".meta");
      if (meta && d.data) {
        if (d.data.error) {
          // 后端在异常路径补发的 result（带 error）：标失败，别让它显示成正常完成。
          meta.textContent = "回复失败";
          const mm = c.assist && c.assist.closest(".msg");
          if (mm) mm.classList.add("failed");
        } else {
          meta.textContent = `${d.data.steps || 0} 步 · ${fmtNum((d.data.usage || {}).total_tokens || 0)} tokens · ${fmtDur(d.data.duration)}`;
          // ★ 2-O：让「自动续跑」可见 —— 这条回复是被截断后分几段接起来的
          if (d.data.continues > 0) {
            const mm = c.assist && c.assist.closest(".msg");
            if (mm && !$(".continues-tag", mm)) {
              const tag = document.createElement("span");
              tag.className = "tag warn continues-tag";
              tag.title = "字数太多，被输出上限截断后自动接着写完。可在设置里调大模型输出上限。";
              tag.textContent = `已自动续写 ${d.data.continues} 次`;
              const who = $(".who", mm) || mm;
              who.appendChild(tag);
            }
          }
        }
      }
      break;
    }
    case "status": {
      if (d.message) setStatus("busy", d.message);
      break;
    }
    case "notify": {
      toast(`${d.title || ""}：${d.message || ""}`, "");
      if (window.Notification && Notification.permission === "granted") {
        try { new Notification(d.title || "Fengcode", { body: d.message || "" }); } catch (e) {}
      }
      break;
    }
  }
}
/** ask_user：AI 主动提问，给出 2~4 个候选，用户点一下即答。
    ★ 这是用户明确要的、「问用户怎么操作然后给选项」。
      后端 AskTool 会 emit `ask.user` 并**阻塞等待**，此处回填 /api/ask 即可让
      工具拿到答案继续干活。 */
function showAsk(a) {
  const el = document.createElement("div");
  el.className = "ask";
  el.id = "ask-" + a.id;
  const multi = !!a.multi_select;
  const opts = (a.options || []).filter(Boolean);
  const optHtml = opts.map((o, i) => `
    <button class="ask-opt${i === 0 ? " rec" : ""}" data-opt="${esc(o)}" type="button">
      <span class="ask-key">${i + 1}</span>
      <span class="ask-label">${esc(o)}</span>
      ${i === 0 ? '<span class="ask-rec">推荐</span>' : ""}
    </button>`).join("");
  el.innerHTML = `
    <div class="h">${icon("info", 15)} 需要你的决定${multi ? "（可多选）" : ""}</div>
    <div class="ask-q">${esc(a.question || "")}</div>
    ${a.context ? `<div class="ask-ctx">${esc(a.context)}</div>` : ""}
    ${opts.length ? `<div class="ask-opts" data-multi="${multi ? "1" : ""}">${optHtml}</div>` : ""}
    <div class="ask-free">
      <input class="ask-input" id="askin-${a.id}" placeholder="${opts.length ? "以上都不合适？直接输入你的答复…" : "输入你的答复…"}">
      <button class="btn primary sm" data-ask-send type="button">提交</button>
    </div>`;
  $("#approvals").appendChild(el);

  const submit = async (answer) => {
    if (answer == null) return;
    try {
      await api("/api/ask", { method: "POST", body: { id: a.id, answer } });
      el.remove();
      addMessage("user", `<span class="ask-echo">${esc(String(answer))}</span>`, { raw: true });
    } catch (e) { toast("提交失败：" + e.message, "err"); }
  };

  // 选项：多选时点一下切换选中，再点「提交」；单选时点一下即答
  const chosen = new Set();
  $$(".ask-opt", el).forEach((b) => b.onclick = () => {
    if (!multi) { submit(b.dataset.opt); return; }
    const v = b.dataset.opt;
    if (chosen.has(v)) { chosen.delete(v); b.classList.remove("on"); }
    else { chosen.add(v); b.classList.add("on"); }
    const send = $("[data-ask-send]", el);
    if (send) send.disabled = chosen.size === 0;
  });
  const sendBtn = $("[data-ask-send]", el);
  if (sendBtn && multi) sendBtn.disabled = true;
  if (sendBtn) sendBtn.onclick = () => {
    if (multi) {
      if (chosen.size) submit(Array.from(chosen).join("、"));
      return;
    }
    const inp = $("#askin-" + a.id);
    const v = inp ? inp.value.trim() : "";
    if (v) submit(v);
    else toast("请先选择一个选项或输入答复", "");
  };
  const inp = $("#askin-" + a.id);
  if (inp) inp.addEventListener("keydown", (e) => {
    if (e.key === "Enter") { e.preventDefault(); sendBtn && sendBtn.click(); }
  });
  if (inp) inp.focus();
  scrollDown();
}

function showApproval(a) {
  const el = document.createElement("div");
  el.className = "approval";
  el.id = "ap-" + a.id;
  const risk = a.risk === "high" ? "高危" : "中等风险";
  el.innerHTML = `
    <div class="h">${icon("warning", 15)} 需要你确认（${esc(risk)}）</div>
    <div style="font-size:12.5px;color:var(--text-soft)">${esc(a.reason || "")}</div>
    <div style="font-size:11.5px;color:var(--text-dim);margin-top:4px">工具：<b>${esc(a.action)}</b></div>
    <div class="t">${esc(a.target || a.preview || "")}</div>
    <div class="a">
      <button class="btn primary sm" data-a="session">本会话允许</button>
      <button class="btn sm" data-a="once">仅本次允许</button>
      <button class="btn sm" data-a="always">始终允许</button>
      <button class="btn danger sm" data-a="deny">拒绝</button>
    </div>`;
  $("#approvals").appendChild(el);
  $$("[data-a]", el).forEach((b) => b.onclick = async () => {
    const mode = b.dataset.a;
    try {
      await api("/api/approval", {
        method: "POST",
        body: {
          id: a.id, allowed: mode !== "deny",
          remember: mode === "deny" ? "once" : mode,
          action: a.action, target: a.target, session_id: a.session_id || S.sessionId,
        },
      });
      el.remove();
    } catch (e) { toast("提交失败：" + e.message, "err"); }
  });
  try {
    if (window.Notification && Notification.permission === "default") Notification.requestPermission();
  } catch (e) {}
}
function setStatus(kind, text) {
  const dot = $("#sdot");
  if (dot) {
    dot.className = "status-dot " + (kind === "ok" ? "ok" : kind === "busy" ? "busy" : kind === "err" ? "err" : "");
  }
  setText("#stext", text || "");
}
async function refreshFooter() {
  try {
    const st = await api("/api/status");
    const parts = [`${st.providers_with_key}/${st.providers} 供应商`];
    if (st.mcp) parts.push(`MCP ${st.mcp.ready}/${st.mcp.total}`);
    if (st.plugins) parts.push(`插件 ${st.plugins.loaded}`);
    $("#sinfo").textContent = parts.join(" · ");
    setStatus("ok", st.model ? st.model.split("/").slice(-1)[0] : "未配置模型");
  } catch (e) {
    setStatus("err", "未连接");
  }
}
/* 换模型后的收尾：★ 必须重算上下文上限 —— 不同模型的窗口差别巨大
   （1M vs 128K），旧写法只改 S.model 不重算，右侧栏就一直显示**上一个模型**
   的窗口（实测反馈「切换模型后上下文还是之前那个」）。 */
function onModelPicked() {
  applyContextLimit(S.boot);
  // ★ 换模型时占用读数也要跟着刷新（实测「换模型后显示的是新模型的窗口，
  //   但占用数字还是旧的；再换回来又没效果」）。
  //   窗口上限由 applyContextLimit 负责；这里同步「本会话已用多少」——
  //   换模型不改变本会话的历史，但必须让读数按新模型的上限重新算百分比，
  //   而不是把上一个模型的读数原样留着。
  try {
    const used = Number((S.turnUsage && S.turnUsage.prompt_tokens) || 0);
    S.turnUsage = Object.assign({}, S.turnUsage || {}, { prompt_tokens: used });
  } catch (e) {}
  renderInfoPanel();
  renderStatusBar();
}
function syncModelSelect() {
  const sel = $("#model-select");
  const models = (S.boot && S.boot.models) || [];
  const usable = models.filter((m) => m.has_key);
  const list = usable.length ? usable : models;
  const cur = S.model || S.boot.default_model || "";
  if (!list.length) {
    sel.innerHTML = '<option value="">（未配置模型）</option>';
    sel.onchange = () => { S.model = sel.value; onModelPicked(); };
    return;
  }
  // ★ 按供应商分组（optgroup），下拉里能直接看出每个供应商各有哪些模型
  const groups = new Map();
  for (const m of list) {
    const g = m.provider_display || m.provider || "其他";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(m);
  }
  const html = [];
  for (const [g, arr] of groups) {
    html.push(`<optgroup label="${esc(g)}">`);
    for (const m of arr) {
      html.push(`<option value="${esc(m.ref)}"${m.ref === cur ? " selected" : ""}>` +
        `${esc(m.model)}${m.has_key ? "" : "（无密钥）"}</option>`);
    }
    html.push("</optgroup>");
  }
  sel.innerHTML = html.join("");
  sel.onchange = () => { S.model = sel.value; onModelPicked(); };
}
/** 取文件的真实绝对路径。
    ★ 桌面端必须走 preload 暴露的 webUtils.getPathForFile —— Electron 32 起
      File.path 已被移除，这是唯一可靠途径。网页版拿不到，返回空串。
      旧代码只存 f.name，于是界面上只能显示文件名、还要提示「路径请直接告诉我」。 */
function realPathOf(f) {
  try {
    if (window.fengcode && typeof window.fengcode.getPathForFile === "function") {
      const p = window.fengcode.getPathForFile(f);
      if (p) return p;
    }
  } catch (e) {}
  return (f && f.path) || "";
}

const IMG_EXT_RE = /\.(png|jpe?g|gif|webp|bmp|svg|ico)$/i;
const MAX_ATT_BYTES = 8 * 1024 * 1024;   // 单个附件上限 8MB（base64 后约 11MB，别把请求撑爆）

function fmtSize(n) {
  n = Number(n) || 0;
  if (n >= 1048576) return (n / 1048576).toFixed(1) + "MB";
  if (n >= 1024) return Math.round(n / 1024) + "KB";
  return n + "B";
}

/** 生成图片缩略图（dataURL）。缩到 160px 以内：预览够用，又不把内存吃光。 */
function makeThumb(f) {
  return new Promise((resolve) => {
    let url = "";
    try {
      url = URL.createObjectURL(f);
      const img = new Image();
      img.onload = () => {
        try {
          const max = 160;
          const w0 = img.naturalWidth || img.width || 1;
          const h0 = img.naturalHeight || img.height || 1;
          const sc = Math.min(1, max / Math.max(w0, h0));
          const cv = document.createElement("canvas");
          cv.width = Math.max(1, Math.round(w0 * sc));
          cv.height = Math.max(1, Math.round(h0 * sc));
          cv.getContext("2d").drawImage(img, 0, 0, cv.width, cv.height);
          resolve(cv.toDataURL("image/png"));
        } catch (e) { resolve(null); }
        finally { try { URL.revokeObjectURL(url); } catch (e) {} }
      };
      img.onerror = () => { try { URL.revokeObjectURL(url); } catch (e) {} resolve(null); };
      img.src = url;
    } catch (e) { resolve(null); }
  });
}

/** 读文件为 dataURL（发给模型用：与缩略图分开，模型要的是原图）。 */
function readDataUrl(f) {
  return new Promise((resolve) => {
    try {
      const rd = new FileReader();
      rd.onload = () => resolve(String(rd.result || ""));
      rd.onerror = () => resolve("");
      rd.readAsDataURL(f);
    } catch (e) { resolve(""); }
  });
}

/** 把引用路径追加到输入框末尾（另起一行，前面加 @）。这就是用户要的「路径直接给我」。 */
function insertRefs(paths) {
  const inp = $("#input");
  const list = (paths || []).filter(Boolean);
  if (!inp || !list.length) return;
  const refs = list.map((p) => "@" + p).join("\n");
  const cur = inp.value.replace(/\s*$/, "");
  inp.value = (cur ? cur + "\n" : "") + refs;
  inp.style.height = "auto";
  inp.style.height = Math.min(260, inp.scrollHeight) + "px";
  inp.focus();
}

/** 统一入口：拖拽与「+」选文件都走这里，拿到真实路径 + 缩略图 + 原图 dataURL。 */
async function addFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;
  const addedPaths = [];
  let added = 0, skipped = 0;
  for (const f of files) {
    const path = realPathOf(f);
    const dup = S.attachments.some((x) =>
      (path && x.path === path) || (!path && x.name === f.name && x.size === f.size));
    if (dup) { skipped++; continue; }
    if (f.size > MAX_ATT_BYTES) { toast(`${f.name} 超过 8MB，已跳过`, "err"); skipped++; continue; }
    const isImg = String(f.type || "").startsWith("image/") || IMG_EXT_RE.test(f.name);
    const att = {
      name: f.name, size: f.size, path,
      kind: isImg ? "image" : "file",
      mime: f.type || (isImg ? "image/png" : ""),
      thumb: null, dataUrl: null,
    };
    if (isImg) {
      att.thumb = await makeThumb(f);
      att.dataUrl = await readDataUrl(f);
      att.mime = f.type || "image/png";
    }
    S.attachments.push(att);
    if (path) addedPaths.push(path);
    added++;
  }
  renderAtts();
  if (addedPaths.length) insertRefs(addedPaths);
  if (added) toast(`已附加 ${added} 个文件${skipped ? `，跳过 ${skipped} 个` : ""}`, "");
  else if (skipped) toast(`已跳过 ${skipped} 个（重复或过大）`, "");
}

function renderAtts() {
  const box = $("#atts");
  if (!box) return;
  const list = S.attachments || [];
  box.hidden = !list.length;
  if (!list.length) { box.innerHTML = ""; return; }
  box.innerHTML = list.map((a, i) => {
    const isImg = a.kind === "image";
    const thumb = a.thumb
      ? `<span class="att-thumbwrap"><img class="att-thumb" src="${a.thumb}" alt="">`
        + `<button class="att-zoom" data-zoom="${i}" title="预览大图">${icon("eye", 11)}</button></span>`
      : `<span class="att-ic">${icon(isImg ? "image" : "file", 14)}</span>`;
    return `<span class="att${isImg ? " att-img" : ""}" data-att="${i}" `
      + `title="${esc(a.path || a.name)}\n点一下把路径插入输入框">`
      + thumb
      + `<span class="n">${esc(a.name)}</span>`
      + `<span class="att-sz">${fmtSize(a.size)}</span>`
      + `<button data-rm="${i}" class="att-x" title="移除">${icon("close", 12)}</button></span>`;
  }).join("");
  $$("[data-rm]", box).forEach((b) => b.onclick = (e) => {
    e.stopPropagation();
    S.attachments.splice(Number(b.dataset.rm), 1);
    renderAtts();
  });
  $$("[data-zoom]", box).forEach((b) => b.onclick = (e) => {
    e.stopPropagation();
    const a = S.attachments[Number(b.dataset.zoom)];
    if (a) openImagePreview(a);
  });
  $$("[data-att]", box).forEach((el) => el.onclick = () => {
    const a = S.attachments[Number(el.dataset.att)];
    if (a) insertRefs([a.path || a.name]);
  });
}

/** 图片预览：点缩略图的放大镜打开大图。
    ★ 用原图 dataURL 而不是缩略图（缩略图只有 160px，放大就糊）。
      同时给出真实路径 + 「插入路径」按钮 —— 用户要的就是「路径直接发给我」。 */
function openImagePreview(att) {
  const big = att.dataUrl || att.thumb || "";
  const path = att.path || "";
  const body = `
    <div class="img-preview">
      ${big ? `<img src="${big}" alt="${esc(att.name)}">` : `<div class="empty">（没有可预览的图片数据）</div>`}
    </div>
    <div class="img-meta">
      <div class="row-line"><span class="k">文件名</span><span class="v">${esc(att.name)}</span></div>
      <div class="row-line"><span class="k">大小</span><span class="v">${fmtSize(att.size)}</span></div>
      <div class="row-line"><span class="k">类型</span><span class="v">${esc(att.mime || "—")}</span></div>
      <div class="row-line"><span class="k">路径</span><span class="v mono" id="img-preview-path">${esc(path || "（未取到，网页版无法读取本地路径）")}</span></div>
    </div>`;
  const foot = (path ? `<button class="btn primary" data-copy-path>复制路径</button>` : "")
    + `<button class="btn" data-insert-path${path ? "" : " disabled"}>插入到输入框</button>`
    + `<button class="btn ghost" data-close>关闭</button>`;
  const box = modal("图片预览", body, foot);
  const cp = $("[data-copy-path]", box.parentNode) || $$("[data-copy-path]")[0];
  if (cp) cp.onclick = () => {
    try {
      if (navigator.clipboard) navigator.clipboard.writeText(path);
      else if (window.fengcode && window.fengcode.copy) window.fengcode.copy(path);
      toast("路径已复制", "ok");
    } catch (e) { toast("复制失败：" + e.message, "err"); }
  };
  $$("[data-insert-path]").forEach((b) => b.onclick = () => {
    if (b.disabled) return;
    insertRefs([path]);
    closeModal();
    toast("已插入路径", "ok");
  });
}

/* ---- 排队区：执行中发的指令列在这里，可取回 / 删除 / 插队立即发 ----
   ★ 的三件事都在这里：
     · 取回：点「取回」把文本放回输入框（改完再发）；
     · 修改：点文本本身也是取回；
     · 立即强制发送：中断当前轮，把这条提到队首发出去。 */
function renderQueue() {
  const box = $("#composer-queue");
  if (!box) return;
  const q = S.queue || [];
  box.hidden = !q.length;
  if (!q.length) { box.innerHTML = ""; return; }
  box.innerHTML = q.map((it, i) => `
    <div class="q-item" data-q="${esc(it.id)}" draggable="true" title="可上下拖动调整顺序">
      <span class="q-drag" title="拖动调整顺序">⋮⋮</span>
      <span class="q-idx">${i + 1}</span>
      <span class="q-text" title="点一下取回编辑">${esc(it.text || "（附件）")}</span>
      <button class="q-btn" data-q-now="${esc(it.id)}" title="中断当前回答，立刻发这条">立即发送</button>
      <button class="q-btn" data-q-back="${esc(it.id)}" title="取回输入框，改完再发">取回</button>
      <button class="q-btn q-del" data-q-del="${esc(it.id)}" title="从队列删除">${icon("close", 12)}</button>
    </div>`).join("");
  $$("[data-q-del]", box).forEach((b) => b.onclick = () => {
    S.queue = S.queue.filter((x) => x.id !== b.dataset.qDel);
    renderQueue();
  });
  $$("[data-q-back]", box).forEach((b) => b.onclick = () => takeBackQueued(b.dataset.qBack));
  $$(".q-text", box).forEach((el) => el.onclick = () => takeBackQueued(el.closest(".q-item").dataset.q));
  $$("[data-q-now]", box).forEach((b) => b.onclick = () => sendQueuedNow(b.dataset.qNow));
  bindQueueDrag(box);
}

/** ★ 2-L：队列拖拽调序。
    为什么需要：排队几条时顺序往往要调整（这条先发」），
    以前只能删掉重打 —— 上下拖一下就好的事。
    用 HTML5 drag 事件，拖到哪一条上就把自己插到那条的位置。 */
let Q_DRAG_ID = "";
function bindQueueDrag(box) {
  const items = $$(".q-item", box);
  items.forEach((el) => {
    el.ondragstart = (e) => {
      Q_DRAG_ID = el.dataset.q;
      el.classList.add("dragging");
      try { e.dataTransfer.setData("text/plain", Q_DRAG_ID); e.dataTransfer.effectAllowed = "move"; } catch (err) {}
    };
    el.ondragend = () => { el.classList.remove("dragging"); Q_DRAG_ID = ""; };
    el.ondragover = (e) => { e.preventDefault(); el.classList.add("drop-target"); };
    el.ondragleave = () => el.classList.remove("drop-target");
    el.ondrop = (e) => {
      e.preventDefault();
      el.classList.remove("drop-target");
      const from = Q_DRAG_ID || (() => { try { return e.dataTransfer.getData("text/plain"); } catch (err) { return ""; } })();
      const to = el.dataset.q;
      if (!from || from === to) return;
      const q = S.queue || [];
      const fi = q.findIndex((x) => x.id === from);
      const ti = q.findIndex((x) => x.id === to);
      if (fi < 0 || ti < 0) return;
      const [moved] = q.splice(fi, 1);
      q.splice(ti, 0, moved);       // 插到目标那一项的位置
      S.queue = q;
      renderQueue();
      toast("已调整顺序", "");
    };
  });
}

/** 取回队列项到输入框（取出即从队列移除）。 */
function takeBackQueued(id) {
  const i = (S.queue || []).findIndex((x) => x.id === id);
  if (i < 0) return;
  const [it] = S.queue.splice(i, 1);
  const inp = $("#input");
  if (inp) {
    inp.value = it.text || "";
    if (it.atts && it.atts.length) { S.attachments = it.atts.slice(); renderAtts(); }
    inp.style.height = "auto";
    inp.style.height = Math.min(260, inp.scrollHeight) + "px";
    inp.focus();
  }
  renderQueue();
}

/** 立即强制发送：把这条提到队首 → 中断当前轮 → 当前轮收尾后立刻发它。 */
function sendQueuedNow(id) {
  const i = (S.queue || []).findIndex((x) => x.id === id);
  if (i < 0) return;
  const [it] = S.queue.splice(i, 1);
  S.queue.unshift(it);
  renderQueue();
  if (S.streaming) {
    S._cancelled = true;
    try { if (S.abort) S.abort.abort(); } catch (e) {}
    try { api("/api/chat/stop", { method: "POST", body: { session_id: S.sessionId } }); } catch (e) {}
    toast("已中断当前回答，正在发送这条…", "");
  } else {
    dispatchNextQueued();
  }
}

/** 把队首真正发出去：写进输入框 → 调 send()（复用完整发送流程）。 */
function dispatchNextQueued() {
  if (S.streaming) return;
  if (!(S.queue || []).length) return;
  const it = S.queue.shift();
  syncQueue();                  // ★ 出队也落盘：否则刷新后已发出去的会「复活」
  const inp = $("#input");
  if (!inp) return;
  inp.value = it.text || "";
  if (it.atts && it.atts.length) { S.attachments = it.atts.slice(); renderAtts(); }
  renderQueue();
  send();
}

/* ★ 待发队列持久化（实测「排 3 条指令，一刷新全没了」）。
   做法：队列任何变化都同步落盘（增/删/取回/调序/出队），
   启动或切换会话时按会话读回来。存储在后端 KVStore，键按会话区分。 */
async function syncQueue() {
  if (!S.sessionId) return;
  try {
    await api("/api/queue", {
      method: "POST",
      body: {
        action: "set",
        items: (S.queue || []).map((x) => ({
          id: x.id, text: x.text || "", created_at: x.created_at || 0,
        })),
      },
    });
  } catch (e) { /* 落盘失败不阻断发送流程 */ }
}

/** 从服务端读回该会话的待发队列（刷新/重开页面后恢复）。 */
async function loadQueue() {
  if (!S.sessionId) return;
  try {
    const d = await api("/api/queue?session_id=" + encodeURIComponent(S.sessionId));
    const items = (d && d.items) || [];
    // 只在本地队列为空时恢复，避免覆盖用户刚敲进去的新条目
    if (!(S.queue || []).length && items.length) {
      S.queue = items.map((x) => ({ id: x.id, text: x.text || "", atts: [] }));
      renderQueue();
      toast(`已恢复 ${items.length} 条排队指令`, "");
    }
  } catch (e) { /* 读不到就当空队 */ }
}
/* 输入框行为 */
const inputEl = $("#input");
inputEl.addEventListener("input", () => {
  inputEl.style.height = "auto";
  inputEl.style.height = Math.min(260, inputEl.scrollHeight) + "px";
});
inputEl.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); send(); }
});
$("#send-btn").onclick = send;
$("#stop-btn").onclick = async () => {
  S._cancelled = true;   // 用户主动停止：断线重试逻辑据此放弃重发
  // ★ 先**就地**断开前端的流式连接，界面立刻停下来；再通知后端取消这一轮。
  try { if (S.abort) S.abort.abort(); } catch (e) {}
  setStatus("ok", "已停止");
  try { await api("/api/chat/stop", { method: "POST", body: { session_id: S.sessionId } }); }
  catch (e) { toast(e.message, "err"); }
};
/* 权限等级改由输入框上方的 #perm-chip 控制（只看不改 / 工作区可改 / 完全权限） */

/* 拖拽文件 */
const ibox = $("#input-box");
["dragenter", "dragover"].forEach((t) => ibox.addEventListener(t, (e) => {
  e.preventDefault(); ibox.classList.add("drag");
}));
["dragleave", "drop"].forEach((t) => ibox.addEventListener(t, (e) => {
  e.preventDefault(); if (t === "drop" || e.target === ibox) ibox.classList.remove("drag");
}));
ibox.addEventListener("drop", (e) => {
  const files = Array.from(e.dataTransfer.files || []);
  // ★ 拖入的文件走统一入口：取真实路径 + 图片缩略图 + 原图，并把 @路径 插进输入框。
  //   旧写法只 push {name, size}，界面上只有文件名，用户得自己去找路径。
  if (files.length) addFiles(files);
  const txt = e.dataTransfer.getData("text");
  if (txt) { inputEl.value += (inputEl.value ? "\n" : "") + txt; inputEl.focus(); }
});

/* ==========================================================================
   会话记录页
   ========================================================================== */
PAGES.sessions = async () => {
  const el = $("#page-sessions");
  const d = await api("/api/sessions?limit=200");
  if (!d.sessions.length) { el.innerHTML = `<div class="empty"><div class="big">${icon("folder", 34)}</div>还没有会话</div>`; return; }
  el.innerHTML = `<div class="card"><h3>共 ${d.sessions.length} 个会话<span class="hint">点击打开，可置顶/归档/删除</span></h3>
    <table><thead><tr><th>标题</th><th>模型</th><th>tokens</th><th>费用</th><th>更新时间</th><th></th></tr></thead>
    <tbody>${d.sessions.map((s) => `<tr>
      <td><a href="#" data-open="${esc(s.id)}" style="color:var(--accent);text-decoration:none">
        ${s.pinned ? icon("star", 13) + " " : ""}${esc(s.title || "(无标题)")}</a></td>
      <td class="mono">${esc((s.model || "—").split("/").slice(-1)[0])}</td>
      <td class="mono">${fmtNum((s.input_tokens || 0) + (s.output_tokens || 0))}</td>
      <td class="mono">${(s.cost || 0).toFixed(4)}</td>
      <td style="color:var(--text-dim)">${ago(s.updated_at)}</td>
      <td style="text-align:right;white-space:nowrap">
        <button class="btn sm ghost" data-pin="${esc(s.id)}" data-v="${s.pinned ? 0 : 1}">${s.pinned ? "取消置顶" : "置顶"}</button>
        <button class="btn sm ghost" data-arch="${esc(s.id)}" data-v="${s.archived ? 0 : 1}">${s.archived ? "取消归档" : "归档"}</button>
        <a class="btn sm ghost" href="/api/sessions/${encodeURIComponent(s.id)}/export?format=md" download>导出</a>
        <button class="btn sm ghost danger" data-del="${esc(s.id)}">删除</button>
      </td></tr>`).join("")}</tbody></table></div>`;
  $$("[data-open]", el).forEach((a) => a.onclick = (e) => { e.preventDefault(); loadSession(a.dataset.open); });
  $$("[data-pin]", el).forEach((b) => b.onclick = async () => {
    await api("/api/sessions/" + encodeURIComponent(b.dataset.pin), { method: "PATCH", body: { pinned: Number(b.dataset.v) } });
    PAGES.sessions();
  });
  $$("[data-arch]", el).forEach((b) => b.onclick = async () => {
    await api("/api/sessions/" + encodeURIComponent(b.dataset.arch), { method: "PATCH", body: { archived: Number(b.dataset.v) } });
    PAGES.sessions();
  });
  $$("[data-del]", el).forEach((b) => b.onclick = async () => {
    if (!confirm("确定删除这个会话？不可恢复。")) return;
    await api("/api/sessions/" + encodeURIComponent(b.dataset.del), { method: "DELETE" });
    toast("已删除", "ok"); PAGES.sessions();
  });
};

/* ==========================================================================
   记忆页
   ========================================================================== */
const KIND_LABEL = { fact: "事实", preference: "偏好", episode: "经历", profile: "画像", task: "任务", decision: "决策" };
PAGES.memory = async () => {
  const el = $("#page-memory");
  const d = await api("/api/memories?limit=300");
  const s = d.stats || {};
  const byKind = Object.entries(s.by_kind || {}).map(([k, v]) => `${KIND_LABEL[k] || k} ${v}`).join(" · ");
  MEM_TAB = MEM_TAB || "active";
  el.innerHTML = `
    <div class="grid c3" style="margin-bottom:14px">
      <div class="stat"><div class="k">记忆总数</div><div class="v">${s.total || 0}</div>
        <div class="s">生效 ${s.active || 0} · 置顶 ${s.pinned || 0} · 归档 ${s.archived || 0}</div></div>
      <div class="stat"><div class="k">检索后端</div><div class="v" style="font-size:15px">
        ${s.fts ? "全文索引" : "关键词"}${s.embedding_backend && s.embedding_backend !== "disabled" ? " + 向量" : ""}</div>
        <div class="s">${esc(s.embedding_backend || "")}</div></div>
      <div class="stat"><div class="k">分类分布</div><div class="v" style="font-size:14px">${byKind || "—"}</div>
        <div class="s">全局记忆所有工作区共享，本项目记忆仅当前工作区可见</div></div>
    </div>
    <div class="card" style="margin-bottom:14px">
      <h3>记忆设置<span class="hint">改完立即生效</span></h3>
      <div class="help" style="margin-bottom:10px">
        记忆分两层：<b>全局</b>（所有工作区共享）与<b>本项目</b>（只在当前工作区生效）。
        下面这几项决定「每次对话最多把几条记忆送给模型」。
      </div>
      <label class="switch"><input type="checkbox" id="ms-on"${mem.enabled !== false ? " checked" : ""}>
        开启长期记忆（跨对话记住你的偏好和项目背景）</label>
      <div class="grid c2" style="margin-top:12px">
        <div class="field"><label>每次最多带几条记忆<span class="hint">是「条数」，不是字数</span></label>
          <input type="number" id="ms-topk" value="${mem.recall_top_k || 6}"></div>
        <div class="field"><label>召回最低相关度<span class="hint">低于这个分数就不带进来（0~1）</span></label>
          <input type="number" id="ms-minscore" step="0.01" value="${mem.recall_min_score != null ? mem.recall_min_score : 0.22}"></div>
      </div>
      <label class="switch" style="margin-top:12px"><input type="checkbox" id="ms-vec"${mem.use_vector !== false ? " checked" : ""}>
        用向量检索提高召回质量（失败自动退回关键词）</label>
      <div class="help" style="margin-top:10px">
        说明：对话变长时的「自动压缩」<b>不在这一页</b> —— 它按<b>上下文窗口的比例</b>触发，
        改它在「设置 → 模型偏好 → 自动压缩阈值」（默认 80%）。
      </div>
    </div>
    <div class="tabs" id="mem-tabs">
      <button class="tab${MEM_TAB === "active" ? " active" : ""}" data-mt="active">背景记忆</button>
      <button class="tab${MEM_TAB === "archived" ? " active" : ""}" data-mt="archived">归档的记忆</button>
      <button class="tab${MEM_TAB === "instr" ? " active" : ""}" data-mt="instr">指令文件</button>
      <button class="tab${MEM_TAB === "recall" ? " active" : ""}" data-mt="recall">召回记录</button>
    </div>
    <div class="card">
      <h3 id="mem-title">背景记忆<span class="hint" id="mem-sub">跨会话生效；置顶的记忆始终参与召回</span></h3>
      <div class="row" style="margin-bottom:10px" id="mem-filters">
        <input type="text" id="mem-q" placeholder="搜索标题、内容或标签…" style="max-width:260px">
        <select id="mem-kind" style="width:auto"><option value="">全部种类</option>
          ${Object.entries(KIND_LABEL).map(([k, v]) => `<option value="${k}">${v}</option>`).join("")}</select>
        <select id="mem-scope" style="width:auto">
          <option value="">全部范围</option>
          <option value="global">全局记忆</option>
          <option value="project">本项目记忆</option>
        </select>
        <span class="spacer"></span>
      </div>
      <div id="mem-list"></div>
    </div>`;

  const listEl = $("#mem-list");

  // ---- 背景记忆 / 归档的记忆：共用一套卡片渲染 ----
  const renderList = (items, archivedView) => {
    if (!items.length) {
      listEl.innerHTML = `<div class="empty">${archivedView
        ? "没有归档的记忆。归档用于把过时但仍有参考价值的内容收起来。"
        : "还没有记忆。当你告诉我偏好或重要背景时，我会记下来。"}</div>`;
      return;
    }
    listEl.innerHTML = items.map((m) => `
      <div style="border:1px solid var(--border);border-radius:9px;padding:11px 13px;margin-bottom:9px">
        <div class="row" style="margin-bottom:5px">
          ${m.pinned ? '<span class="tag accent">置顶</span>' : ""}
          <span class="tag ${m.scope === "global" ? "ok" : ""}">${m.scope === "global" ? "全局" : "本项目"}</span>
          <span class="tag">${esc(KIND_LABEL[m.kind] || m.kind)}</span>
          <b style="font-size:13px">${esc(m.title || "(无标题)")}</b>
          <span class="spacer"></span>
          <span style="font-size:11px;color:var(--text-faint)">重要度 ${(m.importance || 0).toFixed(2)} · 访问 ${m.access_count || 0} 次 · ${ago(m.updated_at)}</span>
        </div>
        <div style="font-size:12.5px;color:var(--text-soft);white-space:pre-wrap;max-height:120px;overflow:auto">${esc(m.content)}</div>
        <div class="row tight" style="margin-top:7px">
          <button class="btn sm ghost" data-edit="${esc(m.id)}">编辑</button>
          <button class="btn sm ghost" data-pin="${esc(m.id)}" data-v="${m.pinned ? 0 : 1}">${m.pinned ? "取消置顶" : "置顶"}</button>
          <button class="btn sm ghost" data-arch="${esc(m.id)}" data-v="${archivedView ? 0 : 1}">${archivedView ? "取消归档" : "归档"}</button>
          <button class="btn sm ghost danger" data-del="${esc(m.id)}">删除</button>
          <span style="font-size:10.5px;color:var(--text-faint);margin-left:auto" class="mono">${esc(m.id)}</span>
        </div>
      </div>`).join("");

    $$("[data-edit]", listEl).forEach((b) => b.onclick = () => {
      const m = items.find((x) => x.id === b.dataset.edit); memoryEdit(m);
    });
    $$("[data-pin]", listEl).forEach((b) => b.onclick = async () => {
      await api("/api/memories", { method: "POST", body: { action: "update", id: b.dataset.pin, pinned: Number(b.dataset.v) } });
      PAGES.memory();
    });
    $$("[data-arch]", listEl).forEach((b) => b.onclick = async () => {
      await api("/api/memories", { method: "POST", body: { action: "update", id: b.dataset.arch, archived: Number(b.dataset.v) } });
      toast(Number(b.dataset.v) ? "已归档" : "已取消归档", "ok");
      PAGES.memory();
    });
    $$("[data-del]", listEl).forEach((b) => b.onclick = async () => {
      if (!confirm("删除这条记忆？")) return;
      await api("/api/memories", { method: "POST", body: { action: "forget", id: b.dataset.del } });
      toast("已删除", "ok"); PAGES.memory();
    });
  };

  // ---- 指令文件：工作区里的 AGENTS.md / .fengcode 约定文件 ----
  const renderInstr = async () => {
    listEl.innerHTML = `<div class="help">正在读取…</div>`;
    try {
      const r = await api("/api/instruction-files");
      const files = r.files || [];
      listEl.innerHTML = `
        <div class="help" style="margin-bottom:10px">
          这些文件会在每轮对话开始时自动注入，用来给 AI 定规矩（项目约定、代码风格、禁忌）。
          放在工作区根目录即生效。
        </div>
        ${files.length ? files.map((f) => `
          <div style="border:1px solid var(--border);border-radius:9px;padding:11px 13px;margin-bottom:9px">
            <div class="row" style="margin-bottom:5px">
              <span class="tag ${f.exists ? "ok" : ""}">${f.exists ? "已生效" : "不存在"}</span>
              <b style="font-size:13px" class="mono">${esc(f.name)}</b>
              <span class="spacer"></span>
              <span style="font-size:11px;color:var(--text-faint)">${f.exists ? `${f.size} 字节` : "点击创建"}</span>
            </div>
            <div class="mono" style="font-size:11px;color:var(--text-dim);margin-bottom:6px">${esc(f.path)}</div>
            <div class="help">${esc(f.purpose || "")}</div>
            <div class="row tight" style="margin-top:7px">
              <button class="btn sm ghost" data-instr-view="${esc(f.path)}"${f.exists ? "" : " disabled"}>查看</button>
              <button class="btn sm ghost" data-instr-edit="${esc(f.path)}">编辑</button>
            </div>
          </div>`).join("") : `<div class="empty">未发现可用的指令文件位置。</div>`}`;

      $$("[data-instr-view]", listEl).forEach((b) => b.onclick = async () => {
        try {
          const r = await api("/api/workspace-file?path=" + encodeURIComponent(b.dataset.instrView));
          modal(b.dataset.instrView.split(/[\\/]/).pop(),
            `<div style="max-height:60vh;overflow:auto"><pre class="mono" style="font-size:12px;white-space:pre-wrap">${esc(r.content || "")}</pre></div>`,
            `<button class="btn" data-close>关闭</button>`);
        } catch (e) { toast(e.message, "err"); }
      });
      $$("[data-instr-edit]", listEl).forEach((b) => b.onclick = () => {
        const path = b.dataset.instrEdit;
        const name = path.split(/[\\/]/).pop();
        modal("编辑 " + name, `
          <div class="help mono" style="margin-bottom:8px">${esc(path)}</div>
          <textarea id="instr-body" class="mono" rows="16" placeholder="# 项目约定&#10;&#10;- 代码风格：…&#10;- 不要动 xxx 目录"></textarea>`,
          `<button class="btn" data-close>取消</button><button class="btn primary" id="instr-save">保存</button>`);
        // 先尝试读现有内容；不存在就留空给用户新建
        api("/api/workspace-file?path=" + encodeURIComponent(path))
          .then((r) => { const t = $("#instr-body"); if (t) t.value = r.content || ""; })
          .catch(() => {});
        $("#instr-save").onclick = async () => {
          try {
            const r = await api("/api/workspace-file", { method: "PUT", body: { path, content: $("#instr-body").value } });
            if (r && r.ok === false) throw new Error(r.error || "保存失败");
            closeModal(); toast("已保存", "ok"); renderInstr();
          } catch (e) { toast(e.message, "err"); }
        };
      });
    } catch (e) {
      listEl.innerHTML = `<div class="empty">读取失败：${esc(e.message)}</div>`;
    }
  };

  // ---- 召回记录：哪些记忆被用过、用了多少次 ----
  const renderRecall = () => {
    const items = (d.memories || []).filter((m) => (m.access_count || 0) > 0)
      .sort((a, b) => (b.access_count || 0) - (a.access_count || 0));
    const never = (d.memories || []).filter((m) => !(m.access_count || 0)).length;
    if (!items.length) {
      listEl.innerHTML = `<div class="empty">还没有召回记录。记忆被 AI 用上之后会显示在这里。</div>`;
      return;
    }
    listEl.innerHTML = `
      <div class="help" style="margin-bottom:10px">
        按被召回次数排序。共 ${items.length} 条被用过，另有 ${never} 条尚未被召回
        —— 长期没被召回的记忆可以考虑归档或删除。
      </div>
      ${items.map((m) => `
        <div style="border:1px solid var(--border);border-radius:9px;padding:11px 13px;margin-bottom:9px">
          <div class="row" style="margin-bottom:5px">
            <span class="tag accent">召回 ${m.access_count || 0} 次</span>
            ${m.pinned ? '<span class="tag accent">置顶</span>' : ""}
            <span class="tag ${m.scope === "global" ? "ok" : ""}">${m.scope === "global" ? "全局" : "本项目"}</span>
            <b style="font-size:13px">${esc(m.title || "(无标题)")}</b>
            <span class="spacer"></span>
            <span style="font-size:11px;color:var(--text-faint)">最后召回 ${m.accessed_at ? ago(m.accessed_at) : "—"}</span>
          </div>
          <div style="font-size:12.5px;color:var(--text-soft);max-height:70px;overflow:auto;white-space:pre-wrap">${esc(m.content)}</div>
        </div>`).join("")}`;
  };

  const q = $("#mem-q"), kf = $("#mem-kind"), sf = $("#mem-scope");
  const refilter = async () => {
    const params = new URLSearchParams({ limit: "300" });
    if (q.value.trim()) params.set("search", q.value.trim());
    if (kf.value) params.set("kind", kf.value);
    if (sf && sf.value) params.set("scope", sf.value);
    if (MEM_TAB === "archived") params.set("archived", "1");
    const nd = await api("/api/memories?" + params.toString());
    renderList(nd.memories || [], MEM_TAB === "archived");
  };
  q.oninput = () => { clearTimeout(q._t); q._t = setTimeout(refilter, 300); };
  kf.onchange = refilter;
  if (sf) sf.onchange = refilter;

  // ---- 标签切换 ----
  const showTab = async (tab) => {
    MEM_TAB = tab;
    $$("#mem-tabs .tab").forEach((b) => b.classList.toggle("active", b.dataset.mt === tab));
    const title = $("#mem-title"), sub = $("#mem-sub"), filters = $("#mem-filters");
    const meta = {
      active: ["背景记忆", "跨会话生效；置顶的记忆始终参与召回", true],
      archived: ["归档的记忆", "已归档的内容不参与召回，但保留备查", true],
      instr: ["指令文件", "每轮对话自动注入的规矩文件", false],
      recall: ["召回记录", "哪些记忆真的被用上了", false],
    }[tab] || [];
    if (title) {
      if (title.firstChild) title.firstChild.textContent = meta[0];
      else title.textContent = meta[0];
    }
    if (sub) sub.textContent = meta[1];
    if (filters) filters.style.display = meta[2] ? "" : "none";
    if (tab === "instr") await renderInstr();
    else if (tab === "recall") renderRecall();
    else await refilter();
  };
  $$("#mem-tabs .tab").forEach((b) => b.onclick = () => showTab(b.dataset.mt));

  // ---- 记忆设置（原「记忆与压缩」面板的内容，合并到这里）----
  // 改完立即写盘（不用点保存），并在右下角给个轻提示。
  const saveMem = async (patch) => {
    try {
      await api("/api/config", { method: "PATCH", body: { memory: patch } });
      toast("记忆设置已保存", "ok");
      try { S.boot = await api("/api/bootstrap"); } catch (e) {}
    } catch (e) { toast("保存失败：" + e.message, "err"); }
  };
  const msOn = $("#ms-on"), msK = $("#ms-topk"), msS = $("#ms-minscore"), msV = $("#ms-vec");
  if (msOn) msOn.onchange = () => saveMem({ enabled: msOn.checked });
  if (msV) msV.onchange = () => saveMem({ use_vector: msV.checked });
  if (msK) msK.onchange = () => saveMem({ recall_top_k: Math.max(1, Math.floor(Number(msK.value) || 6)) });
  if (msS) msS.onchange = () => saveMem({ recall_min_score: Math.min(1, Math.max(0, Number(msS.value) || 0)) });

  await showTab(MEM_TAB);
};
let MEM_TAB = "active";
function memoryEdit(m) {
  const isNew = !m;
  m = m || { title: "", content: "", kind: "fact", tags: [], importance: 0.6, pinned: false };
  const b = modal(isNew ? "新增记忆" : "编辑记忆", `
    <div class="field"><label>标题</label><input type="text" id="m-title" value="${esc(m.title)}"></div>
    <div class="field"><label>内容</label><textarea id="m-content" rows="6">${esc(m.content)}</textarea></div>
    <div class="grid c3">
      <div class="field"><label>种类</label><select id="m-kind">
        ${Object.entries(KIND_LABEL).map(([k, v]) => `<option value="${k}"${m.kind === k ? " selected" : ""}>${v}</option>`).join("")}
      </select></div>
      <div class="field"><label>重要度 (0-1)</label><input type="number" id="m-imp" min="0" max="1" step="0.05" value="${m.importance}"></div>
      <div class="field"><label>置顶</label><label class="switch"><input type="checkbox" id="m-pin"${m.pinned ? " checked" : ""}> 始终参与召回</label></div>
    </div>
    <div class="field"><label>标签（逗号分隔）</label><input type="text" id="m-tags" value="${esc((m.tags || []).join(", "))}"></div>`,
    `<button class="btn" data-close>取消</button><button class="btn primary" id="m-save">保存</button>`);
  $("#m-save").onclick = async () => {
    const payload = {
      action: isNew ? "remember" : "update",
      title: $("#m-title").value.trim(),
      content: $("#m-content").value.trim(),
      kind: $("#m-kind").value,
      importance: Number($("#m-imp").value),
      pinned: $("#m-pin").checked,
      tags: $("#m-tags").value.split(/[,，]/).map((x) => x.trim()).filter(Boolean),
    };
    if (isNew) payload.action = "remember";
    else payload.id = m.id;
    if (!payload.content) return toast("内容不能为空", "err");
    try {
      await api("/api/memories", { method: "POST", body: payload });
      closeModal(); toast("已保存", "ok"); PAGES.memory();
    } catch (e) { toast(e.message, "err"); }
  };
}

/** 渲染权限细粒度规则三列。
 *
 * 与 fillSandboxProbe 同理：设置中心复制 innerHTML，所以可能同时存在多份
 * 同 id 容器，这里要**渲染所有匹配的容器**，否则用户看到的是旧内容。
 */
function paintRulesGlobal() {
  const R = window.__fengcodeRules;
  if (!R) return;
  for (const act of ["deny", "ask", "allow"]) {
    const boxes = $$("#rule-" + act);
    const html = (R[act] || []).length
      ? R[act].map((p, i) => `<div class="rule-item">
          <span class="mono">${esc(p)}</span>
          <span class="spacer"></span>
          <span class="rule-del" data-rule-del="${act}" data-i="${i}" title="删除">${icon("close", 12)}</span>
        </div>`).join("")
      : `<div class="help">无</div>`;
    boxes.forEach((b) => { b.innerHTML = html; });
  }
}

/** 记忆条目列表 + 统计（记忆面板用）。
 *  注意设置中心是搬「真实节点」，所以同一 id 可能同时存在于原页与设置中心；
 *  这里填充**所有匹配的容器**，避免有一处永远停在「正在读取…」。 */
/* ★ 记忆维度补强的两个小标签（易变性 / 主题键）。
   为什么要显示出来：用户看得见「这条是易变的、这条属于某个主题」，
   才能理解为什么两条相似的话只有一条留着、为什么某条排得更靠后。 */
function volatilityTag(m) {
  const v = String((m && m.volatility) || "stable");
  if (v === "permanent") return '<span class="tag" title="几乎不变的事实">永久</span>';
  if (v === "volatile") return '<span class="tag warn" title="容易过时的事实，召回时会看重新鲜度">易变</span>';
  return "";
}
function topicTag(m) {
  const k = String((m && m.topic_key) || "").trim();
  if (!k) return "";
  return `<span class="tag" title="同一主题只保留一个活跃值">主题：${esc(k)}</span>`;
}

async function loadMemoryPanel() {
  const lists = $$("#m-list");
  if (!lists.length) return;
  let d = null;
  try {
    const q = ($("#m-search") || {}).value || "";
    d = await api("/api/memories?limit=200" + (q ? "&search=" + encodeURIComponent(q) : ""));
  } catch (e) {
    lists.forEach((el) => { el.innerHTML = `<div class="memo-empty">读取失败：${esc(e.message)}</div>`; });
    return;
  }
  const items = (d && d.memories) || [];
  const st = (d && d.stats) || {};
  $$("#m-count").forEach((el) => {
    el.textContent = st.total != null
      ? `共 ${st.total} 条（长期 ${st.long_term != null ? st.long_term : "-"} · 情景 ${st.episodic != null ? st.episodic : "-"}）`
      : `共 ${items.length} 条`;
  });
  const html = items.length
    ? items.map((m) => `<div class="memo-item" data-mid="${esc(m.id)}">
        <div class="memo-head">
          <span class="tag">${esc(m.kind || "fact")}</span>
          <b class="memo-title">${esc(m.title || "（无标题）")}</b>
          ${m.pinned ? '<span class="tag warn">置顶</span>' : ""}
          ${volatilityTag(m)}
          ${topicTag(m)}
          <span class="tag">r${esc(String(m.revision || 1))}</span>
          <span class="spacer"></span>
          <button class="btn ghost sm" data-mhist="${esc(m.id)}" title="查看修订历史 / 撤回">历史</button>
          <button class="btn ghost sm" data-mdel="${esc(m.id)}" title="删除这条记忆">${icon("trash", 13)}</button>
        </div>
        <div class="memo-body">${esc(String(m.content || "").slice(0, 400))}</div>
        <div class="memo-hist" id="mh-${esc(m.id)}" style="display:none"></div>
      </div>`).join("")
    : `<div class="memo-empty">还没有记忆。和 AI 说「记住：……」就会出现在这里。</div>`;
  lists.forEach((el) => { el.innerHTML = html; });
  $$("[data-mdel]").forEach((b) => b.onclick = async () => {
    if (!confirm("删除这条记忆？不可撤销。")) return;
    try {
      await api("/api/memories", { method: "POST", body: { action: "forget", id: b.dataset.mdel } });
      toast("已删除", "ok");
      loadMemoryPanel();
    } catch (e) { toast("删除失败：" + e.message, "err"); }
  });
  // ★ 修订历史 / 撤回（记忆改了能回退，也看得清改过什么」）。
  //   点「历史」就地展开该条的版本列表，右侧每条给「撤回」按钮。
  $$("[data-mhist]").forEach((b) => b.onclick = async () => {
    const mid = b.dataset.mhist;
    const box = $("#mh-" + mid);
    if (!box) return;
    if (box.style.display !== "none") { box.style.display = "none"; box.innerHTML = ""; return; }
    box.style.display = "";
    box.innerHTML = `<div class="memo-empty">正在读取历史…</div>`;
    try {
      const r = await api("/api/memories", { method: "POST", body: { action: "revisions", id: mid } });
      const revs = r.revisions || [];
      if (!revs.length) {
        box.innerHTML = `<div class="memo-empty">没有更早的版本（这条还没被改过）。</div>`;
        return;
      }
      box.innerHTML = revs.map((v) => `<div class="memo-rev">
          <span class="tag">r${esc(String(v.revision))}</span>
          <span class="memo-rev-time">${esc(v.created_at ? ago(v.created_at) : "")}</span>
          <span class="memo-rev-why">${esc(v.reason || "")}</span>
          <span class="spacer"></span>
          <button class="btn ghost sm" data-mrestore="${esc(mid)}|${esc(String(v.revision))}">撤回这一版</button>
        </div>
        <div class="memo-rev-body">${esc(String(v.content || "").slice(0, 300))}</div>`).join("");
      $$("[data-mrestore]", box).forEach((rb) => rb.onclick = async () => {
        const [id, rev] = rb.dataset.mrestore.split("|");
        try {
          await api("/api/memories", { method: "POST", body: { action: "restore", id, revision: Number(rev) } });
          toast(`已回退到 r${rev}`, "ok");
          loadMemoryPanel();
        } catch (e) { toast("回退失败：" + e.message, "err"); }
      });
    } catch (e) {
      box.innerHTML = `<div class="memo-empty">读取历史失败：${esc(e.message)}</div>`;
    }
  });
}

/** ★ 1-G：把「写了却永远匹配不到」的权限规则提示出来。
    静默失效的安全规则比没有规则更危险 —— 容易以为禁掉了，其实没有。
    数据来自 /api/status 的 permission_warnings（后端 validate_rules 生成）。 */
async function fillPermWarnings() {
  const boxes = $$("#perm-warn");
  if (!boxes.length) return;
  let warns = [];
  try {
    const st = await api("/api/status");
    warns = st.permission_warnings || [];
  } catch (e) { return; }
  boxes.forEach((b) => {
    if (!warns.length) { b.hidden = true; b.innerHTML = ""; return; }
    b.hidden = false;
    b.innerHTML = `<div class="pw-head">${icon("warning", 13)} 有 ${warns.length} 条权限规则可能永远匹配不到：</div>`
      + warns.map((w) => `<div class="pw-item">${esc(w)}</div>`).join("");
  });
}

/** 填充沙箱环境探测表格。
 *
 * 注意：设置中心是把 #set-safety 的 innerHTML 复制到 #ss-host 里的，
 * 所以同一 id 可能同时存在于原页和设置中心。这里**填充所有匹配的容器**，
 * 避免出现「检测中…」一直不消失。
 */
async function fillSandboxProbe() {
  const boxes = $$("#sb-probe");
  if (!boxes.length) return;
  let p;
  try {
    p = await api("/api/sandbox-probe");
  } catch (e) {
    boxes.forEach((b) => { b.innerHTML = `<div class="help" style="padding:10px 12px">检测失败：${esc(e.message)}</div>`; });
    return;
  }
  const html = (p.items || []).map((it) => `
    <div class="row" style="padding:7px 12px;border-top:1px solid var(--border);font-size:12px">
      <span style="flex:1">${esc(it.label)}</span>
      <span style="width:110px">
        <span class="tag ${it.found ? "ok" : ""}">${it.found ? "已检测到" : "未检测到"}</span>
      </span>
      <span class="spacer"></span>
      <span class="mono" style="font-size:11px;color:var(--text-dim);max-width:46%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
            title="${esc(it.path || it.hint || "")}">${esc(it.path || "—")}</span>
    </div>`).join("");
  boxes.forEach((b) => { b.innerHTML = html; });
  $$("#sb-current").forEach((c) => {
    c.textContent = "当前使用：" + (p.current_label || p.current || "未知");
  });
}

/* ==========================================================================
   技能页
   ========================================================================== */
/** 子智能体：内置预设 */
/** ★ 2-K：子代理运行态 —— 谁在排队、谁在跑、谁刚结束。
    以前只有「开始/结束」两条流水，并发上限卡住几个完全看不见。 */
function renderSubLive(live) {
  const items = (live || []).filter(Boolean);
  if (!items.length) return "";
  const running = items.filter((x) => x.status === "running").length;
  const queued = items.filter((x) => x.status === "queued").length;
  const rows = items.map((x) => {
    const st = x.status || "";
    const label = st === "running" ? "运行中" : st === "queued" ? "排队中"
      : st === "done" ? "已完成" : st === "failed" ? "失败" : st;
    const cls = st === "running" ? "accent" : st === "queued" ? "" : st === "failed" ? "err" : "ok";
    const time = st === "queued" ? `已等 ${x.waited || 0}s` : `${x.elapsed || 0}s`;
    // ★ WaitCause：排队时显示「被什么挡住」，而不是只有一个「排队中」。
    const why = (st === "queued" && x.wait_text)
      ? `<span class="sub-why" title="为什么在等">· ${esc(x.wait_text)}</span>` : "";
    return `<div class="sub-live-row">
      <span class="tag ${cls}">${esc(label)}</span>
      <span class="sub-name">${esc(x.name || "")}</span>
      <span class="sub-type">${esc(x.type || "")}</span>
      ${why}
      <span class="spacer"></span>
      <span class="sub-time">${esc(time)}</span>
    </div>`;
  }).join("");
  return `<div class="sub-live">
    <div class="sub-live-head">当前运行态：运行中 ${running} · 排队 ${queued}（共 ${items.length}）</div>
    ${rows}
  </div>`;
}

PAGES.subagents = async () => {
  const el = $("#page-subagents") || $("#ss-host") || $("#settings-render");
  if (!el) return;
  let d = { types: [] };
  try { d = await api("/api/subagents"); } catch (e) { /* ignore */ }
  const types = d.types || [];
  const models = (S.boot && S.boot.models) || [];
  const ICONS = {
    "general": "sparkles", "explore": "search", "research": "globe",
    "review": "check", "security-review": "shield", "test": "flask", "plan": "map",
  };
  el.innerHTML = `<div class="card">
    <h3>内置子智能体（${types.length}）<span class="hint">提示词与工具范围固定，你可以覆盖模型与推理强度</span></h3>
    <div class="help" style="margin-bottom:12px">
      子智能体是「自己开的小号」：主代理把独立子任务派给它们并行处理，各自有专属工具白名单。
    </div>
    ${renderSubLive(d.live || [])}
    <div style="display:grid;gap:10px">${types.map((t) => `
      <div style="border:1px solid var(--border);border-radius:9px;padding:12px 14px">
        <div class="row" style="margin-bottom:6px">
          <b class="mono" style="font-size:13.5px">/${esc(t.name)}</b>
          <span class="tag">${esc(t.label || "")}</span>
          <span class="tag ok">${(t.tools || []).length} 个工具</span>
          <span class="spacer"></span>
          <button class="btn sm ghost" data-use="${esc(t.name)}">在聊天中使用</button>
        </div>
        <div style="font-size:12.5px;color:var(--text-soft);margin-bottom:8px">${esc(t.description || "")}</div>
        <div class="grid c2">
          <div class="field"><label>模型</label><select data-sam="${esc(t.name)}">
            <option value="">继承默认</option>
            ${models.map((m) => `<option value="${esc(m.ref)}"${t.model === m.ref ? " selected" : ""}>${esc(m.provider_display)} / ${esc(m.model)}</option>`).join("")}
          </select></div>
          <div class="field"><label>推理强度</label><select data-sae="${esc(t.name)}">
            <option value="">继承默认</option>
            <option value="low">低（快）</option>
            <option value="medium">中</option>
            <option value="high">高（准）</option>
          </select></div>
        </div>
      </div>`).join("")}</div>
  </div>`;
  el.querySelectorAll("[data-use]").forEach((b) => b.onclick = () => {
    const cmd = `/${b.dataset.use} <任务>`;
    navigator.clipboard && navigator.clipboard.writeText(cmd);
    toast(`已复制「${cmd}」，粘到对话框即可`, "ok");
  });
  // 覆盖模型：写进 agent.subagent_models
  el.querySelectorAll("[data-sam]").forEach((s) => s.onchange = async () => {
    try {
      const cur = { ...((S.boot && S.boot.agent && S.boot.agent.subagent_models) || {}) };
      if (s.value) cur[s.dataset.sam] = s.value; else delete cur[s.dataset.sam];
      await api("/api/config", { method: "PATCH", body: { agent: { subagent_models: cur } } });
      toast("已保存", "ok");
    } catch (e) { toast(e.message, "err"); }
  });
  // 推理强度：全局一项
  el.querySelectorAll("[data-sae]").forEach((s) => s.onchange = async () => {
    try {
      await api("/api/config", { method: "PATCH", body: { agent: { subagent_effort: s.value } } });
      toast("已保存", "ok");
    } catch (e) { toast(e.message, "err"); }
  });
};

PAGES.skills = async () => {
  const el = $("#page-skills");
  const d = await api("/api/skills");
  const items = d.skills || [];
  const srcMap = { builtin: "内置", user: "我自己建的" };
  el.innerHTML = `<div class="card">
    <h3>技能库（${items.length}）<span class="hint">技能是「怎么做事」的说明书，任务匹配时会自动照着做</span></h3>
    <div class="row" style="margin-bottom:12px">
      <button class="btn primary" id="sk-new">新建技能</button>
      <button class="btn" id="sk-rescan">重新扫描</button>
    </div>
    <div class="grid c2">${items.map((s) => `
      <div style="border:1px solid var(--border);border-radius:9px;padding:12px 14px">
        <div class="row" style="margin-bottom:5px">
          <b style="font-size:13.5px">${esc(s.name)}</b>
          <span class="tag">${esc(srcMap[s.source] || s.source)}</span>
          <span class="tag ${s.enabled ? "ok" : ""}">${s.enabled ? "已启用" : "已停用"}</span>
          <span class="spacer"></span>
          <span style="font-size:11px;color:var(--text-faint)">v${esc(s.version)} · ${s.kind === "python" ? "Python" : "Markdown"}</span>
        </div>
        <div style="font-size:12.5px;color:var(--text-soft);min-height:38px">${esc(s.description || "(无说明)")}</div>
        ${s.when_to_use ? `<div style="font-size:11.5px;color:var(--text-dim);margin-top:5px">何时使用：${esc(s.when_to_use)}</div>` : ""}
        <div class="row tight" style="margin-top:9px">
          <button class="btn sm ghost" data-view="${esc(s.name)}">查看全文</button>
          <button class="btn sm ghost" data-tog="${esc(s.name)}" data-v="${s.enabled ? 0 : 1}">${s.enabled ? "停用" : "启用"}</button>
          ${s.source !== "builtin" ? `<button class="btn sm ghost danger" data-del="${esc(s.name)}">删除</button>` : ""}
          ${s.tags && s.tags.length ? `<span style="margin-left:auto;font-size:10.5px;color:var(--text-faint)">${s.tags.map(esc).join(" · ")}</span>` : ""}
        </div>
      </div>`).join("")}</div></div>`;
  $$("[data-view]").forEach((b) => b.onclick = async () => {
    try {
      const d = await api("/api/skills?name=" + encodeURIComponent(b.dataset.view));
      modal(d.skill.name, `<div style="font-size:12px;color:var(--text-dim);margin-bottom:9px">${esc(d.skill.path)}</div>
        <pre class="block">${esc(d.skill.body || "")}</pre>`);
    } catch (e) { toast(e.message, "err"); }
  });
  $$("[data-tog]").forEach((b) => b.onclick = async () => {
    await api("/api/skills", { method: "POST", body: { action: "toggle", name: b.dataset.tog, enabled: Number(b.dataset.v) } });
    PAGES.skills();
  });
  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm(`删除技能「${b.dataset.del}」？`)) return;
    try { await api("/api/skills", { method: "POST", body: { action: "delete", name: b.dataset.del } });
      toast("已删除", "ok"); PAGES.skills(); } catch (e) { toast(e.message, "err"); }
  });
  const skNew = $("#sk-new");
  if (skNew) skNew.onclick = () => skillCreate();
  const skRescan = $("#sk-rescan");
  if (skRescan) skRescan.onclick = async () => {
    try {
      const r = await api("/api/skills", { method: "POST", body: { action: "reload" } });
      toast(`扫描到 ${r.count} 个技能`, "ok");
      PAGES.skills();
    } catch (e) { toast(e.message, "err"); }
  };
};
function skillCreate() {
  modal("新建技能", `
    <div class="field"><label>技能名称</label><input type="text" id="sk-name" placeholder="如：周报生成"></div>
    <div class="field"><label>一句话说明（用于自动匹配）</label><input type="text" id="sk-desc" placeholder="如：把本周工作整理成周报"></div>
    <div class="field"><label>正文（可用 Markdown；留空则生成模板）</label>
      <textarea id="sk-body" class="mono" rows="10" placeholder="---&#10;name: 周报生成&#10;description: ...&#10;---&#10;&#10;# 步骤&#10;1. ..."></textarea>
      <div class="help">frontmatter 里的 name/description 决定匹配效果</div></div>`,
    `<button class="btn" data-close>取消</button><button class="btn primary" id="sk-save">创建</button>`);
  $("#sk-save").onclick = async () => {
    const name = $("#sk-name").value.trim();
    if (!name) return toast("名称不能为空", "err");
    try {
      await api("/api/skills", { method: "POST", body: {
        action: "create", name, description: $("#sk-desc").value.trim(), content: $("#sk-body").value,
      }});
      closeModal(); toast("已创建", "ok"); PAGES.skills();
    } catch (e) { toast(e.message, "err"); }
  };
}

/* ==========================================================================
   工具页
   ========================================================================== */
PAGES.tools = async () => {
  const el = $("#page-tools");
  const d = await api("/api/tools");
  const total = (d.groups || []).reduce((a, g) => a + g.tools.length, 0);
  el.innerHTML = `
    <div class="card"><h3>内置工具（${total} 个）<span class="hint">可停用不需要的；点击「试运行」手动执行</span></h3>
      ${(d.groups || []).map((g) => `
        <details style="margin-bottom:10px"${g.group === "文件" || g.group === "执行" ? " open" : ""}>
          <summary style="cursor:pointer;padding:6px 0;font-weight:600;font-size:13px">
            ${esc(g.group)} <span class="tag">${g.tools.length}</span></summary>
          <table style="margin-top:6px"><thead><tr><th style="width:180px">名称</th><th>说明</th><th style="width:70px"></th></tr></thead>
          <tbody>${g.tools.map((t) => `<tr>
            <td class="mono">${esc(t.name)}${t.dangerous ? ' <span class="tag warn">写</span>' : ""}${t.read_only ? ' <span class="tag">读</span>' : ""}</td>
            <td style="color:var(--text-soft);font-size:12px">${esc(t.description)}</td>
            <td style="text-align:right;white-space:nowrap">
              <button class="btn sm ghost" data-tog="${esc(t.name)}" data-v="${t.disabled ? 1 : 0}">${t.disabled ? "启用" : "停用"}</button>
            </td></tr>`).join("")}</tbody></table></details>`).join("")}
    </div>
    ${(d.mcp && d.mcp.length) ? `<div class="card"><h3>MCP 工具（${d.mcp.length}）</h3>
      ${d.mcp.map((s) => `<div style="padding:4px 0;font-size:12.5px" class="mono">${esc(s.name)}</div>`).join("")}</div>` : ""}
    ${(d.plugins && d.plugins.length) ? `<div class="card"><h3>插件工具（${d.plugins.length}）</h3>
      ${d.plugins.map((s) => `<div style="padding:4px 0;font-size:12.5px" class="mono">${esc(s.name)}</div>`).join("")}</div>` : ""}`;
  $$("[data-tog]").forEach((b) => b.onclick = async () => {
    await api("/api/tools", { method: "POST", body: { name: b.dataset.tog, enabled: !Number(b.dataset.v) } });
    PAGES.tools();
  });
};

/* ==========================================================================
   MCP 页
   ========================================================================== */
PAGES.mcp = async () => {
  const el = $("#page-mcp");
  const d = await api("/api/mcp");
  const st = {};
  (d.servers || []).forEach((s) => st[s.name] = s);
  const cfg = d.configured || [];
  el.innerHTML = `
    ${d.configs && d.configs.candidates && d.configs.candidates.length ? `
    <div class="card"><h3>检测到本机已有配置<span class="hint">可一键把现有 MCP 服务导进来</span></h3>
      ${d.configs.candidates.map((c) => `<div class="row" style="margin-bottom:6px">
        <span class="tag">${esc(c.label)}</span>
        <span class="mono" style="font-size:11.5px;color:var(--text-dim)">${esc(c.path)}</span>
        <span class="spacer"></span>
        <button class="btn sm primary" data-imp="${esc(c.path)}">导入</button></div>`).join("")}
    </div>` : ""}
    <div class="card"><h3>已配置服务（${cfg.length}）<span class="hint">MCP 让 Fengcode 接入外部工具生态</span></h3>
      ${cfg.length ? `<table><thead><tr><th style="width:150px">名称</th><th style="width:70px">类型</th><th>目标</th>
        <th style="width:90px">状态</th><th style="width:70px">工具</th><th style="width:230px"></th></tr></thead><tbody>
        ${cfg.map((s) => {
          const live = st[s.name] || {};
          const ready = live.connected;
          return `<tr>
            <td><b>${esc(s.name)}</b></td>
            <td><span class="tag">${esc(s.type)}</span></td>
            <td class="mono" style="font-size:11px;color:var(--text-dim);word-break:break-all">
              ${esc(s.command ? [s.command].concat(s.args || []).join(" ") : s.url)}</td>
            <td>${ready ? '<span class="tag ok">已连接</span>'
              : (live.status === "error" ? '<span class="tag err">失败</span>'
              : (live.status === "starting" ? '<span class="tag">连接中…</span>'
              /* ★ 1-L：stopped 不等于「坏了」——MCP 默认按需连接（用到才握手），
                 旧文案写「未启动」会让容易以为 MCP 是故障的。 */
              : '<span class="tag">按需连接</span>'))}
              ${live.error ? `<div style="font-size:10.5px;color:var(--danger);margin-top:2px">${esc(String(live.error).slice(0, 80))}</div>` : ""}</td>
            <td class="mono">${live.tool_count || 0}</td>
            <td style="text-align:right;white-space:nowrap">
              <button class="btn sm ghost" data-test="${esc(s.name)}">测试</button>
              <button class="btn sm ghost" data-start="${esc(s.name)}">${ready ? "重启" : "启动"}</button>
              ${ready ? `<button class="btn sm ghost" data-stop="${esc(s.name)}">停止</button>` : ""}
              <button class="btn sm ghost danger" data-del="${esc(s.name)}">移除</button>
            </td></tr>`;
        }).join("")}</tbody></table>`
      : `<div class="empty">还没有配置 MCP 服务。点右上角「添加服务」，或从已有配置导入。</div>`}
    </div>
    ${(d.servers || []).some((s) => s.tools && s.tools.length) ? `
    <div class="card"><h3>可用工具<span class="hint">这些工具会自动加入模型的工具列表</span></h3>
      ${(d.servers || []).map((s) => s.tools && s.tools.length ? `
        <div style="margin-bottom:8px"><span class="tag accent">${esc(s.name)}</span>
        <span style="font-size:12px;color:var(--text-soft);margin-left:8px">${(s.tools || []).map((t) => `<span class="mono" style="margin-right:8px">${esc(t)}</span>`).join("")}</span></div>` : "").join("")}
    </div>` : ""}`;
  $$("[data-imp]").forEach((b) => b.onclick = async () => {
    try {
      const r = await api("/api/mcp", { method: "POST", body: { action: "import_file", path: b.dataset.imp } });
      toast(`导入 ${r.added.length} 个服务${r.errors && r.errors.length ? "（有警告）" : ""}`, "ok");
      PAGES.mcp();
    } catch (e) { toast(e.message, "err"); }
  });
  $$("[data-test]").forEach((b) => b.onclick = async () => {
    b.textContent = "测试中…"; b.disabled = true;
    try {
      const r = await api("/api/mcp", { method: "POST", body: { action: "test", name: b.dataset.test } });
      if (r.ok) toast(`连接成功，发现 ${r.tool_count} 个工具（${r.duration}s）`, "ok");
      else toast("失败：" + (r.error || ""), "err");
    } catch (e) { toast(e.message, "err"); }
    b.textContent = "测试"; b.disabled = false;
    PAGES.mcp();
  });
  $$("[data-start]").forEach((b) => b.onclick = async () => {
    b.textContent = "启动中…"; b.disabled = true;
    try { await api("/api/mcp", { method: "POST", body: { action: "start", name: b.dataset.start } }); toast("已启动", "ok"); }
    catch (e) { toast("启动失败：" + e.message, "err"); }
    PAGES.mcp();
  });
  $$("[data-stop]").forEach((b) => b.onclick = async () => {
    await api("/api/mcp", { method: "POST", body: { action: "stop", name: b.dataset.stop } });
    PAGES.mcp();
  });
  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm(`移除 MCP 服务「${b.dataset.del}」？`)) return;
    await api("/api/mcp", { method: "POST", body: { action: "delete", name: b.dataset.del } });
    toast("已移除", "ok"); PAGES.mcp();
  });
};
function mcpEdit() {
  modal("添加 MCP 服务", `
    <div class="field"><label>名称</label><input type="text" id="mc-name" placeholder="如 github"></div>
    <div class="field"><label>类型</label><select id="mc-type">
      <option value="stdio">stdio（本地命令，最常用）</option>
      <option value="sse">SSE（远程）</option>
      <option value="http">HTTP（远程）</option>
    </select></div>
    <div id="mc-stdio">
      <div class="field"><label>命令</label><input type="text" id="mc-cmd" placeholder="如 npx 或 node"></div>
      <div class="field"><label>参数（每行一个）</label><textarea id="mc-args" class="mono" rows="3" placeholder="-y&#10;@modelcontextprotocol/server-github"></textarea></div>
      <div class="field"><label>环境变量（每行 KEY=VALUE）</label><textarea id="mc-env" class="mono" rows="2"></textarea></div>
    </div>
    <div id="mc-remote" style="display:none">
      <div class="field"><label>URL</label><input type="text" id="mc-url" placeholder="https://..."></div>
      <div class="field"><label>请求头（每行 KEY: VALUE）</label><textarea id="mc-headers" class="mono" rows="2"></textarea></div>
    </div>`,
    `<button class="btn" data-close>取消</button><button class="btn primary" id="mc-save">添加</button>`);
  $("#mc-type").onchange = (e) => {
    const remote = e.target.value !== "stdio";
    $("#mc-stdio").style.display = remote ? "none" : "";
    $("#mc-remote").style.display = remote ? "" : "none";
  };
  $("#mc-save").onclick = async () => {
    const type = $("#mc-type").value;
    const body = { action: "add", name: $("#mc-name").value.trim(), type };
    if (!body.name) return toast("名称不能为空", "err");
    if (type === "stdio") {
      body.command = $("#mc-cmd").value.trim();
      body.args = $("#mc-args").value.split("\n").map((x) => x.trim()).filter(Boolean);
      const env = {};
      $("#mc-env").value.split("\n").forEach((l) => {
        const i = l.indexOf("=");
        if (i > 0) env[l.slice(0, i).trim()] = l.slice(i + 1).trim();
      });
      body.env = env;
    } else {
      body.url = $("#mc-url").value.trim();
      const h = {};
      $("#mc-headers").value.split("\n").forEach((l) => {
        const i = l.indexOf(":");
        if (i > 0) h[l.slice(0, i).trim()] = l.slice(i + 1).trim();
      });
      body.headers = h;
    }
    try { await api("/api/mcp", { method: "POST", body }); closeModal(); toast("已添加", "ok"); PAGES.mcp(); }
    catch (e) { toast(e.message, "err"); }
  };
}
async function importMcp() {
  try {
    const d = await api("/api/mcp", { method: "GET" });
    toast(`导入 ${d.mcp_added.length} 个 MCP 服务`, "ok");
    PAGES.mcp();
  } catch (e) { toast(e.message, "err"); }
}

/* ==========================================================================
   插件页
   ========================================================================== */
PAGES.plugins = async () => {
  const el = $("#page-plugins");
  const d = await api("/api/plugins");
  const items = d.plugins || [];
  const panels = d.panels || [];
  el.innerHTML = `
    <div class="card"><h3>插件（${items.length}）<span class="hint">给它加新工具</span></h3>
      <div class="row" style="margin-bottom:12px">
        <button class="btn primary" id="plug-new">新建插件</button>
        <button class="btn" id="plug-install">从文件夹安装…</button>
        <button class="btn" id="plug-reload">重新加载</button>
      </div>
      ${items.length ? `<table><thead><tr><th style="width:170px">名称</th><th style="width:70px">版本</th>
        <th>说明</th><th style="width:90px">状态</th><th style="width:200px"></th></tr></thead><tbody>
        ${items.map((p) => `<tr>
          <td><b>${esc(p.display_name || p.name)}</b>${p.builtin ? ' <span class="tag">内置</span>' : ""}
            <div class="mono" style="font-size:10.5px;color:var(--text-faint)">${esc(p.name)}</div></td>
          <td class="mono">${esc(p.version)}</td>
          <td style="font-size:12px;color:var(--text-soft)">${esc(p.description || "")}
            ${p.tool_count ? `<div style="font-size:11px;color:var(--text-dim);margin-top:3px">工具 ${p.tool_count} · 钩子 ${p.hook_count || 0}</div>` : ""}
            ${p.error ? `<div class="err-text" style="margin-top:3px">${esc(String(p.error).slice(0, 160))}</div>` : ""}</td>
          <td>${p.loaded ? '<span class="tag ok">已加载</span>' : '<span class="tag">未加载</span>'}
            ${p.enabled ? "" : '<span class="tag warn">已停用</span>'}</td>
          <td style="text-align:right;white-space:nowrap">
            ${p.enabled
              ? `<button class="btn sm ghost" data-off="${esc(p.name)}">停用</button>`
              : `<button class="btn sm ghost" data-on="${esc(p.name)}">启用</button>`}
            ${!p.builtin ? `<button class="btn sm ghost danger" data-del="${esc(p.name)}">卸载</button>` : ""}
          </td></tr>`).join("")}</tbody></table>`
        : `<div class="empty"><div class="big">${icon("puzzle", 34)}</div>还没有插件。点「新建插件」就能建一个。</div>`}
    </div>
    ${panels.length ? `<div class="card"><h3>插件面板</h3>
      <div class="row">${panels.map((p) => `<button class="btn" data-panel="${esc(p.url)}">${esc(p.title || p.id)}</button>`).join("")}</div>
    </div>` : ""}`;

  $("#plug-new").onclick = () => {
    modal("新建插件", `
      <div class="field"><label>插件名字</label>
        <input type="text" id="np-name" placeholder="例如：我的工具"></div>
      <div class="field" style="margin-top:10px"><label>它是干什么的<span class="hint">给自己看的说明</span></label>
        <input type="text" id="np-desc" placeholder="例如：查天气、发通知"></div>
      <div class="help" style="margin-top:10px">
        会建一个插件文件夹，里面有 manifest.yaml 和 plugin.py，改 plugin.py 就能加功能。
      </div>`,
      `<button class="btn" data-close>取消</button>
       <button class="btn primary" id="np-ok">创建</button>`);
    $("#np-ok").onclick = async () => {
      const name = $("#np-name").value.trim();
      if (!name) return toast("填个名字", "err");
      try {
        const r = await api("/api/plugins", { method: "POST", body: {
          action: "create", name, display_name: name,
          description: $("#np-desc").value.trim(),
        }});
        if (!r.ok) return toast(r.error || "创建失败", "err");
        closeModal();
        toast("已创建，改 plugin.py 加功能");
        PAGES.plugins();
      } catch (e) { toast(e.message, "err"); }
    };
  };
  $("#plug-install").onclick = () => {
    modal("从文件夹安装", `
      <div class="field"><label>插件目录的完整路径</label>
        <input type="text" id="pi-path" placeholder="D:\\my-plugin"></div>
      <div class="help" style="margin-top:9px">
        这个目录里要有 manifest.yaml。安装后会复制到插件目录。
      </div>`,
      `<button class="btn" data-close>取消</button>
       <button class="btn primary" id="pi-ok">安装</button>`);
    $("#pi-ok").onclick = async () => {
      const p = $("#pi-path").value.trim();
      if (!p) return toast("填个路径", "err");
      try {
        const r = await api("/api/plugins", { method: "POST", body: {
          action: "install", path: p, overwrite: true,
        }});
        if (r && r.ok === false) return toast(r.error || "安装失败", "err");
        closeModal(); toast("已安装"); PAGES.plugins();
      } catch (e) { toast(e.message, "err"); }
    };
  };
  $("#plug-reload").onclick = async () => {
    try { await api("/api/plugins", { method: "POST", body: { action: "reload" } });
      toast("已重新加载"); PAGES.plugins(); } catch (e) { toast(e.message, "err"); }
  };
  $$("[data-on]").forEach((b) => b.onclick = async () => {
    try { await api("/api/plugins", { method: "POST", body: { action: "enable", name: b.dataset.on } });
      toast("已启用", "ok"); PAGES.plugins(); } catch (e) { toast(e.message, "err"); }
  });
  $$("[data-off]").forEach((b) => b.onclick = async () => {
    await api("/api/plugins", { method: "POST", body: { action: "disable", name: b.dataset.off } });
    PAGES.plugins();
  });
  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm(`卸载插件「${b.dataset.del}」？目录会被删除。`)) return;
    try { await api("/api/plugins", { method: "POST", body: { action: "uninstall", name: b.dataset.del } });
      toast("已卸载", "ok"); PAGES.plugins(); } catch (e) { toast(e.message, "err"); }
  });
  $$("[data-panel]").forEach((b) => b.onclick = () => {
    modal("插件面板", `<iframe src="${esc(b.dataset.panel)}" style="width:100%;height:60vh;border:1px solid var(--border);border-radius:8px"></iframe>`);
  });
};

/* ==========================================================================
   工作流页
   ========================================================================== */
PAGES.workflows = async () => {
  const el = $("#page-workflows");
  const d = await api("/api/workflows");
  const list = d.workflows || [];
  el.innerHTML = `<div class="card"><h3>工作流（${list.length}）<span class="hint">把多步任务编成 DAG，同层并行执行</span></h3>
    ${list.length ? `<table><thead><tr><th style="width:200px">名称</th><th>说明</th><th style="width:80px">步骤</th>
      <th style="width:160px">更新时间</th><th style="width:180px"></th></tr></thead><tbody>
      ${list.map((w) => `<tr>
        <td><b>${esc(w.name)}</b></td>
        <td style="font-size:12px;color:var(--text-soft)">${esc(w.description || "")}</td>
        <td class="mono">${((w.dag || {}).steps || []).length}</td>
        <td style="color:var(--text-dim)">${ago(w.updated_at)}</td>
        <td style="text-align:right;white-space:nowrap">
          <button class="btn sm" data-run="${esc(w.id)}">运行</button>
          <button class="btn sm ghost" data-edit="${esc(w.id)}">编辑</button>
          <button class="btn sm ghost danger" data-del="${esc(w.id)}">删除</button>
        </td></tr>`).join("")}</tbody></table>`
      : `<div class="empty"><div class="big">${icon("flow", 34)}</div>还没有工作流。点右上角「示例」看看长什么样。</div>`}
  </div>`;
  $$("[data-run]").forEach((b) => b.onclick = async () => {
    b.textContent = "运行中…"; b.disabled = true;
    try {
      const r = await api("/api/workflows", { method: "POST", body: { action: "run", id: b.dataset.run } });
      modal("工作流结果", `<pre class="block">${esc(r.summary || "(无输出)")}</pre>
        <details style="margin-top:10px"><summary style="cursor:pointer;font-size:12.5px">各步骤详情</summary>
        <pre class="block" style="margin-top:8px">${esc(JSON.stringify(r.results || {}, null, 2))}</pre></details>`);
    } catch (e) { toast(e.message, "err"); }
    b.textContent = "运行"; b.disabled = false;
  });
  $$("[data-edit]").forEach((b) => b.onclick = async () => {
    const w = list.find((x) => x.id === b.dataset.edit);
    workflowEdit(w);
  });
  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("删除这个工作流？")) return;
    await api("/api/workflows", { method: "POST", body: { action: "delete", id: b.dataset.del } });
    PAGES.workflows();
  });
};
function workflowEdit(w) {
  const isNew = !w || !w.id;
  w = w || { name: "", description: "", dag: { steps: [] } };
  const dagText = JSON.stringify(w.dag || { steps: [] }, null, 2);
  modal(isNew ? "新建工作流" : "编辑工作流", `
    <div class="field"><label>名称</label><input type="text" id="wf-name" value="${esc(w.name || "")}"></div>
    <div class="field"><label>说明</label><input type="text" id="wf-desc" value="${esc(w.description || "")}"></div>
    <div class="field"><label>DAG 定义（JSON）</label>
      <textarea id="wf-dag" class="mono" rows="14">${esc(dagText)}</textarea>
      <div class="help">每个 step 需要 id 与 kind（prompt/agent/tool/shell/merge）；用 depends_on 声明依赖；
        在 prompt 里用 {{step_id}} 引用上游输出</div></div>
    <div class="field"><label>试运行前先校验</label>
      <button class="btn sm" id="wf-val">校验 DAG</button> <span id="wf-val-result" style="font-size:12px"></span></div>`,
    `<button class="btn" data-close>取消</button><button class="btn primary" id="wf-save">保存</button>`);
  $("#wf-val").onclick = async () => {
    let dag;
    try { dag = JSON.parse($("#wf-dag").value); } catch (e) { return $("#wf-val-result").innerHTML = `<span class="err-text">JSON 语法错误：${esc(e.message)}</span>`; }
    try {
      const r = await api("/api/workflows", { method: "POST", body: { action: "validate", dag } });
      $("#wf-val-result").innerHTML = r.ok ? `<span style="color:var(--success)">校验通过（${r.steps} 个步骤）</span>`
        : `<span class="err-text">${esc(r.error)}</span>`;
    } catch (e) { $("#wf-val-result").innerHTML = `<span class="err-text">${esc(e.message)}</span>`; }
  };
  $("#wf-save").onclick = async () => {
    let dag;
    try { dag = JSON.parse($("#wf-dag").value); } catch (e) { return toast("DAG JSON 语法错误", "err"); }
    try {
      await api("/api/workflows", { method: "POST", body: {
        action: "save", id: w.id, name: $("#wf-name").value.trim() || "未命名流程",
        description: $("#wf-desc").value.trim(), dag,
      }});
      closeModal(); toast("已保存", "ok"); PAGES.workflows();
    } catch (e) { toast(e.message, "err"); }
  };
}

/* ==========================================================================
   定时任务页
   ========================================================================== */
PAGES.jobs = async () => {
  const el = $("#page-jobs");
  const d = await api("/api/jobs");
  const jobs = d.jobs || [];
  el.innerHTML = `
    <div class="card"><h3>调度器${d.enabled ? '<span class="tag ok">已启用</span>' : '<span class="tag warn">未启用</span>'}
      <span class="hint">在设置里可开启调度器；任务到点后自动执行</span></h3></div>
    <div class="card"><h3>定时任务（${jobs.length}）</h3>
    ${jobs.length ? `<table><thead><tr><th style="width:170px">名称</th><th style="width:150px">调度</th>
      <th style="width:70px">类型</th><th style="width:140px">上次运行</th><th>结果</th><th style="width:170px"></th></tr></thead><tbody>
      ${jobs.map((j) => `<tr>
        <td><b>${esc(j.name)}</b>${j.enabled ? "" : ' <span class="tag warn">已停用</span>'}</td>
        <td style="font-size:12px">${esc(j.schedule || j.cron || "—")}
          <div class="hint" style="color:var(--text-faint)">下次：${esc(j.next_run_text || "—")}</div></td>
        <td><span class="tag">${esc(j.kind)}</span></td>
        <td style="color:var(--text-dim);font-size:12px">${j.last_run ? ago(j.last_run) : "从未"}</td>
        <td style="font-size:12px">${j.last_status ? `<span class="tag ${j.last_status === "ok" ? "ok" : "err"}">${esc(j.last_status)}</span>` : "—"}</td>
        <td style="text-align:right;white-space:nowrap">
          <button class="btn sm" data-run="${esc(j.id)}">立即运行</button>
          <button class="btn sm ghost" data-edit="${esc(j.id)}">编辑</button>
          <button class="btn sm ghost danger" data-del="${esc(j.id)}">删除</button>
        </td></tr>`).join("")}</tbody></table>`
      : `<div class="empty"><div class="big">${icon("clock", 34)}</div>还没有定时任务。到点自动跑的那种。</div>`}
    </div>`;
  $$("[data-run]").forEach((b) => b.onclick = async () => {
    b.textContent = "运行中…"; b.disabled = true;
    try { const r = await api("/api/jobs", { method: "POST", body: { action: "run_now", id: b.dataset.run } });
      toast(r.ok ? "已执行" : (r.error || "失败"), r.ok ? "ok" : "err"); }
    catch (e) { toast(e.message, "err"); }
    PAGES.jobs();
  });
  $$("[data-edit]").forEach((b) => b.onclick = () => {
    const j = jobs.find((x) => x.id === b.dataset.edit); jobEdit(j);
  });
  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm("删除这个任务？")) return;
    await api("/api/jobs", { method: "POST", body: { action: "delete", id: b.dataset.del } });
    PAGES.jobs();
  });
};
function jobEdit(j) {
  const isNew = !j || !j.id;
  j = j || { name: "", cron: "0 3 * * *", kind: "prompt", payload: { prompt: "" }, enabled: true };
  modal(isNew ? "新建定时任务" : "编辑定时任务", `
    <div class="field"><label>名称</label><input type="text" id="j-name" value="${esc(j.name || "")}"></div>
    <div class="grid c2">
      <div class="field"><label>cron 表达式（5 段）</label>
        <input type="text" id="j-cron" class="mono" value="${esc(j.cron || "")}" placeholder="0 3 * * *">
        <div class="help">分 时 日 月 周。例：<code>0 3 * * *</code> 每天 3 点；<code>*/30 * * * *</code> 每 30 分钟</div></div>
      <div class="field"><label>或固定间隔（秒）</label>
        <input type="number" id="j-interval" value="${j.interval_seconds || 0}">
        <div class="help">填了就优先按间隔执行（0 表示不用）</div></div>
    </div>
    <div class="grid c2">
      <div class="field"><label>时间窗口开始（HH:MM，可留空）</label>
        <input type="text" id="j-wstart" class="mono" value="${esc(((j.payload || {})._window || {}).start || "")}" placeholder="09:00">
        <div class="help">只在此时段内触发；跨零点会自动识别（如 22:00-06:00）</div></div>
      <div class="field"><label>时间窗口结束（HH:MM，可留空）</label>
        <input type="text" id="j-wend" class="mono" value="${esc(((j.payload || {})._window || {}).end || "")}" placeholder="18:00">
        <div class="help">两端都留空 = 不限制时段</div></div>
    </div>
    <div class="field"><label>任务类型</label><select id="j-kind">
      <option value="prompt"${j.kind === "prompt" ? " selected" : ""}>prompt（让 Agent 执行一段话）</option>
      <option value="tool"${j.kind === "tool" ? " selected" : ""}>tool（调用某个工具）</option>
      <option value="shell"${j.kind === "shell" ? " selected" : ""}>shell（执行命令）</option>
      <option value="workflow"${j.kind === "workflow" ? " selected" : ""}>workflow（运行工作流）</option>
    </select></div>
    <div class="field"><label>载荷（JSON）</label>
      <textarea id="j-payload" class="mono" rows="4">${esc(JSON.stringify(j.payload || {}, null, 2))}</textarea>
      <div class="help">prompt: {"prompt":"…"}　tool: {"tool":"名称","arguments":{}}　shell: {"command":"…"}　workflow: {"workflow_id":"…"}</div></div>
    <label class="switch"><input type="checkbox" id="j-enabled"${j.enabled ? " checked" : ""}> 启用</label>`,
    `<button class="btn" data-close>取消</button><button class="btn primary" id="j-save">保存</button>`);
  $("#j-save").onclick = async () => {
    let payload;
    try { payload = JSON.parse($("#j-payload").value || "{}"); } catch (e) { return toast("载荷 JSON 语法错误", "err"); }
    try {
      await api("/api/jobs", { method: "POST", body: {
        action: "save", id: j.id, name: $("#j-name").value.trim() || "未命名任务",
        cron: $("#j-cron").value.trim(), interval_seconds: Number($("#j-interval").value) || 0,
        window_start: ($("#j-wstart") || {}).value || "",
        window_end: ($("#j-wend") || {}).value || "",
        kind: $("#j-kind").value, payload, enabled: $("#j-enabled").checked,
      }});
      closeModal(); toast("已保存", "ok"); PAGES.jobs();
    } catch (e) { toast(e.message, "err"); }
  };
}

/* ==========================================================================
   远程主机页
   ========================================================================== */
PAGES.remote = async () => {
  const el = $("#page-remote");
  const d = await api("/api/remote");
  const hosts = d.hosts || [];
  el.innerHTML = `<div class="card"><h3>远程主机（${hosts.length}）
    <span class="hint">配置后我可以在服务器上执行命令、看日志、传文件</span></h3>
    <div class="row" style="margin-bottom:12px">
      <button class="btn primary" id="remote-add">添加主机</button>
      <button class="btn" id="remote-refresh">刷新</button>
    </div>
    ${hosts.length ? `<table><thead><tr><th style="width:150px">名称</th><th>地址</th><th style="width:100px">用户</th>
      <th style="width:100px">认证</th><th style="width:150px"></th></tr></thead><tbody>
      ${hosts.map((h) => `<tr>
        <td><b>${esc(h.name)}</b></td>
        <td class="mono">${esc(h.host)}:${h.port}</td>
        <td class="mono">${esc(h.user)}</td>
        <td>${h.has_password ? '<span class="tag ok">有密码</span>' : (h.key_file ? '<span class="tag ok">密钥</span>' : '<span class="tag warn">无认证</span>')}</td>
        <td style="text-align:right;white-space:nowrap">
          <button class="btn sm ghost" data-test="${esc(h.name)}">测试连接</button>
          <button class="btn sm ghost danger" data-del="${esc(h.name)}">删除</button>
        </td></tr>`).join("")}</tbody></table>`
      : `<div class="empty"><div class="big">${icon("server", 34)}</div>还没有远程主机</div>`}
  </div>`;
  $$("[data-test]").forEach((b) => b.onclick = async () => {
    b.textContent = "连接中…"; b.disabled = true;
    try { const r = await api("/api/remote", { method: "POST", body: { action: "test", name: b.dataset.test } });
      toast(r.ok ? `连接成功（${r.duration}s）` : "连接失败：" + (r.error || ""), r.ok ? "ok" : "err"); }
    catch (e) { toast(e.message, "err"); }
    b.textContent = "测试连接"; b.disabled = false;
  });
  $$("[data-del]").forEach((b) => b.onclick = async () => {
    if (!confirm(`删除主机「${b.dataset.del}」？`)) return;
    await api("/api/remote", { method: "POST", body: { action: "delete", name: b.dataset.del } });
    PAGES.remote();
  });
  onClick("#remote-add", () => remoteEdit(null));
  onClick("#remote-refresh", () => PAGES.remote());
};
function remoteEdit() {
  modal("添加远程主机", `
    <div class="grid c2">
      <div class="field"><label>名称</label><input type="text" id="r-name" placeholder="如 生产服务器"></div>
      <div class="field"><label>地址</label><input type="text" id="r-host" placeholder="IP 或域名"></div>
    </div>
    <div class="grid c3">
      <div class="field"><label>端口</label><input type="number" id="r-port" value="22"></div>
      <div class="field"><label>用户名</label><input type="text" id="r-user" value="root"></div>
      <div class="field"><label>默认目录</label><input type="text" id="r-ws" value="~"></div>
    </div>
    <div class="field"><label>密码</label><input type="password" id="r-pass">
      <div class="help">会明文存在本地 config.toml；也可以只填环境变量名，运行时从环境解析</div></div>
    <div class="field"><label>或密码环境变量名</label><input type="text" id="r-passenv" placeholder="如 MY_SSH_PASSWORD"></div>
    <div class="field"><label>或私钥文件</label><input type="text" id="r-key" placeholder="C:\\Users\\...\\.ssh\\id_rsa"></div>`,
    `<button class="btn" data-close>取消</button><button class="btn primary" id="r-save">添加</button>`);
  $("#r-save").onclick = async () => {
    const host = $("#r-host").value.trim();
    if (!host) return toast("地址不能为空", "err");
    try {
      await api("/api/remote", { method: "POST", body: {
        action: "add", name: $("#r-name").value.trim() || host, host,
        port: Number($("#r-port").value) || 22, user: $("#r-user").value.trim() || "root",
        password: $("#r-pass").value || null, password_env: $("#r-passenv").value.trim() || null,
        key_file: $("#r-key").value.trim() || null, workspace: $("#r-ws").value.trim() || "~",
      }});
      closeModal(); toast("已添加", "ok"); PAGES.remote();
    } catch (e) { toast(e.message, "err"); }
  };
}

/* ==========================================================================
   用量统计页
   ========================================================================== */
/* ==========================================================================
   关于页
   ========================================================================== */
PAGES.about = async () => {
  const el = $("#page-about");
  if (!el) return;
  let d = {};
  try { d = await api("/api/status"); } catch (e) {}
  const cfg = (S.boot && S.boot.config) || {};
  const paths = d.paths || {};
  el.innerHTML = `
    <div class="card">
      <h3>当前版本<span class="hint">${esc(d.version || "—")}</span></h3>
      <div class="set-row">
        <div class="sr-main">
          <div class="sr-title">构建身份</div>
          <div class="sr-desc">本地构建，数据全部保存在本机</div>
        </div>
        <div class="sr-ctl"><span class="tag ok">${esc(d.build || "local")}</span></div>
      </div>
      <div class="set-row">
        <div class="sr-main">
          <div class="sr-title">运行状态</div>
          <div class="sr-desc">服务地址与本机环境</div>
        </div>
        <div class="sr-ctl mono" style="font-size:11.5px;color:var(--text-dim)">${esc(d.base_url || location.origin)}</div>
      </div>
      <div class="set-row">
        <div class="sr-main">
          <div class="sr-title">检查更新</div>
          <div class="sr-desc">前往 GitHub 查看最新发布</div>
        </div>
        <div class="sr-ctl">
          <a class="btn sm" href="https://github.com/imcreazy/Fengcode" target="_blank" rel="noopener">在网页查看</a>
        </div>
      </div>
    </div>
    <div class="card">
      <h3>帮助与反馈</h3>
      <div class="set-row">
        <div class="sr-main">
          <div class="sr-title">提交问题</div>
          <div class="sr-desc">报告问题或提建议；请勿提交密钥、对话内容或私人路径</div>
        </div>
        <div class="sr-ctl">
          <a class="btn sm" href="https://github.com/imcreazy/Fengcode/issues" target="_blank" rel="noopener">提交问题</a>
        </div>
      </div>
    </div>
    <div class="card">
      <h3>数据位置<span class="hint">全部在本机</span></h3>
      ${[
        ["配置文件", paths.config || "~/.fengcode/config.toml"],
        ["数据库", paths.data || "~/.fengcode/data"],
        ["日志", paths.logs || "~/.fengcode/logs"],
        ["工作区", d.workspace || paths.workspace || ""],
      ].map(([k, v]) => `
        <div class="set-row">
          <div class="sr-main"><div class="sr-title">${esc(k)}</div></div>
          <div class="sr-ctl mono" style="font-size:11.5px;color:var(--text-dim);max-width:60%;overflow:hidden;text-overflow:ellipsis;white-space:nowrap"
               title="${esc(v || "")}">${esc(v || "—")}</div>
        </div>`).join("")}
    </div>`;
};

PAGES.stats = async () => {
  const el = $("#page-stats");
  const d = await api("/api/stats");
  const al = d.all || {}, td = d.today || {};
  const byDay = d.by_day || [];
  const maxTok = Math.max(1, ...byDay.map((x) => x.total_tokens));
  const models = d.by_model || [];
  const maxCost = Math.max(0.000001, ...models.map((m) => m.cost));
  el.innerHTML = `
    <div class="grid c3" style="margin-bottom:14px">
      <div class="stat"><div class="k">今日调用</div><div class="v">${td.calls || 0}</div>
        <div class="s">${fmtNum(td.total_tokens || 0)} tokens</div></div>
      <div class="stat"><div class="k">今日费用</div><div class="v">${(td.cost || 0).toFixed(4)}</div>
        <div class="s">累计 ${(al.cost || 0).toFixed(4)}</div></div>
      <div class="stat"><div class="k">累计 tokens</div><div class="v">${fmtNum(al.total_tokens || 0)}</div>
        <div class="s">输入 ${fmtNum(al.prompt_tokens || 0)} · 输出 ${fmtNum(al.output_tokens || 0)}</div></div>
      <div class="stat"><div class="k">缓存命中</div><div class="v">${fmtNum(al.cached_tokens || 0)}</div>
        <div class="s">省下的输入费用</div></div>
    </div>
    <div class="grid c2">
      <div class="card"><h3>近 14 天用量</h3>
        <div class="bars">${byDay.map((x) => `<div class="b" style="height:${Math.max(2, (x.total_tokens / maxTok) * 82)}px"
          title="${x.date}：${fmtNum(x.total_tokens)} tokens / ${x.calls} 次"></div>`).join("")}</div>
        <div class="row" style="justify-content:space-between;font-size:10.5px;color:var(--text-faint);margin-top:6px">
          <span>${byDay.length ? byDay[0].date.slice(5) : ""}</span>
          <span>${byDay.length ? byDay[byDay.length - 1].date.slice(5) : ""}</span></div>
      </div>
      <div class="card"><h3>按模型（近 30 天）</h3>
        ${models.length ? models.slice(0, 10).map((m) => `
          <div style="margin-bottom:9px">
            <div class="row" style="font-size:12px;margin-bottom:3px">
              <span class="mono">${esc(m.model)}</span>
              <span class="spacer"></span>
              <span style="color:var(--text-dim)">${m.calls} 次 · ${fmtNum(m.total_tokens)} tok · ${m.cost.toFixed(4)}</span>
            </div>
            <div class="hbar"><i style="width:${Math.max(2, (m.cost / maxCost) * 100)}%"></i></div>
          </div>`).join("") : `<div class="empty">还没有用量数据</div>`}
      </div>
    </div>
    ${(() => {
      // ★ 用量统计面板：来源分布 / 热力图 / 趋势对比。
      const kinds = d.by_kind || [];
      const hm = d.heatmap || { days: [] };
      const tr = d.trend || {};
      const ch = (tr.change_pct || {});
      const cls = (v) => (v == null ? "" : (v > 0 ? "warn" : "ok"));
      const txt = (v) => (v == null ? "—" : (v > 0 ? `+${v}%` : `${v}%`));
      const KIND_LABEL = { chat: "对话", subagent: "子代理", compact: "压缩摘要",
                           scheduler: "定时任务", job: "定时任务", test: "测试" };
      const maxKC = Math.max(0.000001, ...kinds.map((k) => k.cost));
      const cells = (hm.days || []).map((x) =>
        `<span class="hm-cell lv${x.level}" title="${esc(x.date)}：${fmtNum(x.total_tokens)} tokens / ${x.calls} 次"></span>`).join("");
      return `
      <div class="grid c3" style="margin-bottom:14px">
        <div class="stat"><div class="k">近 ${tr.days || 14} 天 tokens</div>
          <div class="v">${fmtNum((tr.current || {}).total_tokens || 0)}</div>
          <div class="s"><span class="tag ${cls(ch.total_tokens)}">环比 ${txt(ch.total_tokens)}</span></div></div>
        <div class="stat"><div class="k">近 ${tr.days || 14} 天费用</div>
          <div class="v">${((tr.current || {}).cost || 0).toFixed(4)}</div>
          <div class="s"><span class="tag ${cls(ch.cost)}">环比 ${txt(ch.cost)}</span></div></div>
        <div class="stat"><div class="k">近 ${tr.days || 14} 天请求</div>
          <div class="v">${(tr.current || {}).calls || 0}</div>
          <div class="s"><span class="tag ${cls(ch.calls)}">环比 ${txt(ch.calls)}</span></div></div>
      </div>
      <div class="card"><h3>用量热力图<span class="hint">每格一天，颜色越深用量越大（近 ${(hm.days || []).length} 天）</span></h3>
        <div class="hm">${cells || '<div class="empty">还没有数据</div>'}</div>
        <div class="row hm-legend"><span>少</span><span class="hm-cell lv0"></span>
          <span class="hm-cell lv1"></span><span class="hm-cell lv2"></span>
          <span class="hm-cell lv3"></span><span class="hm-cell lv4"></span><span>多</span>
          <span class="spacer"></span><span>合计 ${(hm.total_calls || 0)} 次调用</span></div>
      </div>
      <div class="card"><h3>按来源（近 30 天）<span class="hint">这些钱花在哪了</span></h3>
        ${kinds.length ? kinds.map((k) => `
          <div style="margin-bottom:9px">
            <div class="row" style="font-size:12px;margin-bottom:3px">
              <span>${esc(KIND_LABEL[k.kind] || k.kind)}</span>
              <span class="spacer"></span>
              <span style="color:var(--text-dim)">${k.calls} 次 · ${fmtNum(k.total_tokens)} tok · ${k.cost.toFixed(4)}</span>
            </div>
            <div class="hbar"><i style="width:${Math.max(2, (k.cost / maxKC) * 100)}%"></i></div>
          </div>`).join("") : `<div class="empty">还没有用量数据</div>`}
      </div>`;
    })()}
    <div class="card"><h3>最近调用<span class="hint">包含每次请求的耗时与结果</span></h3>
      <div id="recent-usage"></div></div>`;
  try {
    const r = await api("/api/stats?recent=1&limit=40");
    const rows = r.recent || [];
    $("#recent-usage").innerHTML = rows.length ? `<table><thead><tr><th>时间</th><th>模型</th>
      <th>输入</th><th>输出</th><th>费用</th><th>耗时</th><th>结果</th></tr></thead><tbody>
      ${rows.map((x) => `<tr>
        <td style="color:var(--text-dim)">${ago(x.ts)}</td>
        <td class="mono" style="font-size:11px">${esc(x.model)}</td>
        <td class="mono">${fmtNum(x.prompt_tokens)}</td>
        <td class="mono">${fmtNum(x.output_tokens)}</td>
        <td class="mono">${(x.cost || 0).toFixed(5)}</td>
        <td class="mono">${fmtDur(x.duration)}</td>
        <td>${x.error ? `<span class="tag err">失败</span>` : '<span class="tag ok">成功</span>'}</td>
      </tr>`).join("")}</tbody></table>` : `<div class="empty">还没有调用记录</div>`;
  } catch (e) { $("#recent-usage").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
};

/* ==========================================================================
   审计日志页
   ========================================================================== */
PAGES.audit = async () => {
  const el = $("#page-audit");
  const d = await api("/api/stats?audit=1&limit=200");
  const rows = d.audit || [];
  el.innerHTML = `<div class="card"><h3>审计日志（最近 ${rows.length} 条）
    <span class="hint">记录每一次工具执行与审批决定；密钥已自动脱敏</span></h3>
    <div class="row" style="margin-bottom:12px">
      <button class="btn" id="audit-refresh">刷新</button>
      <span class="spacer"></span>
      <span class="hint">最多显示最近 200 条</span>
    </div>
    ${rows.length ? `<table><thead><tr><th style="width:130px">时间</th><th style="width:130px">工具</th>
      <th>目标</th><th style="width:80px">结果</th><th>说明</th></tr></thead><tbody>
      ${rows.map((x) => `<tr>
        <td style="color:var(--text-dim)">${ago(x.ts)}</td>
        <td class="mono">${esc(x.action)}</td>
        <td class="mono" style="font-size:11px;max-width:320px;word-break:break-all">${esc(String(x.target || "").slice(0, 200))}</td>
        <td>${x.ok ? '<span class="tag ok">成功</span>' : '<span class="tag err">失败</span>'}
          ${x.decision === "deny" ? '<span class="tag warn">被拒</span>' : ""}</td>
        <td style="font-size:11.5px;color:var(--text-soft)">${esc(String(x.reason || "").slice(0, 160))}</td>
      </tr>`).join("")}</tbody></table>` : `<div class="empty">还没有审计记录</div>`}
  </div>`;
  onClick("#audit-refresh", () => PAGES.audit());
};

/* ==========================================================================
   设置页
   ========================================================================== */
PAGES.settings = async () => {
  // 渲染目标：
  //  - 设置中心「搬真节点」模式：渲染到 #settings-render（与设置中心容器平级，
  //    不会把 #settings-body 覆盖掉），再由 openSetting 取走目标面板。
  //  - 其他情况：优先 #ss-host，退回 #settings-render。
  const el = PAGES.settings._toPage
    ? ($("#settings-render") || $("#page-settings"))
    : ($("#ss-host") || $("#settings-render") || $("#page-settings"));
  const d = await api("/api/bootstrap");
  const cfg = await api("/api/config");
  S.boot = d;
  const ui = cfg.ui || {};
  const perms = cfg.permissions || {};
  const mem = cfg.memory || {};
  const agent = cfg.agent || {};
  // ★ providers 必须用 /api/providers（它才算 has_key）；/api/config 的 providers 没有 has_key 字段
  let providers = [];
  try {
    const pv = await api("/api/providers");
    providers = pv.providers || [];
  } catch (e) {
    providers = cfg.providers || [];
  }

  el.innerHTML = `
    <div class="tabs" id="set-tabs">
      <button class="tab active" data-t="model">模型</button>
      <button class="tab" data-t="agent">回答方式</button>
      <button class="tab" data-t="safety">权限</button>
      <button class="tab" data-t="memory">记忆</button>
      <button class="tab" data-t="ui">外观</button>
      <button class="tab" data-t="adv">高级</button>
    </div>
    <div id="set-model" class="set-pane">
      <div class="card"><h3>当前用哪个模型</h3>
        <div class="field"><label>默认模型</label>
          <select id="s-model">${(d.models || []).length
            ? (d.models || []).map((m) =>
              `<option value="${esc(m.ref)}"${m.ref === d.default_model ? " selected" : ""}>
                ${esc(m.provider_display)} / ${esc(m.model)}${m.has_key ? "" : "（还没填密钥）"}</option>`).join("")
            : `<option value="">还没有可用的模型</option>`}</select></div>
        <div class="help" style="margin-top:8px">
          还没有模型？在下面填一个密钥就能用了。
        </div>
      </div>
      <div class="card"><h3>模型服务（${providers.length}）<span class="hint">填好地址与密钥即可用</span></h3>
        <div id="prov-list"></div>
        <div class="row" style="margin-top:12px">
          <button class="btn primary" id="prov-add">添加供应商</button>
        </div>
      </div>
    </div>
    <div id="set-agent" class="set-pane" style="display:none">
      <div class="card"><h3>模型分配</h3>
        <div class="help" style="margin-bottom:10px">不同用途可以指定不同模型；留空则跟随默认模型。</div>
        <div class="field"><label>默认模型</label>
          <select id="s-model2">${modelOpts(d.models, d.default_model, "跟随默认")}</select></div>
        <div class="field"><label>独立规划模型<span class="hint">用于拆解任务计划</span></label>
          <select id="a-planner">${modelOpts(d.models, agent.planner_model, "跟随主代理模型")}</select></div>
        <div class="field"><label>图片理解<span class="hint">读图 / 截图理解</span></label>
          <select id="a-vision">${modelOpts(d.models, agent.vision_model, "自动选择")}</select></div>
        <div class="field"><label>网页搜索<span class="hint">联网检索总结</span></label>
          <select id="a-search">${modelOpts(d.models, agent.search_model, "随会话自动选择")}</select></div>
        <div class="field"><label>子代理模型</label>
          <select id="a-subagent">${modelOpts(d.models, agent.subagent_model, "跟随主代理模型")}</select></div>
        <div class="field" style="margin-left:18px"><label>子代理推理强度</label>
          <select id="a-subagent-effort">
            <option value=""${!agent.subagent_effort ? " selected" : ""}>继承默认</option>
            <option value="low"${agent.subagent_effort === "low" ? " selected" : ""}>低（快）</option>
            <option value="medium"${agent.subagent_effort === "medium" ? " selected" : ""}>中</option>
            <option value="high"${agent.subagent_effort === "high" ? " selected" : ""}>高（准）</option>
          </select></div>
      </div>

      <div class="card"><h3>运行偏好</h3>
        <div class="field"><label>思考语言<span class="hint">只影响可见思考过程</span></label>
          <div class="seg" id="a-lang-seg">
            <button class="seg-btn${agent.reasoning_language !== "en" ? " active" : ""}" data-v="zh">自动（中文）</button>
            <button class="seg-btn${agent.reasoning_language === "en" ? " active" : ""}" data-v="en">English</button>
          </div></div>
        <div class="field"><label>温度<span class="hint">0 更稳定，2 更发散；默认 0.6（编码场景推荐值）</span></label>
          <input type="number" id="a-temp" step="0.1" min="0" max="2" value="${agent.temperature != null ? agent.temperature : 0.6}"></div>
      </div>

      <div class="card"><h3>自动压缩阈值</h3>
        <div class="help" style="margin-bottom:8px">对话占满上下文时，较早内容会自动压缩为摘要。</div>
        <div class="seg" id="a-compact-seg">
          <button class="seg-btn${Math.abs((mem.compact_ratio||0.8)-0.7)<0.01 ? " active":""}" data-v="0.7">70%</button>
          <button class="seg-btn${Math.abs((mem.compact_ratio||0.8)-0.75)<0.01 ? " active":""}" data-v="0.75">75%</button>
          <button class="seg-btn${Math.abs((mem.compact_ratio||0.8)-0.8)<0.01 ? " active":""}" data-v="0.8">80%（推荐）</button>
          <button class="seg-btn${Math.abs((mem.compact_ratio||0.8)-0.85)<0.01 ? " active":""}" data-v="0.85">85%</button>
        </div>
        <div class="field" style="margin-top:10px"><label>自定义百分比</label>
          <input type="number" id="a-compact" min="30" max="95" value="${Math.round((mem.compact_ratio || 0.8) * 100)}"> %</div>
      </div>

      <div class="card"><h3>行为</h3>
        <label class="switch"><input type="checkbox" id="a-reflect"${agent.reflection ? " checked" : ""}>
          启用反思（每隔几步检查方向）</label><br>
        <label class="switch"><input type="checkbox" id="a-checklist"${agent.submit_checklist !== false ? " checked" : ""}>
          收尾前自检（核对改动、清理临时文件、确认测试）</label>
        <label class="switch" style="margin-top:8px"><input type="checkbox" id="sa-on"${(agent.subagents || {}).enabled !== false ? " checked" : ""}>
          允许派生子智能体（并行干活）</label>
      </div>

      <div class="card"><h3>自定义指令<span class="hint">会追加到系统提示末尾，优先级最高</span></h3>
        <textarea id="a-extra" rows="4" placeholder="例如：回答尽量简短；代码用中文注释">${esc(agent.system_prompt_extra || "")}</textarea></div>
    </div>
    <!-- ★ 记忆面板。此前导航里有「记忆」入口，但 HTML 从未定义 #set-memory，
         点击后走到 app.js 的「未找到「记忆」面板」分支（实测）。
         这里补齐面板本体，字段与 MemoryConfig 对应，保存走 saveSettingsNow 的 memory 段。 -->
    <div id="set-memory" class="set-pane" style="display:none">
      <div class="card"><h3>记忆总开关</h3>
        <label class="switch"><input type="checkbox" id="m-on"${mem.enabled !== false ? " checked" : ""}>
          启用记忆（跨会话记住你告诉过它的事）</label>
        <div class="help" style="margin-top:8px">
          关闭后不再写入新记忆，已有记忆仍可在下方查看与删除。
        </div>
      </div>

      <div class="card"><h3>分层记忆</h3>
        <label class="switch"><input type="checkbox" id="m-long"${mem.long_term_enabled !== false ? " checked" : ""}>
          长期记忆<span class="hint">稳定事实与偏好，长期保留</span></label><br>
        <label class="switch"><input type="checkbox" id="m-epi"${mem.episodic_enabled !== false ? " checked" : ""}>
          情景记忆<span class="hint">「某次发生了什么」</span></label><br>
        <label class="switch"><input type="checkbox" id="m-prof"${mem.profile_enabled !== false ? " checked" : ""}>
          用户画像<span class="hint">从对话中归纳你的习惯</span></label>
        <label class="switch" style="margin-top:8px"><input type="checkbox" id="m-auto"${mem.auto_summarize !== false ? " checked" : ""}>
          自动归纳<span class="hint">闲置时把本轮要点提炼成记忆</span></label>
      </div>

      <div class="card"><h3>召回</h3>
        <div class="grid c3">
          <div class="field"><label>短期记忆轮数</label>
            <input type="number" id="m-turns" min="0" max="500" value="${mem.short_term_max_turns != null ? mem.short_term_max_turns : 60}"></div>
          <div class="field"><label>每次召回条数</label>
            <input type="number" id="m-top" min="1" max="50" value="${mem.recall_top_k != null ? mem.recall_top_k : 6}"></div>
          <div class="field"><label>召回最低分<span class="hint">0~1，越高越严格</span></label>
            <input type="number" id="m-minscore" step="0.01" min="0" max="1" value="${mem.recall_min_score != null ? mem.recall_min_score : 0.22}"></div>
        </div>
        <label class="switch" style="margin-top:8px"><input type="checkbox" id="m-vec"${mem.use_vector !== false ? " checked" : ""}>
          向量检索（密钥不可用时自动降级为本地检索）</label>
        <div class="field" style="margin-top:10px"><label>遗忘半衰期<span class="hint">天数，越久越不容易被淡忘</span></label>
          <input type="number" id="m-decay" min="1" max="3650" value="${mem.decay_half_life_days != null ? mem.decay_half_life_days : 45}"></div>
      </div>

      <div class="card"><h3>记忆条目<span class="hint" id="m-count"></span></h3>
        <div class="help" style="margin-bottom:9px">
          这里是 AI 实际保存下来的长期记忆，跨对话生效。可以搜索、查看修订历史、删除。
        </div>
        <div class="row" style="margin-bottom:9px">
          <input type="text" id="m-search" placeholder="搜索记忆…" style="flex:1">
          <button class="btn" id="m-reload">刷新</button>
          <button class="btn ghost" id="m-reindex">重建索引</button>
        </div>
        <div id="m-list" class="help">正在读取…</div>
      </div>
    </div>
    <div id="set-safety" class="set-pane" style="display:none">
      <div class="card"><h3>权限等级</h3>
        <!-- ★ 1-G：规则写法有问题时的告警容器（写了却永远匹配不到 = 安全规则静默失效） -->
        <div id="perm-warn" class="perm-warn" hidden></div>
        <div class="field"><label>危险操作如何处理</label><select id="p-mode">
          <option value="deny"${perms.mode === "deny" ? " selected" : ""}>只读 —— 拒绝一切写操作</option>
          <option value="ask"${perms.mode === "ask" ? " selected" : ""}>工作区可改 —— 工作区内直接改，越界需批准（推荐）</option>
          <option value="allow"${perms.mode === "allow" ? " selected" : ""}>完全权限 —— 全部放行，不再询问</option>
        </select></div>
        <div class="help" style="margin-top:8px">
          也可在对话输入框旁随时切换。
        </div>
        <label class="switch" style="margin-top:10px"><input type="checkbox" id="p-audit"${perms.audit_enabled !== false ? " checked" : ""}>
          记录审计日志</label>
      </div>
      <div class="card"><h3>写路径白名单<span class="hint">每行一个</span></h3>
        <div class="help" style="margin-bottom:7px">支持 $WORKSPACE（当前工作区）、$HOME（用户目录）、$TMP（临时目录）</div>
        <label style="font-size:12.5px;color:var(--text-dim)">允许写入</label>
        <textarea id="p-write" class="mono" rows="3">${esc((perms.write_paths || []).join("\n"))}</textarea>
        <label style="font-size:12.5px;color:var(--text-dim);display:block;margin-top:9px">允许读取</label>
        <textarea id="p-read" class="mono" rows="3">${esc((perms.read_paths || []).join("\n"))}</textarea>
        <label style="font-size:12.5px;color:var(--text-dim);display:block;margin-top:9px">禁止访问（通配符）</label>
        <textarea id="p-deny" class="mono" rows="3">${esc((perms.deny_patterns || []).join("\n"))}</textarea>
      </div>
      <div class="card"><h3>执行命令的限制<span class="hint">Shell 与运行环境</span></h3>
        <div class="field" style="margin-bottom:12px">
          <label>Shell 解释器</label>
          <select id="sb-shell">
            <option value="auto"${(cfg.sandbox || {}).bash === "auto" || !(cfg.sandbox || {}).bash ? " selected" : ""}>自动（优先 PowerShell 7）</option>
            <option value="on"${(cfg.sandbox || {}).bash === "on" ? " selected" : ""}>总是用 bash</option>
            <option value="off"${(cfg.sandbox || {}).bash === "off" ? " selected" : ""}>不用 bash</option>
          </select>
          <div class="help" id="sb-current">当前使用：检测中…</div>
        </div>
        <div style="border:1px solid var(--border);border-radius:9px;overflow:hidden;margin-bottom:12px">
          <div class="row" style="padding:8px 12px;background:var(--bg);font-size:11.5px;color:var(--text-dim)">
            <span style="flex:1">环境</span><span style="width:110px">状态</span>
            <span class="spacer"></span><span>路径</span>
          </div>
          <div id="sb-probe"><div class="help" style="padding:10px 12px">检测中…</div></div>
        </div>
        <div class="grid c2">
          <div class="field"><label>命令最多跑多久（秒）</label><input type="number" id="s-timeout" value="${(cfg.sandbox || {}).timeout_seconds || 120}"></div>
          <div class="field"><label>命令输出最多留多少字</label><input type="number" id="s-maxout" value="${(cfg.tools || {}).max_output_chars || 60000}"></div>
        </div>
        <label class="switch" style="margin-top:9px"><input type="checkbox" id="s-net"${(cfg.sandbox || {}).network !== false ? " checked" : ""}>
          允许它联网</label>
      </div>
      <div class="card"><h3>文件工具写入范围<span class="hint">仅约束文件工具，不限制 Shell</span></h3>
        <div class="field">
          <label>当前允许写入</label>
          <div class="mono" style="font-size:11.5px;color:var(--text-dim);padding:6px 9px;background:var(--bg);border-radius:7px;border:1px solid var(--border)">
            ${esc((cfg.agent || {}).workspace_override || "（使用当前工作区）")}
          </div>
          <div class="help">工作区本身始终可写；额外目录在下面的白名单里加。</div>
        </div>
      </div>
      <div class="card"><h3>细粒度规则<span class="hint">优先级 deny &gt; ask &gt; allow</span></h3>
        <div class="help" style="margin-bottom:9px">
          格式：<code>tool:工具名</code>、<code>cmd:命令前缀</code>（如 <code>cmd:git *</code>）、
          <code>path:路径通配</code>（如 <code>path:**/.ssh/*</code>）、<code>risk:关键词</code>。
          匹配到的操作按对应列处理。
        </div>
        <div class="grid c3">
          <div class="field">
            <label style="color:var(--danger,#d33)">阻止</label>
            <div class="help" style="margin-bottom:5px">匹配即拒绝，不再询问</div>
            <div id="rule-deny" class="rule-list"></div>
            <div class="row" style="margin-top:6px">
              <input class="mono" id="rule-deny-in" placeholder="tool:shell" style="flex:1" />
              <button class="btn sm" data-rule-add="deny">添加</button>
            </div>
          </div>
          <div class="field">
            <label>始终确认</label>
            <div class="help" style="margin-bottom:5px">匹配即在执行前问你</div>
            <div id="rule-ask" class="rule-list"></div>
            <div class="row" style="margin-top:6px">
              <input class="mono" id="rule-ask-in" placeholder="cmd:git push" style="flex:1" />
              <button class="btn sm" data-rule-add="ask">添加</button>
            </div>
          </div>
          <div class="field">
            <label style="color:var(--ok,#2a8)">允许</label>
            <div class="help" style="margin-bottom:5px">匹配即直接放行</div>
            <div id="rule-allow" class="rule-list"></div>
            <div class="row" style="margin-top:6px">
              <input class="mono" id="rule-allow-in" placeholder="cmd:pytest *" style="flex:1" />
              <button class="btn sm" data-rule-add="allow">添加</button>
            </div>
          </div>
        </div>
      </div>
    </div>
    <div id="set-ui" class="set-pane" style="display:none">
      <div class="card"><h3>当前外观</h3>
        <div id="ui-current" class="help" style="margin-bottom:10px"></div>
        <div class="grid c3">
          <div class="field"><label>明暗模式</label><select id="u-theme">
            <option value="light"${ui.theme === "light" ? " selected" : ""}>浅色</option>
            <option value="dark"${ui.theme === "dark" ? " selected" : ""}>深色（护眼）</option>
            <option value="auto"${ui.theme === "auto" ? " selected" : ""}>跟随系统</option>
          </select></div>
          <div class="field"><label>字号<span class="hint">与「通用」页一致</span></label><select id="u-fontsize">
            <option value="13"${ui.font_size === 13 ? " selected" : ""}>很小（13）</option>
            <option value="15"${ui.font_size === 15 ? " selected" : ""}>小（15）</option>
            <option value="17"${(ui.font_size || 17) === 17 ? " selected" : ""}>标准（17，默认）</option>
            <option value="19"${ui.font_size === 19 ? " selected" : ""}>大（19）</option>
            <option value="22"${ui.font_size === 22 ? " selected" : ""}>超大（22）</option>
          </select></div>
          <div class="field"><label>语言</label><select id="u-lang">
            <option value="zh"${ui.language === "zh" ? " selected" : ""}>简体中文</option>
          </select></div>
        </div>
      </div>

      <div class="card"><h3>主题画廊<span class="hint">点一下就预览，保存后生效</span></h3>
        <div class="help" style="margin-bottom:9px">基础配色</div>
        <div class="skin-grid" id="ui-skins"></div>
        <div class="help" style="margin:14px 0 9px">背景主题（纯色 / 图片；对话开始后自动变淡，保证文字可读）</div>
        <div id="ui-themes"></div>
        <div class="row" style="margin-top:12px">
          <button class="btn" id="ui-theme-upload">上传自己的图片</button>
          <button class="btn ghost" id="ui-theme-clear">不用图片背景</button>
          <input type="file" id="ui-theme-file" accept="image/*" style="display:none">
        </div>
      </div>

      <div class="card"><h3>界面元素</h3>
        <label class="switch"><input type="checkbox" id="u-diff"${ui.diff_review !== false ? " checked" : ""}>
          改文件时显示前后对比</label><br>
        <label class="switch"><input type="checkbox" id="u-notify"${ui.desktop_notifications !== false ? " checked" : ""}>
          桌面通知（跑完提醒你）</label>
      </div>
    </div>
    <div id="set-adv" class="set-pane" style="display:none">
      <div class="card"><h3>服务<span class="hint">本机监听与访问控制</span></h3>
        <div class="grid c3">
          <div class="field"><label>监听地址</label><input type="text" id="u-host" value="${esc((cfg.server || {}).host || "127.0.0.1")}"></div>
          <div class="field"><label>端口</label><input type="number" id="u-port" value="${(cfg.server || {}).port || 7845}"></div>
          <div class="field"><label>访问密码<span class="hint">留空则谁都能连</span></label><input type="text" id="u-token" value="${esc((cfg.server || {}).token || "")}"></div>
        </div>
        <div class="help">改端口或密码需要重启程序才生效。默认只监听 127.0.0.1，只有本机能连。</div>
      </div>
      <div class="card"><h3>直接改配置文件<span class="hint">高级用法，改错会导致启动失败</span></h3>
        <div class="help" id="raw-path"></div>
        <textarea id="raw-text" class="mono" rows="18" style="margin-top:8px"></textarea>
        <div class="row" style="margin-top:9px">
          <button class="btn primary" id="raw-save">保存并重新加载</button>
          <button class="btn" id="raw-reload">放弃修改，重新读取</button>
        </div>
      </div>
      <div class="card"><h3>数据维护</h3>
        <div class="row">
          <button class="btn" id="maint-vacuum">压缩数据库</button>
          <button class="btn" id="maint-reindex">重建记忆索引</button>
          <button class="btn danger" id="maint-purge">清理 180 天前的记忆</button>
        </div>
      </div>
      <div class="card"><h3>恢复助手<span class="hint">诊断 · 备份 · 回滚</span></h3>
        <div class="help" style="margin-bottom:9px">
          配置改坏了或启动异常时用这里恢复。回滚前会把当前配置也备份一份，不会丢东西。
        </div>
        <div class="row" style="margin-bottom:10px">
          <button class="btn" id="rec-run">运行诊断</button>
          <button class="btn" id="rec-backup">立即备份配置</button>
          <button class="btn" id="rec-refresh">刷新备份列表</button>
          <!-- ★ 2-P：备份区（回收站副本 + 配置备份）会越积越多，给一个清理入口 -->
          <button class="btn ghost" id="rec-cleanup">清理旧备份</button>
        </div>
        <div id="rec-checks"></div>
        <div id="rec-backups" style="margin-top:10px"></div>
      </div>
    </div>
    <div class="row" style="position:sticky;bottom:0;background:var(--bg);padding:12px 0;border-top:1px solid var(--border);margin-top:8px">
      <button class="btn primary" id="set-save">保存设置</button>
      <span id="set-msg" style="font-size:12.5px;color:var(--text-dim)"></span>
    </div>`;

  // 供应商卡片：每个供应商下面**逐个模型一行**，行首有勾选框（=启用），
  // 勾上后可以单独配置「上下文窗口 / 输出上限 / 支持图片」。
  // ★ 用户的核心诉求：模型前面一个框，打勾=启用；启用后逐模型配这三项；
  //   输入留空 = 不限制（交给上游），并在旁边给推荐值提示。
  const renderProviders = () => {
    const modelRow = (p, m) => {
      const ov = (p.model_overrides || {})[m] || {};
      const on = ov.enabled !== false;   // 未显式设置 = 跟随供应商，视为启用
      const supported = (p.models || []).includes(m);
      return `<div class="mrow${on ? "" : " off"}" data-pname="${esc(p.name)}" data-mname="${esc(m)}">
        <label class="switch mck">
          <input type="checkbox" data-mon="${esc(p.name)}|${esc(m)}"${on ? " checked" : ""}>
        </label>
        <div class="mname" title="${esc(m)}">${esc(m)}
          ${supported ? "" : '<span class="tag" style="margin-left:6px">自定义</span>'}
        </div>
        <div class="mfields">
          <label title="这个模型能装多少上下文，自己填数字；留空=不限制">上下文窗口
            <input type="number" data-mf="context_window" value="${ov.context_window || ""}"
              placeholder="如 1048576"></label>
          <label title="单次最多能输出多少 token，自己填数字；留空=不限制">输出上限
            <input type="number" data-mf="max_output_tokens" value="${ov.max_output_tokens || ""}"
              placeholder="如 384000"></label>
          <label class="mvis" title="这个模型能不能读图">
            <input type="checkbox" data-mf="vision"${ov.vision ? " checked" : ""}>支持图片</label>
        </div>
      </div>`;
    };
    const cardHtml = (p) => {
      const models = (p.models || []).length ? p.models : (p.default ? [p.default] : []);
      return `
      <div style="border:1px solid var(--border);border-radius:9px;padding:11px 13px;margin-bottom:9px">
        <div class="row" style="margin-bottom:6px">
          <b>${esc(p.display_name || p.name)}</b>
          <span class="tag">${esc(p.kind)}</span>
          ${p.has_key ? '<span class="tag ok">已配密钥</span>' : '<span class="tag warn">缺密钥</span>'}
          ${p.enabled ? "" : '<span class="tag">已停用</span>'}
          <span class="spacer"></span>
          <button class="btn sm ghost" data-pedit="${esc(p.name)}">编辑</button>
          <button class="btn sm ghost" data-ptest="${esc(p.name)}">测试并获取模型</button>
          <button class="btn sm ghost" data-ptog="${esc(p.name)}" data-v="${p.enabled ? 0 : 1}">${p.enabled ? "停用" : "启用"}</button>
          <button class="btn sm ghost danger" data-pdel="${esc(p.name)}">删除</button>
        </div>
        <div class="mono" style="font-size:11px;color:var(--text-dim);margin-bottom:8px">${esc(p.base_url || "(未填地址)")}</div>
        ${models.length ? `<div class="mhead"><span></span><span>模型</span>
          <span class="mfields"><span>上下文窗口</span><span>输出上限</span><span></span></span></div>`
          + models.map((m) => modelRow(p, m)).join("")
          : '<div class="help">还没有模型。点「测试并获取模型」拉取，或在「编辑」里手填。</div>'}
        <div class="help" style="margin-top:8px">
          留空即不限制（交给上游）；灰色的字是内置能力表给出的推荐值，仅供参考、不会自动填入。
        </div>
      </div>`;
    };

    // ★ 只显示"已配置"（填过密钥）的供应商；预置模板一律不显示，必须点「添加供应商」才看到
    const ready = providers.filter((p) => p.has_key);

    let html = "";
    if (ready.length) {
      html += ready.map(cardHtml).join("");
    } else {
      html = `<div class="empty" style="padding:26px 20px;text-align:center">
        <div style="font-size:15px;font-weight:600;margin-bottom:6px">还没有配置供应商</div>
        <div style="color:var(--text-soft);font-size:12.5px;line-height:1.7">
          点下面的「添加供应商」→ 选一个预设（万象 API / DeepSeek / 智谱 / MiMo …）<br>
          只需粘贴 API Key 即可开始使用
        </div>
      </div>`;
    }
    $("#prov-list").innerHTML = html;
    $$("[data-ptest]").forEach((b) => b.onclick = async () => {
      b.textContent = "测试中…"; b.disabled = true;
      try {
        const r = await api("/api/providers/test", { method: "POST", body: { name: b.dataset.ptest, list_models: true } });
        if (r.models && r.models.length) {
          const cur = providers.find((x) => x.name === b.dataset.ptest);
          modal(`${esc(b.dataset.ptest)} 的可用模型（${r.models.length}）`,
            `<div style="max-height:50vh;overflow:auto">${r.models.map((m) =>
              `<label class="switch" style="display:block;padding:3px 0">
                <input type="checkbox" value="${esc(m)}"${(cur.models || []).includes(m) ? " checked" : ""}> ${esc(m)}</label>`).join("")}</div>`,
            `<button class="btn" data-close>取消</button><button class="btn primary" id="pm-use">用选中的模型替换</button>`);
          $("#pm-use").onclick = async () => {
            const sel = $$("#modal-body input[type=checkbox]:checked").map((x) => x.value);
            if (!sel.length) return toast("至少选一个", "err");
            const p = providers.find((x) => x.name === b.dataset.ptest);
            await api("/api/providers", { method: "POST", body: {
              action: "update", name: p.name,
              patch: { models: sel, default: p.default && sel.includes(p.default) ? p.default : sel[0] },
            }});
            closeModal(); toast(`已更新，选了 ${sel.length} 个模型`, "ok"); PAGES.settings();
          };
        } else {
          toast(r.ok ? "连接成功" : "失败：" + (r.error || "未知错误"), r.ok ? "ok" : "err");
        }
      } catch (e) { toast(e.message, "err"); }
      b.textContent = "测试"; b.disabled = false;
    });
    $$("[data-ptog]").forEach((b) => b.onclick = async () => {
      await api("/api/providers", { method: "POST", body: { action: "toggle", name: b.dataset.ptog, enabled: Number(b.dataset.v) } });
      PAGES.settings();
    });
    $$("[data-pedit]").forEach((b) => b.onclick = () => {
      providerEdit(providers.find((x) => x.name === b.dataset.pedit));
    });
    $$("[data-pdel]").forEach((b) => b.onclick = async () => {
      if (!confirm(`删除供应商「${b.dataset.pdel}」？`)) return;
      await api("/api/providers", { method: "POST", body: { action: "delete", name: b.dataset.pdel } });
      PAGES.settings();
    });

    // ---- 逐模型设置：勾选启用 + 上下文窗口 / 输出上限 / 支持图片 ----
    const saveOverride = async (pname, model, patch) => {
      try {
        const r = await api("/api/providers", {
          method: "POST",
          body: { action: "model_override", name: pname, model, override: patch },
        });
        // 同步本地缓存，避免下次重渲染又读回旧值
        const p = providers.find((x) => x.name === pname);
        if (p) {
          p.model_overrides = p.model_overrides || {};
          if (r.override) p.model_overrides[model] = r.override;
          else delete p.model_overrides[model];
        }
        toast("已保存", "ok");
        // 模型启用状态与上下文窗口都会影响下拉框与右侧栏，刷新一次引导数据，
        // 并重算上下文上限 —— 否则用户填完窗口，右侧栏仍显示「未限制」（实测反馈）。
        try {
          S.boot = await api("/api/bootstrap");
          syncModelSelect();
          applyContextLimit(S.boot);
          renderInfoPanel();
          renderStatusBar();
        } catch (e) {}
      } catch (e) { toast("保存失败：" + e.message, "err"); }
    };
    const fieldVal = (el) => (el.type === "checkbox"
      ? el.checked
      : (el.value.trim() === "" ? null : Math.max(0, Math.floor(Number(el.value) || 0))));
    $$("[data-mon]").forEach((el) => el.onchange = () => {
      const [pn, md] = String(el.dataset.mon || "").split("|");
      if (pn && md) saveOverride(pn, md, { enabled: el.checked });
    });
    $$("[data-mf]").forEach((el) => el.onchange = () => {
      const row = el.closest(".mrow");
      if (!row) return;
      saveOverride(row.dataset.pname, row.dataset.mname, { [el.dataset.mf]: fieldVal(el) });
    });
  };
  renderProviders();

  // 分段按钮（思考语言 / 压缩阈值）：点一个，同组其它取消选中；
  // 压缩那一组还要把值同步进「自定义百分比」输入框。
  document.querySelectorAll(".seg").forEach((seg) => {
    seg.querySelectorAll(".seg-btn").forEach((btn) => {
      btn.onclick = () => {
        seg.querySelectorAll(".seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
        if (seg.id === "a-compact-seg") {
          const inp = $("#a-compact");
          if (inp) inp.value = Math.round(Number(btn.dataset.v) * 100);
        }
      };
    });
  });

  // Tab 切换
  $$("#set-tabs .tab").forEach((t) => t.onclick = () => {
    $$("#set-tabs .tab").forEach((x) => x.classList.remove("active"));
    t.classList.add("active");
    $$(".set-pane").forEach((p) => p.style.display = "none");
    $("#set-" + t.dataset.t).style.display = "";
  });

  // 字号即时预览
  const fsSel = $("#u-fontsize");
  if (fsSel) fsSel.onchange = () => applyFontSize(fsSel.value);

  // ---- 主题画廊：基础配色 + 图片主题 ----
  const paintGallery = () => {
    const skinBox = $("#ui-skins");
    if (skinBox) {
      skinBox.innerHTML = SKINS.map((s) => `
        <button class="skin-card${(S.skin || "graphite") === s.id ? " active" : ""}" data-skin="${s.id}">
          <span class="skin-dot" style="background:${s.color}"></span>
          <span class="skin-name">${esc(s.name)}</span>
        </button>`).join("");
      skinBox.querySelectorAll("[data-skin]").forEach((b) => b.onclick = () => {
        applySkin(b.dataset.skin);
        paintGallery();
      });
    }
    const themeBox = $("#ui-themes");
    if (themeBox) {
      // 纯色主题（即梦生成的极简纹理，体积很小）
      const solids = [
        { id: "solid-white", name: "纯白" }, { id: "solid-mist", name: "米白" },
        { id: "solid-ink", name: "墨黑" }, { id: "solid-lilac", name: "薰衣草紫" },
        { id: "solid-jade", name: "青绿" }, { id: "solid-slate", name: "浅灰蓝" },
      ];
      // 内置图片主题（文件在 static/themes/<id>.webp）；缺失的自动跳过
      const presets = [
        { id: "rose-dawn", name: "玫瑰晨光" }, { id: "amber-studio", name: "鸿运工坊" },
        { id: "crimson-city", name: "赤曜新城" }, { id: "sage-breeze", name: "鼠尾草清风" },
        { id: "memo-paper", name: "灵感手账" }, { id: "violet-night", name: "紫曜星夜" },
        { id: "azure-stage", name: "青岚舞台" }, { id: "black-gold", name: "黑金序曲" },
        { id: "ink-mountain", name: "水墨远山" }, { id: "neon-harbor", name: "霓虹港湾" },
      ];
      const card = (t) => `
        <button class="theme-card${S.imageTheme === t.id ? " active" : ""}" data-theme-img="${t.id}">
          <span class="theme-thumb" style="background-image:url('/static/themes/${t.id}.webp')"></span>
          <span class="theme-name">${esc(t.name)}</span>
        </button>`;
      const all = [...solids, ...presets];
      themeBox.innerHTML =
        `<div class="theme-sub">纯色主题</div>` +
        `<div class="theme-grid">${solids.map(card).join("")}</div>` +
        `<div class="theme-sub">图片主题</div>` +
        `<div class="theme-grid">${presets.map(card).join("")}</div>` +
        (S.imageTheme && !all.some((t) => t.id === S.imageTheme)
        ? `<div class="theme-sub">自定义</div><div class="theme-grid">
             <button class="theme-card active" data-theme-img="${esc(S.imageTheme)}">
               <span class="theme-thumb" style="background-image:url('${esc(S.imageTheme)}')"></span>
               <span class="theme-name">自定义</span></button></div>` : "");
      themeBox.querySelectorAll("[data-theme-img]").forEach((b) => b.onclick = () => {
        applyImageTheme(b.dataset.themeImg);
        paintGallery();
      });
    }
    const cur = $("#ui-current");
    if (cur) {
      const sk = SKINS.find((s) => s.id === (S.skin || "graphite"));
      cur.innerHTML = `当前：<b>${esc(sk ? sk.name : "石墨")}</b> 配色 · ` +
        `${S.imageTheme ? `<b>${esc(S.imageTheme)}</b> 图片主题` : "无图片背景"} · ` +
        `${esc((S.theme === "dark" ? "深色" : S.theme === "auto" ? "跟随系统" : "浅色"))}`;
    }
  };
  paintGallery();
  onClick("#ui-theme-clear", () => { applyImageTheme(""); paintGallery(); });
  onClick("#ui-theme-upload", () => $("#ui-theme-file") && $("#ui-theme-file").click());
  const fileInput = $("#ui-theme-file");
  if (fileInput) fileInput.onchange = () => {
    const f = fileInput.files && fileInput.files[0];
    if (!f) return;
    if (f.size > 6 * 1024 * 1024) return toast("图片太大（请小于 6MB）", "err");
    const rd = new FileReader();
    rd.onload = () => { applyImageTheme(rd.result); paintGallery(); toast("已应用，记得点「保存设置」", "ok"); };
    rd.readAsDataURL(f);
  };

  // 权限等级：改了就立刻同步到后端（不必点保存）
  const pMode = $("#p-mode");
  if (pMode) pMode.onchange = () => setPerm(pMode.value);

  // 供应商操作
  $("#prov-add").onclick = () => providerAdd();

  // 高级：原始配置
  const loadRaw = async () => {
    try {
      const r = await api("/api/config/raw");
      setText("#raw-path", r.path || "");
      const ta = $("#raw-text");
      if (ta) ta.value = r.text || "";
    } catch (e) { toast(e.message, "err"); }
  };
  if (PAGES.settings._needRaw !== false) loadRaw();
  onClick("#raw-reload", loadRaw);
  onClick("#raw-save", async () => {
    try {
      await api("/api/config/raw", { method: "PUT", body: { text: $("#raw-text").value } });
      toast("已保存并重载", "ok"); PAGES.settings();
    } catch (e) { toast(e.message, "err"); }
  });
  onClick("#maint-vacuum", async () => {
    try {
      const r = await api("/api/config", { method: "PATCH", body: {} });
      toast("数据库压缩请重启后生效（SQLite VACUUM）", "");
    } catch (e) { toast(e.message, "err"); }
  });
  onClick("#maint-reindex", async () => {
    try { const r = await api("/api/memories", { method: "POST", body: { action: "reindex" } });
      toast(`重建 ${r.count} 条`, "ok"); } catch (e) { toast(e.message, "err"); }
  });
  onClick("#maint-purge", async () => {
    if (!confirm("清理 180 天前且不重要的记忆？")) return;
    try { const r = await api("/api/memories", { method: "POST", body: { action: "purge", days: 180 } });
      toast(`清理 ${r.removed} 条`, "ok"); } catch (e) { toast(e.message, "err"); }
  });

  // ---- 记忆面板：列表刷新 / 搜索 / 重建索引 ----
  onClick("#m-reload", () => loadMemoryPanel());
  onClick("#m-reindex", async () => {
    try {
      const r = await api("/api/memories", { method: "POST", body: { action: "reindex" } });
      toast(`已重建 ${r.count} 条记忆索引`, "ok");
      loadMemoryPanel();
    } catch (e) { toast("重建失败：" + e.message, "err"); }
  });
  const memoSearch = $("#m-search");
  if (memoSearch) {
    let memoPassed = 0;
    memoSearch.oninput = () => {
      clearTimeout(memoPassed);
      memoPassed = setTimeout(() => loadMemoryPanel(), 250);
    };
  }

  // ---- 沙箱：探测本机 Shell 环境（对齐截图里的「运行环境检测」表格）----
  fillSandboxProbe();

  // ---- 权限细粒度规则：三列（deny / ask / allow），可增删 ----
  // 用内存里的数组维护，保存时一并写进 config.permissions.rules
  const RULES = { deny: [], ask: [], allow: [] };
  for (const r of (perms.rules || [])) {
    if (RULES[r.action]) RULES[r.action].push(r.pattern || "");
  }
  window.__fengcodeRules = RULES;
  // 注意：这里必须调 paintRulesGlobal（函数声明会提升），
  // 不能调 paintRules —— 那是后面的 const 别名，此时还在 TDZ 里，会抛错并
  // 中断整个设置页渲染，导致所有按钮都没绑上事件。
  paintRulesGlobal();
  // 用事件委托绑定：设置中心是「复制 innerHTML」过去的，
  // 直接绑节点会绑到原始页面的副本上，点设置中心里的按钮会没反应。
  // 委托到 document 上则永远有效（这也是沙箱探测踩过的同一个坑）。
  if (!window.__ruleUIBound) {
    window.__ruleUIBound = true;

    document.addEventListener("click", (e) => {
      const addBtn = e.target.closest("[data-rule-add]");
      if (addBtn) {
        e.preventDefault();
        const act = addBtn.dataset.ruleAdd;
        const R = window.__fengcodeRules;
        if (!R || !R[act]) return;
        const inp = document.querySelector("#rule-" + act + "-in");
        const v = inp ? (inp.value || "").trim() : "";
        if (!v) { toast("请填写规则", "err"); return; }
        R[act].push(v);
        if (inp) inp.value = "";
        paintRulesGlobal();
        return;
      }
      const delBtn = e.target.closest("[data-rule-del]");
      if (delBtn) {
        e.preventDefault();
        const act = delBtn.dataset.ruleDel;
        const R = window.__fengcodeRules;
        if (!R || !R[act]) return;
        R[act].splice(Number(delBtn.dataset.i), 1);
        paintRulesGlobal();
      }
    });

    document.addEventListener("keydown", (e) => {
      if (e.key !== "Enter") return;
      const inp = e.target;
      if (!inp || !inp.id || !/^rule-(deny|ask|allow)-in$/.test(inp.id)) return;
      e.preventDefault();
      const act = inp.id.slice(5, -3);
      const R = window.__fengcodeRules;
      if (!R || !R[act]) return;
      const v = (inp.value || "").trim();
      if (!v) { toast("请填写规则", "err"); return; }
      R[act].push(v);
      inp.value = "";
      paintRulesGlobal();
    });
  }
  paintRulesGlobal();

/** 恢复助手：运行诊断并渲染结果。
 *
 * 全局函数 + 渲染所有匹配容器 —— 设置中心复制 innerHTML 后可能有多份
 * 同 id 节点，只填原页面那份用户是看不到的。
 */
async function loadRecoveryGlobal() {
  const cbs = $$("#rec-checks");
  const bbs = $$("#rec-backups");
  if (!cbs.length) return;
  cbs.forEach((el) => { el.innerHTML = `<div class="help">正在诊断…</div>`; });
  try {
    const r = await api("/api/recovery");
    const checksHtml = (r.checks || []).map((c) => `
      <div class="row" style="padding:6px 0;border-bottom:1px solid var(--border);font-size:12.5px;align-items:flex-start">
        <span class="tag ${c.ok ? "ok" : "warn"}" style="flex:none;margin-top:1px">${c.ok ? "正常" : "异常"}</span>
        <span style="flex:1">
          <b>${esc(c.name)}</b>
          ${c.detail ? `<div class="help" style="margin-top:2px">${esc(c.detail)}</div>` : ""}
          ${!c.ok && c.fix ? `<div class="help" style="color:var(--danger,#d33);margin-top:2px">建议：${esc(c.fix)}</div>` : ""}
        </span>
      </div>`).join("")
      + `<div class="help" style="margin-top:8px">共 ${(r.checks || []).length} 项，异常 ${r.failed || 0} 项</div>`;
    cbs.forEach((el) => { el.innerHTML = checksHtml; });

    const backupsHtml = (r.backups || []).length
      ? `<div style="font-size:12.5px;margin-bottom:6px"><b>配置备份</b></div>` + (r.backups || []).map((b) => `
          <div class="row" style="padding:5px 0;font-size:12px">
            <span class="mono" style="flex:1">${esc(b.name)}</span>
            <span style="color:var(--text-faint)">${ago(b.created_at)}</span>
            <button class="btn sm ghost" data-restore="${esc(b.name)}">回滚到此</button>
          </div>`).join("")
      : `<div class="help">还没有配置备份。点「立即备份配置」创建一个。</div>`;
    bbs.forEach((el) => { el.innerHTML = backupsHtml; });
  } catch (e) {
    cbs.forEach((el) => { el.innerHTML = `<div class="help">诊断失败：${esc(e.message)}</div>`; });
  }
}

  if (!window.__recUIBound) {
    window.__recUIBound = true;
    document.addEventListener("click", async (e) => {
      const btn = e.target.closest("#rec-run, #rec-refresh, #rec-backup, #rec-cleanup, [data-restore]");
      if (!btn) return;
      e.preventDefault();

      if (btn.id === "rec-cleanup") {
        // ★ 2-P：先只统计（dry_run），让用户看到要清多少，再确认删除
        try {
          const p = await api("/api/recovery", { method: "POST", body: { action: "cleanup_backups", dry_run: true, keep: 20 } });
          const db = p.deleted_backups || {}, cb = p.config_backups || {};
          const mb = (n) => ((n || 0) / 1048576).toFixed(1) + "MB";
          if (!(db.removed || cb.removed)) { toast("没有需要清理的旧备份", ""); return; }
          const msg = `将清理：\n`
            + `· 回收站副本 ${db.removed || 0} 项（共 ${db.total || 0} 项）\n`
            + `· 配置备份 ${cb.removed || 0} 个（共 ${cb.total || 0} 个）\n`
            + `预计释放约 ${mb((db.freed_bytes || 0) + (cb.freed_bytes || 0))}（每类保留最近 20 个）\n\n确认清理？`;
          if (!confirm(msg)) return;
          const r = await api("/api/recovery", { method: "POST", body: { action: "cleanup_backups", keep: 20 } });
          toast(`已清理，释放约 ${mb(r.freed_total)}`, "ok");
          loadRecoveryGlobal();
        } catch (err) { toast("清理失败：" + err.message, "err"); }
        return;
      }

      if (btn.hasAttribute("data-restore")) {
        const name = btn.dataset.restore;
        if (!confirm(`用 ${name} 覆盖当前配置？\n（当前配置会先自动备份）`)) return;
        try {
          const r = await api("/api/recovery", { method: "POST", body: { action: "restore", name } });
          toast(r.message || "已回滚", "ok");
          loadRecoveryGlobal();
        } catch (err) { toast(err.message, "err"); }
        return;
      }

      if (btn.id === "rec-backup") {
        try {
          const r = await api("/api/recovery", { method: "POST", body: { action: "backup" } });
          if (r.ok && r.path) { toast("已备份", "ok"); loadRecoveryGlobal(); }
          else toast("没有可备份的配置文件", "");
        } catch (err) { toast(err.message, "err"); }
        return;
      }

      loadRecoveryGlobal();
    });
  }
  loadRecoveryGlobal();

  // 保存
  onClick("#set-save", () => saveSettingsNow());
  // 保存设置。抽成命名函数，供「设置中心里补的按钮」与「原页面按钮」共用；
  // 事件用委托绑定，因为设置中心是复制 innerHTML，直接绑节点会绑到副本上。
  const saveSettingsNow = async () => {
    const lines = (s) => {
      const el = $(s);
      return el ? el.value.split("\n").map((x) => x.trim()).filter(Boolean) : [];
    };
    const rules = [];
    const R0 = window.__fengcodeRules || { deny: [], ask: [], allow: [] };
    for (const act of ["deny", "ask", "allow"]) {
      for (const p of (R0[act] || [])) rules.push({ pattern: p, action: act });
    }
    const patch = {
      llm: { default_provider: (d.models.find((m) => m.ref === val("s-model")) || {}).provider,
             default_model: (d.models.find((m) => m.ref === val("s-model")) || {}).model,
             compact_ratio: (Number(val("a-compact")) || 80) / 100 },
      agent: {
        // ★ max_steps 不暴露给用户，保持后端默认
        temperature: Number(val("a-temp")) || 0.6,
        reasoning_language: val("a-lang-seg") || (document.querySelector("#a-lang-seg .seg-btn.active")?.dataset.v) || "zh",
        planner_model: val("a-planner") || null,
        subagent_model: val("a-subagent") || null,
        vision_model: val("a-vision") || null,
        search_model: val("a-search") || null,
        subagent_effort: val("a-subagent-effort"),
        reflection: chk("a-reflect"),
        submit_checklist: chk("a-checklist"),
        system_prompt_extra: val("a-extra"),
        subagents: {
          enabled: chk("sa-on"),
          // 深度/预算/超时用默认值（界面上不再暴露这些术语）
          max_depth: 3,
          default_budget_tokens: 120000,
          default_timeout: 900,
        },
      },
      permissions: {
        mode: val("p-mode"),
        audit_enabled: chk("p-audit"),
        write_paths: lines("#p-write"),
        read_paths: lines("#p-read"),
        deny_patterns: lines("#p-deny"),
        rules,
      },
      sandbox: {
        timeout_seconds: Number(val("s-timeout")) || 120,
        network: chk("s-net"),
        bash: $("#sb-shell") ? val("sb-shell") : "auto",
      },
      tools: { max_output_chars: Number(val("s-maxout")) || 60000 },
      // ★ 记忆设置在「记忆」页（字段与 MemoryConfig 一一对应）。
      //   注意：设置中心一次只显示一个面板，未显示的字段读成空 → 交给下面的 prune
      //   剔除，绝不覆盖用户在别处设过的值。
      memory: {
        enabled: $("#m-on") ? chk("m-on") : undefined,
        long_term_enabled: $("#m-long") ? chk("m-long") : undefined,
        episodic_enabled: $("#m-epi") ? chk("m-epi") : undefined,
        profile_enabled: $("#m-prof") ? chk("m-prof") : undefined,
        auto_summarize: $("#m-auto") ? chk("m-auto") : undefined,
        use_vector: $("#m-vec") ? chk("m-vec") : undefined,
        short_term_max_turns: $("#m-turns") ? (Number(val("m-turns")) || 60) : undefined,
        recall_top_k: $("#m-top") ? (Number(val("m-top")) || 6) : undefined,
        recall_min_score: $("#m-minscore") ? (Number(val("m-minscore")) || 0.22) : undefined,
        decay_half_life_days: $("#m-decay") ? (Number(val("m-decay")) || 45) : undefined,
      },
      ui: {
        // ★ 字号 / 主题 / 语言在两个页面各有一套控件（通用页 g-*、外观页 u-*）。
        //   旧写法只读外观页的 u-*：在**通用页**改完字号再点保存，因为 u-fontsize
        //   根本不存在 → 读成空 → `|| 17` 兜底把字号**写回默认 17**，用户改的字号
        //   就这么被覆盖掉了（实测反馈「选择字号没效果」）。
        //   现在两边都读，且读不到时给 undefined，交给 prune 剔除、不覆盖原值。
        theme: val("u-theme") || val("g-theme") || undefined,
        language: val("u-lang") || val("g-lang") || undefined,
        font_size: Number(val("u-fontsize") || val("g-fontsize")) || undefined,
        // ★ show_turn_usage / show_reasoning / 界面动画 / 信息面板 已固定为「开启」，
        //   不再从界面读取（设置里已移除这些开关）。保留原值不动，避免被 prune 清掉。
        diff_review: chk("u-diff"),
        desktop_notifications: chk("u-notify"),
      },
      server: {
        host: val("u-host").trim() || "127.0.0.1",
        port: Number(val("u-port")) || 7845,
        token: val("u-token").trim() || null,
      },
    };
    // 设置中心一次只显示一个面板，没显示的字段会读成空值。
    // 这里把「空字符串 / undefined」的字段剔掉，避免把好配置覆盖成空。
    const prune = (obj) => {
      for (const k of Object.keys(obj)) {
        const v = obj[k];
        if (v === undefined || v === "") delete obj[k];
        else if (v && typeof v === "object" && !Array.isArray(v)) prune(v);
      }
      return obj;
    };
    prune(patch);
    if (!patch.ui) delete patch.ui;
    try {
      await api("/api/config", { method: "PATCH", body: patch });
      // 只在真取到主题时才应用 —— 在别的页保存时 #u-theme 不存在，
      // 直接 applyTheme("") 会让 data-theme 为空、整套 CSS 变量失效（页面变透明）。
      if (patch.ui && patch.ui.theme) applyTheme(patch.ui.theme);
      if (patch.ui && patch.ui.font_size) applyFontSize(patch.ui.font_size);
      setText("#set-msg", "已保存（" + new Date().toLocaleTimeString() + "）");
      toast("设置已保存", "ok");
      S.boot = await api("/api/bootstrap");
      syncModelSelect();
      refreshFooter();
    } catch (e) {
      const m = $("#set-msg");
      if (m) m.innerHTML = `<span class="err-text">保存失败：${esc(e.message)}</span>`;
      else toast("保存失败：" + e.message, "err");
    }
  };

  // 事件委托：设置中心里的保存按钮是复制出来的副本，直接绑节点绑不上。
  if (!window.__saveBound) {
    window.__saveBound = true;
    document.addEventListener("click", (e) => {
      const b = e.target.closest("#set-save");
      if (!b) return;
      e.preventDefault();
      const fn = window.__saveSettingsNow;
      if (typeof fn === "function") fn();
    });
  }
  window.__saveSettingsNow = saveSettingsNow;
};
async function providerAdd() {
  // 一个弹窗，两种模式：预设（只填 key） / 自定义（填全部）
  let presets = {};
  try {
    const d = await api("/api/providers");
    presets = d.presets || {};
  } catch (e) { presets = {}; }

  const ORDER = ["wanxiang", "deepseek", "zhipu", "mimo", "dashscope", "moonshot", "doubao", "minimax", "baichuan", "stepfun"];
  const ids = ORDER.filter((k) => presets[k]).concat(Object.keys(presets).filter((k) => !ORDER.includes(k) && k !== "custom"));

  // 当前已存在的供应商名（预设已被添加过的话，提示"已添加"）
  let existing = [];
  try {
    const d2 = await api("/api/providers");
    existing = (d2.providers || []).map((p) => p.name);
  } catch (e) { /* ignore */ }

  modal("添加供应商", `
    <div class="tabs" id="prov-mode" style="margin-bottom:14px">
      <button class="tab active" data-mode="preset">预设供应商</button>
      <button class="tab" data-mode="custom">自定义供应商</button>
    </div>

    <div id="pv-preset">
      <div class="field"><label>选择服务商</label>
        <select id="pv-sel">
          ${ids.map((k) => {
            const v = presets[k] || {};
            const used = existing.includes(k) ? "（已添加）" : "";
            return `<option value="${esc(k)}">${esc(v.label || k)}${used}</option>`;
          }).join("")}
        </select>
        <div class="help" id="pv-desc"></div>
      </div>
      <div class="field"><label>API Key <span style="color:var(--err)">*</span></label>
        <input type="password" id="pv-key" placeholder="粘贴密钥"></div>
      <div class="grid c2">
        <div class="field"><label>上下文窗口（可选）</label>
          <input type="number" id="pv-ctx" placeholder="如 131072，留空自动识别"></div>
        <div class="field"><label>输出上限（可选）</label>
          <input type="number" id="pv-out" placeholder="如 8192，留空自动识别"></div>
      </div>
      <div class="field"><label>模型列表（每行一个）</label>
        <div class="row" style="gap:8px;align-items:flex-start">
          <textarea id="pv-models" class="mono" rows="3" style="flex:1"
            placeholder="留空则点右侧「测试并获取模型」自动拉取"></textarea>
          <button class="btn sm" type="button" id="pv-test-inline" style="flex:none">测试并获取模型</button>
        </div></div>
      <div id="pv-result" class="help"></div>
    </div>

    <div id="pv-custom" style="display:none">
      <div class="grid c2">
        <div class="field"><label>名称（唯一标识）</label><input type="text" id="pv-cname" placeholder="my-provider"></div>
        <div class="field"><label>协议</label><select id="pv-ckind">
          <option value="openai">OpenAI 兼容</option>
          <option value="anthropic">Anthropic Claude</option>
          <option value="gemini">Google Gemini</option>
        </select></div>
      </div>
      <div class="field"><label>Base URL</label><input type="text" id="pv-curl" placeholder="https://api.example.com/v1"></div>
      <div class="field"><label>API Key</label><input type="password" id="pv-ckey" placeholder="粘贴密钥">
        <div class="help">只存在本地 config.toml，不会外传</div></div>
      <div class="field"><label>模型列表（每行一个）</label>
        <textarea id="pv-cmodels" class="mono" rows="4"></textarea>
        <div class="help">模型名要和服务商文档一致；有些服务需要带前缀（如 qwen/qwen3-max）</div></div>
      <div class="grid c2">
        <div class="field"><label>上下文窗口（可选）</label><input type="number" id="pv-cctx" placeholder="留空自动识别"></div>
        <div class="field"><label>输出上限（可选）</label><input type="number" id="pv-cout" placeholder="留空自动识别"></div>
      </div>
    </div>`,
    `<button class="btn" data-close>取消</button>
     <button class="btn primary" id="pv-save">保存</button>`);

  // ---- 模式切换 ----
  $$("#prov-mode .tab").forEach((t) => t.onclick = () => {
    $$("#prov-mode .tab").forEach((x) => x.classList.toggle("active", x === t));
    const isPreset = t.dataset.mode === "preset";
    $("#pv-preset").style.display = isPreset ? "" : "none";
    $("#pv-custom").style.display = isPreset ? "none" : "";
  });

  // ---- 预设说明 ----
  const updDesc = () => {
    const p = presets[$("#pv-sel").value] || {};
    const hasKey = existing.includes($("#pv-sel").value);
    $("#pv-desc").innerHTML =
      `${esc(p.description || "")}<br>地址：<span class="mono">${esc(p.base_url || "(自己填)")}</span>` +
      (hasKey ? `<br><span style="color:var(--warn)">该供应商已添加，保存将更新其密钥</span>` : "");
  };
  $("#pv-sel").onchange = updDesc; updDesc();

  const testProvider = async (sourceBtn) => {
    const isPreset = $("#pv-preset").style.display !== "none";
    // 按钮已统一为模型列表右侧那一个（底部重复按钮已移除），
    // 这里仍兼容 sourceBtn 为空的情况，取不到就静默返回，避免 btn.textContent 抛错。
    const btn = sourceBtn || $("#pv-test-inline");
    if (!btn) return;
    const orig = btn.textContent;
    btn.textContent = "测试中…"; btn.disabled = true;
    try {
      if (isPreset) {
        const pid = $("#pv-sel").value;
        const key = $("#pv-key").value.trim();
        if (!key && !existing.includes(pid)) { toast("请先填 API Key", "err"); return; }
        await api("/api/providers", { method: "POST", body: {
          action: existing.includes(pid) ? "update" : "add", name: pid, preset: pid, enabled: true,
          ...(existing.includes(pid) ? { patch: { ...(key ? { api_key: key } : {}) } } : { api_key: key || null }),
        }});
        const r = await api("/api/providers/test", { method: "POST", body: { name: pid, list_models: true } });
        const models = r.models || [];
        $("#pv-result").innerHTML = models.length
          ? `✓ 连接成功，获取到 ${models.length} 个模型`
          : `✓ 连接成功，但没拉到模型列表（请手动填写）`;
        if (models.length) $("#pv-models").value = models.join("\n");
      } else {
        const name = $("#pv-cname").value.trim();
        if (!name) { toast("请先填名称", "err"); return; }
        const key = $("#pv-ckey").value.trim();
        const body = existing.includes(name)
          ? { action: "update", name, patch: { kind: $("#pv-ckind").value, base_url: $("#pv-curl").value.trim(), ...(key ? { api_key: key } : {}) } }
          : { action: "add", name, kind: $("#pv-ckind").value, base_url: $("#pv-curl").value.trim(), api_key: key || null, enabled: true };
        await api("/api/providers", { method: "POST", body });
        const r = await api("/api/providers/test", { method: "POST", body: { name, list_models: true } });
        const models = r.models || [];
        $("#pv-cmodels").value = models.join("\n");
        toast(models.length ? `✓ 获取到 ${models.length} 个模型` : "✓ 连接成功（未返回模型列表）", "ok");
      }
    } catch (e) { toast("测试失败：" + e.message, "err"); }
    finally { btn.textContent = orig; btn.disabled = false; }
  };
  // 只保留模型列表右侧那一个测试按钮（底部重复按钮已移除）
  $("#pv-test-inline").onclick = () => testProvider($("#pv-test-inline"));

  // ---- 保存 ----
  $("#pv-save").onclick = async () => {
    const isPreset = $("#pv-preset").style.display !== "none";
    try {
      if (isPreset) {
        const pid = $("#pv-sel").value;
        const key = $("#pv-key").value.trim();
        const ctx = Number($("#pv-ctx").value) || null;
        const out = Number($("#pv-out").value) || null;
        const models = $("#pv-models").value.split("\n").map((x) => x.trim()).filter(Boolean);
        if (existing.includes(pid)) {
          const patch = {};
          if (key) patch.api_key = key;
          if (ctx) patch.context_window = ctx;
          if (out) patch.max_output_tokens = out;
          if (models.length) { patch.models = models; patch.default = models[0]; }
          await api("/api/providers", { method: "POST", body: { action: "update", name: pid, patch } });
        } else {
          if (!key) return toast("请填 API Key", "err");
          await api("/api/providers", { method: "POST", body: {
            action: "add", name: pid, preset: pid, enabled: true,
            api_key: key, models,
            ...(ctx ? { context_window: ctx } : {}),
            ...(out ? { max_output_tokens: out } : {}),
          }});
        }
        closeModal(); toast("已保存", "ok"); PAGES.settings();
      } else {
        const name = $("#pv-cname").value.trim();
        if (!name) return toast("名称不能为空", "err");
        const key = $("#pv-ckey").value.trim();
        const models = $("#pv-cmodels").value.split("\n").map((x) => x.trim()).filter(Boolean);
        const ctx = Number($("#pv-cctx").value) || null;
        const out = Number($("#pv-cout").value) || null;
        const patch = {
          kind: $("#pv-ckind").value, base_url: $("#pv-curl").value.trim(),
          models, default: models[0] || null, enabled: true,
          ...(ctx ? { context_window: ctx } : {}),
          ...(out ? { max_output_tokens: out } : {}),
        };
        if (existing.includes(name)) {
          if (key) patch.api_key = key;
          await api("/api/providers", { method: "POST", body: { action: "update", name, patch } });
        } else {
          await api("/api/providers", { method: "POST", body: { action: "add", name, ...patch, api_key: key || null } });
        }
        closeModal(); toast("已保存", "ok"); PAGES.settings();
      }
    } catch (e) { toast(e.message, "err"); }
  };
}

function providerEdit(p, models) {
  // 编辑已有供应商（保留原逻辑，补上上下文窗口 / 输出上限）
  const isNew = !p;
  p = p || { name: "", kind: "openai", base_url: "", models: [], default: "", enabled: true };
  modal(isNew ? "添加供应商" : "编辑供应商", `
    <div class="grid c2">
      <div class="field"><label>名称（唯一标识）</label><input type="text" id="pr-name" value="${esc(p.name)}"${isNew ? "" : " readonly"}></div>
      <div class="field"><label>协议</label><select id="pr-kind">
        <option value="openai"${p.kind === "openai" ? " selected" : ""}>OpenAI 兼容（含各类中转站）</option>
        <option value="anthropic"${p.kind === "anthropic" ? " selected" : ""}>Anthropic Claude</option>
        <option value="gemini"${p.kind === "gemini" ? " selected" : ""}>Google Gemini</option>
      </select></div>
    </div>
    <div class="field"><label>Base URL</label><input type="text" id="pr-url" value="${esc(p.base_url)}" placeholder="https://api.example.com/v1"></div>
    <div class="field"><label>API Key</label><input type="password" id="pr-key" placeholder="${p.has_key ? "（已配置，留空则不修改）" : "粘贴密钥"}">
      <div class="help">只存在本地 config.toml，不会外传</div></div>
    <div class="field"><label>模型列表（每行一个）</label>
      <textarea id="pr-models" class="mono" rows="4">${esc((p.models || []).join("\n"))}</textarea>
      <div class="help">模型名要和服务商文档一致；有些中转站需要带前缀（如 qwen/qwen3-max）</div></div>
    <div class="grid c2">
      <div class="field"><label>上下文窗口</label><input type="number" id="pr-ctx" value="${p.context_window || ""}" placeholder="留空自动识别"></div>
      <div class="field"><label>输出上限</label><input type="number" id="pr-out" value="${p.max_output_tokens || ""}" placeholder="留空自动识别"></div>
    </div>
    <div class="grid c2">
      <div class="field"><label>默认模型</label><input type="text" id="pr-default" value="${esc(p.default || "")}"></div>
      <div class="field"><label>启用</label><label class="switch"><input type="checkbox" id="pr-enabled"${p.enabled !== false ? " checked" : ""}> 可用</label></div>
    </div>`,
    `<button class="btn" data-close>取消</button><button class="btn" id="pr-test">测试连接</button><button class="btn primary" id="pr-save">保存</button>`);
  $("#pr-test").onclick = async () => {
    const btn = $("#pr-test"); const o = btn.textContent;
    btn.textContent = "测试中…"; btn.disabled = true;
    try {
      const r = await api("/api/providers/test", { method: "POST", body: { name: p.name, list_models: true } });
      const ms = r.models || [];
      toast(ms.length ? `✓ 成功，${ms.length} 个模型` : "✓ 连接成功", "ok");
      if (ms.length) $("#pr-models").value = ms.join("\n");
    } catch (e) { toast("失败：" + e.message, "err"); }
    finally { btn.textContent = o; btn.disabled = false; }
  };
  $("#pr-save").onclick = async () => {
    const name = $("#pr-name").value.trim();
    if (!name) return toast("名称不能为空", "err");
    const models = $("#pr-models").value.split("\n").map((x) => x.trim()).filter(Boolean);
    const ctx = Number($("#pr-ctx").value) || null;
    const out = Number($("#pr-out").value) || null;
    const body = {
      action: isNew ? "add" : "update",
      name,
      kind: $("#pr-kind").value,
      base_url: $("#pr-url").value.trim(),
      models,
      default: $("#pr-default").value.trim() || (models[0] || null),
      enabled: $("#pr-enabled").checked,
      ...(ctx ? { context_window: ctx } : {}),
      ...(out ? { max_output_tokens: out } : {}),
    };
    const key = $("#pr-key").value.trim();
    if (isNew) { body.api_key = key || null; }
    else if (key) { body.patch = { api_key: key }; }
    try {
      if (isNew) {
        await api("/api/providers", { method: "POST", body });
      } else {
        const patch = {
          kind: body.kind, base_url: body.base_url, models: body.models,
          default: body.default, enabled: body.enabled,
          context_window: ctx, max_output_tokens: out,
        };
        if (key) patch.api_key = key;
        await api("/api/providers", { method: "POST", body: { action: "update", name, patch } });
      }
      closeModal(); toast("已保存", "ok"); PAGES.settings();
    } catch (e) { toast(e.message, "err"); }
  };
}

/* ==========================================================================
   初始化
   ========================================================================== */
/* 由引导数据算出「当前模型的上下文上限」，供右侧栏与状态栏显示。
   ★ 判定规则（对应需求「各模型的上下文，用户设置的多少就显示多少」）：
     · 来源是 model（用户在模型服务页逐模型填的）→ 用他填的值，**一定按它显示**；
     · 来源是 provider / catalog → 用该值（供应商声明或内置能力表）；
     · 来源是 default（没人声明，用默认 1M）→ 也照实显示 1M，不再显示「未限制」；
     · 真取不到数值 → 才视为「未限制」。
   为什么改：旧规则把兜底值整体判成「未限制」，用户填了窗口/换了模型都看不出差别
   （实测反馈「换模型后显示设置里的上下文，换回来又没效果」）。
   抽成函数是因为「保存逐模型设置」「切换模型」后都要立刻重算，不能只算一次。 */
function applyContextLimit(d) {
  d = d || S.boot || {};
  try {
    const refNow = S.model || d.default_model;
    const cur = (d.models || []).find((m) => m.ref === refNow)
      || (d.models || []).find((m) => m.ref === d.default_model);
    if (cur && cur.context_window) {
      S.contextLimit = cur.context_window;
      S.contextLimitSrc = cur.context_window_source || "";
      // 只有「真拿不到数」才算未限制；沿用旧写法会出现「填了也显示未限制」。
      S.contextLimitUnlimited = !(Number(cur.context_window) > 0);
    } else {
      S.contextLimit = 0;
      S.contextLimitSrc = "";
      S.contextLimitUnlimited = true;
    }
  } catch (e) { /* ignore */ }
}

async function boot() {
  renderNav();
  try {
    const d = await api("/api/bootstrap");
    S.boot = d;
    S.model = d.default_model || "";
    if (d.ui && d.ui.theme) applyTheme(d.ui.theme);
    applyFontSize((d.ui && d.ui.font_size) || 17);
    syncModelSelect();
    // 概览面板需要：当前模型的上下文上限 + 压缩阈值
    // ★ 必须跟着「当前会话实际用的模型」走，不能只在启动时取一次默认模型：
    //   用户在下拉框换模型后，上限要跟着换，否则显示的是上一个模型的窗口。
    applyContextLimit(d);
    if (d.llm && d.llm.compact_ratio) S.compactPct = d.llm.compact_ratio;
    // 权限三档：从后端读回来同步界面
    CUR_PERM = (d.permissions || {}).mode || "ask";
    paintPermChip();
    // 工作区（侧边栏分组用）
    try {
      const st = await api("/api/status");
      if (st && st.workspace) {
        S.workspace = st.workspace;
        S.dataDir = st.data_dir || st.home || "";
      }
    } catch (e) {}
    // 徽标
    setBadge("skills", (d.skills || []).length || 0);
    setBadge("tools", (d.tool_groups || []).reduce((a, g) => a + g.tools.length, 0) || 0);
    const mcpc = (d.status && d.status.mcp && d.status.mcp.total) || 0;
    if (mcpc) setBadge("mcp", mcpc);
    const plgc = (d.status && d.status.plugins && d.status.plugins.total) || 0;
    if (plgc) setBadge("plugins", plgc);
    if (!d.first_run_done) {
      // 首次启动：走一遍轻量向导，
      // 但只问真正影响可用性的三件事，可以随时跳过。
      openFirstRunWizard();
    }
    emptyState();
    syncModelSelect();
    await refreshFooter();
    connectWS();
    setStatus("ok", "就绪");
    renderStatusBar();
    toggleInfoPanel(S.infoOpen);
    renderNav();
    // 恢复上次打开的会话（如果有）
    if (!S.sessionId) {
      try {
        const ss = await api("/api/sessions?limit=1");
        const first = (ss.sessions || [])[0];
        if (first) await openSession(first.id);
      } catch (e) {}
    }
  } catch (e) {
    setStatus("err", "连接失败");
    msgBox().innerHTML = `<div class="empty" style="margin-top:80px">
      <div class="big">${icon("warning", 34)}</div>
      <div style="font-size:14px;color:var(--danger);margin-bottom:8px">无法连接后端</div>
      <div style="font-size:12.5px">${esc(e.message)}</div>
      <div style="font-size:12.5px;margin-top:10px">请确认服务已启动：在项目目录运行 <code>start.bat</code></div>
    </div>`;
  }
}
function welcomeWizard(d) {
  /* 已停用：首次启动不再弹出配置向导。
     保留函数体为空实现，避免别处引用时报未定义。 */
  return null;
}
let WS = null, WS_TRIES = 0;
function connectWS() {
  try {
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    const url = proto + "//" + location.host + "/ws" + (TOKEN ? "?token=" + encodeURIComponent(TOKEN) : "");
    WS = new WebSocket(url);
    WS.onopen = () => { WS_TRIES = 0; };
    WS.onmessage = (e) => {
      let ev;
      try { ev = JSON.parse(e.data); } catch (err) { return; }
      if (ev.type === "approval.request") showApproval(ev.data);
      else if (ev.type === "notify") {
        toast(`${(ev.data || {}).title || ""}：${(ev.data || {}).message || ""}`, "");
      } else if (ev.type === "task.update" || ev.type === "goal.update" || ev.type === "memory") {
        // ★ 待办/目标变了就刷新面板（旧写法只顺手点个徽标，清单本身从不显示）
        if (ev.type !== "memory") { try { refreshTodos(); } catch (e2) {} }
        if (S.page !== "memory" && ev.type === "memory") setBadge("memory", "•");
      } else if (ev.type === "scheduler.job.end") {
        const d2 = ev.data || {};
        toast(`定时任务「${d2.name || ""}」${d2.status === "ok" ? "完成" : "失败"}`, d2.status === "ok" ? "ok" : "err");
      }
    };
    WS.onclose = () => {
      WS_TRIES++;
      if (WS_TRIES < 30) setTimeout(connectWS, Math.min(8000, 600 * WS_TRIES));
    };
    WS.onerror = () => {};
  } catch (e) {}
}
/* 移动端菜单 */
$("#menu-btn").onclick = () => $("#app").classList.toggle("side-open");
function checkMobile() {
  $("#menu-btn").style.display = window.innerWidth <= 900 ? "" : "none";
}
window.addEventListener("resize", checkMobile);
checkMobile();

document.addEventListener("click", (e) => {
  const b = e.target.closest("[data-window-action]");
  if (!b || !window.fengcode || typeof window.fengcode.windowAction !== "function") return;
  e.preventDefault();
  window.fengcode.windowAction(b.dataset.windowAction);
});


function paintIcons() {
  const set = (sel, name, size) => {
    const el = $(sel);
    if (el) el.innerHTML = icon(name, size || 16);
  };
  // 应用标记（与生成的 logo 同源：折线 + 收尾圆点）
  const lg = $("#app-logo");
  if (lg) lg.innerHTML = logoSvg(18);
  set("#tool-search", "search", 17);
  set("#tool-clean", "trash", 17);
  set("#tool-settings", "gear", 17);
  set("#new-chat-btn .ico", "plus", 15);
  set("#btn-plus", "plus", 17);
  set("#panel-toggle", "layers", 16);
  set("#ip-close", "close", 15);
  set("#menu-btn", "listChecks", 17);
  set("#back-btn", "chevronLeft", 16);
  set("#mode-chip-ic", "compass", 13);
  set("#perm-chip-ic", "shield", 13);
  set("#send-ic", "arrowUp", 15);
  // ★ 浏览器控制条的 5 个图标按钮：HTML 里是空标签，图标全靠这里补。
  //   之前漏了这一组，界面上只剩 5 个没有内容的圆形底座 —— 看起来像一排
  //   莫名其妙的圆圈，而且用户根本认不出哪个是后退、哪个是刷新。
  set("#bw-back", "chevronLeft", 15);
  set("#bw-forward", "chevronRight", 15);
  set("#bw-reload", "refresh", 15);
  set("#bw-go", "arrowUp", 15);
  set("#bw-close", "close", 15);
  // 侧栏会话搜索框里的放大镜与清除按钮
  set("#side-search-ic", "search", 14);
  set("#side-search-clear", "close", 13);
}

/* ==========================================================================
   执行方式（计划 / 目标，可都不选）与权限等级（三档）

   默认「都不选」，由 AI 自己判断该用计划还是目标；
   用户也可以显式勾选某一个来强制。没有「对话模式」这种强制第三态。
   ========================================================================== */
const MODES = [
  { id: "plan", label: "计划模式", icon: "listChecks",
    desc: "先列出分步计划给你确认，再动手" },
  { id: "goal", label: "目标模式", icon: "target",
    desc: "围绕一个长期目标持续推进，跨轮记住进度" },
];
const PERMS = [
  { id: "deny", label: "只看不改", icon: "eye",
    desc: "只读取和查看，任何写操作都会被拒绝" },
  { id: "ask", label: "工作区可改", icon: "folder",
    desc: "在工作区里可以改文件，动别处会先问你" },
  { id: "allow", label: "完全权限", icon: "unlock",
    desc: "所有操作都放行，不再逐条确认（谨慎使用）" },
];

let CUR_MODE = "";          // "" = 都不选（AI 自主判断）/ "plan" / "goal"
let CUR_PERM = "ask";

function modeMeta(id) { return MODES.find((m) => m.id === id) || null; }
function permMeta(id) { return PERMS.find((p) => p.id === id) || PERMS[1]; }

function paintModeChip() {
  const m = modeMeta(CUR_MODE);
  const lbl = $("#mode-chip-label");
  // 都不选时显示「自动」——不是一个可选模式，只是当前状态的说明
  if (lbl) lbl.textContent = m ? m.label : "自动";
  const chip = $("#mode-chip");
  // 只在显式选了模式时高亮，避免把「自动」伪装成一种模式
  if (chip) chip.classList.toggle("on", !!m);
}
function paintPermChip() {
  const p = permMeta(CUR_PERM);
  const lbl = $("#perm-chip-label");
  if (lbl) lbl.textContent = p.label;
  const chip = $("#perm-chip");
  // 完全权限时红色警示，其余为常态
  if (chip) {
    chip.classList.toggle("on", CUR_PERM === "allow");
    chip.classList.toggle("danger", CUR_PERM === "allow");
  }
}

/** 通用下拉菜单 */
function openChoiceMenu(anchor, items, current, onPick) {
  closeChoiceMenu();
  const pop = document.createElement("div");
  pop.className = "menu-pop show";
  pop.id = "choice-pop";
  pop.innerHTML = items.map((it) => `
    <button class="mi${it.id === current ? " on" : ""}" data-id="${esc(it.id)}">
      ${icon(it.icon, 15)}
      <span class="mi-main">
        <span class="mi-title">${esc(it.label)}</span>
        <span class="mi-desc">${esc(it.desc || "")}</span>
      </span>
      ${it.id === current ? icon("check", 14) : ""}
    </button>`).join("");
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  let top = r.top - h - 6;
  if (top < 8) top = r.bottom + 6;
  let left = r.left;
  if (left + w > window.innerWidth - 10) left = window.innerWidth - w - 10;
  pop.style.top = top + "px";
  pop.style.left = Math.max(8, left) + "px";
  pop.style.position = "fixed";
  $$(".mi", pop).forEach((b) => b.onclick = () => {
    onPick(b.dataset.id);
    closeChoiceMenu();
  });
  setTimeout(() => document.addEventListener("click", closeChoiceMenu, { once: true }), 0);
}
function closeChoiceMenu() {
  const el = $("#choice-pop");
  if (el) el.remove();
}

function openChoiceMenuAt(x, y, items, onPick) {
  closeChoiceMenu();
  const pop = document.createElement("div");
  pop.className = "menu-pop show";
  pop.id = "choice-pop";
  pop.innerHTML = items.map((it) => `<button class="mi" data-id="${esc(it.id)}">${icon(it.icon, 15)}<span class="mi-main"><span class="mi-title">${esc(it.label)}</span></span></button>`).join("");
  document.body.appendChild(pop);
  const w = pop.offsetWidth, h = pop.offsetHeight;
  pop.style.position = "fixed";
  pop.style.left = Math.max(8, Math.min(x, window.innerWidth - w - 10)) + "px";
  pop.style.top = Math.max(8, Math.min(y, window.innerHeight - h - 10)) + "px";
  $$(".mi", pop).forEach((b) => b.onclick = () => { onPick(b.dataset.id); closeChoiceMenu(); });
  setTimeout(() => document.addEventListener("click", closeChoiceMenu, { once: true }), 0);
}

function setMode(id) {
  CUR_MODE = id || "";
  paintModeChip();
  try { localStorage.setItem("fengcode_mode", CUR_MODE); } catch (e) {}
  if (CUR_MODE === "plan") toast("已选计划模式：先给方案，确认后再执行");
  else if (CUR_MODE === "goal") toast("已选目标模式：跨轮记住长期目标");
  else toast("不指定模式：由 AI 自己判断该用计划还是目标");
}

/** 模式菜单：额外带一个「自动（不指定）」项，且已选项可再点一次取消。 */
function openModeMenu() {
  const anchor = $("#mode-chip");
  if (!anchor) return;
  // 复用 openChoiceMenu 的样式，但自己控制选中态与反选逻辑
  closeChoiceMenu();
  const pop = document.createElement("div");
  pop.className = "menu-pop show";
  pop.id = "choice-pop";
  const items = [
    { id: "auto", label: "自动（不指定）", icon: "sparkles",
      desc: "由 AI 自己判断该用计划还是目标" },
    ...MODES,
  ];
  pop.innerHTML = items.map((it) => {
    const on = it.id === "auto" ? !CUR_MODE : it.id === CUR_MODE;
    return `<button class="mi${on ? " on" : ""}" data-id="${esc(it.id)}">
      ${icon(it.icon, 15)}
      <span class="mi-main">
        <span class="mi-title">${esc(it.label)}</span>
        <span class="mi-desc">${esc(it.desc || "")}</span>
      </span>
      ${on ? icon("check", 14) : ""}
    </button>`;
  }).join("");
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  let top = r.top - h - 6;
  if (top < 8) top = r.bottom + 6;
  let left = r.left;
  if (left + w > window.innerWidth - 10) left = window.innerWidth - w - 10;
  pop.style.position = "fixed";
  pop.style.top = top + "px";
  pop.style.left = Math.max(8, left) + "px";
  $$(".mi", pop).forEach((b) => b.onclick = () => {
    const id = b.dataset.id;
    if (id === "auto") setMode("");
    // 点已选中的模式 = 取消（回到自动），这就是「可以不选」
    else setMode(id === CUR_MODE ? "" : id);
    closeChoiceMenu();
  });
  setTimeout(() => document.addEventListener("click", closeChoiceMenu, { once: true }), 0);
}

/* ==========================================================================
   输入区的「+」菜单：添加内容 / 引用 / 命令
   ========================================================================== */
const PLUS_MENU = [
  { group: "添加内容" },
  { id: "attach", label: "添加文件或图片", icon: "image", desc: "从本机选择" },
  { id: "ref-file", label: "引用文件或文件夹", icon: "folder", desc: "从工作区挑选" },
  { id: "ref-session", label: "引用历史会话", icon: "history", desc: "把之前的内容带进来" },
  { id: "slash", label: "使用命令或 Skill", icon: "terminal", desc: "在输入框打 /" },
];

function openPlusMenu() {
  closePlusMenu();
  const anchor = $("#btn-plus");
  if (!anchor) return;
  const pop = document.createElement("div");
  pop.className = "menu-pop show";
  pop.id = "plus-pop";
  pop.innerHTML = PLUS_MENU.map((it) => {
    if (it.group) return `<div class="menu-group">${esc(it.group)}</div>`;
    return `<button class="mi" data-act="${esc(it.id)}">
      ${icon(it.icon, 15)}
      <span class="mi-main">
        <span class="mi-title">${esc(it.label)}</span>
        <span class="mi-desc">${esc(it.desc || "")}</span>
      </span>
    </button>`;
  }).join("");
  document.body.appendChild(pop);
  const r = anchor.getBoundingClientRect();
  const w = pop.offsetWidth, h = pop.offsetHeight;
  let top = r.top - h - 6;
  if (top < 8) top = r.bottom + 6;
  let left = r.left;
  if (left + w > window.innerWidth - 10) left = window.innerWidth - w - 10;
  pop.style.position = "fixed";
  pop.style.top = top + "px";
  pop.style.left = Math.max(8, left) + "px";
  anchor.classList.add("on");
  $$(".mi", pop).forEach((b) => b.onclick = () => {
    closePlusMenu();
    plusAction(b.dataset.act);
  });
  setTimeout(() => document.addEventListener("click", closePlusMenu, { once: true }), 0);
}
function closePlusMenu() {
  const el = $("#plus-pop");
  if (el) el.remove();
  const a = $("#btn-plus");
  if (a) a.classList.remove("on");
}
function plusAction(act) {
  if (act === "attach") { pickAttach(); return; }
  if (act === "ref-file") { openInfoPanel("files"); toast("在右侧点一个文件即可引用"); return; }
  if (act === "ref-session") { refSession(); return; }
  if (act === "slash") {
    const i = $("#input");
    i.value = "/";
    i.focus();
    showSlashMenu("");
    return;
  }
}
function setPerm(id) {
  CUR_PERM = id;
  paintPermChip();
  try { localStorage.setItem("fengcode_perm", id); } catch (e) {}
  api("/api/config", { method: "PATCH", body: { permissions: { mode: id } } })
    .then(() => toast(`权限已切换：${permMeta(id).label}`))
    .catch(() => {});
}

/* ==========================================================================
   斜杠命令菜单
   ========================================================================== */
let SLASH_ITEMS = [];
let SLASH_ACTIVE = 0;

async function buildSlashItems() {
  const items = [
    { name: "/new", desc: "新建会话", kind: "cmd" },
    { name: "/clear", desc: "清空当前显示", kind: "cmd" },
    { name: "/model", desc: "切换模型", kind: "cmd" },
    { name: "/plan", desc: "切到计划模式", kind: "cmd" },
    { name: "/goal", desc: "切到目标模式", kind: "cmd" },
    { name: "/files", desc: "打开项目文件", kind: "cmd" },
    { name: "/compact", desc: "压缩历史上下文（省 token）", kind: "cmd" },
    { name: "/settings", desc: "打开设置", kind: "cmd" },
    { name: "/help", desc: "查看全部命令与技能", kind: "cmd" },
  ];
  try {
    const d = await api("/api/skills");
    for (const s of (d.skills || [])) {
      if (s.enabled === false) continue;
      items.push({ name: "/" + (s.name || ""), desc: s.description || "技能", kind: "skill" });
    }
  } catch (e) {}
  return items;
}

function showSlashMenu(filter) {
  const menu = $("#slash-menu");
  if (!menu) return;
  const q = (filter || "").toLowerCase();
  const hits = SLASH_ITEMS.filter((it) => !q || it.name.slice(1).toLowerCase().includes(q));
  if (!hits.length) { hideSlashMenu(); return; }
  SLASH_ACTIVE = 0;
  const cmds = hits.filter((h) => h.kind === "cmd");
  const skills = hits.filter((h) => h.kind === "skill");
  let html = "";
  const render = (arr) => arr.map((it) => `
    <button class="slash-item" data-name="${esc(it.name)}">
      ${icon(it.kind === "cmd" ? "terminal" : "bolt", 14)}
      <span class="si-name">${esc(it.name)}</span>
      <span class="si-desc">${esc(it.desc || "")}</span>
    </button>`).join("");
  if (cmds.length) html += `<div class="slash-group">命令</div>` + render(cmds);
  if (skills.length) html += `<div class="slash-group">技能</div>` + render(skills);
  menu.innerHTML = html;
  menu.classList.add("show");
  const all = $$(".slash-item", menu);
  if (all.length) all[0].classList.add("active");
  all.forEach((b) => b.onclick = () => applySlash(b.dataset.name));
}

function hideSlashMenu() {
  const menu = $("#slash-menu");
  if (menu) { menu.classList.remove("show"); menu.innerHTML = ""; }
}

async function applySlash(name) {
  hideSlashMenu();
  const inp = $("#input");
  const isCmd = SLASH_ITEMS.some((i) => i.name === name && i.kind === "cmd");
  if (isCmd) {
    inp.value = "";
    inp.style.height = "auto";
    switch (name) {
      case "/new": newSession(); return;
      case "/clear": clearMessages(); emptyState(); toast("已清空当前显示"); return;
      case "/model": $("#model-select").focus(); toast("在上面选一个模型"); return;
      case "/plan": setMode("plan"); return;
      case "/goal": setMode("goal"); return;
      case "/files": openInfoPanel("files"); return;
      case "/compact": runCompact(); return;
      case "/settings": case "/help": SS.current = "general"; go("settings"); return;
      default:
        // ★ 未识别的斜杠命令**不吞**（实测：输入 /xxx 后毫无反应，
        //   以为程序坏了）。当普通消息发给模型，并明确告知「已按普通消息发送」。
        inp.value = name;
        inp.style.height = "auto";
        toast("未识别的命令，已按普通消息发送", "");
        send();
        return;
    }
  }
  inp.value = name + " ";
  inp.focus();
}

/* ★ 2-A：手动压缩（`/compact`）。
   显式入口 + 进度 + 回执。
   三个要点：
     · 回执**渲染在折叠区之外**（就是一条普通 system 消息，别塞进被折叠的轮次里）；
     · **防重复提交**：进行中再点直接忽略；
     · **自动阈值不受影响** —— 这里只是手动触发一次，自动压缩逻辑没动。 */
let COMPACTING = false;
async function runCompact() {
  if (COMPACTING) { toast("已经有一个压缩在进行中", ""); return; }
  if (!S.sessionId) { toast("还没有会话可压缩", ""); return; }
  COMPACTING = true;
  const note = addMessage("system", '<span class="tag accent">压缩</span> 正在整理历史上下文…', { raw: true });
  setStatus("busy", "正在压缩上下文…");
  try {
    const r = await api("/api/chat/compact", {
      method: "POST", body: { session_id: S.sessionId },
    });
    const fmt = (n) => fmtNum(n || 0);
    let msg;
    if (!r.changed) {
      msg = "历史已经很紧凑，这次没有可压缩的内容（自动压缩阈值不受影响）。";
    } else {
      msg = `已压缩：${r.before_messages} → ${r.after_messages} 条，`
        + `约 ${fmt(r.before_tokens)} → ${fmt(r.after_tokens)} tokens`
        + (r.saved_tokens ? `（省下 ${fmt(r.saved_tokens)}）` : "")
        + "。原消息已折叠为摘要，需要细节时让我用工具去查。";
    }
    if (note) note.innerHTML = `<span class="tag ok">压缩完成</span> ${esc(msg)}`;
    toast("上下文已压缩", "ok");
  } catch (e) {
    if (note) note.innerHTML = `<span class="tag err">压缩失败</span> ${esc(e.message || "")}`;
    toast("压缩失败：" + (e.message || ""), "err");
  } finally {
    COMPACTING = false;
    setStatus("ok", "就绪");
    scrollDown(true);
  }
}

/* ==========================================================================
   信息面板：标签切换 + 项目文件树
   ========================================================================== */
let IP_TAB = "info";
let FT_PATH = "";
let CUR_WS_ID = "";

/* ==========================================================================
   内置浏览器（1.2.7）
   --------------------------------------------------------------------------
   桌面端：真实网页由主进程的 WebContentsView 画在 #web-stage 之上（独立图层）。
   网页版（浏览器里打开）：没有 fengcodeBrowser，此时给出说明而不是静默失效。

   ★ 为什么视图要「手动定位」：WebContentsView 不是 DOM 节点，不参与页面布局，
     不会跟着侧栏拖动/窗口缩放自动走。所以每次尺寸变化都要把
     #web-stage 的屏幕坐标算出来交给主进程。
   ========================================================================== */
const BrowserTab = {
  opened: false,
  ready: false,
  unsub: null,

  get api() {
    return (typeof window !== "undefined" && window.fengcodeBrowser) || null;
  },

  /** 把 #web-stage 的屏幕矩形交给主进程（供视图定位）。 */
  bounds() {
    const el = $("#web-stage");
    if (!el) return null;
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8) return null;   // 面板收起/不可见时不定位
    return { x: r.left, y: r.top, width: r.width, height: r.height };
  },

  /** 切到「浏览器」标签：确保视图已创建并定位到占位区。 */
  async enter() {
    const api = this.api;
    if (!api) {
      this.hint("浏览器功能仅在桌面端可用（当前是网页版）。");
      return;
    }
    this.bindOnce();
    const b = this.bounds();
    if (this.opened) {
      // 已经开过：只是重新显形并归位（保持原页面，不重新加载）
      try { await api.setBounds(b || { x: 0, y: 0, width: 0, height: 0 }); } catch (e) {}
      if (!this.ready) {
        try { const r = await api.open("", b); this.ready = !!(r && r.ok); } catch (e) {}
      }
      return;
    }
    this.opened = true;
    try {
      const r = await api.open("", b);
      this.ready = !!(r && r.ok);
      if (!this.ready && r && r.error) this.hint(r.error);
      else this.hint("输入网址后回车即可浏览；点「读取当前页正文」可让 AI 阅读本页。");
    } catch (e) {
      this.hint("打开浏览器失败：" + ((e && e.message) || e));
    }
  },

  /** 切走：隐藏但不销毁（切回来还在原页）。 */
  async leave() {
    const api = this.api;
    if (!api || !this.opened) return;
    try { await api.hide(); } catch (e) {}
  },

  /** 窗口/侧栏尺寸变化时重新定位。 */
  async syncBounds() {
    const api = this.api;
    if (!api || !this.opened || IP_TAB !== "browser") return;
    const b = this.bounds();
    if (!b) return;
    try { await api.setBounds(b); } catch (e) {}
  },

  bindOnce() {
    if (this._bound) return;
    this._bound = true;
    const api = this.api;
    if (!api) return;

    const go = () => this.navigate($("#bw-url").value);
    $("#bw-go").onclick = go;
    $("#bw-url").addEventListener("keydown", (e) => {
      if (e.key === "Enter") { e.preventDefault(); go(); }
    });
    $("#bw-back").onclick = () => api.action("back");
    $("#bw-forward").onclick = () => api.action("forward");
    $("#bw-reload").onclick = () => api.action("reload");
    $("#bw-close").onclick = async () => {
      try { await api.close(); } catch (e) {}
      this.opened = false; this.ready = false;
      const u = $("#bw-url"); if (u) u.value = "";
      this.hint("已关闭网页。输入网址可再次打开。");
      this.setNav(false, false);
    };

    // 窗口缩放 / 侧栏拖动时同步视图位置
    window.addEventListener("resize", () => this.syncBounds());
    const rz = $("#resizer-right");
    if (rz) rz.addEventListener("mouseup", () => setTimeout(() => this.syncBounds(), 60));
    window.addEventListener("mouseup", () => { if (IP_TAB === "browser") setTimeout(() => this.syncBounds(), 60); });

    // 主进程回报的导航/加载状态
    this.unsub = api.onEvent((ev) => {
      if (!ev || !ev.type) return;
      if (ev.type === "loading") {
        const el = $("#bw-status");
        if (el) el.textContent = ev.value ? "正在加载…" : "";
      } else if (ev.type === "navigated") {
        const u = $("#bw-url");
        if (u && ev.url && document.activeElement !== u) u.value = ev.url;
        this.setNav(ev.canGoBack, ev.canGoForward);
        const el = $("#bw-status");
        if (el) el.textContent = "";
      } else if (ev.type === "title") {
        const el = $("#bw-status");
        if (el && ev.title) el.textContent = ev.title;
      } else if (ev.type === "error") {
        this.hint(`页面加载失败（${ev.code}）：${ev.desc || "未知原因"}`);
      }
    });
  },

  setNav(canBack, canFwd) {
    const b = $("#bw-back"), f = $("#bw-forward");
    if (b) b.disabled = !canBack;
    if (f) f.disabled = !canFwd;
  },

  async navigate(url) {
    const api = this.api;
    if (!api) return;
    const s = String(url || "").trim();
    if (!s) return;
    try {
      const r = await api.navigate(s);
      if (r && !r.ok) this.hint(r.error || "打开失败");
      else { this.ready = true; this.hint(""); }
    } catch (e) { this.hint("打开失败：" + ((e && e.message) || e)); }
  },

  hint(text) {
    const el = $("#bw-hint");
    if (el) el.textContent = text || "";
  },

  /** 读取当前页正文（供 AI 调用时复用；界面已不再提供按钮）。 */
  async readPage(maxChars) {
    const api = this.api;
    if (!api) return { ok: false, error: "浏览器功能仅在桌面端可用" };
    try {
      return await api.read(maxChars || 12000);
    } catch (e) {
      return { ok: false, error: String((e && e.message) || e) };
    }
  },
};

function openInfoPanel(tab) {
  S.infoOpen = true;
  $("#infopanel").classList.add("open");
  $("#panel-toggle").classList.add("on");
  switchIpTab(tab || IP_TAB);
}

function switchIpTab(tab) {
  IP_TAB = tab;
  $$(".ip-tab").forEach((b) => b.classList.toggle("active", b.dataset.iptab === tab));
  const info = $("#ip-pane-info"), files = $("#ip-pane-files"), br = $("#ip-pane-browser");
  if (info) info.classList.toggle("active", tab === "info");
  if (files) files.classList.toggle("active", tab === "files");
  if (br) br.classList.toggle("active", tab === "browser");
  if (tab === "files") loadFileTree();
  else if (tab === "browser") BrowserTab.enter();
  else renderInfoPanel();
  // 切走「浏览器」标签时把网页视图藏起来：它是独立图层，不会跟着 DOM 消失
  if (tab !== "browser") BrowserTab.leave();
}

async function loadFileTree() {
  const body = $("#files-body");
  if (!body) return;
  body.innerHTML = `<div class="ft-empty">正在读取…</div>`;
  try {
    const wsQ = CUR_WS_ID ? `&ws=${encodeURIComponent(CUR_WS_ID)}` : "";
    const d = await api(`/api/workspace-files?path=${encodeURIComponent(FT_PATH)}${wsQ}`);
    if (!d.ok) {
      body.innerHTML = `<div class="ft-empty">${esc(d.error || "读取失败")}</div>`;
      return;
    }
    const entries = d.entries || [];
    let html = `<div class="ft-toolbar">
      <button class="btn ghost icon" id="ft-up" title="上一级"${FT_PATH ? "" : " disabled"}>
        ${icon("arrowUp", 14)}</button>
      <span class="ft-path" title="${esc(d.root)}">${esc(d.path ? "/" + d.path : "/")}</span>
      <button class="btn ghost icon" id="ft-home" title="回到根目录">${icon("home", 14)}</button>
      <button class="btn ghost icon" id="ft-refresh" title="刷新">${icon("refresh", 14)}</button>
    </div>`;
    if (!entries.length) {
      html += `<div class="ft-empty">这个文件夹是空的</div>`;
    } else {
      html += entries.map((e) => `
        <button class="ft-item${e.is_dir ? " dir" : ""}" data-name="${esc(e.name)}"
          data-dir="${e.is_dir ? 1 : 0}">
          ${icon(e.is_dir ? "folder" : fileIconName(e.ext), 15)}
          <span class="ft-name" title="${esc(e.name)}">${esc(e.name)}</span>
          <span class="ft-size">${e.is_dir ? "" : fmtBytes(e.size)}</span>
        </button>`).join("");
    }
    body.innerHTML = html;
    $("#ft-up").onclick = () => {
      if (!FT_PATH) return;
      const parts = FT_PATH.split("/").filter(Boolean);
      parts.pop();
      FT_PATH = parts.join("/");
      loadFileTree();
    };
    $("#ft-home").onclick = () => { FT_PATH = ""; loadFileTree(); };
    $("#ft-refresh").onclick = () => loadFileTree();
    $$(".ft-item", body).forEach((b) => b.onclick = () => {
      const full = FT_PATH ? FT_PATH + "/" + b.dataset.name : b.dataset.name;
      if (b.dataset.dir === "1") { FT_PATH = full; loadFileTree(); }
      else previewFile(full);
    });
    $$(".ft-item", body).forEach((b) => b.oncontextmenu = (e) => {
      e.preventDefault();
      const rel = FT_PATH ? FT_PATH + "/" + b.dataset.name : b.dataset.name;
      const abs = d.root ? d.root.replace(/[\\\\\/]$/, "") + "\\\\" + rel.replace(/\//g, "\\\\") : rel;
      openChoiceMenuAt(e.clientX, e.clientY, [
        { id: "abs", label: "复制绝对路径", icon: "copy" },
        { id: "rel", label: "复制相对路径", icon: "copy" },
        { id: "ref", label: "引用到对话", icon: "quote" },
        { id: "open", label: "在文件管理器中显示", icon: "folder" },
      ], async (act) => {
        if (act === "abs") { await navigator.clipboard.writeText(abs); toast("已复制绝对路径", "ok"); }
        if (act === "rel") { await navigator.clipboard.writeText(rel); toast("已复制相对路径", "ok"); }
        if (act === "ref") { const inp = $("#input"); if (inp) { inp.value = `请查看文件：${rel}`; inp.focus(); } }
        if (act === "open") {
          if (window.fengcode && typeof window.fengcode.showInFolder === "function") {
            await window.fengcode.showInFolder(abs);
            toast("已在文件管理器中定位", "ok");
          } else {
            toast("网页版无法调用系统文件管理器，请使用桌面版", "");
          }
        }
      });
    });
  } catch (e) {
    body.innerHTML = `<div class="ft-empty">${esc(e.message)}</div>`;
  }
}

function fileIconName(ext) {
  const e = (ext || "").toLowerCase();
  if (["png", "jpg", "jpeg", "gif", "webp", "bmp", "svg", "ico"].includes(e)) return "image";
  if (["py", "js", "ts", "tsx", "jsx", "go", "rs", "java", "c", "cpp", "h",
       "cs", "rb", "php", "sh", "ps1", "bat", "json", "yaml", "yml",
       "toml", "html", "css", "xml", "sql"].includes(e)) return "code";
  if (["zip", "rar", "7z", "tar", "gz"].includes(e)) return "package";
  if (["db", "sqlite", "sqlite3"].includes(e)) return "database";
  return "file";
}

async function previewFile(rel) {
  const wsQ = CUR_WS_ID ? `ws=${encodeURIComponent(CUR_WS_ID)}&` : "";
  try {
    const d = await api(`/api/workspace-file?${wsQ}path=${encodeURIComponent(rel)}`);
    modal(rel, `<div id="file-preview"><pre>${esc(d.content)}</pre></div>`,
      `<button class="btn" data-close>关闭</button>
       <button class="btn" id="fp-edit">编辑</button>
       <button class="btn primary" id="fp-send">发给 AI</button>`);
    $("#fp-edit").onclick = () => {
      $("#modal-body").innerHTML =
        `<textarea id="fp-text" rows="22" style="width:100%;font-family:Consolas,monospace;font-size:12.5px">${esc(d.content)}</textarea>`;
      $("#modal-foot").innerHTML =
        `<button class="btn" data-close>取消</button>
         <button class="btn primary" id="fp-save">保存</button>`;
      $("#fp-save").onclick = async () => {
        try {
          await api(`/api/workspace-file?${wsQ}`, {
            method: "PUT", body: { path: rel, content: $("#fp-text").value },
          });
          closeModal(); toast("已保存", "ok"); loadFileTree();
        } catch (e) { toast("保存失败：" + e.message, "err"); }
      };
      $$("#modal-foot [data-close]").forEach((b) => b.onclick = closeModal);
    };
    $("#fp-send").onclick = () => {
      closeModal();
      const inp = $("#input");
      inp.value = `看看这个文件：${rel}`;
      inp.focus();
    };
  } catch (e) {
    toast("打不开：" + e.message, "err");
  }
}

/* ==========================================================================
   侧边栏拖拽调宽
   ========================================================================== */
function setupResizers() {
  const sidebar = $("#sidebar");
  const infopanel = $("#infopanel");
  const app = $("#app");

  // 左侧：改 #app 的 grid 列宽（不是改 sidebar.width，
  // 否则在 grid 布局下只会让侧边栏溢出、盖住对话区）。
  const SIDE_MIN = 200, SIDE_MAX = 280;
  const setSideWidth = (w) => {
    const v = Math.round(Math.min(SIDE_MAX, Math.max(SIDE_MIN, w)));
    document.documentElement.style.setProperty("--side-w", v + "px");
    return v;
  };

  const rl = $("#resizer-left");
  if (rl && app) {
    let dragging = false, startX = 0, startW = 0;
    rl.onmousedown = (e) => {
      dragging = true; startX = e.clientX;
      startW = sidebar ? sidebar.getBoundingClientRect().width : 248;
      rl.classList.add("dragging");
      document.body.classList.add("resizing");
      e.preventDefault();
    };
    document.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      setSideWidth(startW + (e.clientX - startX));
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      rl.classList.remove("dragging");
      document.body.classList.remove("resizing");
      try {
        const cur = getComputedStyle(document.documentElement)
          .getPropertyValue("--side-w").trim();
        localStorage.setItem("fg_side_w", cur);
      } catch (err) {}
    });
  }

  const rr = $("#resizer-right");
  if (rr && infopanel) {
    let dragging = false, startX = 0, startW = 0;
    // 与左栏统一：宽度走 CSS 变量 --panel-w，而不是写内联 style.width。
    // 原因：#infopanel.open 有一条 width 声明，内联样式与它同为元素级来源时
    // 谁在后谁赢，旧写法的内联宽度会被 .open 的固定值压住 —— 表现就是
    // 鼠标指针变成拖拽态、拖动却毫无反应（左栏因为改的是变量所以正常）。
    const setPanelWidth = (w) => {
      // ★ 上限改为「屏幕宽度的一半」：右侧栏可以用来长时间放浏览器或项目文件，
      //   固定 560px 在大屏上明显不够。下限保持 240px，避免拖成一条缝。
      const maxW = Math.max(420, Math.floor(window.innerWidth / 2));
      const v = Math.round(Math.min(maxW, Math.max(240, w)));
      document.documentElement.style.setProperty("--panel-w", v + "px");
      // ★ 同时告诉中间对话区「侧栏占了多少」：会话区在侧栏变宽时要跟着收窄，
      //   否则文字会被面板压住（面板是覆盖式定位）。见 CSS 的 --panel-w 用法。
      document.documentElement.style.setProperty("--panel-reserve", v + "px");
      return v;
    };
    rr.onmousedown = (e) => {
      if (!S.infoOpen) return;
      dragging = true; startX = e.clientX;
      startW = infopanel.getBoundingClientRect().width;
      rr.classList.add("dragging");
      document.body.classList.add("resizing");
      e.preventDefault();
    };
    document.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      setPanelWidth(startW - (e.clientX - startX));
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      rr.classList.remove("dragging");
      document.body.classList.remove("resizing");
      try {
        const cur = getComputedStyle(document.documentElement)
          .getPropertyValue("--panel-w").trim();
        localStorage.setItem("fg_panel_w", cur || (Math.round(infopanel.getBoundingClientRect().width) + "px"));
      } catch (err) {}
    });
  }

  const cr = $("#composer-resize");
  const composer = $("#composer");
  const composerInner = $(".composer-inner");  if (cr && composer && composerInner) {
    let dragging = false, startY = 0, startH = 0;
    const MIN_H = 92, MAX_H = 420;   // 卡片高度下限/上限，避免拖成一条缝或占满整屏
    // 把「输入区整块高度」同步给 #messages 的底部留白，保证文字不会从
    // 卡片左右空隙与卡片下方露出来（到对话框以下直接全都看不到）。
    // 变量必须写在 #chat-page 上而不是 #composer 上：#messages 与 #composer
    // 是兄弟节点（index.html:62 / 65），CSS 变量只沿祖先链继承，写在 #composer
    // 上 #messages 读不到，padding-bottom 会落回 CSS 默认值。
    // 高度优先实测 #composer 的真实高度——它是 absolute 容器，高度由卡片顶部
    // 与 bottom:0 共同决定，不等于「卡片高 + padding」，推算值会偏小导致漏字。
    // 页面初始化时 #chat-page 还没 .active（display:none）量到 0，才退回推算。
    const syncComposerBlockVar = () => {
      let blockH = composer.getBoundingClientRect().height;
      if (!blockH) {
        const cardH = parseFloat(getComputedStyle(composerInner).height)
          || parseFloat(composer.style.getPropertyValue("--composer-h")) || 108;
        const padBottom = parseFloat(getComputedStyle(composer).paddingBottom) || 12;
        blockH = cardH + padBottom;
      }
      const host = $("#chat-page") || composer;
      host.style.setProperty("--composer-block-h", Math.ceil(blockH) + "px");
    };
    window.__syncComposerBlock = syncComposerBlockVar;
    cr.onmousedown = (e) => {
      dragging = true; startY = e.clientY;
      // 量卡片自身而不是外层 #composer：#composer 现在是透明容器（只有 padding），
      // 它的高度不含卡片，拿它当基准会让拖动越拖越偏。
      startH = composerInner.getBoundingClientRect().height;
      cr.classList.add("dragging"); document.body.classList.add("resizing"); e.preventDefault();
    };
    document.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      // 向上拖变高：鼠标 y 减小 → startY - clientY 为正
      const h = Math.min(MAX_H, Math.max(MIN_H, startH - (e.clientY - startY)));
      composer.style.setProperty("--composer-h", h + "px");
      syncComposerBlockVar();
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false; cr.classList.remove("dragging"); document.body.classList.remove("resizing");
      syncComposerBlockVar();
      try { localStorage.setItem("fg_composer_h", composer.style.getPropertyValue("--composer-h")); } catch (err) {}
    });
    // 首次进入与窗口尺寸变化时也要对齐一次（视口变化会影响 #composer 实际高度）。
    syncComposerBlockVar();
    window.addEventListener("resize", syncComposerBlockVar);
  }

  // 对话框宽度：左下/右下角手柄。卡片是居中布局，所以按 2 倍位移增长，
  // 这样鼠标所在的那条边始终精确跟着指针走（用户要的「往外扩」手感）。
  const cx = $("#composer-resize-x");
  if (cx && composerInner) {
    let dragging = false, startX = 0, startW = 0;
    const applyW = (w) => {
      const maxW = Math.max(520, window.innerWidth - 60);
      const v = Math.round(Math.min(maxW, Math.max(480, w)));
      document.documentElement.style.setProperty("--chat-max-w", v + "px");
      return v;
    };
    cx.onmousedown = (e) => {
      dragging = true; startX = e.clientX;
      startW = composerInner.getBoundingClientRect().width;
      cx.classList.add("dragging");
      document.body.classList.add("resizing", "resizing-x");
      e.preventDefault(); e.stopPropagation();
    };
    document.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      applyW(startW + (e.clientX - startX) * 2);
    });
    document.addEventListener("mouseup", () => {
      if (!dragging) return;
      dragging = false;
      cx.classList.remove("dragging");
      document.body.classList.remove("resizing", "resizing-x");
      try {
        const cur = getComputedStyle(document.documentElement)
          .getPropertyValue("--chat-max-w").trim();
        if (cur) localStorage.setItem("fg_chat_w", cur);
      } catch (err) {}
    });
  }

  try {
    const sw = localStorage.getItem("fg_side_w");
    if (sw) {
      const n = parseInt(sw, 10);
      if (n) setSideWidth(n);
    }
    const pw = localStorage.getItem("fg_panel_w");
    // 恢复右侧栏宽度也走 CSS 变量（与拖拽时一致），写内联会被 .open 的宽度压住。
    // ★ 必须按当前窗口再夹一次上限：换到更小的屏幕/窗口后，上次存的宽度可能
    //   已经超过「屏幕一半」，直接用会把对话区挤没。
    if (pw && infopanel) {
      const n = parseInt(pw, 10);
      if (n) {
        const maxW = Math.max(420, Math.floor(window.innerWidth / 2));
        const v = Math.round(Math.min(maxW, Math.max(240, n)));
        document.documentElement.style.setProperty("--panel-w", v + "px");
        document.documentElement.style.setProperty("--panel-reserve", v + "px");
      }
    }
    // 对话框宽度：用户拖过就以拖过的为准（覆盖设置里的标准/宽屏预设）
    const cw = localStorage.getItem("fg_chat_w");
    if (cw) document.documentElement.style.setProperty("--chat-max-w", cw);
    const ch = localStorage.getItem("fg_composer_h");
    if (ch && composer) composer.style.setProperty("--composer-h", ch);
    const sm = localStorage.getItem("fengcode_mode");
    // 兼容旧版本存下的 "chat"（那是被去掉的强制对话模式）→ 视为不选
    if (sm && sm !== "chat") CUR_MODE = sm;
    const sp = localStorage.getItem("fengcode_perm");
    if (sp) CUR_PERM = sp;
  } catch (e) {}
}

/* ==========================================================================
   输入框：斜杠菜单 + 自动增高
   ========================================================================== */
function setupComposer() {
  const inp = $("#input");
  if (!inp) return;
  buildSlashItems().then((items) => { SLASH_ITEMS = items; });

  inp.addEventListener("paste", (e) => {
    const text = e.clipboardData && e.clipboardData.getData("text/plain");
    // ★ 提示并入底部状态栏末尾（不要单独一条横条）。
    //   没有换行的短粘贴不提示，免得每粘一句话就闪一下。
    if (!text || !text.includes("\n")) return;
    const strip = $("#sb-paste");
    if (!strip) return;
    const lines = text.split(/\r?\n/).length;
    strip.hidden = false;
    strip.innerHTML = `已粘贴文本 <b>#1</b> · ${lines} 行`;
    // 粘完 6 秒自动隐去，不长期占着状态栏
    clearTimeout(strip._t);
    strip._t = setTimeout(() => { strip.hidden = true; strip.innerHTML = ""; }, 6000);
  });
  inp.addEventListener("input", () => {
    inp.style.height = "auto";
    inp.style.height = Math.min(260, inp.scrollHeight) + "px";
    const v = inp.value;
    if (v.startsWith("/") && !v.includes(" ")) showSlashMenu(v.slice(1));
    else hideSlashMenu();
  });
  inp.addEventListener("keydown", (e) => {
    const menu = $("#slash-menu");
    const open = menu && menu.classList.contains("show");
    if (open) {
      const items = $$(".slash-item", menu);
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        SLASH_ACTIVE = Math.max(0, Math.min(items.length - 1,
          SLASH_ACTIVE + (e.key === "ArrowDown" ? 1 : -1)));
        items.forEach((b, i) => b.classList.toggle("active", i === SLASH_ACTIVE));
        if (items[SLASH_ACTIVE]) items[SLASH_ACTIVE].scrollIntoView({ block: "nearest" });
        return;
      }
      if (e.key === "Enter" && !e.shiftKey && items.length) {
        e.preventDefault();
        applySlash(items[SLASH_ACTIVE].dataset.name);
        return;
      }
      if (e.key === "Escape") { hideSlashMenu(); return; }
    }
  });
  inp.addEventListener("blur", () => setTimeout(hideSlashMenu, 180));
}

/* ---------------- 侧边栏与新按钮绑定 ---------------- */
paintIcons();
paintModeChip();
paintPermChip();

// 新建会话
$("#new-chat-btn").onclick = () => newSession();
// 返回（从设置中心回到对话）
$("#back-btn").onclick = () => go("chat");
// 右侧面板开关
$("#panel-toggle").onclick = () => {
  if (S.infoOpen) toggleInfoPanel(false);
  else openInfoPanel();
};
$("#ip-close").onclick = () => toggleInfoPanel(false);
$$(".ip-tab").forEach((b) => b.onclick = () => switchIpTab(b.dataset.iptab));

// 执行方式 / 权限等级
$("#mode-chip").onclick = (e) => {
  e.stopPropagation();
  openModeMenu();
};
$("#perm-chip").onclick = (e) => {
  e.stopPropagation();
  openChoiceMenu($("#perm-chip"), PERMS, CUR_PERM, setPerm);
};
// 「+」菜单
const plusBtn = $("#btn-plus");
if (plusBtn) plusBtn.onclick = (e) => {
  e.stopPropagation();
  if ($("#plus-pop")) closePlusMenu(); else openPlusMenu();
};

// 添加文件 / 引用文件 / 引用会话（由 + 菜单调用，没有独立按钮了）
function pickAttach() {
  const picker = document.createElement("input");
  picker.type = "file";
  picker.multiple = true;
  picker.onchange = () => {
    // ★ 旧写法 push {name, path: f.path}：Electron 32+ 里 f.path 已不存在，
    //   而且调用的 renderAttachments() 根本没有定义（函数名是 renderAtts）
    //   → 「+ → 添加文件」必然抛 ReferenceError、附件也出不来。
    addFiles(picker.files);
  };
  picker.click();
}

async function refSession() {
  try {
    const d = await api("/api/sessions?limit=40");
    const list = (d.sessions || []).filter((s) => s.id !== S.sessionId);
    modal("引用历史会话", list.length ? `<div style="max-height:50vh;overflow:auto">
      ${list.map((s) => `<button class="ft-item" data-sref="${esc(s.id)}" style="width:100%">
        ${icon("history", 14)}
        <span class="ft-name">${esc(s.title || "未命名")}</span>
        <span class="ft-size">${relTime(s.updated_at || s.created_at)}</span>
      </button>`).join("")}</div>`
      : `<div class="empty">没有别的会话可引用</div>`,
      `<button class="btn" data-close>关闭</button>`);
    $$("[data-sref]").forEach((b) => b.onclick = async () => {
      try {
        const sd = await api("/api/sessions/" + encodeURIComponent(b.dataset.sref));
        const msgs = (sd.messages || []).slice(-12);
        const digest = msgs.map((m) =>
          `【${m.role === "user" ? "我" : "AI"}】${String(m.content || "").slice(0, 300)}`
        ).join("\n");
        const inp = $("#input");
        inp.value = (inp.value ? inp.value + "\n\n" : "") +
          `参考这段历史会话：\n${digest}\n\n基于它继续：`;
        inp.focus();
        closeModal();
        toast("已引用", "ok");
      } catch (e) { toast("读取失败：" + e.message, "err"); }
    });
  } catch (e) { toast(e.message, "err"); }
}

// 底部三个工具
$("#tool-settings").onclick = () => { SS.current = "general"; go("settings"); };
// 会话搜索：展开侧栏里的搜索框并聚焦。
// 之前这里是 prompt() 弹窗——弹窗一关就看不到结果，也没法边改词边看筛选，
// 点下去像没反应。改成内联搜索框，输入即筛，Esc 或清空即恢复全部。
const SESSION_FILTER = { q: "" };
$("#tool-search").onclick = () => {
  const box = $("#side-search");
  if (!box) return;
  if (box.hidden) {
    box.hidden = false;
    const inp = $("#side-search-input");
    if (inp) { inp.value = SESSION_FILTER.q || ""; inp.focus(); inp.select(); }
  } else {
    // 已经展开时再点一次 = 收起并清空筛选
    box.hidden = true;
    SESSION_FILTER.q = "";
    applySessionFilter();
  }
};
$("#tool-clean").onclick = async () => {
  await openSessionTrash();
};

/** 按当前搜索词筛选侧栏会话（不改动数据，只切显示）。 */
function applySessionFilter() {
  const q = (SESSION_FILTER.q || "").trim().toLowerCase();
  $$(".sess-item").forEach((b) => {
    if (!q) { b.style.display = ""; return; }
    const t = (b.querySelector(".s-title")?.textContent || "").toLowerCase();
    b.style.display = t.includes(q) ? "" : "none";
  });
  // 项目分组标题：该组下一条会话都不剩时也藏起来，否则会留一串空标题
  $$("#nav .ws-group").forEach((g) => {
    const items = g.querySelectorAll(".sess-item");
    if (!items.length) { g.style.display = ""; return; }
    const anyVisible = Array.from(items).some((b) => b.style.display !== "none");
    g.style.display = anyVisible ? "" : "none";
  });
}

const ssi = $("#side-search-input");
if (ssi) {
  ssi.addEventListener("input", () => {
    SESSION_FILTER.q = ssi.value || "";
    applySessionFilter();
  });
  ssi.addEventListener("keydown", (e) => {
    if (e.key === "Escape") {
      e.preventDefault();
      ssi.value = "";
      SESSION_FILTER.q = "";
      applySessionFilter();
      const box = $("#side-search");
      if (box) box.hidden = true;
    }
  });
}
const ssc = $("#side-search-clear");
if (ssc) {
  ssc.onclick = () => {
    ssi.value = "";
    SESSION_FILTER.q = "";
    applySessionFilter();
    ssi.focus();
  };
}
setupResizers();
setupComposer();
// 思考块（原生 <details>）展开时定位到最新输出，而不是停在最早那几行。
// 需求：展开思考要能看到最新内容；同时把「点击展开 / 点击收起」文案跟着切换。
document.addEventListener("toggle", (e) => {
  const det = e.target;
  if (!det || !det.classList || !det.classList.contains("reasoning")) return;
  const hint = det.querySelector(".rhint-toggle");
  if (hint) hint.textContent = det.open ? "点击收起" : "点击展开";
  if (!det.open) return;
  const rc = det.querySelector(".rc");
  if (rc) { rc._followTail = true; rc.scrollTop = rc.scrollHeight; }   // 展开时跟到底
  updateJumpBottom();
}, true);
// 思考区滚动：由「用户操作」维护跟随标志。
// ★ 为什么不用内容增长后的距底判断：新内容一进来距底就变大，会把「用户还在底部」
//   误判成「已上滑」，从此不再跟随（实测反馈「滑到最底也不跟」）。
//   scroll 事件不冒泡，所以用 capture 阶段捕获。
document.addEventListener("scroll", (e) => {
  const rc = e.target;
  if (!rc || !rc.classList || !rc.classList.contains("rc")) return;
  const away = rc.scrollHeight - rc.scrollTop - rc.clientHeight;
  rc._followTail = away < 40;   // 滚回底部 → 恢复跟随；上滑离开 → 停止跟随
  updateJumpBottom();
}, true);
const messagesEl = $("#messages");
if (messagesEl) messagesEl.addEventListener("scroll", updateJumpBottom, { passive: true });
watchUserScroll();
const jumpBottom = $("#jump-bottom");
if (jumpBottom) jumpBottom.onclick = jumpToBottom;

// 对话滚动时同步导航高亮
(function setupMsgNav() {
  const box = document.getElementById("messages");
  if (box) {
    let t = null;
    box.addEventListener("scroll", () => {
      clearTimeout(t);
      t = setTimeout(syncMsgNavActive, 80);
    });
  }
})();

/* 快捷键 */
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "k") { e.preventDefault(); newSession(); }
  if ((e.ctrlKey || e.metaKey) && e.key === "/") { e.preventDefault(); go("chat"); $("#input").focus(); }
  if ((e.ctrlKey || e.metaKey) && e.key === ",") { e.preventDefault(); SS.current = "general"; go("settings"); }
  if ((e.ctrlKey || e.metaKey) && e.key === "i") {
    e.preventDefault();
    if (S.infoOpen) toggleInfoPanel(false); else openInfoPanel();
  }
  if ((e.ctrlKey || e.metaKey) && e.key === "b") {
    e.preventDefault();
    const el = $("#sidebar");
    if (el) el.style.display = el.style.display === "none" ? "" : "none";
  }
  if (e.key === "Escape") { hideSlashMenu(); closeChoiceMenu(); }
});

/* 启动 */
go("chat").then(boot);
