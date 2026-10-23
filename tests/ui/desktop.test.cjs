const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8');
const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');
const write = app.split('/* ---------- Write ---------- */')[1].split('/* ---------- End Write ---------- */')[0];
const desktop = (app.split('/* ---------- Desktop window ---------- */')[1] || '')
  .split('/* ---------- End Desktop window ---------- */')[0];

class Element {
  constructor() { this.textContent = ''; this.hidden = false; this.onclick = null; }
  addEventListener() {}
}

// The Write view with one open, edited file; `save` decides what the server answers to a save.
function setup({ running = 0, save = 'ok', confirm = true, quit } = {}) {
  const elements = {}, calls = [], shown = [], intervals = new Map();
  const ctx = vm.createContext({
    Promise, Object, String, Math, Date, Number, URL, calls, shown,
    $: id => elements[id] ||= new Element(),
    store: { get: (k, d) => d, set() {} },
    document: { createElement: () => new Element(), querySelectorAll: () => [], addEventListener() {} },
    window: { confirm: msg => { calls.push(['confirm', msg]); return confirm; }, addEventListener() {} },
    toast: msg => calls.push(['toast', msg]),
    showStopped: () => shown.push('stopped'),
    setTimeout: () => 0, clearTimeout() {},
    setInterval: (fn, ms) => { const id = intervals.size + 1; intervals.set(id, { fn, ms }); return id; },
    clearInterval: id => intervals.delete(id),
    fetch: async (...args) => { calls.push(['fetch', ...args]); return { ok: true, json: async () => ({ hash: 'h2' }) }; },
    api: async url => {
      calls.push(['GET', url]);
      if (url === '/api/running') return { running };
      if (url.startsWith('/api/write/file')) return { name: 'main.tex', text: 'other', hash: 'h9' };
      throw new Error('unexpected ' + url);
    },
    post: async (url, body) => {
      calls.push([url, body]);
      if (url === '/api/write/save') {
        if (save === 'conflict') { const e = new Error('conflict'); e.status = 409; throw e; }
        if (save === 'fail') throw new Error('disk full');
        return { hash: 'h2' };
      }
      if (url === '/api/quit') return quit ? quit(body) : { stopped: true };
      return { ok: true };
    },
  });
  vm.runInContext(write + desktop + '\nthis.W = W; this.prepareClose = prepareClose; this.requestClose = requestClose;' +
    ' this.useDesktopWindow = useDesktopWindow; this.routeLink = routeLink; this.beforeWriteUnload = beforeWriteUnload;', ctx);
  ctx.W.doc = { id: 'd1' }; ctx.W.file = 'main.tex'; ctx.W.hash = 'h1';
  ctx.W.editor = { getText: () => 'edited text' };
  return { ctx, calls, shown, elements, intervals };
}

test('only developer browser sessions send idle heartbeats; desktop mode cancels them', async () => {
  const { ctx, intervals, calls } = setup();
  ctx.useDesktopWindow({ desktop: true });
  assert.equal(intervals.size, 0);
  ctx.useDesktopWindow({ desktop: false });
  assert.equal(intervals.size, 1);
  const beat = [...intervals.values()][0];
  assert.equal(beat.ms, 15000);
  await beat.fn();
  assert.ok(calls.some(c => c[0] === '/api/heartbeat'));
  ctx.useDesktopWindow({ desktop: true });
  assert.equal(intervals.size, 0);
});

test('native close uses its save handshake without a browser unload prompt or duplicate save', () => {
  const { ctx, calls } = setup();
  ctx.useDesktopWindow({ desktop: true });
  ctx.W.dirty = true;
  const ev = { prevented: false, preventDefault() { this.prevented = true; } };
  ctx.beforeWriteUnload(ev);
  assert.equal(ev.prevented, false);
  assert.equal(calls.filter(c => c[0] === 'fetch').length, 0);
});

test('developer browser unload still protects unsaved editor text', () => {
  const { ctx, calls } = setup();
  ctx.W.dirty = true;
  const ev = { prevented: false, preventDefault() { this.prevented = true; } };
  ctx.beforeWriteUnload(ev);
  assert.equal(ev.prevented, true);
  assert.ok(calls.some(c => c[0] === 'fetch' && c[1] === '/api/write/save'));
});

test('the page knows the desktop window: the side note and Quit change, the adapter appears', () => {
  assert.match(html, /id="quit-note"/);
  const { ctx, elements, calls } = setup();
  ctx.useDesktopWindow({ desktop: false });
  assert.equal(ctx.window.LitForgeDesktop, undefined);
  ctx.useDesktopWindow({ desktop: true });
  assert.match(elements['quit-note'].textContent, /Closing this window/);
  assert.doesNotMatch(elements['quit-note'].textContent, /two minutes/);
  assert.equal(typeof ctx.window.LitForgeDesktop.requestClose, 'function');
  ctx.W.dirty = true;
  elements['btn-quit'].onclick();  // Quit saves first, like the close button
  return new Promise(r => setImmediate(r)).then(() => assert.ok(calls.some(c => c[0] === '/api/write/save')));
});

test('closing saves the edited file first, then counts running tasks', async () => {
  const { ctx, calls } = setup({ running: 2 });
  ctx.W.dirty = true;
  const r = await ctx.prepareClose();
  assert.deepEqual({ ...r }, { ready: true, running: 2, error: null });
  const order = calls.map(c => c[0]);
  assert.ok(order.indexOf('/api/write/save') < order.indexOf('GET'));
  assert.equal(ctx.W.dirty, false);
});

