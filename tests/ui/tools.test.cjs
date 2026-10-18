const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8');
const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');

function setup(offerable = true, confirm = true) {
  const elements = {}, calls = [], confirmations = [];
  class Element {
    constructor() { this.children = []; this.textContent = ''; this.hidden = false; }
    appendChild(node) { this.children.push(node); return node; }
    replaceChildren(...nodes) { this.children = nodes; }
  }
  const $ = id => elements[id] ||= new Element();
  const ctx = vm.createContext({ $, document: { createElement: () => new Element() },
    window: { confirm: message => { confirmations.push(message); return confirm; } },
    api: async url => {
      calls.push(['GET', url]);
      return ({
      mineru: { installed: false, available: true, version: null, size: '~4 GB' },
      pix2text: { installed: false, offerable, version: null, size: '~2 GB',
        command: 'uv sync --extra formulas --inexact' },
      vision: { model: '<model>', ready: false, message: 'Configure <model>' }
      });
    },
    post: async (url, body) => { calls.push([url, body]); return { id: 'job' }; },
    showView: view => calls.push(['view', view]), setTimeout: fn => fn(),
    watchToolJob: async () => {}, installMineru: async () => {},
    showMathSetup: status => calls.push(['dialog', status]), toast() {}
  });
  vm.runInContext(app.split('/* ---------- Optional tools ---------- */')[1]
    .split('/* ---------- End Optional tools ---------- */')[0], ctx);
  return { ctx, $, calls, confirmations };
}

test('Settings shows three optional tools with safe dynamic status and install command fallback', async () => {
  assert.match(html, /<h2>Optional tools<\/h2>/);
  assert.match(html, /id="mineru-status"/);
  for (const id of ['pix2text', 'vision']) assert.match(html, new RegExp('id="tool-' + id + '-status"'));
  assert.match(html, /id="tool-pix2text-action" hidden/);
  const unavailable = setup(false);
  await unavailable.ctx.loadOptionalTools();
  assert.match(unavailable.$('tool-pix2text-status').textContent, /uv sync --extra formulas --inexact/);
  assert.match(unavailable.$('tool-vision-status').textContent, /<model>/);
  assert.equal(unavailable.$('tool-vision-action').textContent, 'Open Models');
});

test('Pix2Text confirmation and install start a job; cancellation does not post', async () => {
  const ready = setup();
  await ready.ctx.installPix2Text();
  assert.match(ready.confirmations[0], /~2 GB/);
  assert.ok(ready.calls.some(c => c[0] === '/api/tools/pix2text/install'));
  const cancelled = setup(true, false);
  await cancelled.ctx.installPix2Text();
  assert.equal(cancelled.calls.some(c => c[0] === '/api/tools/pix2text/install'), false);
});

test('Legacy vision action opens the existing setup readiness dialog before posting', async () => {
  const { ctx, calls } = setup();
  ctx.api = async url => {
    calls.push(['GET', url]);
    return { installed: false, requirements: {}, blockers: [], warnings: [], manual: [] };
  };
  await ctx.setupVisionTool();
  assert.ok(calls.some(c => c[0] === 'GET' && c[1] === '/api/math-setup'));
  assert.ok(calls.some(c => c[0] === 'dialog'));
  assert.equal(calls.some(c => c[0] === '/api/math-setup'), false);
});

test('Export medium option guides users to Optional tools settings', () => {
  assert.match(app, /install in Settings → Optional tools/);
});
