/* 审批确认卡改到对话内的防回归（2026-10-05）。

背景：卡片原先挂在右下角固定浮层（#approvals），会盖住右侧信息栏，
      且不随对话滚动 —— 现改为让权限请求跟着对话流走。
搬到对话流（#messages）后带来两个新风险，必须锁死：
  ① 切换会话 / 清空对话会重建 #messages，卡片会被一起抹掉，
     而服务端那次工具调用**仍在阻塞等答复** → 必须能放回来；
  ② 轮次折叠会把它收起来 → 等于把请求藏了。
*/
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const appSrc = fs.readFileSync(
  path.join(__dirname, '../src/fengcode/server/static/app.js'), 'utf8');
const cssSrc = fs.readFileSync(
  path.join(__dirname, '../src/fengcode/server/static/app.css'), 'utf8');

function funcBody(src, name, span = 1600) {
  const i = src.indexOf(`function ${name}`);
  assert.ok(i > 0, `应有 function ${name}`);
  return src.slice(i, i + span);
}

test('审批卡渲染进对话流，而不是右下角浮层', () => {
  const fn = funcBody(appSrc, 'renderApprovalCard', 2600);
  assert.ok(
    fn.includes('msgBox().appendChild(wrap)'),
    '确认卡必须 append 到 #messages（对话流），不再进 #approvals 浮层'
  );
  assert.ok(
    !/#approvals/.test(fn.slice(0, fn.indexOf('msgBox().appendChild'))),
    '渲染路径不得再把卡片塞进 #approvals 浮层'
  );
  assert.ok(
    fn.includes('dataset.approval = "1"'),
    '卡片所在 wrap 需要 data-approval 标记，轮次折叠才能跳过它'
  );
  // ask_user 的提问卡保持原样（用户只要求改审批卡）
  const askFn = funcBody(appSrc, 'showAsk', 3000);
  assert.ok(askFn.includes('#approvals'), 'ask_user 提问卡仍走浮层，别顺手改掉没被要求的东西');
});

test('切换会话 / 清空对话后，未答复的确认卡会被放回来', () => {
  assert.ok(
    /restorePendingApprovals\(\);/.test(funcBody(appSrc, 'clearMessages', 700)),
    'clearMessages 里必须调 restorePendingApprovals —— '
    + '它清空 #messages 时会把卡片一起抹掉，而服务端还在等答复'
  );
  const fn = funcBody(appSrc, 'restorePendingApprovals', 900);
  assert.ok(fn.includes('renderApprovalCard'), '恢复时要重新渲染卡片');
  assert.ok(
    fn.includes('a.session_id !== S.sessionId'),
    '只放回当前会话的请求：别的会话的请求不该串到这条对话里'
  );
});

test('待确认的请求会被登记（否则切换会话后就丢了）', () => {
  const fn = funcBody(appSrc, 'showApproval', 1400);
  assert.ok(fn.includes('S.pendingApprovals'), 'showApproval 必须登记待确认请求');
});

test('轮次折叠不收起确认卡', () => {
  const fn = funcBody(appSrc, 'setTurnCollapsed', 700);
  assert.ok(
    fn.includes('dataset.approval') || fn.includes('dataset && n.dataset.approval'),
    '折叠时要跳过 data-approval 的 wrap —— 否则请求被藏起来'
  );
});

test('approval.done 走统一移除（连登记一起清掉）', () => {
  assert.ok(
    appSrc.includes('removeApprovalCard(d.id)'),
    'approval.done 必须调 removeApprovalCard，否则登记会残留、切会话时复活成幽灵卡'
  );
  const fn = funcBody(appSrc, 'removeApprovalCard', 500);
  assert.ok(fn.includes('delete S.pendingApprovals'), '移除卡片时同时清除登记');
});

test('卡片样式规则跟着搬到对话内', () => {
  assert.ok(
    cssSrc.includes('.msg-wrap.approval-wrap'),
    '需要 .approval-wrap 的对话内样式（消息区间距）'
  );
});
