/* 图 1 缺陷二（重复输出）的行为级验证：停止后 recoverFinalText 必须拒绝补发。
 *
 * 场景还原：
 *   ① 发「你好」→ 正常回复，已落库
 *   ② 发「做个鹅鹅骑自行车的动画」→ 发出去就不想做了，强行停止
 *   ③ 结果界面上冒出了**第二条「你好，我是 Fengcode…」**（上一轮的回答）
 *
 * 根因：后端写库是 `if content:`（core/agent.py），中断轮没有正文就不落库；
 *      而 recoverFinalText 只找「最后一条有内容的 assistant 消息」，
 *      捞到的必然是上一轮的 —— 贴到界面上就成了旧回答重贴。
 *
 * 本脚本用最小 DOM/接口替身把 recoverFinalText 真实跑起来，
 * 断言：S._cancelledTurn 为真时，一次 API 都不该发（提前返回 false）。
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');

const APP = 'D:/Fengcode/cli/src/fengcode/server/static/app.js';
const source = fs.readFileSync(APP, 'utf8');

// 从源码里摘出 recoverFinalText 函数体（花括号配平，避免卷进后续代码）。
function extractFn(name) {
  const start = source.indexOf(`async function ${name}(`);
  assert.ok(start > 0, `应能定位 ${name}`);
  let depth = 0, i = source.indexOf('{', start);
  const from = i;
  for (; i < source.length; i++) {
    if (source[i] === '{') depth++;
    else if (source[i] === '}') { depth--; if (depth === 0) break; }
  }
  return source.slice(start, i + 1);
}

test('停止的回合：recoverFinalText 直接拒绝，不查库、不补发', async () => {
  const fn = extractFn('recoverFinalText');
  let apiCalls = 0;
  let added = 0;

  const sandbox = {
    S: { sessionId: 's1', _cancelledTurn: true },   // ★ 本轮被用户停止
    md: (t) => t,
    addMessage: () => { added++; },
    scrollDown: () => {},
    textAlreadyShown: () => false,
    api: async () => { apiCalls++; return { messages: [] }; },
    encodeURIComponent,
  };
  sandbox.globalThis = sandbox;

  const ctx = vm.createContext(sandbox);
  vm.runInContext(`${fn}; globalThis.__r = recoverFinalText;`, ctx);
  const result = await vm.runInContext('__r({})', ctx);

  assert.equal(result, false, '停止的回合必须返回 false');
  assert.equal(apiCalls, 0, '停止的回合不应发起任何查询');
  assert.equal(added, 0, '停止的回合不应补写任何消息');
});

test('正常的回合：消息数变多时才补发（本轮确实落了库）', async () => {
  const fn = extractFn('recoverFinalText');
  let added = 0;
  const sandbox = {
    S: { sessionId: 's1', _cancelledTurn: false },
    md: (t) => t,
    addMessage: () => { added++; },
    scrollDown: () => {},
    textAlreadyShown: () => false,
    api: async () => ({ messages: [{ role: 'assistant', content: '本轮的交付文字' }] }),
    encodeURIComponent,
  };
  sandbox.globalThis = sandbox;
  const ctx = vm.createContext(sandbox);
  vm.runInContext(`${fn}; globalThis.__r = recoverFinalText;`, ctx);
  const result = await vm.runInContext('__r({})', ctx);

  assert.equal(result, true, '正常回合且本轮有落库时应补发');
  assert.equal(added, 1, '应补写一条');
});

test('新一轮必须复位 _cancelledTurn，否则一次停止会永久禁用补发', () => {
  assert.ok(
    source.includes('S._cancelledTurn = false;'),
    '新一轮起点必须复位 _cancelledTurn'
  );
});
