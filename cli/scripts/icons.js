/* ==========================================================================
   图标系统：内联 SVG，线性抽象风
   ---------------------------------------------------------------------
   为什么不用 emoji：
     · 各平台渲染差异大（Windows/macOS/Linux 长得不一样）
     · 彩色卡通感强，不适合工具类界面
     · 无法跟随主题色，深色下对比度差
   这里统一用 1.6px 描边的线性图标，颜色走 currentColor。
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
  gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2.8v2.4M12 18.8v2.4M4.5 7.2l2 1.2M17.5 15.6l2 1.2M4.5 16.8l2-1.2M17.5 8.4l2-1.2"/>',
  users: '<circle cx="9" cy="8" r="3"/><path d="M3.5 19a5.5 5.5 0 0 1 11 0"/><path d="M16 5.6a3 3 0 0 1 0 5.4M17 13.7a5.5 5.5 0 0 1 3.5 5.3"/>',
  shield: '<path d="M12 3.2 19 6v6c0 4.2-3 7.2-7 8.8-4-1.6-7-4.6-7-8.8V6z"/><path d="M9.2 12.2l2 2 3.6-3.8"/>',
  flask: '<path d="M9.5 3.5h5v4l3.8 9.4a2 2 0 0 1-1.8 2.8H7.5a2 2 0 0 1-1.8-2.8L9.5 7.5z"/><path d="M7 14.5h10"/>',
  robot: '<rect x="4.5" y="7.5" width="15" height="11" rx="2.5"/><path d="M12 4v3.5"/><circle cx="9" cy="12.5" r="1"/><circle cx="15" cy="12.5" r="1"/><path d="M9.5 16h5"/><path d="M2.5 11.5v3M21.5 11.5v3"/>',

  // —— 操作 ——
  plus: '<path d="M12 5v14M5 12h14"/>',
  minus: '<path d="M5 12h14"/>',
  search: '<circle cx="11" cy="11" r="6.2"/><path d="M15.6 15.6 20 20"/>',
  trash: '<path d="M4.5 7h15"/><path d="M9.5 7V5.2a1 1 0 0 1 1-1h3a1 1 0 0 1 1 1V7"/><path d="M6.5 7l.9 12.1a1.4 1.4 0 0 0 1.4 1.3h6.4a1.4 1.4 0 0 0 1.4-1.3L17.5 7"/><path d="M10.5 11v5.5M13.5 11v5.5"/>',
  more: '<circle cx="5.5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="18.5" cy="12" r="1.4"/>',
  close: '<path d="M6 6l12 12M18 6 6 18"/>',
  check: '<path d="M5 12.5l4.5 4.5L19 7"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 4v4h-4"/>',
  download: '<path d="M12 4v11"/><path d="M7.5 11 12 15.5 16.5 11"/><path d="M5 19.5h14"/>',
  upload: '<path d="M12 16V5"/><path d="M7.5 9.5 12 5l4.5 4.5"/><path d="M5 19.5h14"/>',
  edit: '<path d="M4.5 19.5h4l10-10a2.1 2.1 0 0 0-3-3l-10 10z"/><path d="M14.5 7.5l2 2"/>',
  star: '<path d="m12 4 2.4 5.1 5.6.7-4.1 3.9 1 5.6L12 16.6 7.1 19.3l1-5.6L4 9.8l5.6-.7z"/>',
  send: '<path d="M4.5 12 20 5l-6.5 15-2.2-6.3z"/>',
  stop: '<rect x="6.5" y="6.5" width="11" height="11" rx="1.5"/>',
  file: '<path d="M6.5 3.5h7l4.5 4.5v12.5h-11.5z"/><path d="M13.5 3.5V8h4.5"/>',
  folderSmall: '<path d="M3.5 6.8h5l1.7 1.9H20.5v9.5h-17z"/>',
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
  eyeOff: '<path d="M4 4l16 16"/><path d="M9.5 9.6A2.8 2.8 0 0 0 12 14.8c.8 0 1.5-.3 2-.8"/><path d="M6.5 6.9C4.2 8.4 2.5 12 2.5 12s3.5 5.5 9.5 5.5c1.5 0 2.8-.4 3.9-.9"/><path d="M18.4 15.1c1.9-1.6 3.1-3.1 3.1-3.1s-3.5-5.5-9.5-5.5c-.7 0-1.3.1-1.9.2"/>',
  chevronRight: '<path d="M9.5 6 15.5 12l-6 6"/>',
  chevronDown: '<path d="M6 9.5 12 15.5l6-6"/>',
  chevronLeft: '<path d="M14.5 6 8.5 12l6 6"/>',
  arrowUp: '<path d="M12 19V5"/><path d="M6.5 10.5 12 5l5.5 5.5"/>',
  dots: '<circle cx="5.5" cy="12" r="1.4"/><circle cx="12" cy="12" r="1.4"/><circle cx="18.5" cy="12" r="1.4"/>',
  layers: '<path d="m12 3.5 8.5 4.5L12 12.5 3.5 8z"/><path d="m3.5 12.5 8.5 4.5 8.5-4.5"/><path d="m3.5 16.5 8.5 4.5 8.5-4.5"/>',
  target: '<circle cx="12" cy="12" r="8"/><circle cx="12" cy="12" r="4"/><circle cx="12" cy="12" r="1"/>',
  listChecks: '<path d="M4 6.5l1.8 1.8L9 5"/><path d="M4 12.5l1.8 1.8L9 11"/><path d="M4 18.5l1.8 1.8L9 17"/><path d="M12 6.5h8M12 12.5h8M12 18.5h8"/>',
  sparkle: '<path d="m12 4 1.8 5.2L19 11l-5.2 1.8L12 18l-1.8-5.2L5 11l5.2-1.8z"/>',
  paperclip: '<path d="M17 8.5 9.8 15.7a2.5 2.5 0 0 1-3.5-3.5l8-8a3.5 3.5 0 0 1 5 5l-8 8a4.5 4.5 0 0 1-6.4-6.4L12 4.5"/>',
  terminal: '<path d="M5 7l4 4-4 4"/><path d="M11.5 17h8"/>',
  globe: '<circle cx="12" cy="12" r="8.5"/><path d="M3.5 12h17"/><path d="M12 3.5c2.2 2.4 3.3 5.3 3.3 8.5s-1.1 6.1-3.3 8.5c-2.2-2.4-3.3-5.3-3.3-8.5S9.8 5.9 12 3.5z"/>',
  database: '<ellipse cx="12" cy="6.5" rx="7.5" ry="3"/><path d="M4.5 6.5v11c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3v-11"/><path d="M4.5 12c0 1.7 3.4 3 7.5 3s7.5-1.3 7.5-3"/>',
  key: '<circle cx="8" cy="15.5" r="3.5"/><path d="M10.5 13 19 4.5"/><path d="M16.5 7l2.5 2.5"/><path d="M14.5 9l2.5 2.5"/>',
  palette: '<path d="M12 3.5a8.5 8.5 0 0 0 0 17c1 0 1.7-.8 1.7-1.7 0-.5-.2-.9-.5-1.2-.3-.3-.5-.7-.5-1.2 0-1 .8-1.7 1.7-1.7h1.9a4.2 4.2 0 0 0 4.2-4.2c0-3.9-3.8-7-8.5-7z"/><circle cx="7.8" cy="10" r="1.1"/><circle cx="11.5" cy="7.5" r="1.1"/><circle cx="15.8" cy="9.5" r="1.1"/>',
  code: '<path d="M8.5 8 4.5 12l4 4"/><path d="M15.5 8l4 4-4 4"/><path d="M13.5 5.5l-3 13"/>',
  package: '<path d="m12 3.5 8 4.3v8.4l-8 4.3-8-4.3V7.8z"/><path d="m4 7.8 8 4.3 8-4.3"/><path d="M12 12.1v8.4"/>',
  cpu: '<rect x="7" y="7" width="10" height="10" rx="2"/><path d="M10 3.5V7M14 3.5V7M10 17v3.5M14 17v3.5M3.5 10H7M3.5 14H7M17 10h3.5M17 14h3.5"/>',
};

/** 生成一个图标的 HTML。size 默认 16px */
function icon(name, size, cls) {
  const body = ICONS[name] || ICONS.info;
  const s = size || 16;
  return `<svg class="ic${cls ? " " + cls : ""}" width="${s}" height="${s}" viewBox="0 0 24 24" ` +
    `fill="none" stroke="currentColor" stroke-width="1.7" ` +
    `stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${body}</svg>`;
}
