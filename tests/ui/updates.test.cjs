const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8');
const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');

function setup(report = { status: 'up_to_date', command: 'curl | sh' }) {
  const elements = {}, calls = [], timers = [], copied = [];
  const $ = id => elements[id] ||= { textContent: '', hidden: false, checked: true };
  const ctx = vm.createContext({ $, navigator: { clipboard: { writeText: async text => copied.push(text) } },
    api: async url => { calls.push(['GET', url]); return report; },
    post: async (url, body) => { calls.push([url, body]); return report; },
    setTimeout: fn => { timers.push(fn); return timers.length; }, clearTimeout() {}, toast() {}
  });
  vm.runInContext(app.split('/* ---------- Updates ---------- */')[1]
    .split('/* ---------- End Updates ---------- */')[0], ctx);
  return { ctx, $, calls, timers, copied };
}

test('Updates card is first, with manual check, consent, terminal instructions and gear badge', () => {
  assert.ok(html.indexOf('<h2>Updates</h2>') < html.indexOf('<h2>Models</h2>'));
  for (const id of ['update-status', 'update-command', 'update-copy', 'update-check', 'update-auto', 'update-badge']) {
    assert.match(html, new RegExp('id="' + id + '"'));
  }
  assert.match(html, /Close LitForge first, then run this in a terminal/);
  assert.match(html, /GitHub/);
  assert.match(app, /initUpdates\(\)/);
});

test('Available update renders hostile commit text safely, copies command and shows badge', async () => {
  const s = setup({ status: 'available', behind: 3, latest: { subject: '<img onerror=bad>', date: '2026-10-18' },
    command: 'curl | sh', message: 'Details', update_check: true, update_check_enabled: true });
  await s.ctx.loadUpdates();
  assert.match(s.$('update-status').textContent, /An update is available: 3 new changes — <img onerror=bad>, 2026-10-18/);
  assert.equal(s.$('update-badge').hidden, false);
  assert.equal(s.$('update-command').textContent, 'curl | sh');
  s.ctx.initUpdates();
  await s.$('update-copy').onclick();
  assert.deepEqual(s.copied, ['curl | sh']);
  delete s.ctx.navigator.clipboard;
  await s.$('update-copy').onclick(); // still usable when clipboard access is unavailable
});

test('All statuses render and clear the badge; disabled hides the notice until manual check', async () => {
  const s = setup();
  const statuses = { up_to_date: /LitForge is up to date/, diverged: /diverged/i,
    not_git: /not a git install/i, error: /could not check/i, checking: /checking/i };
  for (const [status, pattern] of Object.entries(statuses)) {
    s.ctx.renderUpdate({ status, command: 'command', behind: 1, ahead: 1 });
    assert.match(s.$('update-status').textContent, pattern);
    assert.equal(s.$('update-badge').hidden, true);
  }
  s.ctx.renderUpdate({ status: 'disabled', update_check: false, update_check_enabled: false });
  assert.equal(s.$('update-notice').hidden, true);
  assert.equal(s.$('update-badge').hidden, true);
  s.ctx.initUpdates();
  await s.$('update-check').onclick();
  assert.equal(s.calls.some(c => c[0] === '/api/update/check'), true);
  assert.equal(s.$('update-notice').hidden, false);
});

test('Auto-check toggle saves through settings and checking polls without duplicate forced requests', async () => {
  const s = setup({ status: 'checking' });
  s.ctx.initUpdates();
  s.$('update-auto').checked = false;
  await s.$('update-auto').onchange();
  const saved = s.calls.find(c => c[0] === '/api/settings');
  assert.equal(saved[1].update_check, false);
  await s.ctx.loadUpdates();
  assert.ok(s.timers.length);
  assert.equal(s.calls.some(c => c[0] === '/api/update/check'), false);
});
