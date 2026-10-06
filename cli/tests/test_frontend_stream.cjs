const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/fengcode/server/static/app.js'), 'utf8');

test('stream context retains reasoning box between events', () => {
  const start = source.indexOf('  const streamContext = {');
  // ★ 切片边界必须精确到「这个对象字面量的结尾」，不能用后面第一个 `\n  try {` ——
  //   那样会把中间的分帧/processPart 代码一起卷进来（其中用到 S.sessionId），
  //   沙箱里没有 S，测试就崩在无关的地方（实测踩过：注释一改长就炸）。
  //   这里改为从 start 起做花括号配平，取到对象闭合处为止。
  let depth = 0, end = -1;
  for (let i = start; i < source.length; i++) {
    const ch = source[i];
    if (ch === '{') depth++;
    else if (ch === '}') {
      depth--;
      if (depth === 0) { end = i + 1; break; }
    }
  }
  assert.ok(start >= 0 && end > start, '应能定位 streamContext 对象字面量');
  const context = vm.runInNewContext(`(() => {
    let assistEl, pendingEl, reasoningEl, reasoningText = '', streamBuf = '';
    const paintStream = () => {}, toolTimes = {}, t0 = 0;
    // ★ 沙箱要提供对象字面量引用到的**全部**外部标识符，否则崩在无关的地方：
    //   · sid —— send() 里冻结的会话 id（运行状态按会话隔离，归属由它决定）；
    //   · flushStream —— 同步落盘句柄（见「有输出但不显示」那条历史坑）。
    const sid = 's-test', flushStream = () => {};
    ${source.slice(start, end)}
    return streamContext;
  })()`);
  const box = {};
  context.reasoningBox = box;
  context.rtext += '第一段';
  context.rtext += '第二段';
  assert.equal(context.reasoningBox, box);
  assert.equal(context.rtext, '第一段第二段');
  // ★ 断言的是「事件分发用的是同一个 streamContext 对象」这件事本身。
  //   旧的断言写的是 handleEvent(JSON.parse(line), streamContext) ——
  //   那种调用形态在代码里从未存在过（实际是先把 SSE 帧解析进 processPart，
  //   再 handleEvent(ev, streamContext)），于是这条测试长期恒失败、掩盖了真问题。
  assert.ok(source.includes('handleEvent(ev, streamContext);'));
  assert.ok(!source.includes('handleEvent(ev, {'));
  assert.ok(source.includes('case "done":'));
  assert.ok(source.includes('finish_reason'));
});
