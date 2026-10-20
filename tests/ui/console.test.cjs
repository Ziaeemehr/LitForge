/* Run with node --test tests/ui/. Exercises the Console view without a browser. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8')
  .split('  function loadConsole()')[1].split('  function designGap')[0];

function setup() {
  class Element {
    constructor(tag = 'div') {
      this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {};
      this.classList = { toggle() {}, add() {} }; this.hidden = false;
      this.textContent = ''; this.value = '';
      Object.defineProperty(this, 'innerHTML', { set() { throw Error('Unsafe innerHTML'); } });
    }
    appendChild(element) { this.children.push(element); return element; }
    replaceChildren(...elements) { this.children = elements; }
    remove() {}
  }
  const elements = {};
  const $ = id => elements[id] ||= new Element();
  const calls = [];
  const confirmations = [];
  const ctx = vm.createContext({
    $, state: { project: 'demo', consolePlan: null, consoleRunId: null },
    lensNode(tag, text, cls) {
      const node = new Element(tag);
      if (text != null) node.textContent = String(text);
      if (cls) node.className = cls;
      return node;
    },
    lensButton(text, action, cls) {
      const node = new Element('button'); node.textContent = text; node.onclick = action; node.className = cls || '';
      return node;
    },
    showView(view) { calls.push({ view }); },
    loadLensMatrices(id) { calls.push({ matrix: id }); return Promise.resolve(); },
    loadLensAnswer(id) { calls.push({ answer: id }); return Promise.resolve(); },
    setTimeout() {},
    window: { confirm(message) { confirmations.push(message); return true; } },
    api: async path => {
      calls.push({ path });
      if (path === '/api/console/runs') return [];
      return { id: 'run', request: 'request', status: 'done', current_step: 2,
        updated_at: '2026-10-17T12:00:00Z', results: [] };
    },
    post: async (path, body) => {
      calls.push({ path, body: { ...body } });
      if (path === '/api/console/execute') return { run_id: 'run' };
      return {};
    }
  });
  vm.runInContext('function loadConsole()' + source, ctx);
  return { ctx, $, calls, confirmations };
}

const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(setImmediate); };
const text = node => [node.textContent, ...node.children.map(text)].join(' ');

test('console preview renders untrusted arguments as text and includes per-step estimates', () => {
  const { ctx, $ } = setup();
  const hostile = '<img src=x onerror=alert(1)>';
  ctx.renderConsolePreview({ plan: { request: 'request', steps: [] }, preview: {
    model_call_count: 1, max_cost_usd: 0, time_estimate_seconds: 2, writes: true, blocked_reasons: [],
    steps: [{ step: 1, action: 'ask', args: { question: hostile }, papers_in_scope: 2, model_call_count: 1,
      max_cost_usd: 0, time_estimate_seconds: 2, writes: true }]
  } });
  assert.equal(ctx.state.consolePlan.request, 'request');
  assert.match(text($('console-preview')), /<img src=x onerror=alert\(1\)>/);
  assert.match(text($('console-preview')), /Papers in scope: 2/);
  assert.equal($('console-run').hidden, false);
});

test('cloud planner route is separately approved and only then retried', async () => {
  const { ctx, calls, confirmations } = setup();
  let attempt = 0;
  ctx.post = async (path, body) => {
    calls.push({ path, body: { ...body } });
    if (++attempt === 1) {
      const error = new Error('approval');
      error.status = 403;
      error.preview = { provider: 'remote', model: 'cloud' };
      throw error;
    }
    return { plan: { request: 'request', steps: [] }, preview: { steps: [], blocked_reasons: [] } };
  };
  const result = await ctx.planConsole('request', false);
  assert.equal(result.plan.request, 'request');
  assert.deepEqual(confirmations.length, 1);
  assert.equal(calls[0].body.approved_cloud, false);
  assert.equal(calls[1].body.approved_cloud, true);
});

test('execution needs its own confirmation and a distinct cloud approval', async () => {
  const { ctx, calls, confirmations } = setup();
  ctx.state.consolePlan = { request: 'request', steps: [] };
  let attempt = 0;
  ctx.post = async (path, body) => {
    calls.push({ path, body: { ...body } });
    if (path === '/api/console/execute' && ++attempt === 1) {
      const error = new Error('cloud approval');
      error.status = 403;
      error.preview = { requires_approval: true };
      throw error;
    }
    return { run_id: 'saved-run' };
  };
  ctx.window.confirm('Approve execution?');
  const run = await ctx.runConsolePlan(false);
  assert.equal(run.run_id, 'saved-run');
  assert.equal(confirmations.length, 2);
  assert.equal(calls[0].body.approved, true);
  assert.equal(calls[0].body.approved_cloud, false);
  assert.equal(calls[1].body.approved_cloud, true);
});

test('console run result exposes matrix and answer navigation through safe DOM nodes', () => {
  const { ctx, $, calls } = setup();
  ctx.showConsoleRun({ id: 'run', status: 'done', current_step: 3, results: [
    { step: 1, action: 'create_matrix', output: { matrix_id: 'matrix-id' } },
    { step: 2, action: 'ask', output: { run_id: 'answer-id', answer: {} } }
  ] });
  assert.match(text($('console-run-detail')), /Open matrix/);
  assert.match(text($('console-run-detail')), /Open answer/);
  $('console-run-detail').children[1].children[2].onclick();
  $('console-run-detail').children[2].children[2].onclick();
  assert.ok(calls.some(call => call.view === 'matrix'));
  assert.ok(calls.some(call => call.matrix === 'matrix-id'));
  assert.ok(calls.some(call => call.answer === 'answer-id'));
});

test('console cancel button requests cancellation between steps', async () => {
  const { ctx, $, calls } = setup();
  ctx.state.consoleRunId = 'running-id';
  ctx.cancelConsoleRun();
  await flush();
  assert.equal(calls[0].path, '/api/console/cancel');
  assert.equal(calls[0].body.run_id, 'running-id');
});

test('Console view has navigation and a dedicated request/preview section', () => {
  const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');
  assert.match(html, /data-view="console"[\s\S]*?<span class="nav-label">Console<\/span>/);
  assert.match(html, /id="view-console"/);
  assert.match(html, /id="console-request"/);
  assert.match(html, /id="console-run"[^>]*>Confirm and run/);
});
