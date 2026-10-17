/* Run with node --test tests/ui/lens.test.cjs. No browser or packages needed. */
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8')
  .split('/* ---------- Evidence Lens ---------- */')[1].split('  function init()')[0];

function setup() {
  class Element {
    constructor(tag = 'div') {
      this.tagName = tag.toUpperCase(); this.children = []; this.dataset = {}; this.attrs = {};
      this.value = ''; this.checked = false; this.textContent = '';
      this.classList = { toggle() {} };
    }
    appendChild(el) { this.children.push(el); return el; }
    replaceChildren(...els) { this.children = els; }
    setAttribute(key, value) { this.attrs[key] = value; }
    querySelectorAll() { return []; }
    focus() { this.focused = true; }
    click() { this.onclick(); }
  }
  const elements = {};
  const $ = id => elements[id] ||= new Element();
  const tabs = ['answer', 'evidence', 'review'].map(name => {
    const tab = new Element('button'); tab.dataset.lensTab = name; return tab;
  });
  const calls = [];
  const answer = { run_id: 'run', question: 'Question?', model: 'qwen3:4b', provider: 'ollama',
    sentences: [{ id: 's', kind: 'reported_fact', text: 'Answer.', evidence_ids: [], flags: [], review_status: 'open' }],
    evidence: [], invalid_citations: [], flags: [] };
  const profiles = [{ name: 'local', provider: 'ollama', model: 'qwen3:4b', remote: false },
    { name: 'paid', provider: 'remote', model: 'cloud-model', remote: true }];
  const ctx = vm.createContext({ $, state: { project: 'demo' }, store: { set() {} }, setTimeout,
    toast(message) { throw Error(message); },
    document: { createElement: tag => new Element(tag), querySelectorAll: selector => selector === '[data-lens-tab]' ? tabs : [] },
    api: async path => path === '/api/model-profiles' ? profiles : path.startsWith('/api/jobs/') ?
      { status: 'done', run_id: 'run', report: answer } : answer,
    post: async (path, body) => {
      calls.push({ path, body });
      return path === '/api/preview' ? { blocked_reasons: [], remote: false, requires_approval: false } : { id: 'job' };
    }
  });
  vm.runInContext(source, ctx);
  return { ctx, $, tabs, calls, answer };
}
const flush = async () => { for (let i = 0; i < 5; i++) await new Promise(setImmediate); };

test('badges use readable labels, severity and hide open review status', () => {
  const { ctx, $ } = setup();
  ctx.lensBadges($('result'), ['REPORTED_FACT', 'NOT_REPORTED', 'OPEN', 'SENTENCE', 'partial', 'supported', 'accepted'],
    ['NUMBER_NOT_IN_EVIDENCE', 'uncited_value', 'option_mismatch', 'type_mismatch', 'evidence_changed_since_review']);
  const badges = $('result').children[0].children;
  assert.deepEqual(badges.map(b => b.textContent), ['Reported', 'Not reported', 'Sentence', 'Partly supported',
    'Supported', 'Accepted', 'Number not in evidence', 'No citation', 'Option not allowed', 'Type mismatch',
    'Evidence changed since review']);
  assert.equal(badges[3].className, 'badge warning');
  assert.equal(badges[4].className, 'badge ok');
  assert.ok(badges.slice(6).every(b => b.className === 'badge warning'));
});

test('profile dropdowns show cloud labels and tabs support selection and keyboard navigation', async () => {
  const { ctx, $, tabs } = setup();
  ctx.initLens(); await flush();
  assert.equal($('lens-profile').children[1].textContent, 'paid · cloud-model (cloud)');
  assert.equal($('lens-matrix-profile').children[0].value, 'local');
  tabs[0].click();
  assert.equal(tabs[0].attrs['aria-selected'], 'true');
  tabs[0].onkeydown({ key: 'ArrowRight', preventDefault() {} });
  assert.equal(tabs[0].attrs['aria-selected'], 'false');
  assert.equal(tabs[1].attrs['aria-selected'], 'true');
  assert.equal(tabs[1].tabIndex, 0);
  assert.equal($('lens-answer').attrs['aria-labelledby'], 'lens-tab-evidence');
  assert.ok(tabs[1].focused);
});

for (const enabled of [true, false]) {
  test(`answer automatically audits only when checked (${enabled}) and retains manual audit`, async () => {
    const { ctx, $, calls } = setup();
    ctx.initLens(); await flush();
    $('lens-auto-audit').checked = enabled;
    $('lens-question').value = 'Question?';
    $('lens-ask-form').onsubmit({ preventDefault() {} });
    await flush();
    assert.deepEqual(calls.filter(c => c.path !== '/api/preview').map(c => c.path),
      enabled ? ['/api/ask', '/api/audit'] : ['/api/ask']);
    assert.equal($('lens-ask-status').textContent, '1 sentences · 0 evidence spans · qwen3:4b (local)');
    assert.equal($('lens-run-audit').disabled, false);
    $('lens-run-audit').click(); await flush();
    assert.equal(calls.at(-1).path, '/api/audit');
  });
}

test('cloud audit requires its own preview confirmation', async () => {
  const { ctx, $, calls } = setup();
  let previews = 0, confirmations = 0;
  ctx.post = async (path, body) => {
    calls.push({ path, body });
    return path === '/api/preview' ? { blocked_reasons: [], remote: ++previews === 2, requires_approval: false } : { id: 'job' };
  };
  ctx.confirmLensPreview = async () => { confirmations++; return false; };
  ctx.startLensTask('ask', '/api/ask', {}, 'lens-ask-status', true);
  await flush();
  assert.equal(confirmations, 1);
  assert.equal(calls.filter(c => c.path === '/api/audit').length, 0);
  assert.equal($('lens-run-audit').disabled, false);
});
