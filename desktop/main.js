/**
 * Fengcode 桌面端主进程
 *
 * 架构（双进程设计）：
 *   Electron 主进程  ←-- HTTP/SSE + WebSocket -->  Python 后端（fengcode serve）
 *
 * - 主进程负责：拉起并守护 Python 服务、窗口/托盘/菜单/快捷键/通知
 * - 渲染进程负责：界面（直接加载后端托管的单页 UI，避免重复实现）
 * - 两者通过本地 HTTP 通信，后端崩溃可独立重启而不影响窗口
 *
 * 环境变量：
 *   FENGCODE_PYTHON    指定 Python 解释器路径
 *   FENGCODE_HOME      数据目录
 *   FENGCODE_NO_SPAWN  设为 1 时不自动拉起后端（连已有的服务）
 */

const { app, BrowserWindow, Tray, Menu, shell, dialog,
        globalShortcut, Notification, ipcMain, nativeImage, clipboard,
        WebContentsView } = require("electron");
const { spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const http = require("http");
const net = require("net");

// ---------------------------------------------------------------- 配置

const IS_DEV = process.argv.includes("--dev");
const NO_SPAWN = process.env.FENGCODE_NO_SPAWN === "1";
const HOST = "127.0.0.1";
const PORT = parseInt(process.env.FENGCODE_PORT || "7845", 10);
const BASE_URL = `http://${HOST}:${PORT}`;
// 开发态：本文件在 desktop\，项目根在上两级
// 打包后：__dirname 在 resources\app\，后端由 extraResources 放到 resources\backend\
const DEV_PROJECT_ROOT = path.resolve(__dirname, "..", "cli");

/**
 * 决定用哪个后端、在哪个目录跑。
 *
 * 打包态：后端由 electron-builder 的 extraResources 放到 resources\backend\，
 *         直接调它的 exe（用户机器不需要装 Python）。
 * 开发态：用系统 Python 跑 命令端 的源码。
 */
function resolveBackendTarget() {
  // 1) 打包态：应用自带的 exe
  const packagedExe = path.join(
    process.resourcesPath || "", "backend",
    process.platform === "win32" ? "Fengcode.exe" : "fengcode"
  );
  if (app.isPackaged && fs.existsSync(packagedExe)) {
    return { kind: "exe", command: packagedExe, cwd: path.dirname(packagedExe) };
  }
  // 2) 环境变量显式指定
  if (process.env.FENGCODE_BACKEND && fs.existsSync(process.env.FENGCODE_BACKEND)) {
    const p = process.env.FENGCODE_BACKEND;
    return { kind: "exe", command: p, cwd: path.dirname(p) };
  }
  // 3) 开发态：系统 Python + 源码
  return { kind: "python", command: findPython(), cwd: DEV_PROJECT_ROOT };
}

let mainWindow = null;
let tray = null;
let backend = null;          // Python 子进程
let backendStartedByUs = false;
let quitting = false;
let restartAttempts = 0;
const MAX_RESTART = 3;

// ---------------------------------------------------------------- 日志

const LOG_DIR = path.join(app.getPath("userData"), "logs");
try { fs.mkdirSync(LOG_DIR, { recursive: true }); } catch {}
const LOG_FILE = path.join(LOG_DIR, "desktop.log");

function log(...args) {
  const line = `[${new Date().toISOString()}] ${args.join(" ")}`;
  console.log(line);
  try { fs.appendFileSync(LOG_FILE, line + "\n"); } catch {}
}

// ---------------------------------------------------------------- 工具

/** 找一个可用的 Python 解释器 */
function findPython() {
  if (process.env.FENGCODE_PYTHON) {
    return process.env.FENGCODE_PYTHON;
  }
  const cands = [];
  if (process.platform === "win32") {
    const la = process.env.LOCALAPPDATA || "";
    const pf = process.env.ProgramFiles || "";
    for (const v of ["313", "312", "311", "310"]) {
      cands.push(path.join(la, "Programs", "Python", `Python${v}`, "python.exe"));
      cands.push(path.join(pf, `Python${v}`, "python.exe"));
    }
    cands.push("python.exe", "python");
  } else {
    cands.push("/usr/bin/python3", "/usr/local/bin/python3", "python3", "python");
  }
  for (const c of cands) {
    try {
      if (path.isAbsolute(c)) {
        if (fs.existsSync(c)) return c;
      } else {
        return c;   // 靠 PATH 解析
      }
    } catch {}
  }
  return "python";
}

/** 探测端口上是否已有服务在跑 */
function probeHealth(timeout = 1200) {
  return new Promise((resolve) => {
    const req = http.get(`${BASE_URL}/health`, { timeout }, (res) => {
      let body = "";
      res.on("data", (d) => (body += d));
      res.on("end", () => {
        try {
          const j = JSON.parse(body);
          resolve(j && j.app === "Fengcode" ? j : null);
        } catch { resolve(null); }
      });
    });
    req.on("error", () => resolve(null));
    req.on("timeout", () => { req.destroy(); resolve(null); });
  });
}

/** 端口是否被别的程序占用 */
function portInUse(port, host = HOST) {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once("error", (e) => resolve(e.code === "EADDRINUSE"));
    srv.once("listening", () => srv.close(() => resolve(false)));
    srv.listen(port, host);
  });
}

