const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8');
function fn(name) {
  const start = source.indexOf('  function ' + name + '(');
  const end = source.indexOf('\n  function ', start + 1);
  return source.slice(start, end);
}
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };
class Element {
  constructor(text = '') { this.textContent = text; this.children = []; this.classList = { toggle() {} }; }
  appendChild(child) { this.children.push(child); return child; }
  replaceChildren(...children) { this.children = children; }
  setAttribute() {}
}
function setup(confirmed = true) {
  const elements = {}, calls = [], confirmations = [];
  const context = vm.createContext({
    $: id => elements[id] ||= new Element(), state: { project: 'demo', page: 'gaps' },
    lens: { busy: false }, drawerRequest: 1,
    lensNode: (_tag, text) => new Element(text),
    lensButton: (text, onclick) => Object.assign(new Element(text), { onclick }),
    renderReproCard() {}, renderGapCandidate() {}, loadLivingPanel() {}, loadLensClaims() {},
    lensBusy(busy) { context.lens.busy = busy; },
    setTimeout, window: { confirm(text) { confirmations.push(text); return confirmed; } },
    api: async path => {
      calls.push({ method: 'GET', path });
      if (path === '/api/models') return { tasks: { matrix_extract: { profile: 'remote' } }, profiles: [
        { name: 'remote', model: 'cloud-model', remote: true }] };
      if (path.startsWith('/api/jobs/')) return { status: 'done', messages: [] };
      return [];
    },
    post: async (path, body) => { calls.push({ method: 'POST', path, body });
      return path === '/api/refresh' ? { id: 'job' } : []; },
  });
  return { context, elements, calls, confirmations };
}
test('gap detection uses the protected POST route', async () => {
  const { context, calls } = setup();
  vm.runInContext(fn('loadGaps'), context);
  await context.loadGaps();
  assert.equal(calls[0].method, 'POST');
  assert.equal(calls[0].path, '/api/gaps/detect');
  assert.equal(calls[0].body.project, 'demo');
});
test('repro tab refreshes the cache through POST', async () => {
  const { context, calls, elements } = setup();
  vm.runInContext(fn('attachReproTabs'), context);
  context.attachReproTabs({ id: 'p' }, 1);
  elements.dbody.children[0].children[1].onclick();
  await flush();
  assert.equal(calls[0].method, 'POST');
  assert.equal(calls[0].path, '/api/papers/p/repro');
  assert.equal(calls[0].body.project, 'demo');
});
for (const confirmed of [true, false]) {
  test('refresh honors cloud confirmation: ' + confirmed, async () => {
    const { context, calls, confirmations } = setup(confirmed);
    vm.runInContext(fn('refreshLivingProject'), context);
    context.refreshLivingProject();
    await flush();
    assert.equal(confirmations.length, 1);
    assert.match(confirmations[0], /cloud-model/);
    const refresh = calls.find(c => c.path === '/api/refresh');
    assert.equal(Boolean(refresh), confirmed);
    if (confirmed) assert.equal(refresh.body.approved, true);
  });
}
