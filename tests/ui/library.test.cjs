const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8');
const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');
function setup(confirm = true) {
  const elements = {}, calls = [], messages = [];
  const $ = id => elements[id] ||= { value: '', innerHTML: '', textContent: '', querySelectorAll: () => [] };
  const ctx = vm.createContext({ $, state: { library: [
    { id: 'p', title: '<bad>', authors: [], projects: [], has_pdf: true, unused: true },
    { id: 'q', title: 'Used', authors: [], projects: ['Demo'], unused: false }
  ] }, esc: s => String(s).replaceAll('<', '&lt;'),
    api: async url => { calls.push(['GET', url]); return url === '/api/library' ? [] : {
      paper_count: 1, pdf_count: 1, parsed_folder_count: 1, bytes_freed: 2000000,
      papers: [{ id: 'p' }], refused: [], file_errors: [] }; },
    post: async (url, body) => { calls.push(['POST', url, JSON.parse(JSON.stringify(body))]);
      return { removed: ['p'], refused: [], file_errors: [], bytes_freed: 2000000 }; },
    window: { confirm: msg => { messages.push(msg); return confirm; } }, closeDrawer() {},
    toast: msg => messages.push(msg)
  });
  vm.runInContext(app.slice(app.indexOf('  function loadLibrary()'), app.indexOf('  // "+ New"')), ctx);
  return { ctx, $, calls, messages };
}
test('Library chip, unused filter, and bulk removal control', () => {
  const { ctx, $ } = setup();
  ctx.renderLibrary();
  assert.match($('library-list').innerHTML, /Not in a project/);
  assert.match($('library-list').innerHTML, /&lt;bad>/);
  ctx.state.libraryUnused = true;
  ctx.renderLibrary();
  assert.doesNotMatch($('library-list').innerHTML, />Used</);
  assert.match(html, /id="library-unused"[^>]*>Unused only/);
  assert.match(html, /id="library-remove-unused"[^>]*>Remove unused papers…/);
  assert.match(app, /d.unused[\s\S]*Remove from library/);
  assert.match(app, /btn-remove-paper[^\n]*onclick[^\n]*removeLibraryPapers\(\[d.id\]\)/);
});
test('Removal dialog shows preview counts, irreversible warning, posts preview IDs and refreshes', async () => {
  const { ctx, calls, messages } = setup();
  await ctx.removeLibraryPapers();
  assert.match(messages[0], /1 papers/);
  assert.match(messages[0], /1 PDFs/);
  assert.match(messages[0], /2.00 MB/);
  assert.match(messages[0], /irreversible/i);
  assert.deepEqual(calls[1], ['POST', '/api/library/remove', { paper_ids: ['p'] }]);
  assert.equal(calls[2][1], '/api/library');
  assert.match(messages[1], /Removed 1/);
});
test('Cancel leaves the library untouched; per-paper preview selects the ID', async () => {
  const { ctx, calls } = setup(false);
  await ctx.removeLibraryPapers(['p']);
  assert.match(calls[0][1], /paper_ids=p/);
  assert.equal(calls.length, 1);
});
test('Drawer offers removal only for unused papers and selects that paper', async () => {
  for (const unused of [true, false]) {
    const { ctx, $ } = setup(false);
    ctx.drawerRequest = 0;
    $('drawer').classList = { add() {} };
    $('drawer').setAttribute = () => {};
    ctx.paperLinks = () => '';
    ctx.renderPaperAssets = ctx.lensScreeningDetail = ctx.attachReproTabs = () => {};
    ctx.window.renderMarkdown = () => {};
    ctx.api = async () => ({ id: 'p', unused, authors: [], assets: [] });
    vm.runInContext(app.slice(app.indexOf('  function openDrawer('), app.indexOf('  function closeDrawer(')), ctx);
    ctx.openDrawer({ id: 'p', title: '<bad>' });
    await new Promise(resolve => setImmediate(resolve));
    if (unused) {
      assert.match($('dhead').innerHTML, /Remove from library/);
      let selected;
      ctx.removeLibraryPapers = ids => { selected = Array.from(ids); };
      $('btn-remove-paper').onclick();
      assert.deepEqual(selected, ['p']);
    } else {
      assert.doesNotMatch($('dhead').innerHTML, /Remove from library/);
    }
  }
});

test('Drawer displays the accurate-parser status and installation hint', async () => {
  const { ctx, $ } = setup();
  const children = [];
  ctx.drawerRequest = 0;
  $('drawer').classList = { add() {} };
  $('drawer').setAttribute = () => {};
  $('dhead').appendChild = node => children.push(node);
  ctx.document = { createElement: () => ({ setAttribute() {} }) };
  ctx.paperLinks = () => '';
  ctx.renderPaperAssets = ctx.lensScreeningDetail = ctx.attachReproTabs = () => {};
  ctx.window.renderMarkdown = () => {};
  ctx.mineruEstimate = () => 'about 1 min';
  ctx.api = async () => ({ id: 'p', authors: [], assets: [], pdf_url: '/pdf',
    parse_status_label: 'needs accurate parser',
    parse_status_hint: 'install with `litforge parse install-mineru`' });
  vm.runInContext(app.slice(app.indexOf('  function openDrawer('), app.indexOf('  function closeDrawer(')), ctx);
  ctx.openDrawer({ id: 'p', title: 'Paper' });
  await new Promise(resolve => setImmediate(resolve));
  const status = children.find(node => node.id === 'mineru-paper-status');
  assert.match(status.textContent, /needs accurate parser.*install with `litforge parse install-mineru`/);
  assert.doesNotMatch(status.textContent, /needs_docling/);
});