/** 等后端就绪（间隔短一点，体感更快） */
async function waitReady(maxMs = 45000) {
  const t0 = Date.now();
  while (Date.now() - t0 < maxMs) {
    const h = await probeHealth();
    if (h) return h;
    await new Promise((r) => setTimeout(r, 150));
  }
  return null;
}

// ---------------------------------------------------------------- 后端

async function startBackend() {
  if (NO_SPAWN) {
    log("NO_SPAWN=1，不拉起后端");
    return true;
  }
  // 已有服务在跑就直接用
  const existing = await probeHealth();
  if (existing) {
    log("已检测到运行中的后端，复用：", JSON.stringify(existing));
    backendStartedByUs = false;
    return true;
  }
  if (await portInUse(PORT)) {
    const { response } = await dialog.showMessageBox({
      type: "error",
      title: "端口被占用",
      message: `端口 ${PORT} 已被其它程序占用，Fengcode 无法启动。`,
      detail: "请关闭占用该端口的程序，或设置环境变量 FENGCODE_PORT 换一个端口。",
      buttons: ["打开日志目录", "退出"],
      defaultId: 1,
    });
    if (response === 0) shell.openPath(LOG_DIR);
    return false;
  }

  const target = resolveBackendTarget();
  const packaged = target.kind === "exe";
  const command = target.command;
  log("启动后端：", command, "（" + target.kind + "）cwd =", target.cwd);

  const args = packaged
    ? ["serve", "--host", HOST, "--port", String(PORT)]
    : ["-m", "fengcode.cli.main", "serve", "--host", HOST, "--port", String(PORT)];
  backend = spawn(command, args, {
    cwd: target.cwd,
    env: {
      ...process.env,
      PYTHONIOENCODING: "utf-8",
      PYTHONUTF8: "1",
    },
    windowsHide: true,
  });

  backendStartedByUs = true;

  backend.stdout.on("data", (d) => log("[后端]", String(d).trim()));
  backend.stderr.on("data", (d) => log("[后端错误]", String(d).trim()));
  backend.on("exit", (code, signal) => {
    log(`后端退出：code=${code} signal=${signal}`);
    backend = null;
    if (!quitting && backendStartedByUs) {
      if (restartAttempts < MAX_RESTART) {
        restartAttempts++;
        log(`尝试重启后端（第 ${restartAttempts} 次）`);
        setTimeout(async () => {
          if (await startBackend()) {
            if (mainWindow) mainWindow.loadURL(BASE_URL);
          }
        }, 1500);
      } else {
        notify("后端已停止", "多次重启失败，请检查日志");
      }
    }
  });

  const ready = await waitReady();
  if (!ready) {
    log("后端启动超时");
    const h = await probeHealth();
    if (!h) {
      const { response } = await dialog.showMessageBox({
        type: "error",
        title: "启动失败",
        message: "Fengcode 后端未能在 45 秒内就绪。",
        detail:
          "常见原因：\n" +
          "· 依赖未安装：在项目目录执行  python -m pip install -e .\n" +
          "· Python 路径不对：设置环境变量 FENGCODE_PYTHON\n" +
          `· 日志：${LOG_FILE}`,
        buttons: ["打开日志目录", "查看环境自检", "退出"],
        defaultId: 0,
      });
      if (response === 0) shell.openPath(LOG_DIR);
      if (response === 1) runDoctor();
      return false;
    }
  }
  log("后端已就绪");
  return true;
}

