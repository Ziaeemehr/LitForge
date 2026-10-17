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

test('phase 2 profiles and severity badges use readable labels', async () => {
  const { ctx, $ } = setup();
  ctx.initLens(); await flush();
  assert.equal($('lens-claims-profile').children[1].textContent, 'paid · cloud-model (cloud)');
  assert.equal($('lens-screen-profile').children[0].value, 'local');
  ctx.lensBadges($('badges'), ['single_source', 'replicates', 'contradicts'], ['unsupported_hypothesis']);
  const badges = $('badges').children[0].children;
  assert.deepEqual(badges.map(b => b.textContent), ['Single-source', 'Replicated', 'Contradicted', 'Unsupported hypothesis']);
  assert.deepEqual(badges.map(b => b.className), ['badge warning', 'badge ok', 'badge warning', 'badge warning']);
});

test('Claims render untrusted text, evidence, relation hypotheses and review actions', async () => {
  const { ctx, $, calls } = setup();
  const text = '<img src=x onerror=alert(1)>';
  ctx.api = async () => ({
    papers: { p: { title: text, year: 2020 }, q: { title: "Later title", year: 2022 } },
    clusters: [{ cluster_id: 'cluster', label: text, single_source: false, contradicting_papers: 1,
      replication_count: 2, papers: ['p', 'q'], strength: 0, cited_by_count: 99,
      origin_paper_id: 'p', later_citing_papers: ['q'] }],
    claims: [{ id: 'claim', paper_id: 'p', text, type: 'finding', polarity: 'positive', status: 'proposed', evidence_ids: ['ev'] }],
    evidence: [{ id: 'ev', paper_id: 'p', title: text, text, section: 'Results', page: 2, parser: 'fake', parser_version: '1' }],
    relations: [{ source_claim_id: 'claim', target_claim_id: 'claim', relation: 'contradicts', status: 'proposed', explanation: text,
      causes: [{ factor: 'dataset', detail: text, evidence_ids: [], unsupported_hypothesis: true }] }]
  });
  await ctx.loadLensClaims();
  const list = $('lens-claims-clusters');
  assert.equal(list.children[0].children[0].textContent, text);
  assert.ok(list.children[0].children.some(el => el.textContent.includes('Origin: ' + text + ' (2020)')));
  assert.ok(list.children[0].children.some(el => el.textContent.includes('Later title (2022)')));
  const detail = $('lens-claims-detail');
  assert.equal(detail.children[0].children[0].textContent, text);
  assert.equal(detail.children[0].children[1].textContent, text + ' (2020) · Finding · Positive');
  assert.ok(detail.children[1].children[0].textContent.includes(text + ' (2020)'));
  assert.equal(detail.children[0].children[3].children[1].children[2].textContent, text);
  const hypothesis = detail.children[1].children[3];
  assert.equal(hypothesis.children[0].children[0].textContent, 'Hypothesis');
  assert.equal(hypothesis.children[0].children[1].className, 'badge warning');
  detail.children[0].children[4].children[0].click(); await flush();
  assert.equal(calls[0].path, '/api/claims/review');
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].body)), { id: 'claim', action: 'accept' });
});

test('S2 badges and queue predictions keep untrusted tooltip text inert', () => {
  const { ctx, $ } = setup();
  const reason = '<script>bad()</script>';
  ctx.lensResultBadges($('badges'), { fulltext_screening: { decision: 'uncertain', result: { reason } },
    review_queue: { info: .8, reason, predicted_relevant: .75 } });
  assert.equal($('badges').children[0].textContent, 'S2 · Uncertain');
  assert.equal($('badges').children[0].className, 'badge warning');
  assert.equal($('badges').children[1].title, reason);
  assert.equal($('badges').children[2].textContent, 'Nearby marks: 75% relevant');
});


test('Claims uses the shared view header', () => {
  const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');
  assert.match(html, /id="view-claims"[^>]*>\s*<div class="viewhead"><h2>Claims<\/h2><span class="meta">/);
});
