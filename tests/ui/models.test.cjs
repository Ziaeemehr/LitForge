const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8')
  .split('/* ---------- Models settings ---------- */')[1]?.split('/* ---------- End Models settings ---------- */')[0] || '';

function setup(confirmed = true) {
  class Element {
    constructor() { this.children = []; this.value = ''; this.checked = false; this.textContent = ''; }
    appendChild(e) { this.children.push(e); return e; }
    replaceChildren(...e) { this.children = e; }
    set innerHTML(value) { throw Error('Unsafe innerHTML'); }
    querySelector() { return null; }
  }
  const elements = {}, calls = [];
  const $ = id => elements[id] ||= new Element();
  const radios = ['local', 'cloud', 'hybrid', 'manual'].map(value => ({ value, checked: false }));
  const ctx = vm.createContext({ $, radios, document: { createElement: () => new Element(),
      querySelector: sel => (sel.includes(':checked') ? radios.find(r => r.checked) || null : null),
      querySelectorAll: sel => (sel.includes('models-mode') ? radios : []) },
    window: { confirm: () => confirmed }, setTimeout, toast() {}, refreshModelChoices() {},
    api: async () => ({ hardware: { os: 'Fake', ram_gb: 24 }, tier: 'large', profiles: [
      { name: '<script>bad</script>', model: 'model', provider: 'api', remote: true }],
      proposal: { tier: 'large', available: true, download_gb: 20, memory_gb: 24, notes: 'First use downloads' },
      providers: ['openai'], server: { running: false, models: [] }, tasks: {}, vision_enabled: false }),
    post: async (path, body) => { calls.push({ path, body }); return { token: 'reviewed', diff: 'Exact file diff', notice: 'Evidence leaves machine' }; }
  });
  vm.runInContext(source, ctx);
  return { ctx, $, calls, radios };
}
const flush = async () => { for (let i = 0; i < 8; i++) await new Promise(setImmediate); };

test('Models settings render untrusted profiles with textContent and explain disabled images', async () => {
  const { ctx, $ } = setup();
  assert.equal(typeof ctx.loadModels, 'function');
  ctx.loadModels(); await flush();
  const label = $('models-profiles').children[0].children[0];  // name, then model and kind
  assert.match(label.children.map(c => c.textContent).join(' '), /<script>bad<\/script>/);
  assert.match($('models-vision').textContent, /off/i);
});

test('Apply sends only a confirmed preview token', async () => {
  const { ctx, calls } = setup();
  ctx.reviewModelsChange({ action: 'setup' }); await flush();
  assert.equal(calls.length, 2);
  assert.equal(calls[0].path, '/api/models/preview');
  assert.equal(calls[1].path, '/api/models/apply');
  assert.equal(calls[1].body.confirmed, true);
  assert.equal(calls[1].body.token, 'reviewed');
});

test('Cancelling file confirmation never writes', async () => {
  const { ctx, calls } = setup(false);
  ctx.reviewModelsChange({ action: 'cloud', enabled: true }); await flush();
  assert.equal(calls.length, 1);
});

test('Models hardware, status, provider labels and task checkboxes are readable', async () => {
  const { ctx, $ } = setup();
  ctx.api = async () => ({ hardware: { os: 'Darwin', ram_gb: 36,
    llama_version: 'version: 11429 (abcdef)\nbuilt with Apple clang for arm64' },
    profiles: [], proposal: {}, providers: ['openrouter', 'openai', 'anthropic', 'google', 'deepseek', 'qwen', 'kimi'],
    task_names: ['ask', 'screen_fulltext'], tasks: {}, server: { running: true, owned: true,
      preset_models: ['gemma4', 'qwen3-4b'], models: [
        { id: 'gemma4', status: { value: 'loaded', args: ['verbose'] } },
        { id: 'qwen3-4b', status: 'unloaded' }, { id: 'hf-cache-extra', status: 'loaded' }] } });
  await ctx.loadModels();
  assert.match($('models-hardware').textContent, /llama.cpp build 11429/);
  assert.doesNotMatch($('models-hardware').textContent, /clang|abcdef/);
  assert.equal($('models-server').textContent, 'Model server running · gemma4 loaded · qwen3-4b not loaded');
  assert.equal($('models-provider').children[0].textContent,
    'OpenRouter — one key for GPT, Claude, Gemini, DeepSeek, Qwen, Kimi');
  assert.equal($('models-provider').children[1].textContent, 'OpenAI (GPT)');
  assert.equal($('models-provider-models').href, 'https://openrouter.ai/models');
  const labels = $('models-add-tasks').children;
  assert.equal(labels.length, 2);
  assert.equal(labels[1].children[0].type, 'checkbox');
  assert.equal(labels[1].children[0].value, 'screen_fulltext');
  assert.equal(labels[1].children[1].textContent, 'Full-text screening');
});

