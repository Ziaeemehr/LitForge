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
    this.classList = { toggle() {}, add() {}, remove() {} };
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

test('insertCiteKey: new cite, add to an existing list, unclosed list, duplicates', () => {
  const { ctx } = setup();
  assert.deepEqual({ ...ctx.insertCiteKey('As shown ', 9, 'k1') }, { from: 9, to: 9, insert: '\\cite{k1}' });
  const text = 'See \\citep[p.~3]{a, b} here';
  assert.deepEqual({ ...ctx.insertCiteKey(text, text.indexOf('b'), 'k1') }, { from: 17, to: 21, insert: 'a,b,k1' });
  assert.equal(ctx.insertCiteKey(text, text.indexOf('b'), 'a'), null);
  assert.deepEqual({ ...ctx.insertCiteKey('x \\cite{', 8, 'k1') }, { from: 8, to: 8, insert: 'k1}' });
  assert.deepEqual({ ...ctx.insertCiteKey('{not a cite} ', 5, 'k') }, { from: 5, to: 5, insert: '\\cite{k}' });
  const persian = 'این نتیجه \\cite{a} است';
  assert.deepEqual({ ...ctx.insertCiteKey(persian, persian.indexOf('a}'), 'b') },
    { from: persian.indexOf('a}'), to: persian.indexOf('a}') + 1, insert: 'a,b' });
});

test('cite palette searches, inserts the key and offers the quote for keys under the cursor', async () => {
  const { ctx, $, calls } = setup({
    get: url => {
      if (url.startsWith('/api/write/cite/search')) return { results: [{ paper_id: 'p1', title: '<b>Brains</b>',
        year: 2021, authors: ['Ann Lee'], version: 'preprint', evidence_id: url.includes('evidence') ? 'ev_1' : null,
        quote: url.includes('evidence') ? 'gain was 3.25' : null, page: url.includes('evidence') ? 4 : null }] };
      if (url.startsWith('/api/write/quote')) return { key: 'lee2021brains', paper_id: 'p1', title: 'Brains', year: 2021,
        quotes: [{ text: 'gain was 3.25', page: 4, version: 'Preprint (arXiv)' }] };
    },
    post: url => url === '/api/write/cite' ? { key: 'lee2021brains' } : null
  });
  await ctx.openWriteDoc('d1');
  $('write-cite-panel').hidden = true;
  const area = $('write-cm').children[0];
  area.value = 'Text '; area.setSelectionRange(5, 5);
  await ctx.openWriteCite();
  const row = $('write-cite-results').children[0];
  assert.equal(row.children[1].textContent, '<b>Brains</b> (2021) · Ann Lee');
  assert.equal(row.children[2].textContent, 'preprint');
  await ctx.setWriteCiteTab('evidence');
  $('write-cite-q').value = 'gain';
  await ctx.searchWriteCite();
  await $('write-cite-results').children[0].children[0].onclick();
  assert.deepEqual({ ...calls.find(c => c[0] === '/api/write/cite')[1] }, { doc: 'd1', paper_id: 'p1', evidence_id: 'ev_1' });
  assert.equal(area.value, 'Text \\cite{lee2021brains}');
  area.setSelectionRange(14, 14);
  $('write-cite-panel').hidden = true;
  await ctx.openWriteCite();
  const quoteButton = $('write-cite-results').children[0].children[0];
  assert.equal(quoteButton.textContent, 'Show quote: lee2021brains');
  await quoteButton.onclick();
  const quote = $('write-cite-results').children[1];
  assert.equal(quote.children[0].href, '/api/papers/p1/pdf#page=4');
  assert.equal(quote.children[1].textContent, 'gain was 3.25');
});

function aiSetup(report, approval) {
  let approvals = 0;
  const env = setup({
    get: url => url.startsWith('/api/jobs/job-ai') ? { status: 'done', report } : null,
    post: (url, body) => {
      if (url === '/api/write/assist') {
        if (approval && !body.approved) return { needs_approval: true, sent: { characters: body.text.length, evidence: 0 },
          preview: { model: 'big-cloud', provider: 'openrouter', remote: true, max_cost_usd: 0.0012 } };
        return { id: 'job-ai', preview: { model: 'gemma', profile: 'local', remote: false }, sent: { characters: 3, evidence: 0 } };
      }
      if (url === '/api/write/assist/decide') return { status: body.accepted.length ? 'partial' : 'rejected' };
      if (url === '/api/write/cite') return { key: 'k' };
      return null;
    }
  });
  env.ctx.window.confirm = msg => { approvals++; env.calls.push(['confirm', msg]); return approval !== 'deny'; };
  env.approvals = () => approvals;
  return env;
}

const REPORT = { id: 's1', task: 'write_rewrite', provider: 'ollama', model: 'gemma', note: 'shorter', flags: [],
  original: 'one two three', citations: [],
  hunks: [{ index: 0, start: 0, end: 3, old: 'one', new: '1', flags: [] },
          { index: 1, start: 8, end: 13, old: 'three', new: '3', flags: ['number not in your text or the evidence: 3'] }] };