function stopBackend() {
  if (backend && backendStartedByUs) {
    log("停止后端");
    try {
      if (process.platform === "win32") {
        spawn("taskkill", ["/F", "/T", "/PID", String(backend.pid)], { windowsHide: true });
      } else {
        backend.kill("SIGTERM");
      }
    } catch (e) {
      log("停止后端失败：", e.message);
    }
  }
}

/** 在终端里跑环境自检 */
function runDoctor() {
  const target = resolveBackendTarget();
  const { spawn: sp } = require("child_process");
  // 打包态直接调 exe 的 doctor；开发态用 python -m
  const doctorCmd = target.kind === "exe"
    ? `"${target.command}" doctor`
    : `"${target.command}" -m fengcode.cli.main doctor`;
  if (process.platform === "win32") {
    sp("cmd", ["/c", "start", "cmd", "/k", doctorCmd], {
      cwd: target.cwd, windowsHide: false, shell: true,
    });
  } else {
    sp("x-terminal-emulator", ["-e", "bash", "-c",
      `${target.command} ${target.kind === "exe" ? "doctor" : "-m fengcode.cli.main doctor"}; read -p "按回车关闭"`],
      { cwd: target.cwd });
  }
}

// ---------------------------------------------------------------- 通知

function notify(title, body, onClick) {
  try {
    if (!Notification.isSupported()) return;
    const n = new Notification({ title, body, silent: false });
    if (onClick) n.on("click", onClick);
    n.show();
  } catch (e) {
    log("通知失败：", e.message);
  }
}

// ---------------------------------------------------------------- 窗口状态

const WIN_STATE_FILE = path.join(app.getPath("userData"), "window-state.json");

function loadWindowState() {
  try {
    return JSON.parse(fs.readFileSync(WIN_STATE_FILE, "utf8"));
  } catch {
    return null;
  }
}

function saveWindowState() {
  if (!mainWindow) return;
  try {
    const bounds = mainWindow.getBounds();
    fs.writeFileSync(
      WIN_STATE_FILE,
      JSON.stringify({
        fullscreen: mainWindow.isFullScreen(),
        maximized: mainWindow.isMaximized(),
        width: bounds.width,
        height: bounds.height,
        x: bounds.x,
        y: bounds.y,
      })
    );
  } catch (e) {
    log("保存窗口状态失败：", e.message);
  }
}

// ---------------------------------------------------------------- 窗口

/** 判断保存的位置是否还在当前某个显示器可见范围内。
    显示器拔掉/换分辨率后，旧坐标可能落在屏幕外，这时必须丢弃、回到居中，
    否则窗口会"跑到看不见的地方"（表现为启动后像没位置一样）。 */
