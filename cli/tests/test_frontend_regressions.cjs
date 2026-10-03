const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const source = fs.readFileSync('D:/Fengcode/cli/src/fengcode/server/static/app.js', 'utf8');

test('message template does not render avatars or names', () => {
  assert.ok(!source.includes('<div class="avatar">'));
  assert.ok(source.includes('class="msg ${role}'));
});

test('session archive is inline and restores composer focus', () => {
  assert.ok(source.includes('确认归档'));
  assert.ok(source.includes('input.disabled = false'));
  assert.ok(source.includes('input.readOnly = false'));
  assert.ok(!source.includes('confirm("删除这个会话'));
});

// ★ 事件名必须两端一致：后端 events.py 发什么 type，前端 handleEvent 就得有同名 case。
//   踩过的坑：后端曾发 "tool.delta"（点号），前端只认 "tool_delta"（下划线），
//   SSE 原样透传不归一化 —— 事件被静默丢弃，表现为
//   ①「思考结束后要等十几秒才冒出 write_file 卡片」②「这段窗口里思考计时还在跳」。
test('tool delta event name matches between backend and frontend', () => {
  const events = fs.readFileSync('D:/Fengcode/cli/src/fengcode/events.py', 'utf8');
  const m = events.match(/TOOL_DELTA\s*=\s*"([^"]+)"/);
  assert.ok(m, 'events.py 里应能取到 TOOL_DELTA 常量');
  assert.equal(m[1], 'tool_delta', 'TOOL_DELTA 必须是下划线写法，与前端 case 对齐');
  assert.ok(source.includes(`case "${m[1]}"`), `app.js 里必须有 case "${m[1]}" 分支`);
  assert.ok(!source.includes('case "tool.delta"'), 'app.js 不应残留点号写法');
});
