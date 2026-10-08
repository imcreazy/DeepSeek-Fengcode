/**
 * 预加载脚本：在隔离的上下文中向渲染进程暴露少量安全能力。
 *
 * 只暴露明确需要的接口，不暴露 Node 本身（contextIsolation 已开启）。
 */

const { contextBridge, ipcRenderer, webUtils } = require("electron");

contextBridge.exposeInMainWorld("fengcode", {
  /** 桌面端信息（版本、后端状态、日志路径） */
  info: () => ipcRenderer.invoke("fengcode:info"),

  /** ★ 拿到拖入/选择的文件的**真实绝对路径**。
      Electron 32 起 File.path 已被移除，必须走 webUtils.getPathForFile。
      没有它，前端只能拿到文件名 —— 实测的「只记录名称，路径请直接告诉我」就是这么来的。 */
  getPathForFile: (file) => {
    try { return webUtils.getPathForFile(file) || ""; } catch (e) { return ""; }
  },

  /** 用系统默认浏览器打开链接 */
  openExternal: (url) => ipcRenderer.invoke("fengcode:openExternal", url),

  /** 复制到剪贴板 */
  copy: (text) => ipcRenderer.invoke("fengcode:copy", text),

  /** ★ 读剪贴板（账号页「粘贴」按钮用；输入法打不出字时的替代输入路径） */
  readClipboard: () => ipcRenderer.invoke("fengcode:readClipboard"),

  /** 在系统文件管理器中显示文件或文件夹 */
  showInFolder: (filePath) => ipcRenderer.invoke("fengcode:showInFolder", filePath),

  /** 发系统通知 */
  notify: (title, body) => ipcRenderer.invoke("fengcode:notify", { title, body }),

  /** 重启后端服务 */
  restartBackend: () => ipcRenderer.invoke("fengcode:restartBackend"),

  /** 控制无边框窗口 */
  windowAction: (action) => ipcRenderer.invoke("fengcode:windowAction", action),

  /** 是否运行在桌面端（网页版会是 undefined） */
  isDesktop: true,

  platform: process.platform,
});

/* ★ 内置浏览器（1.2.7）：只在桌面端可用。
   主进程那边还会再校验一次协议白名单（只放 http/https），
   这里只是转发 —— 界面拿不到 webContents，也就无从绕过安全边界。 */
contextBridge.exposeInMainWorld("fengcodeBrowser", {
  /** 打开／切到某个地址，并给出视图该占的矩形 {x,y,width,height} */
  open: (url, bounds) => ipcRenderer.invoke("fengcode:browserOpen", { url, bounds }),

  /** 窗口尺寸/侧栏宽度变化时重新定位（视图是独立图层，不会跟着 DOM 走） */
  setBounds: (bounds) => ipcRenderer.invoke("fengcode:browserSetBounds", bounds),

  /** 暂时隐藏（切到别的标签页时用；不销毁，切回来还在原页） */
  hide: () => ipcRenderer.invoke("fengcode:browserHide"),

  /** 彻底关闭（释放页面） */
  close: () => ipcRenderer.invoke("fengcode:browserClose"),

  navigate: (url) => ipcRenderer.invoke("fengcode:browserNavigate", url),
  action: (name) => ipcRenderer.invoke("fengcode:browserAction", name),

  /** 取当前页正文（给「让 AI 读这个页面」） */
  read: (maxChars) => ipcRenderer.invoke("fengcode:browserRead", maxChars),

  /** 截当前页图（返回 dataURL） */
  screenshot: () => ipcRenderer.invoke("fengcode:browserScreenshot"),

  /** 订阅导航/加载/标题/错误事件；返回取消订阅函数 */
  onEvent: (handler) => {
    const fn = (_e, payload) => { try { handler(payload); } catch (err) {} };
    ipcRenderer.on("fengcode:browser", fn);
    return () => ipcRenderer.removeListener("fengcode:browser", fn);
  },
});