function isBoundsVisible(b) {
  try {
    const { screen } = require("electron");
    const disps = screen.getAllDisplays();
    const minVisible = 80;   // 至少要有 80px 落在某块屏内
    return disps.some((d) => {
      const a = d.workArea;
      const w = Math.min(b.x + b.width, a.x + a.width) - Math.max(b.x, a.x);
      const h = Math.min(b.y + b.height, a.y + a.height) - Math.max(b.y, a.y);
      return w >= minVisible && h >= minVisible;
    });
  } catch {
    return true;
  }
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1280,
    height: 840,
    minWidth: 820,
    minHeight: 560,
    title: "Fengcode",
    frame: false,
    backgroundColor: "#f7f7f8",
    show: false,
    autoHideMenuBar: true,
    icon: path.join(__dirname, "icon.png"),
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      spellcheck: false,
    },
  });

  // 立刻显示窗口：先渲染一个骨架屏，后端就绪后再切到真实界面。
  // 这样用户感觉是"秒开"，而不是盯着空白等后端。
  mainWindow.show();
  // 默认全屏（最大化）。用户上次手动调整过窗口则沿用上次状态。
  try {
    const saved = loadWindowState();
    if (saved && saved.fullscreen) {
      mainWindow.setFullScreen(true);
    } else if (saved && saved.maximized === false) {
      // 用户明确取消过最大化 → 用小窗，但恢复上次尺寸
      if (saved.width && saved.height) {
        mainWindow.setSize(saved.width, saved.height);
      }
      // ★ 位置要先校验还在可见屏幕内：显示器拔掉/换分辨率后旧坐标可能落在屏外，
      //   直接 setPosition 会让窗口"消失"，看起来就像每次都回到左上角。
      if (typeof saved.x === "number" && typeof saved.y === "number"
          && isBoundsVisible({ x: saved.x, y: saved.y, width: saved.width || 1280, height: saved.height || 840 })) {
        mainWindow.setPosition(saved.x, saved.y);
      } else {
        mainWindow.center();
      }
    } else {
      mainWindow.maximize();
    }
  } catch (e) {
    log("恢复窗口状态失败：", e.message);
    try { mainWindow.maximize(); } catch {}
  }

  const SPLASH = "data:text/html;charset=utf-8," + encodeURIComponent(`
    <html><head><meta charset="utf-8"><style>
      html,body{height:100%;margin:0}
      body{
        font-family:-apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
        background:#f7f7f8;color:#1c1c1f;
        display:flex;align-items:center;justify-content:center;
        -webkit-user-select:none;user-select:none;
      }
      .wrap{text-align:center;animation:in .35s ease-out}
      @keyframes in{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
      .logo{
        width:56px;height:56px;border-radius:14px;margin:0 auto 16px;
        background:linear-gradient(135deg,#4f46e5,#0ea5e9);
        display:flex;align-items:center;justify-content:center;position:relative;
      }
      .logo::before{
        content:"";position:absolute;width:26px;height:3.4px;background:#fff;
        border-radius:2px;box-shadow:0 9px 0 0 #fff;transform:translate(-2px,-4px);
      }
      .logo::after{
        content:"";position:absolute;width:8px;height:3.4px;background:#fff;
        border-radius:2px;transform:translate(-8px,5px);opacity:.8;
      }
      h1{font-size:16px;font-weight:650;margin:0 0 6px}
      p{font-size:12.5px;color:#86868f;margin:0}
      .bar{
        width:150px;height:3px;border-radius:2px;background:#e2e2e6;
        margin:18px auto 0;overflow:hidden;
      }
      .bar > i{
        display:block;height:100%;width:40%;border-radius:2px;
        background:linear-gradient(90deg,#4f46e5,#0ea5e9);
        animation:slide 1.1s ease-in-out infinite;
      }
      @keyframes slide{0%{transform:translateX(-110%)}100%{transform:translateX(320%)}}
    </style></head><body>
      <div class="wrap">
        <div class="logo"></div>
        <h1>Fengcode</h1>
        <p>正在启动…</p>
        <div class="bar"><i></i></div>
      </div>
    </body></html>`);

  mainWindow.loadURL(SPLASH).catch(() => {});

  // 外部链接用系统浏览器打开，不在应用内跳走
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    if (/^https?:\/\//.test(url) && !url.startsWith(BASE_URL)) {
      shell.openExternal(url);
      return { action: "deny" };
    }
    return { action: "allow" };
  });

  mainWindow.on("close", (e) => {
    // 关窗口 = 最小化到托盘静默后台运行（除非真退出）。
    // 后台常驻但不弹任何通知，用户需要时点托盘图标即可。
    if (!quitting && tray) {
      e.preventDefault();
      mainWindow.hide();
    }
  });

  mainWindow.on("closed", () => {
    destroyBrowserView();     // ★ 浏览器视图必须先拆：它挂在窗口上，窗口没了会留悬挂引用
    mainWindow = null;
  });

  // 记住窗口尺寸/全屏状态，下次启动沿用
  for (const ev of ["resize", "move", "maximize", "unmaximize", "enter-full-screen", "leave-full-screen"]) {
    mainWindow.on(ev, () => saveWindowState());
  }
}

// ---------------------------------------------------------------- 内置浏览器
/*
 * 右侧栏内置浏览器：用 WebContentsView 嵌一个真实浏览器视图到主窗口上。
 *
 * 为什么用 WebContentsView 而不是 <webview> 标签或 iframe：
 *   · iframe：绝大多数网站用 X-Frame-Options / CSP 拒绝被嵌入，等于打不开；
 *   · <webview>：Electron 已不推荐，且安全边界要自己再搭一层；
 *   · WebContentsView：Electron 33 的现行方案，能设独立 webPreferences，
 *     天然和主界面隔离（不同进程、不同存储分区）。
 *
 * ★ 安全边界（用户明确要求「http(s)、禁 file://、防越权」）：
 *   1. 只允许 http/https —— file:// / javascript: / data: 一律拒绝导航；
 *   2. 独立 session 分区（persist:fengcode-browser），与主界面互不共享登录态；
 *   3. nodeIntegration=false + contextIsolation=true —— 网页拿不到 Node；
 *   4. 新窗口一律交给系统浏览器，不在视图内开（避免弹出无法控制的窗口）；
 *   5. 不暴露任何 preload —— 浏览器页里没有 fengcode.* 接口，防越权。
 */