test('a save conflict keeps the window open and says why', async () => {
  const { ctx, calls } = setup({ save: 'conflict' });
  ctx.W.dirty = true;
  assert.equal(await ctx.requestClose(), false);
  assert.ok(calls.some(c => c[0] === 'toast' && /changed outside LitForge/.test(c[1])));
  assert.ok(!calls.some(c => c[0] === '/api/quit'));
});

test('a failed save keeps the window open and the text in the editor', async () => {
  const { ctx, calls } = setup({ save: 'fail' });
  ctx.W.dirty = true;
  assert.equal(await ctx.requestClose(), false);
  assert.equal(ctx.W.dirty, true);
  assert.ok(calls.some(c => c[0] === 'toast' && /not saved/.test(c[1])));
  assert.ok(!calls.some(c => c[0] === '/api/quit'));
});

test('nothing to save and nothing running: LitForge stops and the page says so', async () => {
  const { ctx, calls, shown } = setup();
  assert.equal(await ctx.requestClose(), true);
  assert.equal(JSON.stringify(calls.find(c => c[0] === '/api/quit')), '["/api/quit",{"force":false}]');
  assert.deepEqual(shown, ['stopped']);
});

test('running tasks are confirmed: keep working cancels the close', async () => {
  const { ctx, calls } = setup({ running: 1, confirm: false });
  assert.equal(await ctx.requestClose(), false);
  assert.ok(calls.some(c => c[0] === 'confirm' && /1 task is still running/.test(c[1])));
  assert.ok(!calls.some(c => c[0] === '/api/quit'));
});

test('running tasks are confirmed: stop them and quit', async () => {
  const { ctx, calls } = setup({ running: 3, confirm: true });
  assert.equal(await ctx.requestClose(), true);
  assert.equal(JSON.stringify(calls.find(c => c[0] === '/api/quit')), '["/api/quit",{"force":true}]');
});

test('a task that started meanwhile keeps LitForge open', async () => {
  const { ctx, shown } = setup({ quit: () => ({ stopped: false, running: 1 }) });
  assert.equal(await ctx.requestClose(), false);
  assert.deepEqual(shown, []);
});

test('repeated close clicks are one request with one save', async () => {
  const { ctx, calls } = setup();
  ctx.W.dirty = true;
  const a = ctx.requestClose(), b = ctx.requestClose();
  assert.equal(a, b);
  await a;
  assert.equal(calls.filter(c => c[0] === '/api/write/save').length, 1);
  assert.equal(calls.filter(c => c[0] === '/api/quit').length, 1);
});

test('links in the window: PDFs to the PDF app, the web to the browser, nothing else', () => {
  const { ctx } = setup();
  const o = 'http://127.0.0.1:54635';
  const route = href => JSON.stringify(ctx.routeLink(href, o));
  assert.equal(route('/api/papers/p-1/pdf'), '{"action":"open-pdf","body":{"paper":"p-1"}}');
  assert.equal(route('/api/write/pdf?doc=d1&t=123'), '{"action":"open-pdf","body":{"doc":"d1"}}');
  assert.equal(route('https://doi.org/10.1/x'), '{"action":"external"}');
  assert.equal(route('mailto:a@b.org'), '{"action":"external"}');
  assert.equal(route(o + '/api/papers/p-1/files/fig.png'), '{"action":"app"}');
  assert.equal(route('javascript:alert(1)'), '{"action":"block"}');
  assert.equal(route('file:///etc/passwd'), '{"action":"block"}');
  assert.equal(route('http://127.0.0.1:9999/other-app'), '{"action":"external"}');
});

test('external paper links retain native anchor navigation; PDFs and downloads keep their routes', async () => {
  const { ctx, calls } = setup();
  let onclick;
  ctx.window.open = () => { throw new Error('Cocoa does not hand off script-created popups'); };
  ctx.document.addEventListener = (type, fn) => { if (type === 'click') onclick = fn; };
  ctx.location = { origin: 'http://127.0.0.1:54635' };
  ctx.useDesktopWindow({ desktop: true });
  const click = attrs => {
    const a = { getAttribute: k => attrs[k] ?? null, hasAttribute: k => k in attrs, href: attrs.href };
    const ev = { prevented: false, target: { closest: () => a }, preventDefault() { this.prevented = true; } };
    onclick(ev);
    return { prevented: ev.prevented, anchor: a };
  };
  for (const href of ['https://doi.org/10.1/x', 'https://openalex.org/W1', 'https://arxiv.org/abs/1',
    'https://example.org/open-access', 'mailto:author@example.org']) {
    const { prevented, anchor } = click({ href });
    assert.equal(prevented, false, href);
    assert.equal(anchor.target, '_blank');
    assert.match(anchor.rel, /noopener/);
    assert.match(anchor.rel, /noreferrer/);
  }
  assert.equal(click({ href: '/api/papers/p-1/pdf', target: '_blank' }).prevented, true);
  await new Promise(r => setImmediate(r));
  assert.equal(JSON.stringify(calls.find(c => c[0] === '/api/open-pdf')), '["/api/open-pdf",{"paper":"p-1"}]');
  assert.equal(click({ href: 'blob:http://127.0.0.1:54635/x', download: 'brief.md' }).prevented, false);
  assert.equal(click({ href: 'javascript:void(0)' }).prevented, true);
  assert.equal(click({ href: '#section' }).prevented, false);
});
