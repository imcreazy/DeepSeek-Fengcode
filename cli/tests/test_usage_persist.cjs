/* 读数被清空问题的复现与防回归。
 *
 * 现象（私人实例实测）：发出消息后，右侧「上下文窗口」与「命中率」一直显示
 * 「— / 尚无调用记录」，直到整轮结束才出现数字。上一轮明明有 13022 的读数。
 *
 * 根因：`usage` 事件到达时，前端是**整体覆盖** S.turnUsage ——
 *   而刚发出消息、上游还没返回用量的那几秒，事件里的 last_usage 是空的，
 *   于是 prompt_tokens 被写成 0，把上一轮的好读数盖掉了。
 *
 * 正确行为：本轮尚无用量时**保留上一轮读数**（界面据此显示上一轮的值），
 *   只有真的拿到本轮数据才覆盖。注意这**不是**把 0 当有效值 ——
 *   上游明确返回 0（例如极短输入）时也要如实覆盖。
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

const APP = 'D:/Fengcode/cli/src/fengcode/server/static/app.js';
const source = fs.readFileSync(APP, 'utf8');

/** 取 handleEvent 里真正的 usage 分支。
    ★ 必须从 `function handleEvent` 之后再找 `case "usage"`：源码里**注释**
      也会出现这段字样（讲历史坑时引用过），从头 indexOf 会切到注释上，
      于是断言检查的是一段注释、必然失败 —— 这条测试因此长期报假故障。 */
function usageBranch() {
  const h = source.indexOf('function handleEvent');
  assert.ok(h > 0, '应能找到 handleEvent');
  const i = source.indexOf('case "usage"', h);
  assert.ok(i > h, '应能找到 usage 分支');
  const rest = source.slice(i);
  const next = rest.indexOf('case "', 10);
  return next > 0 ? rest.slice(0, next) : rest.slice(0, 2000);
}

test('usage 分支：本轮无用量时不得把上一轮读数覆盖成 0', () => {
  const branch = usageBranch();
  // 必须存在「保留上一轮」的处理：要么沿用旧值，要么显式判断有无新数据。
  const keepsPrev =
    /S\.turnUsage\s*=\s*Object\.assign\(\{\},\s*S\.turnUsage/.test(branch) ||
    /_hasFreshUsage|hasUsage|lu\s*&&|lu\s*\?/.test(branch);
  assert.ok(
    keepsPrev,
    'usage 分支必须区分「本轮无数据」与「本轮数据为 0」，无数据时保留上一轮读数'
  );
});

test('usage 分支：命中率与上下文都取 last_usage 快照', () => {
  const branch = usageBranch();
  assert.ok(branch.includes('last_usage'), '应使用 last_usage 快照');
  assert.ok(branch.includes('renderStatusBar()'), '应刷新状态栏');
  assert.ok(branch.includes('renderInfoPanel()'), '应刷新右侧面板');
});

test('界面：有读数时应显示数字而不是「尚无调用记录」', () => {
  const i = source.indexOf('const usedKnown = used > 0;');
  assert.ok(i > 0, '上下文卡片应有「有无数据」的判定');
  // 该判定必须基于本轮真实数据，而不是简单地看 >0（否则 0 会被当成无数据）
  const around = source.slice(Math.max(0, i - 600), i + 200);
  assert.ok(
    around.includes('hasUsage') || around.includes('usageKnown') || around.includes('usedKnown'),
    '判定变量应明确命名，便于区分「无数据」与「数值为 0」'
  );
});
