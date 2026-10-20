const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8');
const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');
function setup() {
  class Element {
    constructor() { this.children = []; this.value = ''; this.textContent = ''; this.checked = false; }
    appendChild(e) { this.children.push(e); return e; }
    replaceChildren(...e) { this.children = e; }
    set innerHTML(value) { throw Error('Unsafe innerHTML'); }
    querySelectorAll() { return []; }
  }
  const elements = {}, calls = [];
  const $ = id => elements[id] ||= new Element();
  const ctx = vm.createContext({ $, document: { createElement: () => new Element(), querySelectorAll: () => [] }, setTimeout,
    window: { confirm: () => true }, api: async () => ({ keys: [], available: true }),
    post: async (url, body) => { calls.push([url, body]); return {}; },
    modelButton: (label, action) => { const b = new Element(); b.textContent = label; b.onclick = action; return b; },
    modelsInfo: {}, modelTaskLabels: {}, loadModels: async () => {}, refreshModelChoices() {},
    reviewModelsChange() {}, toast() {}, state: {}, showPaths() {}, showView() {}
  });
  vm.runInContext(app.split('/* ---------- Settings ---------- */')[1].split('/* ---------- End Settings ---------- */')[0], ctx);
  return { ctx, $, calls };
}
test('Settings opens from footer and comma; Models nav removed; cards ordered', () => {
  assert.match(html, /side-foot[\s\S]*data-view="settings"[^>]*>⚙ Settings/);
  assert.doesNotMatch(html, /data-view="models"/);
  assert.match(html, /id="view-settings"/);
  assert.match(app, /e.key === ','[\s\S]*showView\('settings'\)/);
  assert.match(html, /<kbd>,<\/kbd> Settings/);
  const titles = ['Library &amp; export', 'Models', 'API keys', 'Add API model', 'Cloud &amp; budget'];
  let at = html.indexOf('id="view-settings"');
  for (const title of titles) { const next = html.indexOf('>' + title + '</h2>', at); assert.ok(next > at, title); at = next; }
});
test('Key input is a password, cleared after save; no browser storage in settings', async () => {
  const { ctx, $, calls } = setup();
  ctx.initSettings();
  $('keys-name').value = 'OPENAI_API_KEY'; $('keys-value').value = 'secret';
  await $('keys-form').onsubmit({ preventDefault() {} });
  assert.equal($('keys-value').value, '');
  assert.equal(calls[0][0], '/api/keys');
  assert.match(html, /id="keys-value"[^>]*type="password"/);
  const section = app.split('/* ---------- Settings ---------- */')[1].split('/* ---------- End Settings ---------- */')[0];
  assert.doesNotMatch(section, /localStorage|sessionStorage|store\.set/);
});
test('Catalogue picker renders hostile names safely with price, context and image badge', () => {
  const { ctx, $ } = setup();
  ctx.renderCatalogue('cloud-cheap', [{ id: 'untrusted/id', name: '<img onerror=bad>',
    context_length: 8192, input_per_million_usd: .1, output_per_million_usd: .2, input_modalities: ['text', 'image'] }]);
  assert.match($('cloud-cheap').children[1].textContent, /<img onerror=bad>/);
  assert.match($('cloud-cheap').children[1].textContent, /accepts images/);
  assert.match($('cloud-cheap').children[1].textContent, /8192/);
});
test('Hugging Face results use textContent and safe model card links', () => {
  const { ctx, $ } = setup();
  ctx.renderLocalRepos([{ id: 'user/<script>', downloads: 1, badge: 'community — check the model card',
    card_url: 'javascript:alert(1)' }]);
  assert.match($('local-results').children[0].children[0].textContent, /<script>/);
  assert.notEqual($('local-results').children[0].children[1].href, 'javascript:alert(1)');
});

test('Settings has a page title and subtitle before its cards', () => {
  const section = html.split('id="view-settings"')[1].split('</section>')[0];
  assert.match(section, /<h1>Settings<\/h1>\s*<p[^>]*>[^<]+<\/p>/);
});
test('Free variants and non-chat repositories are labelled', () => {
  const { ctx, $ } = setup();
  ctx.renderCatalogue('cloud-cheap', [{ id: 'm:free', name: 'Free model', context_length: 100,
    input_per_million_usd: 0, output_per_million_usd: 0, input_modalities: ['text'],
    label: 'free — often rate-limited; may be unavailable with data_collection: deny' }]);
  assert.match($('cloud-cheap').children[1].textContent, /free — often rate-limited; may be unavailable with data_collection: deny/);
  ctx.renderLocalRepos([{ id: 'user/embed', chat_model: false, label: 'not a chat model' }]);
  assert.match($('local-results').children[0].children[0].textContent, /not a chat model/);
  assert.match(html, /id="local-show-all"[^>]*type="checkbox"/);
  assert.match(app, /show_all=/);
});

test('Settings cards sit in four tabs, General first; the review output shows under every tab', () => {
  const section = html.split('id="view-settings"')[1].split('</section>')[0];
  const tabs = [...section.matchAll(/<button type="button" data-stab="(\w+)"/g)].map(m => m[1]);
  assert.deepEqual(tabs, ['general', 'models', 'tools', 'cloud']);
  const cards = [...section.matchAll(/<div data-stab="(\w+)" class="card settings-card"[^>]*>\s*<h2>([^<]+)<\/h2>/g)]
    .map(m => m[1] + ':' + m[2]);
  assert.deepEqual(cards, ['general:Library &amp; export', 'general:Updates', 'models:Models', 'tools:Optional tools',
    'cloud:API keys', 'cloud:Add API model', 'cloud:Cloud &amp; budget']);
  assert.match(section, /<div class="card settings-card">\s*<details><summary>File changes/);
});

test('showSettingsTab shows only that tab and marks its button', () => {
  const block = app.split('/* ---------- Settings ---------- */')[1].split('/* ---------- End Settings ---------- */')[0];
  const make = (tag, stab) => ({ tagName: tag, dataset: { stab }, hidden: false, on: false,
    classList: { toggle(_, v) { this.owner.on = v; } } });
  const els = [make('BUTTON', 'general'), make('BUTTON', 'tools'), make('DIV', 'general'), make('DIV', 'tools')];
  els.forEach(e => { e.classList.owner = e; });
  const state = {};
  const ctx = vm.createContext({ state, document: { querySelectorAll: () => els } });
  vm.runInContext(block, ctx);
  ctx.showSettingsTab('tools');
  assert.equal(state.settingsTab, 'tools');
  assert.deepEqual(els.map(e => e.tagName === 'BUTTON' ? e.on : !e.hidden), [false, true, false, true]);
});
