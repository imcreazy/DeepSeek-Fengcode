const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../src/fengcode/server/static/app.js'), 'utf8');

test('stream context retains reasoning box between events', () => {
  const start = source.indexOf('  const streamContext = {');
  const end = source.indexOf('\n  try {', start);
  assert.ok(start >= 0 && end > start);
  const context = vm.runInNewContext(`(() => {
    let assistEl, pendingEl, reasoningEl, reasoningText = '', streamBuf = '';
    const paintStream = () => {}, toolTimes = {}, t0 = 0;
    ${source.slice(start, end)}
    return streamContext;
  })()`);
  const box = {};
  context.reasoningBox = box;
  context.rtext += '第一段';
  context.rtext += '第二段';
  assert.equal(context.reasoningBox, box);
  assert.equal(context.rtext, '第一段第二段');
  assert.ok(source.includes('handleEvent(JSON.parse(line), streamContext);'));
  assert.ok(!source.includes('handleEvent(ev, {'));
  assert.ok(source.includes('case "done":'));
  assert.ok(source.includes('finish_reason'));
});