test('Models form uses full width fields and a task checkbox group', () => {
  const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');
  const css = fs.readFileSync('src/litforge/ui/static/app.css', 'utf8');
  assert.match(html, /<fieldset[^>]*id="models-add-tasks"/);
  assert.match(html, /id="models-provider-models"/);
  assert.match(css, /#models-add input[^}]*width:\s*100%/);
});

test('Models add submits selected task IDs and updates provider model link', async () => {
  const { ctx, $, calls } = setup(false);
  await ctx.loadModels();
  $('models-provider').value = 'google';
  $('models-provider').onchange();
  assert.equal($('models-provider-models').href, 'https://ai.google.dev/gemini-api/docs/models');
  $('models-id').value = 'example-model';
  $('models-add-tasks').querySelectorAll = selector => {
    assert.equal(selector, 'input:checked');
    return [{ value: 'ask' }, { value: 'screen_fulltext' }];
  };
  ctx.initModels();
  $('models-add').onsubmit({ preventDefault() {} });
  await flush();
  assert.equal(calls[0].body.provider, 'google');
  assert.deepEqual(Array.from(calls[0].body.tasks), ['ask', 'screen_fulltext']);
});

test('Hardware uses the explicit build in semantic version output', async () => {
  const { ctx, $ } = setup();
  ctx.api = async () => ({ hardware: { os: 'Darwin', ram_gb: 36,
    llama_version: 'version: 0.6.0 (build 11429, commit d81235049)' },
    profiles: [], proposal: {}, providers: [], tasks: {}, server: {} });
  await ctx.loadModels();
  assert.match($('models-hardware').textContent, /llama.cpp build 11429/);
});

test('Models tab: cloud choices only for the Cloud and Hybrid modes, readable task names', async () => {
  const { ctx, $, radios } = setup();
  await ctx.loadModels(); await flush();
  const pick = mode => { radios.forEach(r => { r.checked = r.value === mode; }); ctx.showCloudPickers(); };
  pick('local'); assert.equal($('cloud-pickers').hidden, true);
  pick('manual'); assert.equal($('cloud-pickers').hidden, true);
  pick('cloud'); assert.equal($('cloud-pickers').hidden, false);
  pick('hybrid'); assert.equal($('cloud-pickers').hidden, false);
  assert.equal(ctx.taskLabel('write_cite'), 'Write: suggest citations');
  assert.equal(ctx.taskLabel('some_new_task'), 'some new task');
});

test('a model test reads as one short line next to the model', () => {
  const { ctx } = setup();
  assert.equal(ctx.testResult({ status: 'done', report: { reachable: true, valid: true, latency_ms: 840 } }), '✓ Works · 0.8 s');
  assert.match(ctx.testResult({ status: 'done', report: { reachable: true, valid: false, latency_ms: 100 } }), /^⚠ Answers, but not/);
  assert.equal(ctx.testResult({ status: 'failed', error: 'connection refused\ntrace…' }), '✗ connection refused');
});

test('Models tab: the saved mode is selected, an older setup reads as its mode, Apply sends the mode', async () => {
  const { ctx, $, calls, radios } = setup();
  await ctx.loadModels(); await new Promise(setImmediate);
  assert.equal(radios.find(r => r.checked).value, 'local');  // tier "large" from before modes existed
  assert.match($('models-current').textContent, /^Now: Local \(set up before modes existed\)/);
  radios.forEach(r => { r.checked = r.value === 'hybrid'; });
  ctx.showCloudPickers();
  assert.equal($('cloud-pickers').hidden, false);
  assert.match($('cloud-strong-label').textContent, /questions, analysis and writing$/);
  ctx.initModels();
  $('cloud-strong').value = 'fixture/strong';
  $('models-apply').onclick();
  await new Promise(setImmediate);
  const sent = calls.find(c => c.path === '/api/models/preview').body;
  assert.equal(sent.mode, 'hybrid'); assert.equal(sent.strong, 'fixture/strong');
});

test('Math: high names its cloud cost', () => {
  const { ctx, $ } = setup();
  const high = { value: 'high', textContent: 'Math: high (best)', title: '' };
  $('export-math').querySelector = () => high;
  ctx.showMathCost({ model: 'vendor/vision', usd_per_page: 0.0123 });
  assert.equal(high.textContent, 'Math: high (cloud, ~$0.0123/page)');
  assert.match(high.title, /vendor\/vision/);
  ctx.showMathCost({ model: 'vendor/vision', usd_per_page: 0.0123, enabled: false });
  assert.equal(high.textContent, 'Math: high (cloud, ~$0.0123/page, enable cloud first)');
  ctx.showMathCost(null);
  assert.equal(high.textContent, 'Math: high (best)');
});
