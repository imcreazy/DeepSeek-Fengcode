/* 桌面端日志递归崩溃的防回归（2026-10-07 实测事故）。

事故经过：桌面端 stdout 断开后 console.log 抛 EPIPE；
        uncaughtException 处理器调 log() 去记录这个异常 → 写日志又抛 → 又被同一处理器接住，
        无限递归，每轮往 desktop.log 追加一段堆栈 → 日志涨到 7.7GB，
        主进程与磁盘 IO 被占满，窗口点不动也关不掉。

本测试从 main.js 里**取出真实实现**来跑（不是另抄一份），锁死三条：
  ① log() 自身绝不向外抛异常；
  ② 崩溃处理器不会递归（同一进程内只记一次）；
  ③ 日志有上限，会轮转。
*/
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');

const MAIN = path.join(__dirname, '../main.js');
const src = fs.readFileSync(MAIN, 'utf8');

/** 从 main.js 里切出「日志实现」+「崩溃处理器」两段真实源码。 */
function extractImpl(overrides = {}) {
  const a = src.indexOf('const LOG_DIR = path.join(app.getPath("userData"), "logs");');
  assert.ok(a > 0, '应能定位日志实现段');
  const aEnd = src.indexOf('// ---------------------------------------------------------------- 工具', a);
  assert.ok(aEnd > a, '应能定位日志实现段结尾');
  let region1 = src.slice(a, aEnd);

  const b = src.indexOf('// 崩溃兜底');
  assert.ok(b > 0, '应能定位崩溃兜底段');
  const bEnd = src.indexOf('process.on("unhandledRejection"', b);
  assert.ok(bEnd > b, '应能定位崩溃兜底段结尾');
  const region2 = src.slice(b, src.indexOf('});', bEnd) + 3);

  let body = region1 + '\n' + region2;
  for (const [k, v] of Object.entries(overrides)) {
    body = body.replace(k, v);
  }
  return body;
}

/** 在受控环境里跑那段真实源码：stub 掉 app / fs / console。 */
function makeHarness(overrides, stubs) {
  const body = extractImpl(overrides);
  const fn = new Function('fs', 'console', 'app', 'Buffer', 'path',
    body + '\nreturn { log, rotateLog, logCrash };');
  return fn(stubs.fs, stubs.console, stubs.app, Buffer, path);
}

function tmpDir() {
  return fs.mkdtempSync(path.join(os.tmpdir(), 'fg-log-'));
}

test('★ log() 自身绝不抛异常（console 抛 EPIPE 时）', () => {
  const dir = tmpDir();
  const app = { getPath: () => dir };
  const consoleStub = { log() { const e = new Error('EPIPE: broken pipe, write'); e.code = 'EPIPE'; throw e; } };
  const { log } = makeHarness({}, { fs, console: consoleStub, app });

  // 事故里就是这一句把异常抛进了 uncaughtException 处理器
  assert.doesNotThrow(() => log('未捕获异常：', 'boom'), 'log() 不能把异常抛出去');
  assert.doesNotThrow(() => { for (let i = 0; i < 50; i++) log('x', i); });

  const logFile = path.join(dir, 'logs', 'desktop.log');
  const lines = fs.readFileSync(logFile, 'utf8').trim().split('\n').filter(Boolean);
  assert.equal(lines.length, 51, '每次调用只写一行 —— 不能因为 console 抛错而反复重写');
});

test('★★ 崩溃处理器不递归：同一进程内只记一次', () => {
  const dir = tmpDir();
  const app = { getPath: () => dir };
  // 让 console.log 抛错（复现事故），并统计它被调用的次数
  let consoleCalls = 0;
  const consoleStub = { log() { consoleCalls++; throw new Error('EPIPE'); } };
  const { logCrash } = makeHarness({}, { fs, console: consoleStub, app });

  for (let i = 0; i < 20; i++) logCrash('未捕获异常：', new Error('boom ' + i));

  const logFile = path.join(dir, 'logs', 'desktop.log');
  const text = fs.readFileSync(logFile, 'utf8');
  // ★ 注意：一次记录会写入**多行**（异常堆栈自带换行），所以不能数行数 ——
  //   要数「记录了几条异常」，即 boom 出现的次数。
  const recorded = (text.match(/boom \d+/g) || []).length;
  assert.equal(recorded, 1, `只应记录第一次那个异常，实际 ${recorded} 条（递归就会暴涨）`);
  assert.ok(text.includes('boom 0'), '记的是第一次那个异常');
  assert.ok(consoleCalls <= 2, `console 调用次数应很少，实际 ${consoleCalls}`);
});

test('★ 日志有上限，超了会轮转', () => {
  const dir = tmpDir();
  const app = { getPath: () => dir };
  const consoleStub = { log() {} };
  // 把 8MB 阈值换成 2KB，用同一套逻辑验证轮转
  const { log } = makeHarness(
    { 'const LOG_MAX_BYTES = 8 * 1024 * 1024;': 'const LOG_MAX_BYTES = 2048;' },
    { fs, console: consoleStub, app });

  for (let i = 0; i < 400; i++) log('填充行', i, 'x'.repeat(60));

  const logFile = path.join(dir, 'logs', 'desktop.log');
  const size = fs.statSync(logFile).size;
  assert.ok(size <= 2048 + 200, `当前日志应受上限约束，实际 ${size} 字节`);
  assert.ok(fs.existsSync(logFile + '.1'), '应有轮转出来的上一份 .1');
  // 保留份数不超过设定值
  assert.ok(!fs.existsSync(logFile + '.5'), '不应无限保留历史份数');
});

test('★ 日志目录不可写时也不抛（磁盘满 / 权限不足）', () => {
  const dir = tmpDir();
  const app = { getPath: () => dir };
  const consoleStub = { log() {} };
  const fsStub = {
    mkdirSync: fs.mkdirSync,
    statSync() { throw new Error('ENOENT'); },
    appendFileSync() { const e = new Error('ENOSPC'); e.code = 'ENOSPC'; throw e; },
    existsSync: () => false,
    renameSync() { throw new Error('EPERM'); },
  };
  const { log, logCrash } = makeHarness({}, { fs: fsStub, console: consoleStub, app });

  assert.doesNotThrow(() => log('写不进去也不能炸'));
  assert.doesNotThrow(() => logCrash('未捕获异常：', new Error('boom')));
});
