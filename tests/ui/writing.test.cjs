const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8');
const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');
const block = app.split('/* ---------- Write ---------- */')[1].split('/* ---------- End Write ---------- */')[0];

class Element {
  constructor(tag) {
    this.tagName = tag; this.children = []; this.textContent = ''; this.hidden = false; this.value = '';
    this.listeners = {}; this.className = ''; this.selectionStart = 0; this.selectionEnd = 0;
  }
  appendChild(node) { this.children.push(node); return node; }
  replaceChildren(...nodes) { this.children = nodes; }
  addEventListener(type, fn) { (this.listeners[type] ||= []).push(fn); }
  fire(type) { (this.listeners[type] || []).forEach(fn => fn({})); }
  focus() {}
  setSelectionRange(a, b) { this.selectionStart = a; this.selectionEnd = b; }
  setRangeText(text, from, to) { this.value = this.value.slice(0, from) + text + this.value.slice(to); }
}

function setup(routes = {}) {
  const elements = {}, calls = [], timers = [];
  const files = { 'main.tex': { text: '\\documentclass{article}\n', hash: 'h0' } };
  const $ = id => elements[id] ||= new Element('div');
  const fail = (status, body) => { const e = new Error(body.error || 'HTTP ' + status); e.status = status; return Promise.reject(e); };
  const ctx = vm.createContext({
    $, calls, files, timers, Promise, String, Object, Math, Date,
    document: { createElement: tag => new Element(tag) },
    window: { confirm: () => true, prompt: () => 'Renamed', addEventListener() {} },
    projectName: () => 'demo', state: { projects: [{ name: 'demo' }] }, toast: msg => calls.push(['toast', msg]),
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout: () => {},
    fetch: async () => ({ ok: true, json: async () => ({ name: 'figures/plot.png' }) }),
    api: async url => {
      calls.push(['GET', url]);
      if (routes.get) { const r = routes.get(url); if (r) return r; }
      if (url.startsWith('/api/write/docs')) {
        return { documents: [{ id: 'd1', name: 'مقاله', template: 'persian', updated_at: '2026-10-19T10:00:00' }],
          templates: { article: 'English article', persian: 'Persian article' },
          engines: { tectonic: { installed: true, version: '0.17.0' }, sandbox: 'macos' } };
      }
      if (url.startsWith('/api/write/doc?')) return { id: 'd1', name: 'Draft', files: [{ name: 'main.tex', kind: 'text' }], pdf: false };
      if (url.startsWith('/api/write/file')) return { name: 'main.tex', text: files['main.tex'].text, hash: files['main.tex'].hash };
      if (url.startsWith('/api/write/snapshots')) return { snapshots: [{ id: 's1', created_at: '2026-10-19T10:00:00', note: 'auto' }] };
      if (url.startsWith('/api/jobs/')) return { status: 'done', report: { ok: false, engine: 'tectonic', elapsed_ms: 500,
        errors: [{ message: 'Undefined control sequence.', file: 'main.tex', line: 2 }], warnings: [] } };
      throw new Error('unexpected ' + url);
    },
    post: async (url, body) => {
      calls.push([url, body]);
      if (routes.post) { const r = routes.post(url, body); if (r) return r; }
      if (url === '/api/write/save') {
        if (body.hash !== files['main.tex'].hash) return fail(409, { error: 'conflict' });
        files['main.tex'] = { text: body.text, hash: 'h' + (calls.length) };
        return { hash: files['main.tex'].hash };
      }
      if (url === '/api/write/compile') return { id: 'job1' };
      if (url === '/api/write/create') return { id: 'd2' };
      return { ok: true };
    }
  });
  vm.runInContext(block + '\nthis.W = W;', ctx);
  return { ctx, $, calls, timers, files };
}

