/* 桌面端「无法连接后端」误报的防回归（2026-10-08 实测事故）。

事故经过：用户重启电脑后桌面端打不开，界面显示「无法连接 Fengcode 后端」，
        而后端其实**已经起来并在服务**：
          · 日志里同一秒有 `[后端错误] WebSocket /ws [accepted]`；
          · 手动 `GET /api/status` 返回 200；
          · 日志里那条报错带的是**骨架屏自己**的 URL：
            `加载界面失败： (-3) loading 'data:text/html;charset=utf-8,...'`
        根因：`mainWindow.loadURL(BASE_URL).catch(...)` 把 loadURL 的 promise 成败
        当成「后端健不健康」的判据，而该 promise 会因**导航被顶掉**（骨架屏那次
        loading 被后一次取代）而 reject —— 错误码正是 -3 / ERR_ABORTED。
        一 reject 就把已经画出来的正常界面替换成错误页，所以「时好时坏、重启就打不开」。

本测试从 main.js 里**取出真实实现**来跑（不是另抄一份），锁死三条：
  ① 导航失败但后端探活正常 → 必须重试加载并返回 true，**绝不显示错误页**；
  ② 导航失败且后端探活失败 → 才显示错误页；
  ③ 先停掉在途的骨架屏导航（避免它被顶掉而报 -3）。
*/
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const MAIN = path.join(__dirname, '../main.js');
const src = fs.readFileSync(MAIN, 'utf8');

/** 从 main.js 切出「错误页 + 界面加载」两段真实源码。 */
function extractImpl() {
  const a = src.indexOf('// ---------------------------------------------------------------- 界面加载');
  assert.ok(a > 0, '应能定位界面加载段');
  const b = src.indexOf('// ---------------------------------------------------------------- 生命周期', a);
  assert.ok(b > a, '应能定位界面加载段结尾');
  return src.slice(a, b);
}

/** 在受控环境里跑那段真实源码，loadURL / probeHealth 用桩替换。 */
function makeHarness({ loadURL, probeHealth }) {
  const body = extractImpl();
  const logs = [];
  const fn = new Function(
    'mainWindow', 'BASE_URL', 'splashNav', 'probeHealth', 'log', 'encodeURIComponent',
    body.replace(/^async function loadAppUI/m, 'async function loadAppUI')
        .replace(/^function showOfflinePage/m, 'function showOfflinePage')
      + '\nreturn { loadAppUI, showOfflinePage };'
  );
  const win = {
    stopCalls: 0,
    loadCalls: [],
    webContents: { stop() { win.stopCalls++; } },
  };
  win.loadURL = (u) => { win.loadCalls.push(u); return loadURL(u); };
  const api = fn(
    win, 'http://127.0.0.1:7845',
    // ★ 桩必须是**真正的 Promise**：main.js 里 splashNav 就是 `loadURL(...).catch(...)`。
    //   给一个「带 then 但不回调」的对象会让 `await splashNav` 永久挂起（实测把测试跑超时）。
    Promise.resolve(),
    probeHealth, (...a) => logs.push(a.join(' ')), encodeURIComponent
  );
  return { api, win, logs };
}

test('★ 导航报 -3 但后端活着 → 重试加载，绝不显示错误页', async () => {
  let attempt = 0;
  const { api, win, logs } = makeHarness({
    // 第一次导航抛 ERR_ABORTED（事故现场的错误码），第二次成功
    loadURL: async () => {
      attempt++;
      if (attempt === 1) throw new Error("(-3) loading 'data:text/html;charset=utf-8,...'");
    },
    probeHealth: async () => ({ app: 'Fengcode' }),   // 后端活着
  });

  const ok = await api.loadAppUI('启动加载');
  assert.equal(ok, true, '后端活着就必须判成加载成功');
  assert.equal(win.stopCalls, 1, '必须先停掉在途的骨架屏导航');
  assert.ok(!win.loadCalls.some((u) => String(u).includes('无法连接')),
    '探活正常时绝不能显示错误页');
  assert.equal(win.loadCalls.filter((u) => u === 'http://127.0.0.1:7845').length, 2,
    '应当重试一次真实界面');
  assert.ok(logs.some((l) => l.includes('ERR_ABORTED')), '要如实记下这是导航竞态');
});

test('★ 导航失败且后端确实不可用 → 才显示错误页', async () => {
  const { api, win } = makeHarness({
    loadURL: async () => { throw new Error('ERR_CONNECTION_REFUSED'); },
    probeHealth: async () => null,                    // 后端真没了
  });

  const ok = await api.loadAppUI('启动加载');
  assert.equal(ok, false);
  assert.ok(win.loadCalls.some((u) => String(u).includes('data:text/html')),
    '后端不可用时应显示错误页');
});

test('★ 正常情况：一次加载成功，不做多余动作', async () => {
  const { api, win } = makeHarness({
    loadURL: async () => {},
    probeHealth: async () => ({ app: 'Fengcode' }),
  });

  const ok = await api.loadAppUI('启动加载');
  assert.equal(ok, true);
  assert.equal(win.loadCalls.length, 1, '成功就不该重复加载');
  assert.equal(win.stopCalls, 1, '进入时先停掉骨架屏导航');
});

test('★★ 启动主路径与重启路径都必须走 loadAppUI（静态）', () => {
  // 反向验证：不允许任何「只看 loadURL 成败就亮错误页」的写法回潮
  assert.ok(src.includes('await loadAppUI("启动加载")'), '启动主路径未走 loadAppUI');
  assert.ok(src.includes('await loadAppUI("手动重启后端")'), '托盘重启路径未走 loadAppUI');
  assert.ok(src.includes('await loadAppUI("界面里点重启后端")'), 'IPC 重启路径未走 loadAppUI');
  assert.ok(!/loadURL\(BASE_URL\)\.catch\(/.test(src),
    '不得再用 loadURL(...).catch(...) 直接判后端健康');
  assert.ok(src.includes('splashNav = mainWindow.loadURL(SPLASH)'),
    '骨架屏导航句柄必须被记下来（否则无法先 stop 再加载）');
});
