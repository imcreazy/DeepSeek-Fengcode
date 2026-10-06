/* 会话级运行状态的隔离（多对话任务串台）。
 *
 * 现象：
 *   · 在 A 里跑任务时切到新对话 B，B 的输入卡片仍写着 A 的「字斟句酌中… 113.5 t/s」；
 *   · 在 B 里发消息却提示「立即发送 / 取回」（因为它以为 B 也在跑）。
 * 根因：streaming / 运行时长 / 速率 / 中止句柄 / 排队队列 / 本轮用量**只有一份全局状态**，
 *   切会话时一个都不重置 —— 于是 B 读到的是 A 的状态。
 *
 * 本用例锁死隔离语义：
 *   ① 状态按会话分桶，互不影响；
 *   ② S.<字段> 访问器落到**当前上下文会话**那一份上；
 *   ③ withBox 划定的上下文在同步与 async（含跨 await）两种情形下都成立。
 *      ★ ③ 是「后台会话继续跑、切回来能接上」的地基：如果 withBox 在第一个
 *        await 处就恢复上下文，await 之后的收尾代码会写到别人的会话上。
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const source = fs.readFileSync(
  path.join(__dirname, '../src/fengcode/server/static/app.js'), 'utf8');

/** 取「会话级运行状态」那一段（CUR / sessDefaults / SESS / sessState / withBox / isCurrentSid）。 */
function scopedBlock() {
  const start = source.indexOf('const CUR = { box: "" };');
  assert.ok(start > 0, '应能找到 CUR 定义');
  const endTag = 'function isCurrentSid(sid) { return (sid || "") === (S.sessionId || ""); }';
  const end = source.indexOf(endTag);
  assert.ok(end > start, '应能找到 isCurrentSid');
  return source.slice(start, end + endTag.length);
}

/** 在最小沙箱里把这套机制真跑起来（S 只保留 sessionId）。 */
function sandbox() {
  return vm.runInNewContext(`(() => {
    const S = { sessionId: "a" };
    ${scopedBlock()}
    return { S, CUR, sessState, sessDefaults, withBox, isCurrentSid, activeSid };
  })()`);
}

test('会话状态按会话分桶：两个对话互不影响', () => {
  const s = sandbox();
  s.sessState('a').streaming = true;
  s.sessState('a').turnSpeed = 113.5;
  // B 是另一个对话：它的状态必须是干净的，不能看到 A 的读数
  assert.equal(s.sessState('b').streaming, false, 'B 不该继承 A 的 running');
  assert.equal(s.sessState('b').turnSpeed, 0, 'B 不该继承 A 的速率');
  assert.equal(s.sessState('b').queue.length, 0, 'B 不该继承 A 的排队队列');
  // 再取一次 a：还是同一份（不能每次新建，否则状态会丢）
  assert.equal(s.sessState('a'), s.sessState('a'));
});

test('S.<字段> 访问器落到当前上下文会话那一份上', () => {
  const s = sandbox();
  s.S.sessionId = 'a';
  s.S.turnUsage = { prompt_tokens: 111 };
  assert.equal(s.sessState('a').turnUsage.prompt_tokens, 111);
  // 切到 b：读到的是 b 自己那份（空），而不是 a 的
  s.S.sessionId = 'b';
  assert.equal(s.S.turnUsage, null, 'B 不该读到 A 的用量');
  s.S.turnUsage = { prompt_tokens: 222 };
  assert.equal(s.sessState('a').turnUsage.prompt_tokens, 111, 'A 的读数不该被 B 覆盖');
  assert.equal(s.sessState('b').turnUsage.prompt_tokens, 222);
});

test('withBox：同步回调结束后恢复上下文', () => {
  const s = sandbox();
  s.S.sessionId = 'a';
  s.withBox('b', () => { s.S.streamOutEst = 7; });
  assert.equal(s.sessState('b').streamOutEst, 7, '应写进 b');
  assert.equal(s.CUR.box, '', '结束后应恢复（不该把上下文留在 b）');
  s.S.streamOutEst = 9;
  assert.equal(s.sessState('a').streamOutEst, 9, '恢复后应写回 a');
});

test('withBox：async 回调跨 await 仍保持上下文（后台会话的地基）', async () => {
  const s = sandbox();
  s.S.sessionId = 'a';
  await s.withBox('b', async () => {
    s.S.turnElapsed = 1;
    await new Promise((r) => setTimeout(r, 1));
    // ★ 关键：await 之后上下文不能已经恢复，否则这里会写到 a 上
    s.S.turnElapsed = 2;
  });
  assert.equal(s.sessState('b').turnElapsed, 2, 'await 之后的写入必须仍在 b 上');
  assert.equal(s.sessState('a').turnElapsed, 0, 'a 不该被后台会话的收尾写到');
  assert.equal(s.CUR.box, '', '异步回调落定后也要恢复');
});

test('withBox：回调抛错也要恢复上下文（否则后续全写错会话）', () => {
  const s = sandbox();
  s.S.sessionId = 'a';
  assert.throws(() => s.withBox('b', () => { throw new Error('boom'); }));
  assert.equal(s.CUR.box, '', '抛错路径必须恢复上下文');
});

test('isCurrentSid：判定「是不是眼前这个会话」', () => {
  const s = sandbox();
  s.S.sessionId = 'a';
  assert.equal(s.isCurrentSid('a'), true);
  assert.equal(s.isCurrentSid('b'), false);
  assert.equal(s.isCurrentSid(''), false);
});
