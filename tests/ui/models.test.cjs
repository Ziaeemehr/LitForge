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
  }
  const elements = {}, calls = [];
  const $ = id => elements[id] ||= new Element();
  const ctx = vm.createContext({ $, document: { createElement: () => new Element() },
    window: { confirm: () => confirmed }, setTimeout, toast() {}, refreshModelChoices() {},
    api: async () => ({ hardware: { os: 'Fake', ram_gb: 24 }, tier: 'large', profiles: [
      { name: '<script>bad</script>', model: 'model', provider: 'api', remote: true }],
      proposal: { tier: 'large', available: true, download_gb: 20, memory_gb: 24, notes: 'First use downloads' },
      providers: ['openai'], server: { running: false, models: [] }, tasks: {}, vision_enabled: false }),
    post: async (path, body) => { calls.push({ path, body }); return { token: 'reviewed', diff: 'Exact file diff', notice: 'Evidence leaves machine' }; }
  });
  vm.runInContext(source, ctx);
  return { ctx, $, calls };
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

test('Models tab: cloud choices only for Cloud-light, readable task names', async () => {
  const { ctx, $ } = setup();
  await ctx.loadModels(); await flush();
  $('models-tier').value = 'large'; ctx.showCloudPickers();
  assert.equal($('cloud-pickers').hidden, true);
  $('models-tier').value = 'cloud'; ctx.showCloudPickers();
  assert.equal($('cloud-pickers').hidden, false);
  assert.equal(ctx.taskLabel('write_cite'), 'Write: suggest citations');
  assert.equal(ctx.taskLabel('some_new_task'), 'some new task');
});
