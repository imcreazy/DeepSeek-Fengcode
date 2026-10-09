/* 三处界面缺陷的防回归（2026-10-05 实际使用中）。
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

/* ---------------- 缺陷三：待办面板挡住了最后一行文字（1.2.19） ----------------
 * 现象：待办面板浮在输入卡片上方，鼠标滑到最下面，最后一行仍被面板盖住。
 *   根因：#messages 的底部留白只算了输入区（--composer-block-h），
 *         面板自己占的那一段没算，卡片上方那 200px 空白里正好叠着面板。
 * 本用例锁死：① 底部留白必须再加一份待办面板的高度；
 *            ② 这份高度由 JS 同步、面板隐藏时归 0；
 *            ③ 让位量（8px）两边一致。
 */

test('消息区底部留白除了输入区，还要加上待办面板占的那段', () => {
  const i = cssSrc.indexOf('#messages {');
  assert.ok(i > 0, '应有 #messages 规则');
  const rule = cssSrc.slice(i, cssSrc.indexOf('}', i));
  assert.ok(
    /padding:[^;]*--todo-block-h/.test(rule),
    '底部留白必须叠加 --todo-block-h：不加就复现「滑到最下面还是被待办挡住」'
  );
});

test('待办面板高度由 syncTodoBlockVar 同步，隐藏时归 0', () => {
  assert.ok(
    appSrc.includes('function syncTodoBlockVar'),
    '应有专职函数同步 --todo-block-h'
  );
  const i = appSrc.indexOf('function syncTodoBlockVar');
  const body = appSrc.slice(i, appSrc.indexOf('\n}', i));
  assert.ok(/let h = 0/.test(body), '面板隐藏时高度必须是 0，否则会白留一块');
  assert.ok(/--todo-block-h/.test(body), '必须写到 --todo-block-h');
  assert.ok(
    /renderTodoPanel/.test(appSrc.slice(appSrc.indexOf('function renderTodoPanel'),
      appSrc.indexOf('function renderTodoPanel') + 2600)),
    '面板每次重渲染后都要重算（展开/收起会改高度）'
  );
});

test('待办面板的让位量与 JS 记的高度口径一致（8px）', () => {
  const i = cssSrc.indexOf('.todo-panel {');
  const rule = cssSrc.slice(i, cssSrc.indexOf('}', i));
  const m = rule.match(/bottom:\s*calc\(var\(--composer-block-h[^+]*\+\s*(\d+)px\)/);
  assert.ok(m, '.todo-panel 的 bottom 应是「输入区高度 + Npx」');
  const px = m[1];
  const j = appSrc.indexOf('function syncTodoBlockVar');
  const body = appSrc.slice(j, appSrc.indexOf('\n}', j));
  assert.ok(
    body.includes(`+ ${px}`),
    `JS 记的高度必须带上同一个让位量 ${px}px，否则面板顶端仍会压住文字`
  );
});

/* ---------------- 缺陷四：待办面板默认展开（1.2.19） ----------------
 * 默认收起：AI 刚排好待办时不再自动摊开占掉半屏，
 * 要看细节由点击标题栏手动展开。
 * 本用例锁死：① 渲染出来的正文默认 hidden；
 *            ② 展开态按会话记住（每次重渲染会重建内层 DOM，记不住就弹回去）；
 *            ③ 记录的是**翻转后**的状态（写翻前值会让展开一刷新就复位）。
 */

test('待办面板默认收起，展开态按会话记住', () => {
  const i = appSrc.indexOf('function renderTodoPanel');
  const body = appSrc.slice(i, i + 2600);
  assert.ok(
    /todo-body"\$\{open \? "" : " hidden"\}/.test(body),
    '渲染时正文必须默认 hidden（展开由用户点标题栏决定）'
  );
  assert.ok(body.includes('TODO_OPEN[S.sessionId]'), '展开态必须按会话记住，不能只放 DOM 上');
});

test('展开态记录写在翻转之后', () => {
  const i = appSrc.indexOf('function renderTodoPanel');
  const body = appSrc.slice(i, i + 2600);
  const flip = body.indexOf('body.hidden = !body.hidden;');
  const remember = body.indexOf('TODO_OPEN[S.sessionId] =');
  assert.ok(flip > 0 && remember > 0, '应有「翻转 body.hidden」与「记下展开态」两处');
  assert.ok(
    remember > flip,
    '必须先翻转再记录：`TODO_OPEN = !body.hidden` 写在翻转前记的是旧值，'
    + '展开后下一次重渲染（task.update）就被弹回收起（实测）'
  );
});

test('收起态也要显式覆盖 hidden（否则正文照样摊开）', () => {
  assert.ok(
    /\.todo-body\[hidden\]\s*\{[^}]*display:\s*none/.test(cssSrc),
    'markdown 里大量元素是 display:flex，单靠 hidden 属性不一定真隐藏，要显式压住'
  );
});
