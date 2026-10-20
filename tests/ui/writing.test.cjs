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
    this.attrs = {}; this.dataset = {}; this.style = { props: {}, setProperty(k, v) { this.props[k] = v; } };
  }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  appendChild(node) { this.children.push(node); return node; }
  cloneNode() { const c = new Element(this.tagName); c.textContent = this.textContent; c.children = this.children.slice(); return c; }
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
  const saved = {}, segButtons = ['editor', 'split', 'pdf'].map(l => { const b = new Element('button'); b.dataset.layout = l; return b; });
  const ctx = vm.createContext({
    $, calls, files, timers, saved, segButtons, Promise, String, Object, Math, Date, Number,
    store: { get: (k, d) => (k in saved ? saved[k] : d), set: (k, v) => { saved[k] = v; } },
    document: { createElement: tag => new Element(tag), querySelectorAll: sel => (sel === '.seg [data-layout]' ? segButtons : []) },
    window: { confirm: () => true, prompt: () => 'Renamed', addEventListener() {},
      LitForgeWords: require('../../src/litforge/ui/static/latex_words.js'),
      LitForgeSymbols: require('../../src/litforge/ui/static/latex_symbols.js') },
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
  vm.runInContext(block + '\nthis.W = W; this.AI_pending = function () { return AI.pending; };', ctx);
  return { ctx, $, calls, timers, files, saved, segButtons };
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
  assert.equal(sent.first_line, 15);
  assert.equal(sent.text.split('\n')[0], 'line15');
  assert.equal(sent.text.split('\n').at(-1), 'line25');
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

test('layout switch shows one pane or both, marks the pressed button and is remembered', () => {
  const { ctx, $, saved, segButtons } = setup();
  assert.equal(ctx.setWriteLayout('pdf'), 'pdf');
  assert.equal($('write-split').dataset.layout, 'pdf');
  assert.deepEqual(segButtons.map(b => b.attrs['aria-pressed']), ['false', 'false', 'true']);
  assert.equal(saved['write-layout'], 'pdf');
  assert.equal(ctx.setWriteLayout('bogus'), 'split');  // an unknown stored value falls back to split
  assert.match(html, /data-layout="editor"/); assert.match(html, /data-layout="pdf"/);
});

test('split position is clamped, exposed to assistive tech and remembered', () => {
  const { ctx, $, saved } = setup();
  assert.equal(ctx.setWriteSplit(63.4), 63);
  assert.equal($('write-split').style.props['--write-left'], '63%');
  assert.equal($('write-gutter').attrs['aria-valuenow'], '63');
  assert.equal(ctx.setWriteSplit(5), 20);
  assert.equal(ctx.setWriteSplit(99), 80);
  assert.equal(ctx.setWriteSplit('x'), 50);
  assert.equal(saved['write-split'], 50);
  assert.match(html, /id="write-gutter"[^>]*role="separator"/);
});

test('closing a suggestion clears the highlight in the editor', async () => {
  const { ctx, $ } = aiSetup(REPORT);
  await ctx.openWriteDoc('d1');
  const marks = [];
  ctx.W.editor.mark = r => marks.push(r);
  const area = $('write-cm').children[0];
  area.value = 'Intro: one two three.'; area.setSelectionRange(7, 20);
  await ctx.runWriteAi('shorter', false);
  assert.equal(marks[0].from, 7); assert.equal(marks[0].to, 20);
  await ctx.applyWriteSuggestion();
  assert.equal(marks[marks.length - 1], null);
  assert.equal($('write-suggestion').hidden, true);
});

// Review round 3: unsaved text must survive file switches, closing and restoring; suggestions stay with their file.
test('switching files is refused while a conflict holds unsaved text', async () => {
  const { ctx, $, calls, files } = setup();
  await ctx.openWriteDoc('d1');
  files['main.tex'] = { text: 'changed elsewhere', hash: 'other' };
  const area = $('write-cm').children[0];
  area.value = 'mine'; area.fire('input');
  assert.equal(await ctx.openWriteFile('refs.tex'), false);
  assert.equal($('write-cm').children[0].value, 'mine');
  assert.ok(!calls.some(c => c[0] === 'GET' && /name=refs\.tex/.test(c[1])));
  assert.ok(calls.some(c => c[0] === 'toast' && /conflict/i.test(c[1])));
});

test('closing keeps the document open when the save fails', async () => {
  const fail = { on: false };
  const { ctx, $ } = setup({ post: url => (fail.on && url === '/api/write/save' ? Promise.reject(new Error('disk full')) : null) });
  await ctx.openWriteDoc('d1');
  fail.on = true;
  const area = $('write-cm').children[0];
  area.value = 'unsaved words'; area.fire('input');
  await ctx.closeWriteDoc(false);
  assert.ok(ctx.W.doc, 'the document is still open');
  assert.equal(ctx.W.editor.getText(), 'unsaved words');
  assert.equal($('write-editor').hidden, false);
});

test('restore saves the editor first, so the kept version has the latest text', async () => {
  const { ctx, $, calls } = setup();
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'typed after opening versions'; area.fire('input');
  await ctx.restoreWriteVersion('s1');
  const save = calls.findIndex(c => c[0] === '/api/write/save' && c[1].text === 'typed after opening versions');
  const restore = calls.findIndex(c => c[0] === '/api/write/restore');
  assert.ok(save >= 0 && restore > save, 'save before restore');
});

test('a suggestion is dropped (and recorded as rejected) when the file changes; it never applies elsewhere', async () => {
  const { ctx, $, calls } = aiSetup(REPORT);
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Intro: one two three.'; area.setSelectionRange(7, 20);
  await ctx.runWriteAi('shorter', false);
  assert.equal($('write-suggestion').hidden, false);
  await ctx.openWriteFile('main.tex');  // re-open (the same happens for another file or document)
  assert.equal($('write-suggestion').hidden, true);
  assert.deepEqual([...calls.find(c => c[0] === '/api/write/assist/decide')[1].accepted], []);
  $('write-cm').children[0].value = 'Intro: one two three.';
  await ctx.applyWriteSuggestion();
  assert.equal($('write-cm').children[0].value, 'Intro: one two three.');
});

test('a reply for a file the user left is not shown', async () => {
  const { ctx, $, calls } = aiSetup(REPORT);
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Intro: one two three.'; area.setSelectionRange(7, 20);
  $('write-suggestion').hidden = true;  // as in index.html
  const range = { from: 7, to: 20, text: 'one two three', doc: 'd1', file: 'other.tex' };
  await ctx.pollWriteAi({ id: 'job-ai' }, range);
  assert.equal($('write-suggestion').hidden, true);  // never shown
  assert.equal(ctx.AI_pending(), null);
  assert.ok(calls.some(c => c[0] === '/api/write/assist/decide'));
});

test('the floating bar stays inside the editor pane', () => {
  const { ctx, $ } = setup();
  const box = $('write-suggestion');
  box.parentNode = { getBoundingClientRect: () => ({ top: 100, height: 400 }) };
  box.offsetHeight = 100;
  ctx.W.editor = { coords: () => ({ top: 20, bottom: 40 }), mark() {} };  // text scrolled above the pane
  ctx.placeWriteSuggestion({ to: 0 });
  assert.equal(box.style.top, '6px');
  ctx.W.editor.coords = () => ({ top: 640, bottom: 660 });  // below the pane
  ctx.placeWriteSuggestion({ to: 0 });
  assert.equal(box.style.top, '294px');
});

test('a suggestion without changes says so', async () => {
  const { ctx, $ } = aiSetup({ ...REPORT, hunks: [], flags: [] });
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Intro: one two three.'; area.setSelectionRange(7, 20);
  await ctx.runWriteAi('shorter', false);
  assert.ok($('write-suggestion-hunks').children.some(c => c.textContent === 'The model returned the text unchanged.'));
});

// ---------- Autocomplete (\ commands, \ref{ labels, \begin{ environments, \cite{ keys) ----------
test('latexContext finds what is being completed before the cursor', () => {
  const { ctx } = setup();
  const c = s => { const r = ctx.latexContext(s); return r && { kind: r.kind, prefix: r.prefix, start: r.start }; };
  assert.deepEqual(c('text \\sec'), { kind: 'command', prefix: 'sec', start: 5 });
  assert.deepEqual(c('text \\'), { kind: 'command', prefix: '', start: 5 });
  assert.equal(c('a line break \\\\'), null);  // \\ is a line break, not a command
  assert.deepEqual(c('see \\ref{sec:mo'), { kind: 'ref', prefix: 'sec:mo', start: 9 });
  assert.deepEqual(c('\\eqref{'), { kind: 'ref', prefix: '', start: 7 });
  assert.deepEqual(c('\\begin{fi'), { kind: 'env', prefix: 'fi', start: 7 });
  assert.deepEqual(c('\\cite{a, de'), { kind: 'cite', prefix: 'de', start: 9 });
  assert.deepEqual(c('\\citep[p.~2]{'), { kind: 'cite', prefix: '', start: 13 });
  assert.equal(c('plain words'), null);
  assert.equal(c('\\cite{done} more'), null);
});

test('labels and own commands are read from the document text', () => {
  const { ctx } = setup();
  assert.deepEqual([...ctx.docLabels(['\\label{sec:a} x \\label{fig:b}', '\\label{sec:a}\\label{eq:c}'])],
    ['sec:a', 'fig:b', 'eq:c']);
  assert.deepEqual([...ctx.docCommands('\\newcommand{\\tvb}{TVB} \\newcommand\\R{\\mathbb{R}} \\renewcommand*{\\vec}[1]{x}')],
    ['tvb', 'R', 'vec']);
});

test('command options: common LaTeX and xepersian commands plus the document\'s own, filtered by prefix', () => {
  const { ctx } = setup();
  const labels = ctx.commandOptions('sec', []).map(o => o.label);
  assert.ok(labels.includes('\\section') && labels.includes('\\subsection') === false);
  assert.ok(ctx.commandOptions('l', []).some(o => o.label === '\\lr'));
  assert.ok(ctx.commandOptions('tv', ['tvb']).some(o => o.label === '\\tvb'));
  assert.equal(ctx.commandOptions('section', [])[0].apply, '\\section{}');
});

test('choosing an environment adds the matching \\end and puts the cursor inside', () => {
  const { ctx } = setup();
  assert.ok(ctx.envOptions('fi').some(o => o.label === 'figure'));
  const full = { text: 'itemize}\n  \n\\end{itemize}', cursor: 11 };
  assert.deepEqual({ ...ctx.envReplacement('itemize', true, '') }, { ...full, consume: 0 });
  // the editor auto-closes { with }: that brace is replaced, not taken as "already closed" (real run)
  assert.deepEqual({ ...ctx.envReplacement('itemize', true, '}') }, { ...full, consume: 1 });
  assert.deepEqual({ ...ctx.envReplacement('itemize', true, '}[t]') }, { text: 'itemize', cursor: 7, consume: 0 });
  // after \\end{ the name is completed and the cursor steps past the closing brace
  assert.deepEqual({ ...ctx.envReplacement('itemize', false, '}') }, { text: 'itemize}', cursor: 8, consume: 1 });
  assert.deepEqual({ ...ctx.envReplacement('itemize', false, '') }, { text: 'itemize', cursor: 7, consume: 0 });
});

test('cite options: keys in this document first, then library papers, then the full search', () => {
  const { ctx } = setup();
  const rows = [{ paper_id: 'p1', key: 'deco2012ongoing', title: 'Ongoing', year: 2012, authors: ['Deco, G.'] },
                { paper_id: 'p2', key: null, title: 'Identification of optimal', year: 2014, authors: ['Deco, G.'] }];
  const opts = ctx.citeOptions('de', ['deco2012ongoing', 'sanz2013'], rows);
  assert.deepEqual([...opts.map(o => o.source)], ['document', 'library', 'search']);
  assert.equal(opts[0].label, 'deco2012ongoing');
  assert.match(opts[1].detail, /Deco.*2014/);
  assert.equal(opts.at(-1).label, 'Search the library and evidence…');
});

test('typing \\cite{ no longer opens the large panel; the search row does', async () => {
  const { ctx, $ } = setup();
  await ctx.openWriteDoc('d1');
  $('write-cite-panel').hidden = true;
  const area = $('write-cm').children[0];
  area.value = 'Text \\cite{'; area.setSelectionRange(11, 11); area.fire('input');
  assert.equal($('write-cite-panel').hidden, true);
  await ctx.applyCiteCompletion({ source: 'search' }, 11, 11);
  assert.equal($('write-cite-panel').hidden, false);
});

test('a library paper chosen from the list is registered for references.bib and its key inserted', async () => {
  const { ctx, $, calls } = setup({ post: url => (url === '/api/write/cite' ? { key: 'deco2014identification' } : null) });
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Text \\cite{de}'; area.setSelectionRange(13, 13);
  await ctx.applyCiteCompletion({ source: 'library', paper_id: 'p2' }, 11, 13);
  assert.equal(area.value, 'Text \\cite{deco2014identification}');
  assert.equal(calls.find(c => c[0] === '/api/write/cite')[1].paper_id, 'p2');
});

test('labels from the document\'s other .tex files are available to \\ref{', async () => {
  const { ctx } = setup({ get: url => {
    if (url.startsWith('/api/write/doc?')) return { id: 'd1', name: 'Draft', pdf: false,
      files: [{ name: 'main.tex', kind: 'text' }, { name: 'methods.tex', kind: 'text' }, { name: 'references.bib', kind: 'text' }] };
    if (url.includes('name=methods.tex')) return { name: 'methods.tex', text: '\\section{M}\\label{sec:methods}', hash: 'm' };
    return null;
  } });
  await ctx.openWriteDoc('d1');
  await ctx.W.otherTextsLoading;
  assert.deepEqual([...ctx.docLabels([ctx.W.editor.getText()].concat(ctx.W.otherTexts))], ['sec:methods']);
});

test('the key being typed is not offered as a key "in this document"', () => {
  const { ctx } = setup();
  const text = '\\cite{a} x \\cite{de}';
  assert.deepEqual([...ctx.docCiteKeys(text, 18, 20)], ['a']);
});

test('library papers whose key or first author starts with the typed text come first', () => {
  const { ctx } = setup();
  const rows = [{ paper_id: 'p1', key: 'jirsa2017', title: 'Brain models', year: 2017, authors: ['Jirsa, V.'] },
                { paper_id: 'p2', key: 'sanz2013', title: 'The Virtual Brain', year: 2013, authors: ['Sanz Leon, P.'] },
                { paper_id: 'p3', key: null, title: 'Ongoing activity', year: 2012, authors: ['Deco, G.'] },
                { paper_id: 'p4', key: 'deco2014', title: 'Identification', year: 2014, authors: ['Deco, G.'] }];
  assert.deepEqual([...ctx.citeOptions('de', [], rows).map(o => o.paper_id || o.source)], ['p4', 'p3', 'p1', 'p2', 'search']);
});

test('a picked label steps past the closing brace', () => {
  const { ctx } = setup();
  assert.deepEqual({ ...ctx.closedReplacement('sec:intro', '}') }, { text: 'sec:intro}', cursor: 10, consume: 1 });
  assert.deepEqual({ ...ctx.closedReplacement('sec:intro', '') }, { text: 'sec:intro', cursor: 9, consume: 0 });
  assert.deepEqual({ ...ctx.closedReplacement('sec:intro', ', x}') }, { text: 'sec:intro', cursor: 9, consume: 0 });
  // mid-sentence: \\ref{▌} and more
  assert.deepEqual({ ...ctx.closedReplacement('sec:intro', '} and more') }, { text: 'sec:intro}', cursor: 10, consume: 1 });
});

test('a library key arriving after the text changed is added at the cursor, not at the old place', async () => {
  let release;
  const { ctx, $ } = setup({ post: url => (url === '/api/write/cite' ? new Promise(r => { release = r; }) : null) });
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Text \\cite{de}'; area.setSelectionRange(13, 13);
  const done = ctx.applyCiteCompletion({ source: 'library', paper_id: 'p2' }, 11, 13);
  area.value = 'New start. Text \\cite{de}'; area.setSelectionRange(24, 24);  // typed while waiting
  release({ key: 'deco2014' });
  await done;
  assert.equal(area.value, 'New start. Text \\cite{deco2014}');
});

test('a library key arriving after leaving the file is not inserted anywhere', async () => {
  let release;
  const { ctx, $, calls } = setup({ post: url => (url === '/api/write/cite' ? new Promise(r => { release = r; }) : null) });
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Text \\cite{de}'; area.setSelectionRange(13, 13);
  const done = ctx.applyCiteCompletion({ source: 'library', paper_id: 'p2' }, 11, 13);
  await ctx.openWriteFile('main.tex', true);  // leaving the file always replaces the editor
  release({ key: 'deco2014' });
  await done;
  assert.ok(!/deco2014/.test(ctx.W.editor.getText()));
  assert.ok(calls.some(c => c[0] === 'toast' && /deco2014/.test(c[1])));
});

test('picking \\cite, \\ref or \\begin from the command list opens the next list at once', () => {
  const { ctx } = setup();
  for (const c of ['\\cite{}', '\\citep{}', '\\ref{}', '\\eqref{}', '\\begin{}', '\\end{}']) assert.equal(ctx.chainsCompletion(c), true, c);
  for (const c of ['\\textbf{}', '\\section{}', '\\maketitle']) assert.equal(ctx.chainsCompletion(c), false, c);
});

function fakeContext(text, pos) {
  const start = text.lastIndexOf('\n', pos - 1) + 1, end = text.indexOf('\n', pos);
  const line = { from: start, to: end < 0 ? text.length : end, text: text.slice(start, end < 0 ? text.length : end) };
  return { pos, state: { doc: { lineAt: () => line, toString: () => text }, sliceDoc: (a, b) => text.slice(a, b) } };
}

test('when the library search fails, \\cite{ still lists the document keys and the full search', async () => {
  const { ctx } = setup({ get: url => (url.startsWith('/api/write/cite/search') ? Promise.reject(new Error('offline')) : null) });
  await ctx.openWriteDoc('d1');
  const text = 'A \\cite{lee2021} and \\cite{l';
  const result = await ctx.latexCompletionSource(fakeContext(text, text.length));
  assert.deepEqual([...result.options.map(o => o.label)], ['lee2021', 'Search the library and evidence…']);
  assert.equal(result.from, text.length - 1);
});

// Review (Codex) of the completion: late replies, broader prefixes after Backspace, label cache races.
test('a late key for an empty \\cite{} goes where the cursor is, never at the old offset', async () => {
  let release;
  const { ctx, $ } = setup({ post: url => (url === '/api/write/cite' ? new Promise(r => { release = r; }) : null) });
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = '\\cite{} tail'; area.setSelectionRange(6, 6);
  const done = ctx.applyCiteCompletion({ source: 'library', paper_id: 'p' }, 6, 6);
  area.value = 'PREFIX \\cite{} tail'; area.setSelectionRange(13, 13);
  release({ key: 'smith2020' });
  await done;
  assert.equal(area.value, 'PREFIX \\cite{smith2020} tail');
});

test('a late key is not inserted when the cursor has left the citation', async () => {
  let release;
  const { ctx, $, calls } = setup({ post: url => (url === '/api/write/cite' ? new Promise(r => { release = r; }) : null) });
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = '\\cite{} tail'; area.setSelectionRange(6, 6);
  const done = ctx.applyCiteCompletion({ source: 'library', paper_id: 'p' }, 6, 6);
  area.value = 'PREFIX \\cite{} tail'; area.setSelectionRange(17, 17);  // now in "tail"
  release({ key: 'smith2020' });
  await done;
  assert.equal(area.value, 'PREFIX \\cite{} tail');
  assert.ok(calls.some(c => c[0] === 'toast' && /smith2020/.test(c[1])));
});

test('a late key is dropped when the file was closed and opened again', async () => {
  let release;
  const { ctx, $, calls } = setup({ post: url => (url === '/api/write/cite' ? new Promise(r => { release = r; }) : null) });
  await ctx.openWriteDoc('d1');
  $('write-cm').children[0].value = '\\cite{}';
  const done = ctx.applyCiteCompletion({ source: 'library', paper_id: 'p' }, 6, 6);
  await ctx.openWriteFile('main.tex', true);  // same file name, new editor
  release({ key: 'smith2020' });
  await done;
  assert.ok(!/smith2020/.test(ctx.W.editor.getText()));
  assert.ok(calls.some(c => c[0] === 'toast' && /smith2020/.test(c[1])));
});

test('the command and environment lists are complete, so Backspace can widen them', () => {
  const { ctx } = setup();
  const labels = r => [...r.options.map(o => o.label)];
  const text = '\\sec';
  assert.ok(labels(ctx.latexCompletionSource(fakeContext(text, text.length))).includes('\\settextfont'));
  const env = '\\begin{fig';
  assert.ok(labels(ctx.latexCompletionSource(fakeContext(env, env.length))).includes('table'));
});

test('labels of other files: only the newest load is kept', async () => {
  const pending = [];
  const { ctx } = setup({ get: url => {
    if (url.startsWith('/api/write/doc?')) return { id: 'd1', name: 'D', pdf: false,
      files: [{ name: 'main.tex', kind: 'text' }, { name: 'a.tex', kind: 'text' }] };
    if (url.includes('name=a.tex') && pending.length < 5) return new Promise(r => pending.push(r));
    return null;
  } });
  await ctx.openWriteDoc('d1');           // load 1 (pending[0]) for main.tex
  ctx.W.doc.files.push({ name: 'b.tex', kind: 'text' });
  const second = ctx.loadOtherTexts();    // load 2 (pending[1])
  pending[1]({ text: '\\label{new}' });
  await second;
  pending[0]({ text: '\\label{old}' });
  await new Promise(r => setImmediate(r));
  assert.ok(ctx.W.otherTexts.join(' ').includes('new') && !ctx.W.otherTexts.join(' ').includes('old'));
});

// ---------- Text ↔ PDF (SyncTeX + PDF.js viewer) ----------
test('a click on a page becomes a PDF position in points', () => {
  const { ctx } = setup();
  const p = ctx.pdfPoint({ left: 100, top: 50 }, 100 + 150, 50 + 300, 1.5);
  assert.deepEqual({ ...p }, { x: 100, y: 200 });
});

test('cursor line counts newlines before the cursor', () => {
  const { ctx } = setup();
  assert.equal(ctx.cursorLine('a\nb\nc', 0), 1);
  assert.equal(ctx.cursorLine('a\nb\nc', 2), 2);
  assert.equal(ctx.cursorLine('a\nb\nc', 5), 3);
});

test('find in PDF asks for the cursor line and highlights the boxes on that page', async () => {
  const { ctx, $, calls } = setup({ get: url => (url.startsWith('/api/write/synctex?') ?
    { page: 2, line: 3, boxes: [{ x: 10, y: 20, w: 100, h: 12 }] } : null) });
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'one\ntwo\nthree'; area.setSelectionRange(9, 9);
  const page2 = new (area.constructor)('div');
  ctx.PDFV.scale = 2; ctx.PDFV.pages = [{ wrap: new (area.constructor)('div') }, { wrap: page2 }];
  page2.scrollIntoView = () => calls.push(['scrolled']);
  await ctx.syncToPdf();
  assert.ok(calls.some(c => c[0] === 'GET' && /synctex\?doc=d1&file=main\.tex&line=3$/.test(c[1])));
  const mark = page2.children.find(c => c.className === 'pdf-mark');
  assert.ok(mark, 'highlight added');
  assert.deepEqual([mark.style.left, mark.style.top, mark.style.width, mark.style.height], ['20px', '40px', '200px', '24px']);
});

test('find in PDF without a compiled PDF says so', async () => {
  const { ctx, calls } = setup();
  await ctx.openWriteDoc('d1');
  ctx.PDFV.pages = [];
  await ctx.syncToPdf();
  assert.ok(calls.some(c => c[0] === 'toast' && /Compile first/.test(c[1])));
});

test('a double-click in the PDF opens the source line, switching files when needed', async () => {
  const opened = [];
  const { ctx, $ } = setup({ get: url => {
    if (url.startsWith('/api/write/synctex?')) return { file: 'methods.tex', line: 7 };
    if (url.includes('name=methods.tex')) { opened.push('methods.tex'); return { name: 'methods.tex', text: 'a\nb\nc\nd\ne\nf\ng\nh', hash: 'm' }; }
    return null;
  } });
  await ctx.openWriteDoc('d1');
  await ctx.syncFromPdf(1, 145.2, 153.9);
  assert.deepEqual(opened, ['methods.tex']);
  const area = $('write-cm').children[0];
  assert.equal(area.selectionStart, 12);  // start of line 7
});

test('without PDF.js the browser\'s own viewer is used', async () => {
  const { ctx, $ } = setup();
  await ctx.openWriteDoc('d1');
  ctx.W.doc.pdf = true;
  ctx.showWritePdf(true);
  assert.match($('write-pdf').src, /\/api\/write\/pdf\?doc=d1/);
  assert.equal($('write-pdf').hidden, false);
});

test('in fit mode the PDF is re-fitted when its pane changes width, not for small changes', () => {
  const { ctx } = setup();
  assert.equal(ctx.needsRefit(400, 640, true), true);
  assert.equal(ctx.needsRefit(400, 410, true), false);
  assert.equal(ctx.needsRefit(400, 640, false), false);  // the user chose a zoom: keep it
  assert.equal(ctx.needsRefit(0, 640, true), false);     // hidden pane (Editor only) has no width
});

// Review (Codex) of the PDF viewer: zoom must not cancel a load, stale documents are released, late lookups.
function pendingPdfLib() {
  const loads = [];
  return { loads, lib: { getDocument: () => { let resolve; const promise = new Promise(r => { resolve = r; }); loads.push(resolve); return { promise }; } } };
}
function fakePdf(name, destroyed) {
  return { name, numPages: 1, destroy: () => destroyed.push(name), getPage: () => new Promise(() => {}) };
}

test('zooming while a new PDF loads does not cancel that load', () => {
  const { ctx } = setup();
  const before = ctx.PDFV.loadId;
  assert.equal(typeof before, 'number');
  ctx.PDFV.doc = fakePdf('old', []);
  ctx.zoomPdf(1.2);
  assert.equal(ctx.PDFV.loadId, before);
});

test('a PDF that loses the race to a newer one is released', async () => {
  const { ctx, $ } = setup();
  const { loads, lib } = pendingPdfLib();
  ctx.window.pdfjsLib = lib;
  $('write-pdf-pages').scrollHeight = 0;
  const destroyed = [];
  ctx.loadPdf('/a'); ctx.loadPdf('/b');
  loads[1](fakePdf('new', destroyed));
  await new Promise(r => setImmediate(r));
  loads[0](fakePdf('stale', destroyed));
  await new Promise(r => setImmediate(r));
  assert.deepEqual(destroyed, ['stale']);
  assert.equal(ctx.PDFV.doc.name, 'new');
});

test('a late PDF → editor lookup is ignored after switching documents', async () => {
  let release;
  const { ctx, $, calls } = setup({ get: url => (url.startsWith('/api/write/synctex?') ? new Promise(r => { release = r; }) : null) });
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'a\nb\nc'; area.setSelectionRange(0, 0);
  const done = ctx.syncFromPdf(1, 10, 10);
  ctx.W.doc = { id: 'd2', files: [] };
  release({ file: 'main.tex', line: 3 });
  await done;
  assert.equal(area.selectionStart, 0);
  assert.ok(!calls.some(c => c[0] === 'GET' && /doc=d2/.test(c[1])));
});

// ---------- Spell check ----------
test('spell query: known-bad words become underlines, unknown words are asked once', () => {
  const { ctx } = setup();
  const cache = new Map([['en:teh', false], ['en:brain', true]]);
  const text = 'The teh brain \\cite{teh} and teh مدل';
  const q = ctx.spellQuery(text, [[0, text.length]], cache);
  assert.deepEqual([...q.spans.map(s => [s.from, s.to, s.word])], [[4, 7, 'teh'], [29, 32, 'teh']]);
  // unknown words are asked once each; the key inside \\cite{} is never checked
  assert.deepEqual([...q.unknown.map(w => w.lang + ':' + w.word)], ['en:The', 'en:and', 'fa:مدل']);
});

test('the spelling menu offers suggestions, then add and ignore', () => {
  const { ctx } = setup();
  const items = ctx.spellMenu('teh', ['the', 'ten']);
  assert.deepEqual([...items.map(i => i.label)], ['the', 'ten', 'Add to my dictionary', 'Ignore']);
  assert.deepEqual([...ctx.spellMenu('xqzt', []).map(i => i.label)], ['No suggestions', 'Add to my dictionary', 'Ignore']);
});

test('choosing a suggestion replaces the word; adding remembers it for next time', async () => {
  const { ctx, $, saved } = setup();
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'one teh two';
  ctx.applySpellChoice({ action: 'replace', text: 'the' }, 4, 7, 'teh', 'en');
  assert.equal(area.value, 'one the two');
  ctx.applySpellChoice({ action: 'add' }, 0, 3, 'connectome', 'en');
  assert.deepEqual([...saved['spell-words']], ['connectome']);
  assert.equal(ctx.SPELL.cache.get('en:connectome'), true);
});

test('spell checking can be turned off and the choice is remembered', () => {
  const { ctx, $, saved } = setup();
  ctx.setSpell(false);
  assert.equal(saved.spell, false);
  assert.equal($('write-spell').attrs['aria-pressed'], 'false');
  ctx.setSpell(true);
  assert.equal($('write-spell').attrs['aria-pressed'], 'true');
});

test('the spelling menu opens above the word when there is no room below, and stays inside the pane', () => {
  const { ctx } = setup();
  const pane = { left: 100, top: 50, width: 600, height: 400 };
  assert.deepEqual({ ...ctx.menuPosition(pane, 300, 120, 180, 160) }, { left: 200, top: 74 });   // below the click
  assert.deepEqual({ ...ctx.menuPosition(pane, 300, 420, 180, 160) }, { left: 200, top: 206 });  // above: 420-50-160-4
  assert.deepEqual({ ...ctx.menuPosition(pane, 690, 120, 180, 160) }, { left: 414, top: 74 });   // pulled in from the right
});

test('spell query reads the whole text for context but reports only words in the visible ranges', () => {
  const { ctx } = setup();
  const text = '\\begin{align}\nx &= teh\\\\\ny &= wrod\n\\end{align}\nvisible teh';
  const visible = [[text.indexOf('y &='), text.length]];
  const q = ctx.spellQuery(text, visible, new Map([['en:teh', false]]));
  assert.deepEqual([...q.spans.map(s => s.word)], ['teh']);       // only the prose one, not inside align
  assert.equal(q.spans[0].from, text.lastIndexOf('teh'));
  assert.deepEqual([...q.unknown.map(w => w.word)], ['visible']);  // "wrod" is math: never asked
});

test('a spelling choice is not applied when the word changed or the file was switched meanwhile', async () => {
  const { ctx, $, calls } = setup();
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'one teh two';
  const editor = ctx.W.editor;
  area.value = 'xx one teh two';  // typed before the word while the menu was open
  ctx.applySpellChoice({ action: 'replace', text: 'the' }, 4, 7, 'teh', 'en', editor);
  assert.equal(area.value, 'xx one teh two');
  assert.ok(calls.some(c => c[0] === 'toast' && /changed/.test(c[1])));
  await ctx.openWriteFile('main.tex', true);
  const now = $('write-cm').children[0];
  now.value = 'one teh two';
  ctx.applySpellChoice({ action: 'replace', text: 'the' }, 4, 7, 'teh', 'en', editor);  // old editor
  assert.equal(now.value, 'one teh two');
});

// ---------- Version history: names, change counts, differences ----------
test('a version is labelled by its name, else its note, with its change counts', () => {
  const { ctx } = setup();
  assert.equal(ctx.versionLabel({ id: 's', created_at: '2026-10-19T10:00:00', note: 'auto', name: '' }), '2026-10-19 10:00:00 · auto');
  assert.equal(ctx.versionLabel({ id: 's', created_at: '2026-10-19T10:00:00', note: 'auto', name: 'Submitted' }), '2026-10-19 10:00:00 · Submitted');
  assert.equal(ctx.changeCounts({ added: 12, removed: 3 }), '+12 −3');
  assert.equal(ctx.changeCounts({ added: 0, removed: 0 }), 'no change');
  assert.equal(ctx.changeCounts({}), '');
});

test('differences are drawn as hunks with added, removed and unchanged lines', () => {
  const { ctx } = setup();
  const box = ctx.renderDiff({ file: 'main.tex', added: 1, removed: 1, truncated: true, hunks: [
    { a_start: 3, a_lines: 2, b_start: 3, b_lines: 2, lines: [
      { op: ' ', text: 'same' }, { op: '-', text: 'old <b>' }, { op: '+', text: 'جدید' }] }] });
  const rows = box.children;
  assert.equal(rows[0].className, 'diff-hunk'); assert.match(rows[0].textContent, /line 3/);
  assert.deepEqual([...rows.slice(1, 4).map(r => [r.className, r.textContent])],
    [['diff-line ctx', 'same'], ['diff-line del', 'old <b>'], ['diff-line add', 'جدید']]);
  assert.equal(rows[3].dir, 'auto');
  assert.match(rows.at(-1).textContent, /only the first part/);
});

test('identical versions say so', () => {
  const { ctx } = setup();
  assert.match(ctx.renderDiff({ file: 'main.tex', added: 0, removed: 0, hunks: [] }).children[0].textContent, /No differences/);
});

test('saving a named version saves the text first, then names it', async () => {
  const { ctx, $, calls } = setup({ get: url => (url.startsWith('/api/write/snapshots') ? { snapshots: [] } : null) });
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'edited'; area.fire('input');
  $('write-version-name').value = '  Before submission ';
  await ctx.saveNamedVersion();
  const save = calls.findIndex(c => c[0] === '/api/write/save');
  const snap = calls.findIndex(c => c[0] === '/api/write/snapshot');
  assert.ok(save >= 0 && snap > save);
  assert.equal(calls[snap][1].name, 'Before submission');
  assert.equal($('write-version-name').value, '');
});

test('selecting a version shows its differences from the current text of the open file', async () => {
  const { ctx, $, calls } = setup({ get: url => (url.startsWith('/api/write/diff') ?
    { file: 'main.tex', added: 0, removed: 0, hunks: [] } : null) });
  await ctx.openWriteDoc('d1');
  await ctx.selectVersion('20261019T100000-000000001');
  assert.ok(calls.some(c => c[0] === 'GET' && c[1] === '/api/write/diff?doc=d1&a=20261019T100000-000000001&b=current&file=main.tex'));
  assert.match($('write-version-diff').children[0].textContent, /No differences/);
});

test('naming an existing version sends the new name and refreshes the list', async () => {
  const { ctx, calls } = setup({ get: url => (url.startsWith('/api/write/snapshots') ? { snapshots: [] } : null) });
  await ctx.openWriteDoc('d1');
  ctx.window.prompt = () => 'Draft 2';
  await ctx.nameVersion({ id: 's9', name: '' });
  assert.deepEqual({ ...calls.find(c => c[0] === '/api/write/snapshot/name')[1] }, { doc: 'd1', snapshot: 's9', name: 'Draft 2' });
  assert.ok(calls.some(c => c[0] === 'GET' && c[1].startsWith('/api/write/snapshots')));
});

test('by default a version is compared with the one before it, matching its change counts', async () => {
  const { ctx, $, calls } = setup({ get: url => (url.startsWith('/api/write/diff') ? { file: 'main.tex', hunks: [] } : null) });
  await ctx.openWriteDoc('d1');
  ctx.VERS.list = [{ id: 's3' }, { id: 's2' }, { id: 's1' }];  // newest first
  $('write-version-against').value = 'previous';
  await ctx.selectVersion('s2');
  assert.ok(calls.some(c => c[0] === 'GET' && c[1] === '/api/write/diff?doc=d1&a=s1&b=s2&file=main.tex'));
  await ctx.selectVersion('s1');  // the oldest has nothing before it: compared with the current text
  assert.ok(calls.some(c => c[0] === 'GET' && c[1] === '/api/write/diff?doc=d1&a=s1&b=current&file=main.tex'));
  $('write-version-against').value = 'current';
  await ctx.selectVersion('s3');
  assert.ok(calls.some(c => c[0] === 'GET' && c[1] === '/api/write/diff?doc=d1&a=s3&b=current&file=main.tex'));
});

test('opening Versions selects the newest version that changed something', async () => {
  const { ctx, $, calls } = setup({ get: url => {
    if (url.startsWith('/api/write/snapshots')) return { snapshots: [
      { id: 's3', created_at: '2026-10-19T12:00:00', added: 0, removed: 0 },
      { id: 's2', created_at: '2026-10-19T11:00:00', added: 3, removed: 1 },
      { id: 's1', created_at: '2026-10-19T10:00:00', added: 9, removed: 0 }] };
    if (url.startsWith('/api/write/diff')) return { file: 'main.tex', hunks: [] };
    return null;
  } });
  await ctx.openWriteDoc('d1');
  $('write-versions').hidden = true;
  $('write-version-against').value = 'previous';
  await ctx.showWriteVersions();
  assert.equal(ctx.VERS.selected, 's2');
  assert.ok(calls.some(c => c[0] === 'GET' && c[1] === '/api/write/diff?doc=d1&a=s1&b=s2&file=main.tex'));
});

// Review (Codex) of the Versions panel: late replies, file switches, refreshed lists.
test('a late difference reply for an earlier selection is not shown', async () => {
  const pending = [];
  const { ctx, $ } = setup({ get: url => (url.startsWith('/api/write/diff') ? new Promise(r => pending.push(r)) : null) });
  await ctx.openWriteDoc('d1');
  $('write-version-against').value = 'current';
  const first = ctx.selectVersion('a'), second = ctx.selectVersion('b');
  pending[1]({ file: 'main.tex', hunks: [{ a_start: 1, a_lines: 1, b_start: 1, b_lines: 1, lines: [{ op: '+', text: 'B' }] }] });
  await second;
  pending[0]({ file: 'main.tex', hunks: [] });
  await first;
  assert.equal($('write-version-diff').children[1].textContent, 'B');
});

test('switching files closes the Versions panel', async () => {
  const { ctx, $ } = setup();
  await ctx.openWriteDoc('d1');
  $('write-versions').hidden = false;
  await ctx.openWriteFile('main.tex', true);
  assert.equal($('write-versions').hidden, true);
});

test('after saving a named version its differences are shown', async () => {
  const { ctx, $, calls } = setup({
    get: url => (url.startsWith('/api/write/snapshots') ? { snapshots: [{ id: 'new' }, { id: 'old' }] } :
      url.startsWith('/api/write/diff') ? { file: 'main.tex', hunks: [] } : null),
    post: url => (url === '/api/write/snapshot' ? { id: 'new' } : null) });
  await ctx.openWriteDoc('d1');
  $('write-version-against').value = 'previous';
  $('write-version-name').value = 'Named';
  await ctx.saveNamedVersion();
  assert.ok(calls.some(c => c[0] === 'GET' && c[1] === '/api/write/diff?doc=d1&a=old&b=new&file=main.tex'));
});

test('when the compared version is gone after a refresh, the differences are fetched again', async () => {
  const { ctx, $, calls } = setup({ get: url => (url.startsWith('/api/write/snapshots') ? { snapshots: [{ id: 's2' }, { id: 's1' }] } :
    url.startsWith('/api/write/diff') ? { file: 'main.tex', hunks: [] } : null) });
  await ctx.openWriteDoc('d1');
  ctx.VERS.selected = 's2';
  $('write-version-against').value = 'removed-version';
  await ctx.loadVersions();
  assert.ok(calls.some(c => c[0] === 'GET' && c[1] === '/api/write/diff?doc=d1&a=s1&b=s2&file=main.tex'));
});

// ---------- Assistant chat ----------
function chatSetup(report, approval) {
  const env = setup({
    get: url => (url.startsWith('/api/jobs/job-chat') ? { status: 'done', report } : null),
    post: (url, body) => {
      if (url === '/api/write/chat') {
        if (approval && !body.approved) return { needs_approval: true, sent: { characters: 10, evidence: 2 },
          preview: { model: 'big-cloud', provider: 'openrouter', remote: true, max_cost_usd: 0.002 } };
        return { id: 'job-chat', preview: { model: 'gemma', profile: 'local', remote: false }, sent: { characters: 10, evidence: 1 } };
      }
      if (url === '/api/write/cite') return { key: body.paper_id === 'p1' ? 'lee2021' : 'k' };
      return null;
    } });
  env.ctx.window.confirm = msg => { env.calls.push(['confirm', msg]); return approval !== 'deny'; };
  return env;
}
const ANSWER = { answer: 'The gain was 3.25.', flags: [], suggestion: null, model: 'gemma', provider: 'ollama',
  citations: [{ key: 'lee2021', paper_id: 'p1', page: 4, quote: 'the coupling gain was 3.25', evidence_id: 'e1' }] };

test('the context around the cursor is a few lines on each side', () => {
  const { ctx } = setup();
  const text = Array.from({ length: 40 }, (_, i) => 'l' + i).join('\n');
  const c = ctx.chatContext(text, text.indexOf('l20'));
  assert.ok(c.startsWith('l12') && c.endsWith('l28'));
});

test('a question sends the selection, the history and gets an answer with its sources', async () => {
  const { ctx, $, calls } = chatSetup(ANSWER);
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Intro. Gain is high.'; area.setSelectionRange(7, 20);
  $('write-chat-input').value = 'What gain was used?';
  await ctx.sendChat();
  const sent = calls.find(c => c[0] === '/api/write/chat')[1];
  assert.equal(sent.question, 'What gain was used?'); assert.equal(sent.selection, 'Gain is high.');
  assert.equal(sent.context, ''); assert.deepEqual([...sent.history], []);
  assert.equal($('write-chat-input').value, '');
  const log = $('write-chat-log').children;
  assert.equal(log[0].className, 'chat-msg user'); assert.equal(log[0].children[0].textContent, 'What gain was used?');
  assert.equal(log[1].className, 'chat-msg assistant'); assert.equal(log[1].children[0].textContent, 'The gain was 3.25.');
  assert.match(log[1].children[1].children[0].textContent, /lee2021 · p\.4/);
  await ctx.sendChat();  // empty input: nothing is sent
  assert.equal(calls.filter(c => c[0] === '/api/write/chat').length, 1);
  $('write-chat-input').value = 'And the subjects?';
  await ctx.sendChat();
  const second = calls.filter(c => c[0] === '/api/write/chat')[1][1];
  assert.deepEqual([...second.history.map(t => t.role + ':' + t.text)], ['user:What gain was used?', 'assistant:The gain was 3.25.']);
});

test('inserting a source adds \\cite{key} at the cursor and registers the paper', async () => {
  const { ctx, $, calls } = chatSetup(ANSWER);
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Gain was high.'; area.setSelectionRange(14, 14);
  await ctx.insertChatCitation(ANSWER.citations[0]);
  assert.equal(area.value, 'Gain was high.\\cite{lee2021}');
  assert.equal(calls.find(c => c[0] === '/api/write/cite')[1].evidence_id, 'e1');
});

test('an edit proposed by the assistant opens the usual suggestion bar for the selection', async () => {
  const suggestion = { id: 's1', task: 'write_chat', provider: 'ollama', model: 'gemma', note: '', flags: [],
    original: 'Gain is high.', citations: [], hunks: [{ index: 0, start: 8, end: 12, old: 'high', new: '3.25', flags: [] }] };
  const { ctx, $ } = chatSetup({ ...ANSWER, suggestion });
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Intro. Gain is high.'; area.setSelectionRange(7, 20);
  $('write-chat-input').value = 'Use the number';
  await ctx.sendChat();
  assert.equal($('write-suggestion').hidden, false);
  await ctx.applyWriteSuggestion();
  assert.equal(area.value, 'Intro. Gain is 3.25.');
});

test('a cloud route asks first; declining sends nothing', async () => {
  const { ctx, $, calls } = chatSetup(ANSWER, 'deny');
  await ctx.openWriteDoc('d1');
  $('write-chat-input').value = 'Question?';
  await ctx.sendChat();
  assert.match(calls.find(c => c[0] === 'confirm')[1], /big-cloud/);
  assert.equal(calls.filter(c => c[0] === '/api/write/chat').length, 1);
  assert.equal($('write-chat-input').value, 'Question?');  // kept so it can be sent again
});

test('opening another document starts a new conversation', async () => {
  const { ctx, $ } = chatSetup(ANSWER);
  await ctx.openWriteDoc('d1');
  $('write-chat-input').value = 'Q?';
  await ctx.sendChat();
  await ctx.openWriteDoc('d1');
  assert.equal(ctx.CHAT.turns.length, 0);
  assert.equal($('write-chat-log').children.length, 0);
});

// Review (Codex) of the assistant: approval covers exactly what is sent; late replies; typing meanwhile.
test('after approval exactly the approved request is sent, even if the selection changed meanwhile', async () => {
  let approveNow;
  const { ctx, $, calls } = chatSetup(ANSWER, 'ask');
  ctx.window.confirm = () => { approveNow(); return true; };
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Approved text. Secret text.'; area.setSelectionRange(0, 14);
  approveNow = () => area.setSelectionRange(15, 27);  // the user selects something else while the dialog is open
  $('write-chat-input').value = 'Shorter?';
  await ctx.sendChat();
  const sent = calls.filter(c => c[0] === '/api/write/chat').map(c => c[1]);
  assert.equal(sent.length, 2);
  assert.equal(sent[1].approved, true);
  assert.equal(sent[1].selection, 'Approved text.');
});

test('a message typed while the question was being sent is kept', async () => {
  let release;
  const { ctx, $ } = setup({ post: url => (url === '/api/write/chat' ? new Promise(r => { release = r; }) : null),
    get: url => (url.startsWith('/api/jobs/') ? { status: 'done', report: ANSWER } : null) });
  await ctx.openWriteDoc('d1');
  $('write-chat-input').value = 'First?';
  const done = ctx.sendChat();
  $('write-chat-input').value = 'Second, being typed';
  release({ id: 'job-chat', preview: { model: 'm', profile: 'local', remote: false }, sent: { characters: 1, evidence: 0 } });
  await done;
  assert.equal($('write-chat-input').value, 'Second, being typed');
});

test('a source inserted after switching documents goes nowhere', async () => {
  let release;
  const { ctx, $, calls } = setup({ post: url => (url === '/api/write/cite' ? new Promise(r => { release = r; }) : null) });
  await ctx.openWriteDoc('d1');
  const done = ctx.insertChatCitation(ANSWER.citations[0]);
  await ctx.openWriteFile('main.tex', true);  // new editor (another document behaves the same)
  $('write-cm').children[0].value = 'Other.';
  release({ key: 'lee2021' });
  await done;
  assert.equal($('write-cm').children[0].value, 'Other.');
  assert.ok(calls.some(c => c[0] === 'toast' && /lee2021/.test(c[1])));
});

/* ---- title, abstract and LaTeX from an image ---- */

const PAPER = '\\documentclass{article}\n% \\title{Old commented}\n\\title{A {draft} title}\n\\begin{document}\n\\maketitle\n' +
  '\\begin{abstract}\nShort.\n\\end{abstract}\nWe fit 40 subjects. % a note\n\\end{document}\n';

test('the AI menu offers title, abstract and the two image actions', () => {
  for (const v of ['title', 'abstract', 'equation-image', 'table-image']) assert.match(html, new RegExp('<option value="' + v + '"'));
  assert.match(html, /id="write-image"/);
  assert.match(html, /id="write-image-file"[^>]*accept="[^"]*image\/png/);
});

test('titleRange and abstractRange find the content, skip comments and nested braces', () => {
  const { ctx } = setup();
  const t = ctx.titleRange(PAPER);
  assert.equal(PAPER.slice(t.from, t.to), 'A {draft} title');
  const a = ctx.abstractRange(PAPER);
  assert.equal(PAPER.slice(a.from, a.to), '\nShort.\n');
  assert.equal(ctx.titleRange('\\begin{document}x\\end{document}'), null);
  assert.equal(ctx.abstractRange('% \\begin{abstract}x\\end{abstract}'), null);
  assert.equal(ctx.titleRange('\\title{never closed'), null);
  const opt = '\\title[Short]{Long one}';
  const o = ctx.titleRange(opt);
  assert.equal(opt.slice(o.from, o.to), 'Long one');
});

test('documentBody is the text between begin and end document without comments', () => {
  const { ctx } = setup();
  const body = ctx.documentBody(PAPER);
  assert.match(body, /We fit 40 subjects\./);
  assert.doesNotMatch(body, /a note|documentclass|Old commented/);
  assert.equal(ctx.documentBody('plain 50\\% text % gone'), 'plain 50\\% text ');
});

test('"Suggest title" sends the current title and the document, and applies to the title only', async () => {
  const report = { id: 's2', task: 'write_summary', action: 'title', provider: 'ollama', model: 'qwen', note: '', flags: [],
    original: 'A {draft} title', citations: [], hunks: [{ index: 0, start: 0, end: 15, old: 'A {draft} title', new: 'Better', flags: [] }] };
  const { ctx, $, calls } = aiSetup(report);
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = PAPER;
  await ctx.runWriteAi('title', false);
  const sent = calls.find(c => c[0] === '/api/write/assist')[1];
  assert.equal(sent.text, 'A {draft} title');
  assert.match(sent.context, /We fit 40 subjects/);
  assert.equal($('write-suggestion-title').textContent, 'Title');
  await ctx.applyWriteSuggestion();
  assert.match(area.value, /\\title\{Better\}\n\\begin\{document\}/);
});

test('title and abstract need the command or environment first', async () => {
  const { ctx, $, calls } = aiSetup(REPORT);
  await ctx.openWriteDoc('d1');
  $('write-cm').children[0].value = 'Text.';
  await ctx.runWriteAi('title', false);
  await ctx.runWriteAi('abstract', false);
  const toasts = calls.filter(c => c[0] === 'toast').map(c => c[1]).join(' | ');
  assert.match(toasts, /\\title\{\}/); assert.match(toasts, /\\begin\{document\}/);
  assert.ok(!calls.some(c => c[0] === '/api/write/assist'));
});

test('imagePads puts an inserted block on its own lines', () => {
  const { ctx } = setup();
  assert.deepEqual({ ...ctx.imagePads('ab\ncd', 3) }, { pad_before: false, pad_after: true });
  assert.deepEqual({ ...ctx.imagePads('ab\ncd', 4) }, { pad_before: true, pad_after: true });
  assert.deepEqual({ ...ctx.imagePads('ab\n', 3) }, { pad_before: false, pad_after: false });
  assert.deepEqual({ ...ctx.imagePads('', 0) }, { pad_before: false, pad_after: false });
});

test('pastedImage takes an image from the clipboard and ignores text', () => {
  const { ctx } = setup();
  const file = { type: 'image/png', size: 10 };
  const ev = items => ({ clipboardData: { items } });
  assert.equal(ctx.pastedImage(ev([{ kind: 'string', type: 'text/plain' }, { kind: 'file', type: 'image/png', getAsFile: () => file }])), file);
  assert.equal(ctx.pastedImage(ev([{ kind: 'string', type: 'text/plain' }])), null);
  assert.equal(ctx.pastedImage({}), null);
});

test('equation from an image: the panel opens, the image is sent and the result goes in at the cursor', async () => {
  const report = { id: 's3', task: 'write_image', action: 'equation-image', provider: 'ollama', model: 'qwen2.5vl:7b',
    note: '', flags: ['transcribed from an image: check every symbol and number against it'], original: '', citations: [],
    hunks: [{ index: 0, start: 0, end: 0, old: '', new: '\n\\[ x \\]\n', flags: [] }] };
  const { ctx, $, calls } = aiSetup(report);
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Before it. After it.'; area.setSelectionRange(10, 10);
  await ctx.runWriteAi('equation-image', false);
  assert.equal($('write-image').hidden, false);
  assert.ok(!calls.some(c => c[0] === '/api/write/assist'));  // nothing is sent before an image is given
  await ctx.sendWriteImage('data:image/png;base64,iVBORw0KGgo=');
  assert.equal($('write-image').hidden, true);
  const sent = calls.find(c => c[0] === '/api/write/assist')[1];
  assert.equal(sent.action, 'equation-image'); assert.equal(sent.text, '');
  assert.equal(sent.image, 'data:image/png;base64,iVBORw0KGgo=');
  assert.equal(sent.pad_before, true); assert.equal(sent.pad_after, true);
  assert.equal($('write-suggestion-title').textContent, 'From image');
  await ctx.applyWriteSuggestion();
  assert.equal(area.value, 'Before it.\n\\[ x \\]\n After it.');
});

test('an image result is refused when the text before the cursor changed', async () => {
  const report = { id: 's4', task: 'write_image', provider: 'ollama', model: 'v', note: '', flags: [], original: '', citations: [],
    hunks: [{ index: 0, start: 0, end: 0, old: '', new: 'X', flags: [] }] };
  const { ctx, $, calls } = aiSetup(report);
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'abc def'; area.setSelectionRange(3, 3);
  await ctx.runWriteAi('table-image', false);
  await ctx.sendWriteImage('data:image/png;base64,AA==');
  area.value = 'abZc def';
  await ctx.applyWriteSuggestion();
  assert.equal(area.value, 'abZc def');
  assert.ok(calls.some(c => c[0] === 'toast' && /changed since/.test(c[1])));
});

test('closing the image panel sends nothing; a too large file is refused', async () => {
  const { ctx, $, calls } = aiSetup(REPORT);
  await ctx.openWriteDoc('d1');
  await ctx.runWriteAi('equation-image', false);
  ctx.closeWriteImage();
  assert.equal($('write-image').hidden, true);
  await ctx.writeImageFile({ type: 'image/png', size: 9 * 1024 * 1024 });
  assert.ok(calls.some(c => c[0] === 'toast' && /at most 8 MB/.test(c[1])));
  await ctx.writeImageFile({ type: 'application/pdf', size: 10 });
  assert.ok(calls.some(c => c[0] === 'toast' && /PNG, JPEG/.test(c[1])));
  assert.ok(!calls.some(c => c[0] === '/api/write/assist'));
});

test('the image panel floats under the cursor, like the suggestion bar', async () => {
  const { ctx, $ } = aiSetup(REPORT);
  await ctx.openWriteDoc('d1');
  const box = $('write-image');
  box.parentNode = { getBoundingClientRect: () => ({ top: 100, height: 400 }) };
  box.offsetHeight = 80;
  const area = $('write-cm').children[0];
  area.value = 'abc'; area.setSelectionRange(1, 1);
  ctx.W.editor.coords = () => ({ top: 150, bottom: 170 });
  await ctx.runWriteAi('equation-image', false);
  assert.equal(box.style.top, '76px');
});

test('suggested text keeps its line breaks', () => {
  const css = fs.readFileSync('src/litforge/ui/static/app.css', 'utf8');
  assert.match(css, /\.write-suggestion \.hunk del, \.write-suggestion \.hunk ins \{[^}]*white-space: pre-wrap/);
});

test('a new AI action waits until the open suggestion is applied or rejected', async () => {
  const { ctx, $, calls } = aiSetup(REPORT);
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'Intro: one two three.'; area.setSelectionRange(7, 20);
  await ctx.runWriteAi('shorter', false);
  assert.ok(ctx.AI_pending());
  await ctx.runWriteAi('clearer', false);
  await ctx.runWriteAi('equation-image', false);
  assert.equal(calls.filter(c => c[0] === '/api/write/assist').length, 1);
  assert.ok(calls.some(c => c[0] === 'toast' && /Apply or reject the open suggestion first/.test(c[1])));
  assert.notEqual($('write-image').hidden, false);
});

test('"Suggest abstract" without an abstract inserts a new one after \\maketitle', async () => {
  const report = { id: 's5', task: 'write_summary', action: 'abstract', provider: 'ollama', model: 'q', note: '', flags: [],
    original: '', citations: [], hunks: [{ index: 0, start: 0, end: 0, old: '', new: '\\begin{abstract}\nNew.\n\\end{abstract}\n', flags: [] }] };
  const { ctx, $, calls } = aiSetup(report);
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = '\\title{T}\n\\begin{document}\n\\maketitle\n\\section{A} Text.\n\\end{document}\n';
  await ctx.runWriteAi('abstract', false);
  const sent = calls.find(c => c[0] === '/api/write/assist')[1];
  assert.equal(sent.text, ''); assert.equal(sent.new_env, true);
  assert.match(sent.context, /Text\./);
  await ctx.applyWriteSuggestion();
  assert.equal(area.value, '\\title{T}\n\\begin{document}\n\\maketitle\n\\begin{abstract}\nNew.\n\\end{abstract}\n\\section{A} Text.\n\\end{document}\n');
});

test('abstractInsertAt: after \\maketitle, else after \\begin{document}, else nowhere', () => {
  const { ctx } = setup();
  assert.equal(ctx.abstractInsertAt('a\n\\begin{document}\n\\maketitle\nb'), 'a\n\\begin{document}\n\\maketitle\n'.length);
  assert.equal(ctx.abstractInsertAt('\\begin{document}\nb'), '\\begin{document}\n'.length);
  assert.equal(ctx.abstractInsertAt('% \\maketitle\n\\begin{document} x'), '% \\maketitle\n\\begin{document} x'.length);
  assert.equal(ctx.abstractInsertAt('no document here'), -1);
});

/* ---- round 3: Codex review ---- */

test('an approval resends exactly the request shown, and nothing when the user moved to another document', async () => {
  const env = aiSetup(REPORT, true);
  await env.ctx.openWriteDoc('d1');
  const area = env.$('write-cm').children[0];
  area.value = '\\title{Old}\n\\begin{document}\nBody one.\n\\end{document}';
  env.ctx.window.confirm = () => { area.value = '\\title{Other}\n\\begin{document}\nBody two.\n\\end{document}'; return true; };
  await env.ctx.runWriteAi('title', false);
  const posts = env.calls.filter(c => c[0] === '/api/write/assist').map(c => c[1]);
  assert.equal(posts.length, 2);
  assert.equal(posts[1].text, 'Old'); assert.match(posts[1].context, /Body one/);
  assert.equal(posts[1].approved, true);
  const moved = aiSetup(REPORT, true);
  await moved.ctx.openWriteDoc('d1');
  moved.$('write-cm').children[0].value = 'one two three'; moved.$('write-cm').children[0].setSelectionRange(0, 13);
  moved.ctx.window.confirm = () => { moved.ctx.W.doc = { id: 'd2', name: 'Other' }; return true; };
  await moved.ctx.runWriteAi('clearer', false);
  assert.equal(moved.calls.filter(c => c[0] === '/api/write/assist').length, 1);
});

test('an empty title is not filled in at an old offset after text was added before it', async () => {
  const report = { id: 's6', task: 'write_summary', action: 'title', provider: 'o', model: 'q', note: '', flags: [], original: '',
    citations: [], hunks: [{ index: 0, start: 0, end: 0, old: '', new: 'New title', flags: [] }] };
  const { ctx, $, calls } = aiSetup(report);
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = '\\title{}\n\\begin{document}\nBody.\n\\end{document}';
  await ctx.runWriteAi('title', false);
  area.value = 'xx' + area.value;
  await ctx.applyWriteSuggestion();
  assert.equal(area.value, 'xx\\title{}\n\\begin{document}\nBody.\n\\end{document}');
  assert.ok(calls.some(c => c[0] === 'toast' && /changed since/.test(c[1])));
});

test('title and abstract are found as LaTeX reads comments and escapes', () => {
  const { ctx } = setup();
  const t1 = '\\title{Old % }\n continued}';
  const r1 = ctx.titleRange(t1);
  assert.equal(t1.slice(r1.from, r1.to), 'Old % }\n continued');
  const t2 = '\\begin{abstract}Old\n% \\end{abstract}\ncontinued\n\\end{abstract}';
  const r2 = ctx.abstractRange(t2);
  assert.equal(t2.slice(r2.from, r2.to), 'Old\n% \\end{abstract}\ncontinued\n');
  const t3 = '\\\\% \\title{Commented}\n\\title{Real}';
  const r3 = ctx.titleRange(t3);
  assert.equal(t3.slice(r3.from, r3.to), 'Real');
  const t4 = '50\\% of \\title{Kept}';
  const r4 = ctx.titleRange(t4);
  assert.equal(t4.slice(r4.from, r4.to), 'Kept');
  assert.equal(ctx.titleRange('\\title{a \\} b}').to, '\\title{a \\} b'.length);
});

test('a slow image read does not go to a panel opened after it', async () => {
  const { ctx, $, calls } = aiSetup(REPORT);
  const readers = [];
  ctx.FileReader = class { constructor() { readers.push(this); } readAsDataURL() {} };
  await ctx.openWriteDoc('d1');
  $('write-cm').children[0].value = 'abc';
  await ctx.runWriteAi('equation-image', false);
  const reading = ctx.writeImageFile({ type: 'image/png', size: 10 });
  ctx.closeWriteImage();
  await ctx.runWriteAi('table-image', false);
  readers[0].result = 'data:image/png;base64,AA=='; readers[0].onload();
  await reading;
  assert.ok(!calls.some(c => c[0] === '/api/write/assist'));
  assert.equal($('write-image').hidden, false);  // the table panel is still waiting for its own image
});

/* ---- editor basics: outline, word count, compile settings ---- */

const THESIS = '\\documentclass{report}\n\\begin{document}\n\\chapter{Intro}\n% \\section{Old}\nText.\n' +
  '\\section*{Aim of \\textbf{this} work}\n\\input{sections/methods}\n\\subsection[Short]{A long \\lr{TVB} title}\n' +
  '\\section{50\\% done \\cite{a}}\n\\end{document}\n';

test('outlineOf lists sections with levels and lines, skips comments and shows included files', () => {
  const { ctx } = setup();
  const items = [...ctx.outlineOf(THESIS)].map(i => ({ ...i }));
  assert.deepEqual(items, [
    { kind: 'section', level: 1, title: 'Intro', line: 3 },
    { kind: 'section', level: 2, title: 'Aim of this work', line: 6 },
    { kind: 'file', name: 'sections/methods.tex', line: 7 },
    { kind: 'section', level: 3, title: 'A long TVB title', line: 8 },
    { kind: 'section', level: 2, title: '50% done', line: 9 }]);
  assert.equal(ctx.outlineOf('no sections').length, 0);
});

test('outlineAt finds the section the cursor is in', () => {
  const { ctx } = setup();
  const items = ctx.outlineOf(THESIS);
  assert.equal(ctx.outlineAt(items, 2), -1);
  assert.equal(ctx.outlineAt(items, 5), 0);
  assert.equal(ctx.outlineAt(items, 7), 1);  // an \input line is not a section
  assert.equal(ctx.outlineAt(items, 40), 4);
});

test('countWords counts the text: no preamble, commands, math, citations or comments', () => {
  const { ctx } = setup();
  assert.equal(ctx.countWords('\\documentclass{article}\n\\usepackage{x}\n\\begin{document}\nOne two \\textbf{three}.' +
    ' % not this\n$a + b$ and \\cite{k} \\ref{f} four.\n\\begin{equation}x=1\\end{equation}\n\\end{document}\nafter'), 5);
  assert.equal(ctx.countWords('A model of 40 patients (Wilson--Cowan, MRI).'), 7);
  assert.equal(ctx.countWords('این مدل شخصی‌سازی‌شده است \\lr{TVB}.'), 5);
  assert.equal(ctx.countWords('\\begin{tabular}{l c}\nA & B \\\\\n\\end{tabular}'), 2);
  assert.equal(ctx.countWords(''), 0);
});

test('the outline panel opens, lists the file and jumps to a line', async () => {
  const { ctx, $, saved } = setup();
  await ctx.openWriteDoc('d1');
  $('write-cm').children[0].value = THESIS;
  ctx.setWriteOutline(true);
  assert.equal($('write-outline').hidden, false); assert.equal(saved['write-outline'], true);
  ctx.refreshWriteStats();
  const rows = $('write-outline-list').children;
  assert.equal(rows.length, 5);
  assert.equal(rows[1].textContent, 'Aim of this work'); assert.equal(rows[1].dataset.level, '2');
  rows[1].onclick();
  assert.equal($('write-cm').children[0].selectionStart, THESIS.indexOf('\\section*'));
  assert.match($('write-words').textContent, /^\d+ words$/);
  ctx.setWriteOutline(false);
  assert.equal($('write-outline').hidden, true);
});

test('a document opens its main file, and the settings menu changes it', async () => {
  const doc = { id: 'd1', name: 'Thesis', main_file: 'thesis.tex', draft: false, engine: 'tectonic', pdf: false,
    main_candidates: ['main.tex', 'thesis.tex'], files: [{ name: 'main.tex', kind: 'text' }, { name: 'thesis.tex', kind: 'text' }] };
  const { ctx, $, calls } = setup({
    get: url => url.startsWith('/api/write/doc?') ? doc : null,
    post: (url, body) => url === '/api/write/settings' ? { ...doc, ...body, draft: !!body.draft, main_file: body.main_file || doc.main_file } : null
  });
  await ctx.openWriteDoc('d1');
  assert.ok(calls.some(c => c[0] === 'GET' && /name=thesis\.tex/.test(c[1])));
  assert.equal($('write-main').value, 'thesis.tex');
  assert.equal($('write-main').children.length, 2);
  assert.equal($('write-engine-pick').value, 'tectonic');
  $('write-draft').checked = true;
  await ctx.changeWriteSettings({ draft: true });
  assert.deepEqual({ ...calls.find(c => c[0] === '/api/write/settings')[1] }, { doc: 'd1', draft: true });
  assert.equal(ctx.W.doc.draft, true);
  assert.match($('write-export').href, /\/api\/write\/export\.zip\?doc=d1$/);
});

test('importing a ZIP posts the file and opens the new document', async () => {
  const { ctx, calls } = setup();
  const sent = [];
  ctx.fetch = async (url, opts) => { sent.push([url, opts]); return { ok: true, json: async () => ({ id: 'd1', name: 'paper' }) }; };
  await ctx.importWriteZip({ name: 'My paper.zip', size: 1000 });
  assert.equal(sent[0][0], '/api/write/import?project=demo&name=My%20paper');
  assert.equal(sent[0][1].method, 'POST');
  assert.ok(calls.some(c => c[0] === 'GET' && c[1].startsWith('/api/write/doc?doc=d1')));
  ctx.fetch = async () => ({ ok: false, json: async () => ({ error: 'no .tex file with \\documentclass' }) });
  await ctx.importWriteZip({ name: 'x.zip', size: 10 });
  assert.ok(calls.some(c => c[0] === 'toast' && /documentclass/.test(c[1])));
  await ctx.importWriteZip({ name: 'big.zip', size: 101 * 1024 * 1024 });
  assert.ok(calls.some(c => c[0] === 'toast' && /at most 100 MB/.test(c[1])));
});

test('countWords: \\bibliographystyle and figure options are not words', () => {
  const { ctx } = setup();
  assert.equal(ctx.countWords('\\begin{document}\n\\begin{figure}[h]\\centering\\caption{An equation.}\\end{figure}\n' +
    '\\bibliographystyle{plain}\n\\bibliography{refs}\n\\end{document}'), 2);
});

/* ---- editor basics round 3: Codex review ---- */

test('outline titles leave out comments, and an escaped \\\\section is not a heading', () => {
  const { ctx } = setup();
  const items = [...ctx.outlineOf('\\section{Visible% secret\n title}\nA line\\\\section{Fake}\n\\\\\\section{Real}')];
  assert.deepEqual(items.map(i => i.title), ['Visible title', 'Real']);
  assert.equal(ctx.titleRange('a\\\\title{Fake}'), null);
  assert.equal(ctx.abstractRange('\\\\begin{abstract}x\\end{abstract}'), null);
});

test('countWords: an escaped dollar is not math', () => {
  const { ctx } = setup();
  assert.equal(ctx.countWords('Pay \\$5 and $x$ today'), 4);
  assert.equal(ctx.countWords('50\\% of \\$ and \\& more'), 4);
});

test('settings requests run one after another, and a late failure leaves another document alone', async () => {
  const replies = [];
  const doc = { id: 'd1', name: 'A', main_file: 'main.tex', draft: false, engine: 'tectonic', pdf: false,
    main_candidates: ['main.tex'], files: [{ name: 'main.tex', kind: 'text' }] };
  const { ctx, $, calls } = setup({
    get: url => url.startsWith('/api/write/doc?') ? doc : null,
    post: (url, body) => {
      if (url !== '/api/write/settings') return null;
      return new Promise((resolve, reject) => replies.push({ body, resolve, reject }));
    }
  });
  await ctx.openWriteDoc('d1');
  const on = ctx.changeWriteSettings({ draft: true });
  const off = ctx.changeWriteSettings({ draft: false });
  await new Promise(r => setImmediate(r));
  assert.equal(replies.length, 1);  // the second waits for the first
  replies[0].resolve({ ...doc, draft: true });
  await on; await new Promise(r => setImmediate(r));
  assert.equal(replies.length, 2);
  replies[1].resolve({ ...doc, draft: false });
  await off;
  assert.equal($('write-draft').checked, false); assert.equal(ctx.W.doc.draft, false);
  const failing = ctx.changeWriteSettings({ engine: 'xelatex' });
  await new Promise(r => setImmediate(r));
  ctx.W.doc = { ...doc, id: 'd2', engine: 'xelatex' };
  $('write-engine-pick').value = 'xelatex';
  replies[2].reject(new Error('boom'));
  await failing;
  assert.equal($('write-engine-pick').value, 'xelatex');  // d2's controls are untouched
  assert.ok(calls.some(c => c[0] === 'toast' && c[1] === 'boom'));
});

/* ---- writing aids: symbols, hover previews, notes ---- */

test('mathSpanAt finds inline, display and environment math, and skips escaped dollars and comments', () => {
  const { ctx } = setup();
  const t = 'Cost \\$5, $a+b$ and \\[ c \\] then \\begin{align*} d \\end{align*} % $x$\n\\(e\\) $$f$$';
  const at = s => { const r = ctx.mathSpanAt(t, t.indexOf(s)); return r && t.slice(r.from, r.to); };
  assert.equal(at('a+b'), '$a+b$');
  assert.equal(at(' c '), '\\[ c \\]');
  assert.equal(at(' d '), '\\begin{align*} d \\end{align*}');
  assert.equal(at('e\\)'), '\\(e\\)');
  assert.equal(at('f$$'), '$$f$$');
  assert.equal(at('5,'), null);  // after an escaped dollar
  assert.equal(at('x$'), null);  // in a comment
  assert.equal(at('then'), null);
});

test('figureAt finds the \\includegraphics under the pointer', () => {
  const { ctx } = setup();
  const t = 'See \\includegraphics[width=3cm]{figures/plot} here';
  assert.deepEqual({ ...ctx.figureAt(t, t.indexOf('plot')) }, { from: 4, to: t.indexOf(' here'), name: 'figures/plot' });
  assert.deepEqual({ ...ctx.figureAt(t, t.indexOf('includegraphics')) }, { ...ctx.figureAt(t, t.indexOf('plot')) });
  assert.equal(ctx.figureAt(t, t.indexOf('here')), null);
  assert.equal(ctx.figureAt('% \\includegraphics{a}', 5), null);
});

test('symbolInsert wraps in $...$ outside math and puts the cursor in the first argument', () => {
  const { ctx } = setup();
  assert.deepEqual({ ...ctx.symbolInsert('\\alpha', true) }, { text: '\\alpha', cursor: 6 });
  assert.deepEqual({ ...ctx.symbolInsert('\\alpha', false) }, { text: '$\\alpha$', cursor: 8 });
  assert.deepEqual({ ...ctx.symbolInsert('\\frac{}{}', false) }, { text: '$\\frac{}{}$', cursor: 7 });
  assert.deepEqual({ ...ctx.symbolInsert('\\mathbb{R}', true) }, { text: '\\mathbb{R}', cursor: 10 });
});

test('the symbol palette filters, and a click inserts at the cursor (in math as a command)', async () => {
  const { ctx, $ } = setup();
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = 'We set $x = $ here.'; area.setSelectionRange(12, 12);
  ctx.openWriteSymbols();
  assert.equal($('write-symbols').hidden, false);
  $('write-symbols-q').value = 'alpha'; ctx.renderWriteSymbols();
  const first = $('write-symbols-grid').children[0];
  assert.equal(first.textContent, 'α'); assert.equal(first.title, '\\alpha');
  first.onclick();
  assert.equal(area.value, 'We set $x = \\alpha$ here.');
  assert.equal($('write-symbols').hidden, true);
  area.setSelectionRange(0, 0);
  ctx.openWriteSymbols(); $('write-symbols-q').value = 'sum'; ctx.renderWriteSymbols();
  $('write-symbols-grid').children[0].onclick();
  assert.equal(area.value.slice(0, 6), '$\\sum$');
});

test('hoverPreview tells math from figures', () => {
  const { ctx } = setup();
  const t = 'A $x^2$ and \\includegraphics{fig}.';
  assert.deepEqual({ ...ctx.hoverPreview(t, 4) }, { kind: 'math', from: 2, to: 7, tex: '$x^2$' });
  assert.equal(ctx.hoverPreview(t, t.indexOf('fig')).kind, 'figure');
  assert.equal(ctx.hoverPreview(t, 0), null);
  assert.equal(ctx.hoverPreview('$' + 'x'.repeat(5000) + '$', 3), null);  // too long to preview
});

function notesSetup(extra = {}) {
  const notes = [{ id: 'n1', file: 'main.tex', line: 2, anchor: 'Second line', body: 'Check this claim', moved: false, orphan: false }];
  const env = setup({
    get: url => url.startsWith('/api/write/notes?') ? { notes } : null,
    post: (url, body) => {
      if (url === '/api/write/note') return { id: body.id || 'n2', file: body.file, line: body.line, anchor: body.anchor, body: body.body };
      if (url === '/api/write/note/delete') return { ok: true };
      if (url === '/api/write/notes/move') return { moved: body.notes.length };
      return extra.post ? extra.post(url, body) : null;
    }
  });
  env.files['main.tex'] = { text: 'First line\nSecond line\nThird line\n', hash: 'h0' };
  return env;
}

test('notes load with the file, show in the outline panel and open by line', async () => {
  const { ctx, $, calls } = notesSetup();
  await ctx.openWriteDoc('d1');
  await ctx.loadWriteNotes();
  assert.ok(calls.some(c => c[0] === 'GET' && c[1] === '/api/write/notes?doc=d1&file=main.tex'));
  ctx.setWriteOutline(true);
  const rows = [...$('write-outline-list').children];
  const note = rows.find(r => r.className === 'note');
  assert.equal(note.textContent, '2 · Check this claim');
  note.onclick();
  assert.equal($('write-note').hidden, false);
  assert.equal($('write-note-text').value, 'Check this claim');
  assert.match($('write-note-title').textContent, /line 2/);
});

test('a note is added on a line, edited and deleted', async () => {
  const { ctx, $, calls } = notesSetup();
  await ctx.openWriteDoc('d1');
  await ctx.loadWriteNotes();
  ctx.openWriteNote(3);
  assert.equal($('write-note-text').value, '');
  $('write-note-text').value = '  Add a citation  ';
  await ctx.saveWriteNote();
  const added = calls.find(c => c[0] === '/api/write/note')[1];
  assert.deepEqual({ ...added }, { doc: 'd1', file: 'main.tex', line: 3, anchor: 'Third line', body: 'Add a citation' });
  assert.equal($('write-note').hidden, true);
  assert.equal(ctx.writeNotes().length, 2);
  ctx.openWriteNote(2);
  await ctx.deleteWriteNote();
  assert.ok(calls.some(c => c[0] === '/api/write/note/delete' && c[1].id === 'n1'));
  assert.deepEqual(ctx.writeNotes().map(n => n.id), ['n2']);
  ctx.openWriteNote(1); $('write-note-text').value = '   ';
  await ctx.saveWriteNote();
  assert.ok(calls.some(c => c[0] === 'toast' && /Write a note/.test(c[1])));
});

test('after a save, notes that moved with the text are stored at their new lines', async () => {
  const { ctx, $, calls } = notesSetup();
  await ctx.openWriteDoc('d1');
  await ctx.loadWriteNotes();
  ctx.W.editor.noteLines = () => [{ id: 'n1', line: 4 }];
  $('write-cm').children[0].value = 'New\nFirst line\nX\nSecond line\nThird line\n';
  await ctx.syncWriteNotes();
  const move = calls.find(c => c[0] === '/api/write/notes/move')[1];
  assert.deepEqual(JSON.parse(JSON.stringify(move)), { doc: 'd1', file: 'main.tex', notes: [{ id: 'n1', line: 4, anchor: 'Second line' }] });
  calls.length = 0;
  await ctx.syncWriteNotes();  // nothing moved since
  assert.ok(!calls.some(c => c[0] === '/api/write/notes/move'));
});

test('katexSource strips delimiters, labels and references; katexMacros reads the document\'s definitions', () => {
  const { ctx } = setup();
  assert.deepEqual({ ...ctx.katexSource('$x$') }, { tex: 'x', display: false });
  assert.deepEqual({ ...ctx.katexSource('\\[ y \\]') }, { tex: ' y ', display: true });
  assert.deepEqual({ ...ctx.katexSource('\\begin{equation}\\label{eq:a} z = \\eqref{b}\\end{equation}') },
    { tex: ' z = ??', display: true });
  assert.equal(ctx.katexSource('\\begin{align} a &= b \\nonumber \\end{align}').tex, '\\begin{align} a &= b  \\end{align}');
  const m = ctx.katexMacros(['\\newcommand{\\R}{\\mathbb{R}}\n\\renewcommand\\vec[1]{\\mathbf{#1}}\n' +
    '% \\newcommand{\\hidden}{x}\n\\DeclareMathOperator*{\\argmax}{arg\\,max}']);
  assert.deepEqual({ ...m }, { '\\R': '\\mathbb{R}', '\\vec': '\\mathbf{#1}', '\\argmax': '\\operatorname*{arg\\,max}' });
});

test('a math preview is drawn by KaTeX when it can, without asking the server', async () => {
  const { ctx, calls } = setup();
  await ctx.openWriteDoc('d1');
  const rendered = [];
  ctx.window.katex = { render: (tex, el, opts) => { rendered.push([tex, opts.displayMode, Object.keys(opts.macros)]); el.textContent = 'K'; } };
  const dom = new (ctx.document.createElement('div').constructor)('div');
  await ctx.renderWritePreview({ kind: 'math', tex: '$x^2$' }, dom);
  assert.deepEqual(rendered[0].slice(0, 2), ['x^2', false]);
  assert.equal(dom.children[0].textContent, 'K');
  assert.ok(!calls.some(c => c[0] === '/api/write/preview'));
});

test('a symbol command before a letter gets a space, so \\alpha N does not become \\alphaN', () => {
  const { ctx } = setup();
  assert.deepEqual({ ...ctx.symbolInsert('\\alpha', true, 'N') }, { text: '\\alpha ', cursor: 7 });
  assert.deepEqual({ ...ctx.symbolInsert('\\alpha', true, '^') }, { text: '\\alpha', cursor: 6 });
  assert.deepEqual({ ...ctx.symbolInsert('\\frac{}{}', true, 'x') }, { text: '\\frac{}{}', cursor: 6 });
  assert.deepEqual({ ...ctx.symbolInsert('\\alpha', false, 'N') }, { text: '$\\alpha$', cursor: 8 });
});

/* ---- writing aids round 3: Codex review ---- */

function movableNotes(env, mapped) {  // an editor whose note marks have moved with the text
  env.ctx.W.editor.setNotes = notes => { env.setCalls.push(notes.map(n => n.id + '@' + n.line)); };
  env.ctx.W.editor.noteLines = () => mapped.slice();
}

test('notes arrive with the file, before any edit', async () => {
  const env = notesSetup();
  await env.ctx.openWriteDoc('d1');
  assert.deepEqual(env.ctx.writeNotes().map(n => n.id), ['n1']);
  const fileAt = env.calls.findIndex(c => c[0] === 'GET' && c[1].startsWith('/api/write/file'));
  const notesAt = env.calls.findIndex(c => c[0] === 'GET' && c[1].startsWith('/api/write/notes'));
  assert.ok(notesAt >= 0 && fileAt >= 0);
});

test('a notes reply for text edited meanwhile is not applied', async () => {
  let release;
  const env = setup({ get: url => url.startsWith('/api/write/notes?') ? new Promise(r => { release = r; }) : null });
  const opening = env.ctx.openWriteDoc('d1');
  await new Promise(r => setImmediate(r));
  release({ notes: [] });
  await opening;
  const later = env.ctx.loadWriteNotes();
  env.$('write-cm').children[0].value = 'edited\n' + env.$('write-cm').children[0].value;
  release({ notes: [{ id: 'old', file: 'main.tex', line: 1, anchor: 'x', body: 'stale' }] });
  await later;
  assert.deepEqual(env.ctx.writeNotes().map(n => n.id), []);
});

test('saving one note keeps the other notes where the text moved them', async () => {
  const env = notesSetup(); env.setCalls = [];
  env.files['main.tex'] = { text: 'a\nb\nc\nd\ne\n', hash: 'h0' };
  await env.ctx.openWriteDoc('d1');
  env.ctx.W.doc && (env.ctx.writeNotes().length = 0);
  env.ctx.writeNotes().push({ id: 'n1', line: 2, anchor: 'b', body: 'one' }, { id: 'n2', line: 4, anchor: 'd', body: 'two' });
  movableNotes(env, [{ id: 'n1', line: 3 }, { id: 'n2', line: 5 }]);
  env.$('write-cm').children[0].value = 'new\na\nb\nc\nd\ne\n';
  env.ctx.openWriteNote(3);
  env.$('write-note-text').value = 'one, edited';
  await env.ctx.saveWriteNote();
  const sent = env.calls.find(c => c[0] === '/api/write/note')[1];
  assert.equal(sent.id, 'n1'); assert.equal(sent.line, 3); assert.equal(sent.anchor, 'b');
  assert.deepEqual([...env.setCalls.at(-1)].sort(), ['n1@3', 'n2@5']);
});

test('a note dialog left open follows its line while the text above changes', async () => {
  const env = notesSetup(); env.setCalls = [];
  env.files['main.tex'] = { text: 'a\nb\nc\nd\n', hash: 'h0' };
  await env.ctx.openWriteDoc('d1');
  env.ctx.writeNotes().length = 0;
  movableNotes(env, []);
  env.ctx.openWriteNote(4);  // a new note
  assert.ok(env.setCalls.at(-1).includes('__new@4'));  // a mark that moves with the text
  movableNotes(env, [{ id: '__new', line: 5 }]);
  env.$('write-cm').children[0].value = 'x\na\nb\nc\nd\n';
  env.$('write-note-text').value = 'on d';
  await env.ctx.saveWriteNote();
  const sent = env.calls.find(c => c[0] === '/api/write/note')[1];
  assert.equal(sent.line, 5); assert.equal(sent.anchor, 'd'); assert.equal(sent.id, undefined);
});

test('a changed line text is synced even when the line did not move, and a failed sync is retried', async () => {
  let fail = true;
  const env = notesSetup({ post: () => null });
  env.files['main.tex'] = { text: 'a\nclaim\nclaim\n', hash: 'h0' };
  await env.ctx.openWriteDoc('d1');
  env.ctx.writeNotes().length = 0;
  env.ctx.writeNotes().push({ id: 'n1', line: 2, anchor: 'claim', body: 'x' });
  env.$('write-cm').children[0].value = 'a\nrevised\nclaim\n';
  const realPost = env.ctx.post;
  env.ctx.post = (url, body) => (url === '/api/write/notes/move' && fail ? (env.calls.push([url, body]), Promise.reject(new Error('down'))) : realPost(url, body));
  await env.ctx.syncWriteNotes();
  assert.equal(env.ctx.writeNotes()[0].anchor, 'claim');  // not acknowledged: kept for the next try
  fail = false;
  await env.ctx.syncWriteNotes();
  const moves = env.calls.filter(c => c[0] === '/api/write/notes/move');
  assert.equal(moves.length, 2);
  assert.deepEqual(JSON.parse(JSON.stringify(moves[1][1].notes)), [{ id: 'n1', line: 2, anchor: 'revised' }]);
  assert.equal(env.ctx.writeNotes()[0].anchor, 'revised');
});

test('preview cache keys change with the macros, and figure previews expire', () => {
  const { ctx } = setup();
  const hit = { kind: 'math', tex: '$\\foo$' };
  assert.notEqual(ctx.previewKey('d1', hit, '{"\\\\foo":"a"}'), ctx.previewKey('d1', hit, '{"\\\\foo":"b"}'));
  assert.equal(ctx.previewFresh({ kind: 'math', at: 0 }, 10 ** 9), true);
  assert.equal(ctx.previewFresh({ kind: 'figure', at: 0 }, 5000), true);
  assert.equal(ctx.previewFresh({ kind: 'figure', at: 0 }, 60000), false);
});

/* ---- git sync ---- */

const CONFLICTED = 'Intro.\n<<<<<<< HEAD\nMy sentence.\n=======\nTheir sentence.\n>>>>>>> refs/remotes/origin/master\nMiddle.\n' +
  '<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> refs/remotes/origin/master\nEnd.\n';

test('conflictBlocks finds each block with both sides; resolveConflict keeps mine, theirs or both', () => {
  const { ctx } = setup();
  const blocks = [...ctx.conflictBlocks(CONFLICTED)];
  assert.equal(blocks.length, 2);
  assert.equal(blocks[0].mine, 'My sentence.\n'); assert.equal(blocks[0].theirs, 'Their sentence.\n');
  assert.equal(blocks[0].line, 2); assert.equal(blocks[1].line, 8);
  assert.equal(CONFLICTED.slice(blocks[1].from, blocks[1].to), '<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> refs/remotes/origin/master\n');
  assert.equal(ctx.resolveConflict(CONFLICTED, blocks[0], 'mine').startsWith('Intro.\nMy sentence.\nMiddle.'), true);
  assert.equal(ctx.resolveConflict(CONFLICTED, blocks[0], 'theirs').startsWith('Intro.\nTheir sentence.\nMiddle.'), true);
  assert.equal(ctx.resolveConflict(CONFLICTED, blocks[1], 'both').endsWith('Middle.\nA\nB\nEnd.\n'), true);
  assert.equal(ctx.conflictBlocks('no markers here\n=======\n').length, 0);
});

test('the conflict bar lists the blocks of the open file and resolves one', async () => {
  const { ctx, $ } = setup();
  await ctx.openWriteDoc('d1');
  const area = $('write-cm').children[0];
  area.value = CONFLICTED;
  ctx.showWriteConflicts();
  assert.equal($('write-conflicts').hidden, false);
  assert.match($('write-conflicts-msg').textContent, /2 conflicting/);
  const first = $('write-conflicts-list').children[0];
  first.querySelector = undefined;
  const theirs = first.children.find(b => b.textContent === 'Theirs');
  theirs.onclick();
  assert.equal(area.value.startsWith('Intro.\nTheir sentence.\nMiddle.'), true);
  assert.match($('write-conflicts-msg').textContent, /1 conflicting/);
  $('write-conflicts-list').children[0].children.find(b => b.textContent === 'Mine').onclick();
  assert.equal($('write-conflicts').hidden, true);
});

function gitSetup(status, extra = {}) {
  const env = setup({
    get: url => {
      if (url.startsWith('/api/write/git?')) return status;
      if (url.startsWith('/api/jobs/git1')) return { status: 'done', report: extra.report || { pulled: 1, pushed: 2, conflicts: [], status } };
      if (url.startsWith('/api/jobs/clone1')) return { status: 'done', report: { id: 'd1', name: 'Cloned' } };
      return null;
    },
    post: (url, body) => {
      if (url === '/api/write/git/sync') return { id: 'git1' };
      if (url === '/api/write/git/clone') return { id: 'clone1' };
      if (url === '/api/write/git/connect') return { ...status, repo: true, remote: body.url, branch: 'main' };
      if (url === '/api/write/git/abort') return { ...status, merging: false, conflicts: [] };
      return null;
    }
  });
  return env;
}

const CONNECTED = { repo: true, remote: 'https://ann:***@github.com/ann/paper.git', branch: 'main', changed: ['main.tex'],
  ahead: 1, behind: 0, merging: false, conflicts: [], last_sync: '2026-10-20T10:00:00+00:00' };

test('the git card shows the remote and syncs after saving, then reloads the file', async () => {
  const { ctx, $, calls } = gitSetup(CONNECTED);
  await ctx.openWriteDoc('d1');
  await ctx.showWriteGit();
  assert.equal($('write-git').hidden, false);
  assert.match($('write-git-state').textContent, /github\.com\/ann\/paper\.git · main/);
  assert.match($('write-git-state').textContent, /1 changed file/);
  assert.equal($('write-git-connect-row').hidden, true); assert.equal($('write-git-sync').hidden, false);
  $('write-cm').children[0].value += 'x'; $('write-cm').children[0].fire('input');
  await ctx.syncWriteGit();
  const order = calls.map(c => c[0]).filter(u => u === '/api/write/save' || u === '/api/write/git/sync');
  assert.deepEqual(order, ['/api/write/save', '/api/write/git/sync']);
  assert.match($('write-git-msg').textContent, /1 commit pulled, 2 pushed/);
  assert.ok(calls.filter(c => c[0] === 'GET' && c[1].startsWith('/api/write/file')).length >= 2);  // reloaded
});

test('a sync with conflicts says so and opens the conflict bar', async () => {
  const merging = { ...CONNECTED, merging: true, conflicts: ['main.tex'] };
  const env = gitSetup(CONNECTED, { report: { pulled: 0, pushed: 0, conflicts: ['main.tex'], status: merging } });
  env.files['main.tex'] = { text: CONFLICTED, hash: 'h0' };
  await env.ctx.openWriteDoc('d1');
  await env.ctx.showWriteGit();
  await env.ctx.syncWriteGit();
  assert.match(env.$('write-git-msg').textContent, /Conflicts in main\.tex/);
  assert.equal(env.$('write-conflicts').hidden, false);
  assert.equal(env.$('write-git-abort').hidden, false);
});

test('a document without a remote offers to connect one; cloning opens the new document', async () => {
  const { ctx, $, calls } = gitSetup({ repo: false, remote: null, branch: null, changed: [], ahead: 0, behind: 0,
    merging: false, conflicts: [], last_sync: null });
  await ctx.openWriteDoc('d1');
  await ctx.showWriteGit();
  assert.equal($('write-git-connect-row').hidden, false); assert.equal($('write-git-sync').hidden, true);
  $('write-git-url').value = '  git@github.com:ann/paper.git ';
  await ctx.connectWriteGit();
  assert.equal(calls.find(c => c[0] === '/api/write/git/connect')[1].url, 'git@github.com:ann/paper.git');
  ctx.window.prompt = () => 'https://git.overleaf.com/abc123';
  await ctx.cloneWriteGit();
  const clone = calls.find(c => c[0] === '/api/write/git/clone')[1];
  assert.deepEqual({ ...clone }, { project: 'demo', name: 'abc123', url: 'https://git.overleaf.com/abc123' });
  assert.ok(calls.some(c => c[0] === 'GET' && c[1].startsWith('/api/write/doc?doc=d1')));
});