let browserView = null;
let browserVisible = false;
let browserBounds = { x: 0, y: 0, width: 0, height: 0 };

/** 允许的协议：只放 http/https。 */
function isAllowedBrowserUrl(url) {
  return typeof url === "string" && /^https?:\/\//i.test(url.trim());
}

/** 把用户输入补成完整 URL（没写协议时按 https 处理，本地地址按 http）。 */
function normalizeBrowserUrl(input) {
  const s = String(input || "").trim();
  if (!s) return "";
  if (/^https?:\/\//i.test(s)) return s;
  if (/^(localhost|127\.0\.0\.1|\[::1\])(:\d+)?(\/|$)/i.test(s)) return "http://" + s;
  // 明显不是域名（无点、无空格）时交给搜索引擎，避免「输入关键词打不开」
  if (!/^[\w-]+(\.[\w-]+)+/.test(s)) {
    return "https://www.bing.com/search?q=" + encodeURIComponent(s);
  }
  return "https://" + s;
}

function ensureBrowserView() {
  if (!mainWindow) return null;
  if (browserView && !browserView.webContents.isDestroyed()) return browserView;

  browserView = new WebContentsView({
    webPreferences: {
      // ★ 独立分区：与主界面不共享 cookie/存储，网页登不进 Fengcode 自己的会话
      partition: "persist:fengcode-browser",
      nodeIntegration: false,
      contextIsolation: true,
      sandbox: true,
      // 不挂 preload：浏览器页里没有 fengcode.* 接口（防越权）
      webSecurity: true,
    },
  });
  const wc = browserView.webContents;

  // 导航白名单：非 http(s) 一律拦下
  wc.on("will-navigate", (e, url) => {
    if (!isAllowedBrowserUrl(url)) {
      e.preventDefault();
      log("内置浏览器已拦截非 http(s) 导航：", url.slice(0, 80));
    }
  });
  wc.on("will-redirect", (e, url) => {
    if (!isAllowedBrowserUrl(url)) e.preventDefault();
  });
  // 新窗口 → 系统浏览器
  wc.setWindowOpenHandler(({ url }) => {
    if (isAllowedBrowserUrl(url)) shell.openExternal(url);
    return { action: "deny" };
  });
  // 加载状态回报给界面
  const send = (channel, payload) => {
    try {
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.webContents.send(channel, payload);
    } catch (e) { /* 窗口已关，忽略 */ }
  };
  wc.on("did-start-loading", () => send("fengcode:browser", { type: "loading", value: true }));
  wc.on("did-stop-loading", () => send("fengcode:browser", { type: "loading", value: false }));
  wc.on("did-navigate", (_e, url) => send("fengcode:browser", {
    type: "navigated", url, canGoBack: wc.navigationHistory.canGoBack(),
    canGoForward: wc.navigationHistory.canGoForward(),
  }));
  wc.on("did-navigate-in-page", (_e, url) => send("fengcode:browser", {
    type: "navigated", url, canGoBack: wc.navigationHistory.canGoBack(),
    canGoForward: wc.navigationHistory.canGoForward(),
  }));
  wc.on("page-title-updated", (_e, title) => send("fengcode:browser", { type: "title", title }));
  wc.on("did-fail-load", (_e, code, desc, url) => {
    if (code === -3) return;    // -3 = 用户主动中止，不算错误
    send("fengcode:browser", { type: "error", code, desc: String(desc || ""), url: String(url || "") });
  });

  mainWindow.contentView.addChildView(browserView);
  browserView.setVisible(false);
  return browserView;
}

/** 显示并定位浏览器视图（矩形由界面按右侧栏实际位置算好传进来）。 */
function showBrowserView(bounds) {
  const v = ensureBrowserView();
  if (!v || !mainWindow) return false;
  const b = bounds || {};
  browserBounds = {
    x: Math.max(0, Math.round(Number(b.x) || 0)),
    y: Math.max(0, Math.round(Number(b.y) || 0)),
    width: Math.max(0, Math.round(Number(b.width) || 0)),
    height: Math.max(0, Math.round(Number(b.height) || 0)),
  };
  v.setBounds(browserBounds);
  v.setVisible(true);
  browserVisible = true;
  return true;
}

function hideBrowserView() {
  if (browserView && !browserView.webContents.isDestroyed()) browserView.setVisible(false);
  browserVisible = false;
}

function destroyBrowserView() {
  try {
    if (browserView && !browserView.webContents.isDestroyed()) {
      browserView.webContents.stop();
      if (mainWindow) mainWindow.contentView.removeChildView(browserView);
      browserView.webContents.close();
    }
  } catch (e) { /* 关窗过程中失败可忽略 */ }
  browserView = null;
  browserVisible = false;
}

/** 取当前页正文（给「让 AI 读这个页面」用）。
    只取可读文本，顺带限制长度 —— 避免整页 HTML 把上下文撑爆。 */
async function readBrowserText(maxChars = 12000) {
  const v = browserView;
  if (!v || v.webContents.isDestroyed()) return { ok: false, error: "浏览器尚未打开" };
  try {
    const info = await v.webContents.executeJavaScript(`(() => {
      const pick = (sel) => {
        const el = document.querySelector(sel);
        return el ? (el.innerText || "").trim() : "";
      };
      const main = pick("article") || pick("main") || pick('[role="main"]');
      const body = (document.body ? document.body.innerText : "") || "";
      return {
        title: document.title || "",
        url: location.href,
        text: (main || body).replace(/\\n{3,}/g, "\\n\\n").trim(),
      };
    })()`, true);
    const text = String((info && info.text) || "").slice(0, maxChars);
    return {
      ok: true,
      url: String((info && info.url) || ""),
      title: String((info && info.title) || ""),
      text,
      truncated: String((info && info.text) || "").length > maxChars,
    };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
}

function showWindow() {
  if (!mainWindow) {
    createWindow();
    return;
  }
  if (mainWindow.isMinimized()) mainWindow.restore();
  mainWindow.show();
  mainWindow.focus();
}

// ---------------------------------------------------------------- 托盘

function createTray() {
  const iconPath = path.join(__dirname, "tray.png");
  let img;
  try {
    img = nativeImage.createFromPath(fs.existsSync(iconPath) ? iconPath : path.join(__dirname, "icon.png"));
    if (img.isEmpty()) img = nativeImage.createEmpty();
  } catch {
    img = nativeImage.createEmpty();
  }
  tray = new Tray(img);
  tray.setToolTip("Fengcode");

  const menu = Menu.buildFromTemplate([
    { label: "打开 Fengcode", click: showWindow },
    { type: "separator" },
    {
      label: "新建对话",
      click: () => {
        showWindow();
        if (mainWindow) mainWindow.loadURL(BASE_URL + "/?new=1");
      },
    },
    { label: "打开数据目录", click: () => openDataDir() },
    { type: "separator" },
    {
      label: "重启后端",
      click: async () => {
        stopBackend();
        await new Promise((r) => setTimeout(r, 800));
        if (await startBackend() && mainWindow) mainWindow.loadURL(BASE_URL);
      },
    },
    { label: "环境自检", click: runDoctor },
    { label: "查看日志", click: () => shell.openPath(LOG_FILE) },
    { type: "separator" },
    {
      label: "退出",
      click: () => {
        quitting = true;
        app.quit();
      },
    },
  ]);
  tray.setContextMenu(menu);
  tray.on("click", showWindow);
  tray.on("double-click", showWindow);
}

function openDataDir() {
  // 优先问后端数据目录在哪
  http.get(`${BASE_URL}/api/status`, { timeout: 2000 }, (res) => {
    let b = "";
    res.on("data", (d) => (b += d));
    res.on("end", () => {
      try {
        const j = JSON.parse(b);
        if (j.workspace) return shell.openPath(j.workspace);
      } catch {}
      shell.openPath(resolveBackendTarget().cwd);
    });
  }).on("error", () => shell.openPath(resolveBackendTarget().cwd));
}

// ---------------------------------------------------------------- 应用菜单

function createMenu() {
  const template = [
    {
      label: "文件",
      submenu: [
        { label: "新建对话", accelerator: "CmdOrCtrl+N", click: () => { showWindow(); if (mainWindow) mainWindow.loadURL(BASE_URL + "/?new=1"); } },
        { label: "重新加载界面", accelerator: "CmdOrCtrl+R", click: () => mainWindow && mainWindow.reload() },
        { type: "separator" },
        { label: "打开数据目录", click: openDataDir },
        { type: "separator" },
        { label: "退出", accelerator: "CmdOrCtrl+Q", click: () => { quitting = true; app.quit(); } },
      ],
    },
    {
      label: "编辑",
      submenu: [
        { role: "undo", label: "撤销" },
        { role: "redo", label: "重做" },
        { type: "separator" },
        { role: "cut", label: "剪切" },
        { role: "copy", label: "复制" },
        { role: "paste", label: "粘贴" },
        { role: "selectAll", label: "全选" },
      ],
    },
    {
      label: "视图",
      submenu: [
        { role: "zoomIn", label: "放大" },
        { role: "zoomOut", label: "缩小" },
        { role: "resetZoom", label: "重置缩放" },
        { type: "separator" },
        { role: "togglefullscreen", label: "全屏" },
        { label: "开发者工具", accelerator: "F12", click: () => mainWindow && mainWindow.webContents.toggleDevTools() },
      ],
    },
    {
      label: "帮助",
      submenu: [
        { label: "环境自检", click: runDoctor },
        { label: "查看日志", click: () => shell.openPath(LOG_FILE) },
        { label: "日志目录", click: () => shell.openPath(LOG_DIR) },
        { type: "separator" },
        { label: "项目主页", click: () => shell.openExternal("https://github.com/imcreazy/DeepSeek-Fengcode") },
        {
          label: "关于",
          click: () =>
            dialog.showMessageBox({
              type: "info",
              title: "关于 Fengcode",
              message: "Fengcode 桌面端",
              detail:
                `版本：${app.getVersion()}\n` +
                `Electron：${process.versions.electron}\n` +
                `后端：${BASE_URL}\n` +
                `数据目录日志：${LOG_FILE}`,
              buttons: ["确定"],
            }),
        },
      ],
    },
  ];
  Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

// ---------------------------------------------------------------- IPC

ipcMain.handle("fengcode:info", async () => {
  const health = await probeHealth();
  const target = resolveBackendTarget();
  return {
    baseUrl: BASE_URL,
    backend: health,
    version: app.getVersion(),
    logFile: LOG_FILE,
    packaged: app.isPackaged,
    backendKind: target.kind,
    backendCommand: target.command,
    projectRoot: target.cwd,
  };
});

ipcMain.handle("fengcode:openExternal", (_e, url) => {
  if (typeof url === "string" && /^https?:\/\//.test(url)) shell.openExternal(url);
});

ipcMain.handle("fengcode:copy", (_e, text) => {
  clipboard.writeText(String(text || ""));
});

ipcMain.handle("fengcode:showInFolder", (_e, filePath) => {
  if (typeof filePath !== "string" || !filePath.trim()) return false;
  const target = path.resolve(filePath);
  if (process.platform === "win32") {
    shell.showItemInFolder(target);
  } else {
    shell.openPath(path.dirname(target));
  }
  return true;
});

ipcMain.handle("fengcode:windowAction", (_e, action) => {
  if (!mainWindow) return false;
  if (action === "minimize") mainWindow.minimize();
  else if (action === "maximize") mainWindow.isMaximized() ? mainWindow.unmaximize() : mainWindow.maximize();
  else if (action === "close") mainWindow.close();
  else return false;
  return true;
});

ipcMain.handle("fengcode:notify", (_e, { title, body }) => {
  notify(String(title || "Fengcode"), String(body || ""));
});

ipcMain.handle("fengcode:restartBackend", async () => {
  stopBackend();
  await new Promise((r) => setTimeout(r, 800));
  const ok = await startBackend();
  if (ok && mainWindow) mainWindow.loadURL(BASE_URL);
  return ok;
});

// ---- 内置浏览器 IPC（1.2.7）------------------------------------------
// 界面只通过这些受控入口操作浏览器，不直接碰 webContents。

ipcMain.handle("fengcode:browserOpen", (_e, payload) => {
  const p = payload || {};
  const v = ensureBrowserView();
  if (!v) return { ok: false, error: "窗口尚未就绪" };
  const want = normalizeBrowserUrl(p.url);
  if (want && !isAllowedBrowserUrl(want)) {
    return { ok: false, error: "只允许打开 http / https 地址" };
  }
  showBrowserView(p.bounds);
  if (want) v.webContents.loadURL(want).catch(() => {});
  return { ok: true, url: want };
});

ipcMain.handle("fengcode:browserSetBounds", (_e, bounds) => {
  if (!browserView || !browserVisible) return false;
  showBrowserView(bounds);
  return true;
});

ipcMain.handle("fengcode:browserHide", () => { hideBrowserView(); return true; });

ipcMain.handle("fengcode:browserClose", () => { destroyBrowserView(); return true; });

ipcMain.handle("fengcode:browserNavigate", (_e, url) => {
  const v = browserView;
  if (!v || v.webContents.isDestroyed()) return { ok: false, error: "浏览器尚未打开" };
  const want = normalizeBrowserUrl(url);
  if (!isAllowedBrowserUrl(want)) return { ok: false, error: "只允许打开 http / https 地址" };
  v.webContents.loadURL(want).catch(() => {});
  return { ok: true, url: want };
});

ipcMain.handle("fengcode:browserAction", (_e, action) => {
  const v = browserView;
  if (!v || v.webContents.isDestroyed()) return { ok: false, error: "浏览器尚未打开" };
  const wc = v.webContents;
  const hist = wc.navigationHistory;
  if (action === "back" && hist.canGoBack()) hist.goBack();
  else if (action === "forward" && hist.canGoForward()) hist.goForward();
  else if (action === "reload") wc.reload();
  else if (action === "stop") wc.stop();
  else return { ok: false, error: "未知操作" };
  return { ok: true };
});

ipcMain.handle("fengcode:browserRead", async (_e, maxChars) => {
  return await readBrowserText(Number(maxChars) || 12000);
});

ipcMain.handle("fengcode:browserScreenshot", async () => {
  const v = browserView;
  if (!v || v.webContents.isDestroyed()) return { ok: false, error: "浏览器尚未打开" };
  try {
    const img = await v.webContents.capturePage();
    if (!img || img.isEmpty()) return { ok: false, error: "截图失败（页面为空）" };
    return { ok: true, dataUrl: img.toDataURL() };
  } catch (e) {
    return { ok: false, error: String((e && e.message) || e) };
  }
});

// ---------------------------------------------------------------- 生命周期

// 单实例：第二次启动就把已有窗口拉起来
const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => showWindow());

  app.whenReady().then(async () => {
    log("=== Fengcode 桌面端启动 ===", `Electron ${process.versions.electron}`);
    createMenu();
    createTray();          // 托盘先建好，用户能立刻看到图标

    // 关键：先开窗口（显示骨架屏），后端在后台并行启动。
    // 这样不用等后端就绪才看到界面，体感快很多。
    createWindow();

    const ok = await startBackend();
    if (ok && mainWindow) {
      // 后端好了，切到真实界面（失败时给一张错误页）
      mainWindow.loadURL(BASE_URL).catch((e) => {
        log("加载界面失败：", e.message);
        if (mainWindow) {
          mainWindow.loadURL(
            "data:text/html;charset=utf-8," +
              encodeURIComponent(
                `<body style="font-family:system-ui;padding:40px;color:#333">
                 <h2>无法连接 Fengcode 后端</h2>
                 <p>请确认服务已启动，或查看日志目录。</p>
                 <p style="color:#666;font-size:13px">${BASE_URL}</p></body>`
              )
          );
        }
      });
    }

    // 全局快捷键：随时唤起
    const accel = process.platform === "darwin" ? "Cmd+Alt+F" : "Ctrl+Alt+F";
    try {
      globalShortcut.register(accel, showWindow);
      log("已注册全局快捷键：", accel);
    } catch (e) {
      log("全局快捷键注册失败：", e.message);
    }

    if (!ok) {
      notify("Fengcode 启动异常", "请检查日志或运行环境自检");
    }

    app.on("activate", () => showWindow());
  });

  app.on("window-all-closed", () => {
    // 有托盘时保持后台运行
    if (!tray && process.platform !== "darwin") app.quit();
  });

  app.on("before-quit", () => {
    quitting = true;
    // ★ 退出前一定要把窗口位置/尺寸写盘。
    //   旧版只在 resize/move 事件里保存；而「关窗口」默认是**隐藏到托盘**，
    //   用户真正退出往往走托盘菜单/快捷键，那条路径不触发 move，
    //   于是窗口位置丢失 → 下次启动回到默认位置（用户反馈「每次都在左上角」）。
    saveWindowState();
    try { globalShortcut.unregisterAll(); } catch {}
    stopBackend();
  });
}

// 崩溃兜底
process.on("uncaughtException", (e) => {
  log("未捕获异常：", e && e.stack ? e.stack : String(e));
});
process.on("unhandledRejection", (e) => {
  log("未处理的 Promise 拒绝：", e && e.stack ? e.stack : String(e));
});
