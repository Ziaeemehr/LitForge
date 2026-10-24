const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8');
const source = app.slice(app.indexOf("    $('export-scope').value = store.get"), app.indexOf("    $('bulk-top').onclick"));

test('Markdown export sends and remembers the Obsidian option', async () => {
  const elements = {}, calls = [], saved = {};
  const $ = id => elements[id] ||= { value: '', checked: false };
  const ctx = vm.createContext({ $, state: { project: 'Review' },
    store: { get: (key, fallback) => key === 'export-obsidian' ? true : fallback,
      set: (key, value) => { saved[key] = value; } },
    scopeWords: () => 'all', exportSelection: () => ({ only: 'all' }),
    post: async (url, body) => { calls.push([url, body]); return { papers: 2, path: '/export' }; },
    toast() {}, loadProjects() {}, Object });
  vm.runInContext(source, ctx);
  assert.equal($('export-obsidian').checked, true);
  for (const enabled of [true, false]) {
    $('export-obsidian').checked = enabled;
    $('export-obsidian').onchange();
    assert.equal(saved['export-obsidian'], enabled);
    $('btn-export').onclick();
    await new Promise(setImmediate);
    assert.equal(calls.at(-1)[0], '/api/export');
    assert.equal(calls.at(-1)[1].obsidian, enabled);
  }
  const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');
  assert.match(html, /title="[^"]*tags[^"]*aliases[^"]*\[\[links\]\][^"]*"><input type="checkbox" id="export-obsidian"/);
});

test('Add-PDF export selection carries the Obsidian option', () => {
  const start = app.indexOf('  function exportSelection()');
  const selection = app.slice(start, app.indexOf('\n  // Upload', start));
  const elements = { 'export-scope': { value: 'all' }, 'export-obsidian': { checked: true } };
  const ctx = vm.createContext({ $: id => elements[id], state: { data: {}, view: [{ id: 'a' }] } });
  vm.runInContext(selection, ctx);
  assert.equal(ctx.exportSelection().obsidian, true);
  elements['export-scope'].value = 'visible';
  assert.equal(ctx.exportSelection().obsidian, true);
  assert.deepEqual(Array.from(ctx.exportSelection().paper_ids), ['a']);
});
