/* 三处界面缺陷的防回归（2026-10-05 实测反馈）。
 *
 * 缺陷一：打开工具详情后鼠标滚轮像卡死、强制锁在最下面。
 *   根因：恢复「跟随底部」用了**滚动生效前**的旧位置 ——
 *     · wheel 分支刚把 FOLLOW_TAIL 置 false，滚动还没应用上去；
 *     · updateJumpBottom() 里又按旧距底（< 180px）把它改回 true，
 *       流式期间每个事件都调 scrollDown()，于是每一帧都被拽回底部。
 *   另一个诱因：滚轮落在工具详情 <pre>（内层滚动区）上时会冒泡到 #messages，
 *   把「在内层往上翻」误当成「外层要停止跟随」。
 *   本用例锁死：① 只有**真实 scroll 事件**能恢复跟随（那里距底是滚动后的真值）；
 *              ② 内层滚动区自己还能滚时，不动外层状态。
 *
 * 缺陷二：AI 排的待办生成了，但面板被聊天框挡住（只露出标题上沿）。
 *   根因：#todo-panel 占 #chat-page 的 grid 第 2 行，而 #composer 是
 *   position:absolute; bottom:0 —— 两者落在同一块区域（实测被压住 114px）。
 *   本用例锁死：待办面板必须脱离文档流、悬浮在输入卡片上方。
 */
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const STATIC = path.join(__dirname, '../src/fengcode/server/static');
const appSrc = fs.readFileSync(path.join(STATIC, 'app.js'), 'utf8');
const cssSrc = fs.readFileSync(path.join(STATIC, 'app.css'), 'utf8');

/** 取一段函数体：从 `function <name>` 到下一个顶层 `function` / `}` 收尾。 */
function funcBody(src, name) {
  const i = src.indexOf(`function ${name}`);
  assert.ok(i > 0, `应有 function ${name}`);
  const rest = src.slice(i + 1);
  const next = rest.search(/\n(\/\*\*|function |const |let |\/\/ )/);
  return rest.slice(0, next > 0 ? next : rest.length);
}

/* ---------------- 缺陷一：跟随滚动 ---------------- */

test('恢复「跟随底部」只发生在真实 scroll 事件里，不在滚轮处理里', () => {
  // 必须存在这个专职函数，并由 #messages 的 scroll 监听调用。
  const fn = funcBody(appSrc, 'updateFollowTailByPosition');
  assert.ok(
    fn.includes('FOLLOW_TAIL = true'),
    'updateFollowTailByPosition 负责「真的贴底了才恢复跟随」'
  );
  const i = appSrc.indexOf('const messagesEl = $("#messages")');
  assert.ok(i > 0, '应有 #messages 的监听注册');
  const reg = appSrc.slice(i, i + 400);
  assert.ok(
    reg.includes('updateFollowTailByPosition()'),
    '#messages 的 scroll 监听必须调用 updateFollowTailByPosition（距底取滚动后的真值）'
  );
});

test('滚轮处理不再依据「滚动前」的距底恢复跟随', () => {
  const w = appSrc.slice(appSrc.indexOf('function watchUserScroll'));
  const body = w.slice(0, w.indexOf('\nfunction ') > 0 ? w.indexOf('\nfunction ') : 1200);
  // 旧写法的复发特征：滚轮里再按距底把 FOLLOW_TAIL 改回 true。
  assert.ok(
    !/else\s+if\s*\(\s*m\.scrollHeight\s*-\s*m\.scrollTop\s*-\s*m\.clientHeight\s*<\s*24\s*\)/.test(body),
    '滚轮事件触发时 scrollTop 还没被应用 —— 不能在这里按距底恢复跟随'
  );
});

test('滚轮落在内层滚动区（.rc / 工具详情 pre）上时不影响外层跟随', () => {
  const w = appSrc.slice(appSrc.indexOf('function watchUserScroll'));
  const body = w.slice(0, 1600);
  assert.ok(
    body.includes('.rc, .tbody pre'),
    '必须识别内层滚动区：它们自己还能滚时这格滚轮不属于外层'
  );
  assert.ok(
    /canUp|scrollTop\s*>\s*0/.test(body),
    '内层「还能往上滚」要显式判断'
  );
});

test('updateJumpBottom 只单向停止跟随，不反向恢复', () => {
  const fn = funcBody(appSrc, 'updateJumpBottom');
  assert.ok(fn.includes('FOLLOW_TAIL = false'), '离开底部时要停止跟随');
  assert.ok(
    !fn.includes('FOLLOW_TAIL = true'),
    'updateJumpBottom 不得把 FOLLOW_TAIL 置回 true：'
    + '它在「内容已增长但用户还在底部」与「刚上滑一格」两种情形下读到的是同一个旧位置，'
    + '一旦在这里恢复就把用户的上滑意图抹掉了（实测滚轮锁底）'
  );
});

/* ---------------- 缺陷二：待办面板 ---------- */

test('待办面板脱离文档流、悬浮在输入卡片上方', () => {
  const i = cssSrc.indexOf('.todo-panel {');
  assert.ok(i > 0, '应有 .todo-panel 规则');
  const rule = cssSrc.slice(i, cssSrc.indexOf('}', i));
  assert.ok(
    rule.includes('position: absolute'),
    '待办面板必须 absolute 悬浮：留在 grid 第 2 行会被 absolute 的 #composer 压住'
  );
  assert.ok(
    /bottom:\s*calc\(var\(--composer-block-h/.test(rule),
    '底部让位量必须用输入区整块高度 --composer-block-h（与 #jump-bottom 同一口径）'
  );
  assert.ok(
    !/margin:\s*0\s+auto\s+6px/.test(rule),
    '不应再保留「在文档流里占一行」的外边距写法'
  );
});
