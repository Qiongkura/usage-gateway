// Offline regression test: node tests/test_float.cjs
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const assert = require('node:assert/strict');

const html = fs.readFileSync(path.join(__dirname, '../web/float.html'), 'utf8');
const begin = html.indexOf('function renderSpark()');
const end = html.indexOf('// 页面隐藏时降频', begin);
assert(begin >= 0 && end > begin);
const elements = new Map();
function element(id) {
  if (!elements.has(id)) elements.set(id, {
    textContent: '', innerHTML: '', style: {}, className: '',
    classList: {add() {}, remove() {}}, animate() {},
  });
  return elements.get(id);
}
let latest = null;
const timers = new Map();
let nextTimer = 0;
const context = {
  API: '', SPARK_N: 36, POLL_TIMEOUT_MS: 10000, history: [], lastRecord: null, statsKey: '',
  pollSerial: 0, pollInFlight: false, pollController: null, timer: null,
  $: element, Date, Math, Number, String, isNaN,
  AbortController,
  setStatus(text, error) { context.status = {text, error}; },
  setInterval() { return 1; }, clearInterval() {},
  setTimeout(fn) { const id = ++nextTimer; timers.set(id, fn); return id; },
  clearTimeout(id) { timers.delete(id); },
  localStorage: {setItem() {}},
  fetch: async () => ({ok: true, json: async () => ({latest})}),
};
vm.createContext(context);
vm.runInContext(html.slice(begin, end), context);

(async () => {
  const row = {ts: 100, source: 'demo', model: 'model', input_tokens: 0,
    output_tokens: 10, latency_ms: 1000, tok_per_sec: 10, gen_tok_per_sec: 10};
  latest = {...row, id: 1};
  await context.poll();
  assert.equal(element('tps').textContent, '10.0');
  latest = {...row, id: 2, output_tokens: 20, tok_per_sec: 20, gen_tok_per_sec: 20};
  await context.poll();
  assert.equal(element('tps').textContent, '20.0');
  assert.equal(context.history.length, 2);
  await context.poll();
  assert.equal(context.history.length, 2);
  latest = null;
  await context.poll();
  assert.equal(element('tps').textContent, '—');
  latest = {...row, id: 2};
  await context.poll();
  assert.equal(element('tps').textContent, '10.0');
  latest = {...row, ts: 101};
  await context.poll();
  assert.equal(element('tps').textContent, '10.0');
  latest = {...row, ts: 102, tok_per_sec: 30, gen_tok_per_sec: 30};
  await context.poll();
  assert.equal(element('tps').textContent, '30.0');

  const pending = [];
  context.fetch = (url, options) => new Promise((resolve, reject) => pending.push({resolve, reject, options}));
  const response = item => ({ok: true, json: async () => ({latest: item})});
  const older = context.poll();
  await context.poll();
  assert.equal(pending.length, 1, 'periodic polls must not overlap');
  context.statsKey = 'new key';
  const newer = context.poll(true);
  assert.equal(pending.length, 2);
  assert.equal(pending[1].options.headers['X-Stats-Key-Encoded'], 'new%20key');
  assert.equal(pending[0].options.signal.aborted, true);
  pending[1].resolve(response({...row, id: 4, tok_per_sec: 40, gen_tok_per_sec: 40}));
  await newer;
  pending[0].resolve(response({...row, id: 3, tok_per_sec: 15, gen_tok_per_sec: 15}));
  await older;
  assert.equal(element('tps').textContent, '40.0');
  assert.equal(context.status.error, false);

  const staleError = context.poll();
  context.stop();
  const restarted = context.poll();
  pending[3].resolve(response({...row, id: 5, tok_per_sec: 50, gen_tok_per_sec: 50}));
  await restarted;
  pending[2].reject(new Error('old failure'));
  await staleError;
  assert.equal(element('tps').textContent, '50.0');
  assert.equal(context.status.error, false);

  context.fetch = (url, options) => new Promise((resolve, reject) => {
    options.signal.addEventListener('abort', () => reject(new Error('timeout')));
  });
  const timedOut = context.poll();
  assert.equal(timers.size, 1);
  [...timers.values()][0]();
  await timedOut;
  assert.equal(context.status.error, true);
  assert.equal(context.status.text, '连接超时，等待重试');
  assert.equal(context.pollInFlight, false);
  context.fetch = async () => response({...row, id: 6, tok_per_sec: 60, gen_tok_per_sec: 60});
  await context.poll();
  assert.equal(element('tps').textContent, '60.0');
  assert.equal(context.status.error, false);
  assert.equal(timers.size, 0);
  console.log('floating speed regression passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
