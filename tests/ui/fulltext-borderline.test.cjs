const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8')
  .split('/* ---------- Full-text selection ---------- */')[1]?.split('/* ---------- End full-text selection ---------- */')[0] || '';

function setup(screened, saved = null) {
  const elements = {}, calls = [], stored = [];
  const ctx = vm.createContext({
    $: id => elements[id] ||= { value: '100', checked: false },
    state: { data: { stats: { screened } } },
    store: { get: () => saved, set: (key, value) => { saved = value; stored.push([key, value]); } },
    saveCriteria: async () => {},
    startLensTask: (...args) => calls.push(args),
    toast: message => { throw Error(message); }
  });
  vm.runInContext(source, ctx);
  return { ctx, elements, calls, stored };
}

test('full-text screening sends the borderline selection and remembers reviewer changes', async () => {
  const { ctx, elements, calls, stored } = setup(51);
  ctx.wireFulltextScreening();
  assert.equal(elements['lens-screen-borderline'].checked, true);
  await elements['lens-screen-fulltext'].onclick();
  assert.equal(calls[0][0], 'screen_fulltext');
  assert.equal(calls[0][1], '/api/screen-fulltext');
  assert.equal(calls[0][2].only, 'borderline');
  elements['lens-screen-borderline'].checked = false;
  elements['lens-screen-borderline'].onchange();
  ctx.state.data.stats.screened = 100;
  ctx.updateFulltextSelection();
  await elements['lens-screen-fulltext'].onclick();
  assert.equal(calls[1][2].only, 'all');
  assert.deepEqual(stored, [['screen-borderline', false]]);
});

test('borderline default follows S1 count until changed, including restored choices', () => {
  const { ctx, elements } = setup(50);
  ctx.wireFulltextScreening();
  assert.equal(elements['lens-screen-borderline'].checked, false);
  ctx.state.data.stats.screened = 51;
  ctx.updateFulltextSelection();
  assert.equal(elements['lens-screen-borderline'].checked, true);
  const restored = setup(1, true);
  restored.ctx.wireFulltextScreening();
  assert.equal(restored.elements['lens-screen-borderline'].checked, true);
});