test('Write view is in the sidebar, has the editor, PDF frame and loads the bundle offline', () => {
  assert.match(html, /data-view="write"/);
  for (const id of ['view-write', 'write-cm', 'write-pdf', 'write-errors', 'write-conflict', 'write-compile'])
    assert.match(html, new RegExp('id="' + id + '"'));
  assert.match(html, /\/static\/vendor\/codemirror\.bundle\.js/);
  assert.doesNotMatch(html, /https?:\/\/[^"]*codemirror/);
});

test('document list renders names as text and shows the engine', async () => {
  const { ctx, $ } = setup();
  await ctx.loadWrite();
  const li = $('write-docs').children[0];
  assert.equal(li.children[0].textContent, 'مقاله');
  assert.match($('write-engine').textContent, /Tectonic 0\.17\.0 · sandboxed/);
  assert.equal($('write-new-template').children.length, 2);
});

test('partial sandbox is stated', async () => {
  const { ctx, $ } = setup({ get: url => url.startsWith('/api/write/docs') ? { documents: [], templates: {},
    engines: { tectonic: { installed: true, version: '0.17.0' }, sandbox: 'scan' } } : null });
  await ctx.loadWrite();
  assert.match($('write-engine').textContent, /partial sandbox/);
});

test('auto-save waits for the idle delay and sends the read hash', async () => {
  const { ctx, $, calls, timers, files } = setup();
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value += 'x'; area.fire('input');
  area.value += 'y'; area.fire('input');
  assert.equal(calls.filter(c => c[0] === '/api/write/save').length, 0);
  assert.equal(timers.at(-1).ms, 2000);
  await timers.at(-1).fn();
  const saves = calls.filter(c => c[0] === '/api/write/save');
  assert.equal(saves.length, 1);
  assert.equal(saves[0][1].hash, 'h0');
  assert.match(files['main.tex'].text, /xy$/);
  assert.equal($('write-save-state').textContent, 'Saved');
});

test('a conflicting save shows the banner and does not overwrite until "Keep mine"', async () => {
  const { ctx, $, calls, files } = setup();
  await ctx.openWriteDoc('d1');
  files['main.tex'] = { text: 'changed elsewhere', hash: 'other' };
  const area = $('write-cm').children[0];
  area.value = 'mine'; area.fire('input');
  await ctx.saveWriteNow();
  assert.equal($('write-conflict').hidden, false);
  assert.match($('write-save-state').textContent, /Conflict/);
  assert.equal(files['main.tex'].text, 'changed elsewhere');
  await ctx.saveWriteNow();  // further auto-saves wait for the choice
  assert.equal(calls.filter(c => c[0] === '/api/write/save').length, 1);
  await ctx.keepMine();
  assert.equal(files['main.tex'].text, 'mine');
  assert.equal($('write-conflict').hidden, true);
});

test('"Reload theirs" replaces the editor text', async () => {
  const { ctx, $, files } = setup();
  await ctx.openWriteDoc('d1');
  files['main.tex'] = { text: 'theirs', hash: 'other' };
  const area = $('write-cm').children[0];
  area.value = 'mine'; area.fire('input');
  await ctx.saveWriteNow();
  await ctx.reloadTheirs();
  assert.equal($('write-cm').children[0].value, 'theirs');
  assert.equal(ctx.W.hash, 'other');
});

test('compile saves first, shows errors and a click jumps to the line', async () => {
  const { ctx, $, calls } = setup();
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'a\n\\foo\n'; area.fire('input');
  await ctx.compileWrite();
  const order = calls.filter(c => c[0] === '/api/write/save' || c[0] === '/api/write/compile').map(c => c[0]);
  assert.deepEqual(order, ['/api/write/save', '/api/write/compile']);
  const item = $('write-errors').children[0];
  assert.equal(item.textContent, 'main.tex:2 · Undefined control sequence.');
  assert.match($('write-compile-state').textContent, /1 error/);
  await item.onclick();
  assert.equal(area.selectionStart, 2);
});

test('versions list and restore re-open the file', async () => {
  const { ctx, $, calls } = setup();
  await ctx.openWriteDoc('d1');
  $('write-versions').hidden = true;  // as in index.html
  await ctx.showWriteVersions();
  const li = $('write-version-list').children[0];
  assert.match(li.children[0].textContent, /2026-10-19 10:00:00 · auto/);
  await li.children[1].onclick();
  assert.ok(calls.some(c => c[0] === '/api/write/restore' && c[1].snapshot === 's1'));
});

test('figure upload inserts includegraphics at the cursor', async () => {
  const { ctx, $ } = setup();
  await ctx.openWriteDoc('d1');
  await ctx.uploadWriteFigure({ name: 'plot.png' });
  assert.match($('write-cm').children[0].value, /\\includegraphics\[width=\\linewidth\]\{figures\/plot\.png\}/);
});

test('right-to-left: the fallback editor and list entries use automatic direction', async () => {
  const { ctx, $ } = setup();
  await ctx.loadWrite();
  assert.equal($('write-docs').children[0].children[0].dir, 'auto');
  await ctx.openWriteDoc('d1');
  assert.equal($('write-cm').children[0].dir, 'auto');
  assert.match(block, /perLineTextDirection\.of\(true\)/);
});

test('an unknown project asks the user to choose one without a request', async () => {
  const { ctx, $, calls } = setup();
  ctx.state.projects = [];
  await ctx.loadWrite();
  assert.match($('write-docs').children[0].textContent, /Choose or create a project/);
  assert.equal(calls.length, 0);
});
