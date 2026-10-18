const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8');
const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');
function setup(installed = true, confirm = true) {
  const calls = [], elements = {}, confirmations = [];
  const $ = id => elements[id] ||= { textContent: '', disabled: false, hidden: false };
  const ctx = vm.createContext({ $, window: { confirm: message => { confirmations.push(message); return confirm; } },
    api: async url => { calls.push(['GET', url]); return url.includes('/jobs/') ?
      { status: 'done', messages: ['Finished <unsafe>'] } : { installed, version: '<version>', models_bytes: 2000000000, notice: 'About 2 GB models' }; },
    post: async (url, body) => { calls.push(['POST', url, JSON.parse(JSON.stringify(body))]); return { id: 'job' }; },
    setTimeout: fn => fn(), showView: view => calls.push(['view', view]), openDrawer() {}
  });
  vm.runInContext(app.split('/* ---------- MinerU ---------- */')[1].split('/* ---------- End MinerU ---------- */')[0], ctx);
  return { ctx, $, calls, confirmations };
}
test('MinerU settings show version and size safely; install requires confirmation', async () => {
  assert.match(html, /id="mineru-status"/);
  const { ctx, $, calls } = setup();
  await ctx.loadMineru();
  assert.match($('mineru-status').textContent, /<version>.*2.00 GB/);
  await ctx.installMineru();
  assert.equal(calls.find(c => c[0] === 'POST')[1], '/api/mineru/install');
  assert.equal(calls.find(c => c[0] === 'POST')[2].confirmed, true);
  const cancelled = setup(false, false);
  await cancelled.ctx.installMineru();
  assert.equal(cancelled.calls.filter(c => c[0] === 'POST').length, 0);
});
test('MinerU estimates use library timing and display about N min', async () => {
  const measured = setup();
  await measured.ctx.rereadMineru({ id: 'p', page_count: 8, mineru_seconds_per_page: 45 });
  assert.match(measured.confirmations[0], /about 6 min/);
  const fallback = setup();
  await fallback.ctx.rereadMineru({ id: 'p', page_count: 8 });
  assert.match(fallback.confirmations[0], /about 3 min/);
  assert.doesNotMatch(measured.confirmations[0], /20 s/);
});
test('Re-read shows estimate, starts a job, and links missing MinerU to settings', async () => {
  const { ctx, $, calls } = setup();
  await ctx.rereadMineru({ id: 'p', page_count: 8 });
  assert.ok(calls.some(c => c[1] === '/api/papers/p/parse-mineru'));
  assert.match($('mineru-paper-status').textContent, /Finished <unsafe>/);
  const absent = setup(false);
  await absent.ctx.rereadMineru({ id: 'p', page_count: 8 });
  assert.ok(absent.calls.some(c => c[0] === 'view' && c[1] === 'settings'));
  assert.equal(absent.calls.filter(c => c[0] === 'POST').length, 0);
  assert.match(app, /d.pdf_url[\s\S]*Re-read accurately \(MinerU\)/);
  const section = app.split('/* ---------- MinerU ---------- */')[1].split('/* ---------- End MinerU ---------- */')[0];
  assert.doesNotMatch(section, /innerHTML/);
});
