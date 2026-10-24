const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8')
  .split('/* ---------- Search strings ---------- */')[1]?.split('/* ---------- End search strings ---------- */')[0] || '';
function setup(clipboard = true) {
  const elements = {}, calls = [], messages = [], copied = [];
  function element() { return { children: [], replaceChildren() { this.children = []; },
    appendChild(el) { this.children.push(el); }, value: '' }; }
  const $ = id => elements[id] ||= element();
  const ctx = vm.createContext({ $, state: { project: 'Review', criteriaFor: 'Review' },
    document: { createElement: element, querySelectorAll: () => [{value: 'pubmed'}, {value: 'openalex'}] },
    navigator: { clipboard: clipboard ? { writeText: async text => copied.push(text) } : undefined },
    toast: text => messages.push(text), common: () => ({project: 'Review', export_dir: '/exports'}),
    setBusy() {}, poll: job => calls.push(['poll', job]), saveCriteria: async () => {},
    post: async (url, body) => { calls.push([url, body]); return { id: 'job' }; },
    api: async () => ({}), confirmLensPreview: async () => true });
  vm.runInContext(source, ctx);
  ctx.wireSearchStrategy();
  ctx.renderSearchStrategy({queries: {pubmed: 'word[tiab]', openalex: 'word'}, warnings: [], openalex_queries: ['word one', 'word two']}, 'Review');
  return {ctx, $, calls, copied, messages};
}
test('Copy uses clipboard and OpenAlex Search reuses the search job flow', async () => {
  const { $, calls, copied } = setup();
  await $('strategy-pubmed-copy').onclick();
  assert.deepEqual(copied, ['word[tiab]']);
  await $('strategy-search').onclick();
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0])), ['/api/search', {
    project: 'Review', export_dir: '/exports', queries: ['word one', 'word two'], sources: ['openalex']
  }]);
  assert.equal(calls[1][0], 'poll');
});
test('Copy without clipboard selects text and gives a fallback toast', async () => {
  const { $, messages } = setup(false);
  let selected = false;
  $('strategy-pubmed').select = () => { selected = true; };
  $('strategy-pubmed').focus = () => {};
  await $('strategy-pubmed-copy').onclick();
  assert.ok(selected);
  assert.match(messages.at(-1), /copy.*manually/i);
});
test('stale project results cannot render or start a search', async () => {
  const {ctx, $, calls} = setup();
  ctx.state.project = 'Other';
  ctx.renderSearchStrategy({queries: {pubmed: 'stale'}, warnings: []}, 'Review');
  assert.equal($('strategy-pubmed').value, 'word[tiab]');
  await $('strategy-search').onclick();
  assert.equal(calls.length, 0);
});
test('static strategy card belongs inside screening after actions', () => {
  const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');
  const panel = html.split('id="screen-panel"')[1].split('</details>')[0];
  assert.match(panel, /class="screen-actions"[\s\S]*id="search-strategy"/);
  assert.match(panel, /Search strings/);
  for (const db of ['pubmed', 'scopus', 'openalex']) assert.match(panel, new RegExp('value="' + db + '" checked'));
});

test('saving changed criteria invalidates and reloads the displayed strategy', async () => {
  const {ctx, $, calls} = setup();
  let lookups = 0;
  ctx.api = async () => { lookups++; const error = new Error('Needs model'); error.preview = {}; throw error; };
  await ctx.searchStrategyCriteriaSaved();
  assert.equal(lookups, 1);
  assert.equal($('strategy-pubmed-row').hidden, true);
  await $('strategy-search').onclick();
  assert.equal(calls.length, 0);
  const app = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8');
  const save = app.split('function saveCriteria()')[1].split('function renderYears')[0];
  assert.match(save, /searchStrategyCriteriaSaved/);
});

test('Build saves criteria and retries only after approval of the returned preview', async () => {
  const { ctx, $, calls } = setup();
  let saved = false, approved = false;
  ctx.saveCriteria = async () => { saved = true; };
  ctx.confirmLensPreview = async plan => { assert.equal(plan.models[0], 'cloud'); approved = true; return true; };
  ctx.post = async (url, body) => {
    assert.ok(saved);
    calls.push([url, JSON.parse(JSON.stringify(body))]);
    if (!body.approved) {
      const error = new Error('Approval required');
      error.preview = { requires_approval: true, remote: true, model: 'cloud', profile: 'cloud', provider: 'test' };
      throw error;
    }
    assert.ok(approved);
    return {queries: {pubmed: 'new[tiab]'}, warnings: [], openalex_queries: []};
  };
  await $('strategy-build').onclick();
  assert.equal(calls.length, 2);
  assert.equal(calls[0][1].approved, false);
  assert.equal(calls[1][1].approved, true);
  assert.equal($('strategy-pubmed').value, 'new[tiab]');
  assert.equal($('strategy-build').disabled, false);
});

test('strategy controls use database names and specific accessible Copy labels', () => {
  const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');
  const card = html.split('id="search-strategy"')[1].split('</details>')[0];
  for (const [id, name] of Object.entries({pubmed: 'PubMed', scopus: 'Scopus', wos: 'Web of Science', arxiv: 'arXiv', openalex: 'OpenAlex'})) {
    assert.ok(card.includes(`<label class="check"><input type="checkbox" name="strategy-db" value="${id}"${['pubmed', 'scopus', 'openalex'].includes(id) ? ' checked' : ''}> ${name}</label>`));
    assert.ok(card.includes(`<label for="strategy-${id}">${name}</label>`));
    assert.ok(card.includes(`id="strategy-${id}-copy" title="Copy the ${name} string" aria-label="Copy the ${name} string"`));
  }
  assert.doesNotMatch(card, /\sstyle=/);
});
