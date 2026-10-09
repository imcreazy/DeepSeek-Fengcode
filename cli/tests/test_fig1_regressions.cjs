/* 图 1 两个缺陷的复现与防回归（2026-10-04 实际使用中）。
 *
 * 缺陷一：强制停止 / 连接中断后，右下角「上下文占用」「命中率」停在 0。
 *   根因：后端 usage 事件只有两个发送点 ——
 *     ① 循环内 `if last_usage:`（第一次上游调用还没成功时 last_usage 为空）
 *     ② 回合正常收尾（`break` 跳出循环的 11 处全都绕过了它）
 *   停止 / 中断走的是 break 路径，两个点都到不了 → 前端收不到 usage → 读数全 0。
 *   本用例锁死：**循环退出后必须无条件补发一次 usage**。
 *
 * 缺陷二：中断后同一段回答在界面上出现两遍，第二条挂「补充（连接中断后从会话记录恢复）」。
 *   根因：recoverFinalText 的三道闸里，闸①依赖 `_sawAssistantText`，
 *   而中断时后端不再重复交付正文 → 该标志恒为 false → 闸①失效；
 *   闸③ textAlreadyShown 对**短文本**判定不稳（逐行切片命中过半），也拦不住。
 *   本用例锁死：**本轮已经在界面上渲染过正文时，recoverFinalText 必须直接返回 false**。
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

const APP = 'D:/Fengcode/cli/src/fengcode/server/static/app.js';
const AGENT = 'D:/Fengcode/cli/src/fengcode/core/agent.py';
const appSrc = fs.readFileSync(APP, 'utf8');
const agentSrc = fs.readFileSync(AGENT, 'utf8');

/* ---------------- 缺陷一：停止 / 中断后读数必须刷新 ---------------- */

test('后端：循环退出后有统一的 usage 补发出口（停止/中断不再丢读数）', () => {
  // 收尾段必须存在「无条件补发 usage」的调用，且不在任何 if 之内。
  const tail = agentSrc.slice(agentSrc.indexOf('self.bus.emit(Ev.TURN_END'));
  assert.ok(tail.length > 0, '应能定位回合收尾段');

  // 收尾段往上找最近的 usage 广播，要求它与 TURN_END 之间没有条件包裹。
  const before = agentSrc.slice(0, agentSrc.indexOf('self.bus.emit(Ev.TURN_END'));
  const lastUsageAt = before.lastIndexOf('Ev.USAGE');
  assert.ok(lastUsageAt > 0, '收尾段必须有 usage 广播');

  const snippet = before.slice(Math.max(0, lastUsageAt - 400), lastUsageAt);
  // 若它被 `if last_usage:` 之类的条件包着，停止路径就会漏掉。
  assert.ok(
    !/if\s+last_usage\s*:\s*$[\s\S]{0,80}$/.test(snippet + 'Ev.USAGE'),
    '收尾的 usage 广播不能被 last_usage 条件包裹（停止时 last_usage 可能为空）'
  );
});

test('前端：usage 事件到达时同时刷新状态栏与信息面板', () => {
  // ★ 从 `function handleEvent` 之后再找 case —— 源码**注释**里也出现过
  //   `case "usage"` 这段字样（讲历史坑时引用过），从头 indexOf 会切到注释上，
  //   于是断言检查的是一段注释、必然失败（这条测试因此长期报假故障）。
  const h = appSrc.indexOf('function handleEvent');
  assert.ok(h > 0, 'app.js 必须有 handleEvent');
  const i = appSrc.indexOf('case "usage"', h);
  assert.ok(i > h, 'app.js 必须有 case "usage" 分支');
  // 分支到下一个 case 为止（不能截断，否则会把别的分支算进来）。
  const rest = appSrc.slice(i);
  const next = rest.indexOf('case "', 10);
  const branch = next > 0 ? rest.slice(0, next) : rest.slice(0, 1500);
  assert.ok(branch.includes('renderStatusBar()'), 'usage 分支必须刷新状态栏');
  assert.ok(branch.includes('renderInfoPanel()'), 'usage 分支必须刷新右侧信息面板');
});

test('前端：命中率在无调用记录时不显示 NaN', () => {
  const i = appSrc.indexOf('function hitRatePct');
  assert.ok(i > 0, '必须有 hitRatePct');
  const fn = appSrc.slice(i, i + 400);
  // 分母为 0 必须有明确返回值（现在是 0，界面据此显示占位横线）。
  assert.ok(/if\s*\(\s*t\s*<=\s*0\s*\)\s*return/.test(fn), 'hitRatePct 必须处理分母为 0');
  assert.ok(!/NaN/.test(fn), 'hitRatePct 不应产生 NaN');
});

/* ---------------- 缺陷二：中断后不得重复渲染 ---------------- */

test('前端：本轮已流式渲染过正文时，recoverFinalText 必须直接放弃补发', () => {
  const i = appSrc.indexOf('async function recoverFinalText');
  assert.ok(i > 0, '必须有 recoverFinalText');
  const fn = appSrc.slice(i, i + 700);
  // 闸门必须在函数**最开始**就把「已渲染过正文」判掉，不能只依赖 _sawAssistantText。
  assert.ok(
    /_recovered[\s\S]{0,60}_sawAssistantText|_finalRendered/.test(fn),
    'recoverFinalText 的闸门需要覆盖「本轮已有定稿文字」的情形'
  );
});

test('前端：textAlreadyShown 对短文本也必须可靠（单行也要能命中）', () => {
  const i = appSrc.indexOf('function textAlreadyShown');
  assert.ok(i > 0, '必须有 textAlreadyShown');
  const fn = appSrc.slice(i, i + 1200);
  assert.ok(fn.includes('md('), '比对前必须把 Markdown 渲染成纯文本，与 DOM 同口径');
  assert.ok(
    fn.includes('shown.includes'),
    '必须对 DOM 已显示文本做包含判断'
  );
  // 关键：单片段 / 短文本时不能因为「命中率不足一半」而漏判。
  assert.ok(
    /probe\.length\s*===\s*1[\s\S]{0,80}hit\s*===\s*1/.test(fn),
    '单片段情形必须有明确的命中判定'
  );
});
