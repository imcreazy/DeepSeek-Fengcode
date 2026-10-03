/**
 * 桌面端启动自检：起 Electron，确认是真窗口（不是跳浏览器），然后自动退出。
 * 用法：npx electron scripts/probe-desktop.js
 */
const { app, BrowserWindow } = require("electron");
const http = require("http");
const path = require("path");
const fs = require("fs");
const { spawn } = require("child_process");

const HOST = "127.0.0.1";
const PORT = parseInt(process.env.FENGCODE_PORT || "7845", 10);
const BASE = `http://${HOST}:${PORT}`;
// 本脚本位于 桌面端\desktop\scripts\，项目根在 上三级
const PROJECT_ROOT = path.resolve(__dirname, "..", "..", "..", "命令端");
const LOG = [];
let backend = null;

function log(...a) {
  const line = "[probe] " + a.join(" ");
  console.log(line);
  LOG.push(line);
}

function probeHealth() {
  return new Promise((resolve) => {
    const req = http.get(`${BASE}/health`, { timeout: 1500 }, (res) => {
      let b = "";
      res.on("data", (d) => (b += d));
      res.on("end", () => {
        try { const j = JSON.parse(b); resolve(j.app === "Fengcode" ? j : null); }
        catch { resolve(null); }
      });
    });
    req.on("error", () => resolve(null));
    req.on("timeout", () => { req.destroy(); resolve(null); });
  });
}

async function waitReady(maxMs = 40000) {
  const t0 = Date.now();
  while (Date.now() - t0 < maxMs) {
    if (await probeHealth()) return true;
    await new Promise((r) => setTimeout(r, 400));
  }
  return false;
}

function findPython() {
  if (process.env.FENGCODE_PYTHON) return process.env.FENGCODE_PYTHON;
  const cands = [];
  const la = process.env.LOCALAPPDATA || "";
  const pf = process.env.ProgramFiles || "";
  for (const v of ["313", "312", "311", "310"]) {
    cands.push(path.join(la, "Programs", "Python", `Python${v}`, "python.exe"));
    cands.push(path.join(pf, `Python${v}`, "python.exe"));
  }
  cands.push("python.exe", "python");
  for (const c of cands) {
    if (path.isAbsolute(c)) {
      if (fs.existsSync(c)) return c;
    } else {
      return c;
    }
  }
  return "python";
}

function startBackend() {
  if (process.env.FENGCODE_NO_SPAWN === "1") return;
  const env = { ...process.env, PYTHONIOENCODING: "utf-8", PYTHONUTF8: "1" };
  const py = findPython();
  backend = spawn(py, ["-m", "fengcode.cli.main", "serve",
                       "--host", HOST, "--port", String(PORT)], {
    cwd: PROJECT_ROOT, env, windowsHide: true,
  });
  backend.stdout.on("data", (d) => console.log("[后端]", String(d).trim()));
  backend.stderr.on("data", (d) => console.log("[后端err]", String(d).trim()));
  log("已拉起后端（PID " + backend.pid + "），python =", py, "cwd =", PROJECT_ROOT);
}

function stopBackend() {
  if (backend) {
    try {
      spawn("taskkill", ["/F", "/T", "/PID", String(backend.pid)], { windowsHide: true });
    } catch {}
    backend = null;
  }
}

app.whenReady().then(async () => {
  log("Electron 版本:", process.versions.electron);
  if (!(await probeHealth())) {
    startBackend();
  } else {
    log("已有后端在跑，复用");
  }
  log("等待后端…");
  const up = await waitReady();
  log("后端就绪:", up);

  const win = new BrowserWindow({
    width: 1100, height: 760, show: true, title: "Fengcode 自检",
    webPreferences: { contextIsolation: true, nodeIntegration: false },
  });

  win.once("ready-to-show", () => log("窗口 ready-to-show（已显示）"));

  try {
    await win.loadURL(BASE);
    log("已加载 URL:", win.webContents.getURL());
  } catch (e) {
    log("加载失败:", e.message);
  }

  // 等页面渲染
  await new Promise((r) => setTimeout(r, 4500));

  const title = win.webContents.getTitle();
  log("窗口标题:", title);

  const visible = win.isVisible();
  const bounds = win.getBounds();
  log("窗口可见:", visible, "尺寸:", JSON.stringify(bounds));

  const html = await win.webContents.executeJavaScript(
    "document.body ? document.body.innerText.slice(0,300) : '(无 body)'"
  );
  log("页面文本片段:", JSON.stringify(html.slice(0, 200)));

  // 截图留证（多试几次，窗口刚显示时可能抓到空图）
  for (let i = 0; i < 5; i++) {
    try {
      const img = await win.webContents.capturePage();
      const buf = img.toPNG();
      if (buf.length > 1000) {
        const out = path.join(__dirname, "..", "_desktop_probe.png");
        fs.writeFileSync(out, buf);
        log("截图已保存:", out, buf.length, "字节");
        break;
      }
      log(`截图第 ${i + 1} 次为空（${buf.length} 字节），重试…`);
      await new Promise((r) => setTimeout(r, 1000));
    } catch (e) {
      log("截图失败:", e.message);
      break;
    }
  }

  log("==== 结论 ====");
  log("真窗口可见:", visible);
  log("标题含 Fengcode:", /Fengcode/i.test(title));
  log("页面有内容:", html.length > 20);

  setTimeout(() => { stopBackend(); app.quit(); }, 800);
});

app.on("window-all-closed", () => { stopBackend(); app.quit(); });