test('AI rewrite shows hunks; flagged hunks start rejected; only accepted hunks are applied and recorded', async () => {
  const { ctx, $, calls } = aiSetup(REPORT);
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Intro: one two three.'; area.setSelectionRange(7, 20);
  await ctx.runWriteAi('shorter', false);
  const sent = calls.find(c => c[0] === '/api/write/assist')[1];
  assert.equal(sent.text, 'one two three'); assert.equal(sent.approved, false);
  assert.equal($('write-suggestion').hidden, false);
  const hunks = $('write-suggestion-hunks').children;
  assert.equal(hunks[0].children[0].textContent, 'one');
  assert.equal(hunks[1].children[2].textContent, '⚠ number not in your text or the evidence: 3');
  assert.match(hunks[1].children[3].textContent, /^Rejected/);
  assert.equal(area.value, 'Intro: one two three.');  // nothing changes before applying
  await ctx.applyWriteSuggestion();
  assert.equal(area.value, 'Intro: 1 two three.');
  assert.deepEqual([...calls.find(c => c[0] === '/api/write/assist/decide')[1].accepted], [0]);
});

test('AI suggestion is refused when the text changed meanwhile', async () => {
  const { ctx, $, calls } = aiSetup(REPORT);
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Intro: one two three.'; area.setSelectionRange(7, 20);
  await ctx.runWriteAi('shorter', false);
  area.value = 'Intro: ONE two three.';
  await ctx.applyWriteSuggestion();
  assert.equal(area.value, 'Intro: ONE two three.');
  assert.ok(calls.some(c => c[0] === 'toast' && /changed since/.test(c[1])));
  assert.ok(!calls.some(c => c[0] === '/api/write/assist/decide'));
});

test('cloud route asks for approval with what is sent; declining sends nothing', async () => {
  const denied = aiSetup(REPORT, 'deny');
  await denied.ctx.openWriteDoc('d1');
  let area = denied.$('write-cm').children[0];
  area.value = 'one two three'; area.setSelectionRange(0, 13);
  await denied.ctx.runWriteAi('clearer', false);
  assert.match(denied.calls.find(c => c[0] === 'confirm')[1], /13 characters.*big-cloud \(openrouter, cloud, at most \$0\.0012\)/);
  assert.equal(denied.calls.filter(c => c[0] === '/api/write/assist').length, 1);
  assert.ok(!denied.calls.some(c => c[0] === 'GET' && c[1].startsWith('/api/jobs/')));
  assert.equal(denied.$('write-compile-state').textContent, 'Not sent.');
  const ok = aiSetup(REPORT, true);
  await ok.ctx.openWriteDoc('d1');
  area = ok.$('write-cm').children[0];
  area.value = 'one two three'; area.setSelectionRange(0, 13);
  await ok.ctx.runWriteAi('clearer', false);
  const posts = ok.calls.filter(c => c[0] === '/api/write/assist');
  assert.deepEqual(posts.map(p => p[1].approved), [false, true]);
});

test('fix needs a clicked error and sends the lines around it', async () => {
  const { ctx, $, calls } = aiSetup({ ...REPORT, task: 'write_fix', hunks: [], original: '' });
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = Array.from({ length: 40 }, (_, i) => 'line' + (i + 1)).join('\n');
  await ctx.runWriteAi('fix', false);
  assert.ok(calls.some(c => c[0] === 'toast' && /Click a compile error first/.test(c[1])));
  ctx.W.lastError = { line: 20, file: 'main.tex', message: 'Undefined control sequence.' };
  await ctx.runWriteAi('fix', false);
  const assist = calls.find(c => c[0] === '/api/write/assist');
  assert.ok(assist, JSON.stringify(calls.slice(-3)));
  const sent = assist[1];
  assert.equal(sent.first_line, 5);
  assert.equal(sent.text.split('\n')[0], 'line5');
  assert.equal(sent.text.split('\n').at(-1), 'line35');
  assert.equal(sent.error, 'Undefined control sequence.');
});

test('accepted citations are registered for references.bib', async () => {
  const report = { ...REPORT, task: 'write_cite', original: 'Gain was high.',
    hunks: [{ index: 0, start: 13, end: 13, old: '', new: ' \\cite{lee2021}', flags: [] }],
    citations: [{ key: 'lee2021', paper_id: 'p1', evidence_id: 'ev_1', page: 2, quote: 'gain was 3.25' }] };
  const { ctx, $, calls } = aiSetup(report);
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Gain was high.'; area.setSelectionRange(0, 14);
  await ctx.runWriteAi('cite', false);
  await ctx.applyWriteSuggestion();
  assert.equal(area.value, 'Gain was high \\cite{lee2021}.');
  assert.deepEqual({ ...calls.find(c => c[0] === '/api/write/cite')[1] }, { doc: 'd1', paper_id: 'p1', evidence_id: 'ev_1' });
});

test('applying hunks counts code points like Python (emoji before the change)', () => {
  const { ctx } = setup();
  const original = 'نتیجه 😀 خوب است';
  const hunks = [{ index: 0, start: 8, end: 11, old: 'خوب', new: 'عالی' }];
  assert.equal(ctx.appliedSuggestion(original, hunks, { 0: true }), 'نتیجه 😀 عالی است');
  assert.equal(ctx.appliedSuggestion(original, hunks, {}), original);
});
