/* 账号（可选账号源）的防回归测试。

背景：Fengcode 加了一个**可选**的账号登录 —— 登录后能在状态栏看到账户余额。
这件事有三条不能退化的约束：
  ① 它必须是**可选**的：不登录时界面不能冒出余额、不能挡着用；
  ② 凭证**绝不进前端**：界面只拿用户名/余额，密码只用于那一次登录请求；
  ③ 询问弹窗**只问一次**：用户选过（包括选「暂不」）就不再打扰。
*/
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const staticDir = path.join(__dirname, '../src/fengcode/server/static');
const appSrc = fs.readFileSync(path.join(staticDir, 'app.js'), 'utf8');
const cssSrc = fs.readFileSync(path.join(staticDir, 'app.css'), 'utf8');
const appPy = fs.readFileSync(
  path.join(__dirname, '../src/fengcode/server/app.py'), 'utf8');

function funcBody(src, name, span = 2400) {
  let i = src.indexOf(`function ${name}`);
  if (i < 0) i = src.indexOf(`async def ${name}`);
  if (i < 0) i = src.indexOf(`def ${name}`);
  assert.ok(i > 0, `应有 function/def ${name}`);
  return src.slice(i, i + span);
}

test('状态栏余额：未登录或用户关掉时不显示', () => {
  const fn = funcBody(appSrc, 'accountStatusHtml', 700);
  assert.ok(
    fn.includes('if (!a.logged_in || a.show_balance === false) return ""'),
    '未登录、或用户关掉「显示余额」时必须返回空串（不能显示 0 或占位）'
  );
  assert.ok(fn.includes('余额'), '登录且开启时应给出余额项');
  // 必须真的接进状态栏渲染，否则函数写了也没人调
  const bar = funcBody(appSrc, 'renderStatusBar', 3000);
  assert.ok(bar.includes('accountStatusHtml()'), 'renderStatusBar 必须调用 accountStatusHtml()');
});

test('★ 凭证不进前端：界面只提交账密、只接收脱敏快照', () => {
  const fn = funcBody(appSrc, 'renderAccountPane', 6000);
  // 密码框必须是 password 类型，且不回显任何已存密码
  assert.ok(fn.includes('type="password"'), '密码输入框必须是 password 类型');
  assert.ok(!/value="\$\{[^}]*pass/.test(fn), '密码框不得回填任何已存密码');
  // 登录只把账密发给我们自己的后端，由后端转发；界面不直连站点
  assert.ok(fn.includes('action: "login"'), '登录应走 { action: "login" } 由后端代理');
  assert.ok(
    !/liufengzi|qd\.je/.test(appSrc),
    '前端不得出现账号站点的真实地址（地址只在后端配置里）'
  );
  // 后端必须做脱敏：响应里不能带凭证字段
  const api = funcBody(appPy, 'api_account', 4000);
  assert.ok(api.includes('snapshot'), '接口应返回脱敏快照');
});

test('询问弹窗只问一次，且选「暂不」也算问过', () => {
  const fn = funcBody(appSrc, 'maybePromptAccount', 2600);
  assert.ok(
    fn.includes('if (a.logged_in || a.prompt_done) return'),
    '已登录或已问过时必须直接返回，不再打扰'
  );
  assert.ok(fn.includes('acct-no'), '应有「暂不登录」出口');
  assert.ok(fn.includes('acct-yes'), '应有「去登录」入口');
  // 「暂不」也必须记下问过了 —— 否则每次启动都弹
  assert.ok(
    /done\(\)[\s\S]*prompt_done/.test(fn) || fn.includes('prompt_done: true'),
    '选「暂不」也要记录 prompt_done，避免每次启动都弹'
  );
  assert.ok(fn.includes('action: "prompt_done"'), '应把「问过了」写回后端持久化');
});

test('账号面板与导航接线完整', () => {
  // 导航项 + tab + 面板三者必须齐（缺一个就点不开）
  assert.ok(/id:\s*"account"/.test(appSrc), 'SETTINGS_NAV 应有 account 项');
  assert.ok(appSrc.includes('data-t="account"'), '#set-tabs 应有账号 tab');
  assert.ok(appSrc.includes('id="set-account"'), '应有 #set-account 面板');
  assert.ok(appSrc.includes('id="acct-box"'), '面板里应有 #acct-box 承载两态');
  // 首屏必须把账号快照存下来，否则状态栏拿不到
  assert.ok(
    appSrc.includes('S.account = d.account'),
    'boot 里必须把 bootstrap 的 account 存进 S.account'
  );
});

test('账号面板样式存在（余额是主角）', () => {
  assert.ok(cssSrc.includes('.acct-row'), '应有 .acct-row 布局');
  assert.ok(cssSrc.includes('.acct-bal-v'), '应有余额大字样式');
  // 字号必须走 --ui-scale，跟着用户设置的字号缩放
  const block = cssSrc.slice(cssSrc.indexOf('.acct-bal-v'), cssSrc.indexOf('.acct-bal-v') + 260);
  assert.ok(block.includes('var(--ui-scale)'), '余额字号必须用 calc(Npx * var(--ui-scale))');
});
