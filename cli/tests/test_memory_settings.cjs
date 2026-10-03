/* 记忆页结构（图 2 反馈）：只把「真正要看的东西」摆在明面上。
 *
 * 反馈原话：「中间那个召回和上面的那个是啥意思」「reasonix 为啥那么整洁？」
 * 结论：这一页真正要看的是「AI 到底记住了什么」（记忆条目列表）。
 * 召回参数（轮数 / 条数 / 最低分 / 半衰期 / 向量检索）属于细分设置，
 * 用户没设过也不懂，默认值本来够用 —— 收进折叠，需要时再展开。
 *
 * 本用例锁死这个结构，防止后续又被塞回明面。
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');

const APP = 'D:/Fengcode/cli/src/fengcode/server/static/app.js';
const source = fs.readFileSync(APP, 'utf8');

// 取出设置页里记忆面板那一段
function memoryPane() {
  const start = source.indexOf('<div id="set-memory"');
  assert.ok(start > 0, '应能找到记忆设置面板');
  const next = source.indexOf('<div id="set-safety"', start);
  assert.ok(next > start, '应能找到面板结束位置');
  return source.slice(start, next);
}

test('记忆面板：明面上只有「记忆总开关」与「记忆条目」两张卡片', () => {
  const pane = memoryPane();
  // 明面部分 = 去掉 <details> 折叠块之后剩下的内容
  const detailsStart = pane.indexOf('<details');
  const detailsEnd = pane.indexOf('</details>');
  assert.ok(detailsStart > 0 && detailsEnd > detailsStart, '必须有细分设置的折叠块');
  const visible = pane.slice(0, detailsStart) + pane.slice(detailsEnd);

  assert.ok(visible.includes('记忆总开关'), '明面应保留总开关');
  assert.ok(visible.includes('记忆条目'), '明面应保留记忆条目列表');
  assert.ok(!visible.includes('分层记忆'), '分层记忆应移入折叠');
  assert.ok(!visible.includes('<h3>召回'), '召回参数应移入折叠');
});

test('记忆面板：折叠区保留全部原有控件（只是收起，不是删掉）', () => {
  const pane = memoryPane();
  for (const id of ['m-long', 'm-epi', 'm-prof', 'm-auto',
                    'm-turns', 'm-top', 'm-minscore', 'm-vec', 'm-decay']) {
    assert.ok(pane.includes(`id="${id}"`), `折叠区内应保留 #${id}`);
  }
  assert.ok(pane.includes('细分设置'), '折叠块应有说明性的标题');
});

test('记忆面板：召回参数带可读说明（不再只有参数名）', () => {
  const pane = memoryPane();
  assert.ok(pane.includes('回看最近几轮对话'), '轮数应有说明');
  assert.ok(pane.includes('最多挑几条记忆'), '条数应有说明');
  assert.ok(pane.includes('0 = 不限'), '最低分应说明「0 等于不设门槛」');
  assert.ok(pane.includes('权重减半'), '半衰期应有说明');
});
