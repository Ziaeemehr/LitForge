const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8')
  .split('/* ---------- Review report ---------- */')[1]?.split('/* ---------- End review report ---------- */')[0] || '';

function setup() {
  const elements = {}, created = [], calls = [];
  class Element {
    constructor(tag) { this.tagName = tag; this.children = []; this.attributes = []; this.textContent = ''; }
    set id(value) { elements[value] = this; }
    setAttribute(name, value) { this.attributes.push({ name, value }); }
    appendChild(el) { this.children.push(el); return el; }
    prepend(el) { this.children.unshift(el); }
    replaceChildren(...els) { this.children = els; }
    querySelectorAll() { return this.children; }
  }
  const $ = id => elements[id] ||= new Element('div');
  const malicious = new Element('script'); malicious.remove = () => { malicious.removed = true; };
  const foreign = new Element('foreignObject'); foreign.remove = () => { foreign.removed = true; };
  const svg = new Element('svg');
  svg.children = [malicious, foreign]; svg.attributes = [{ name: 'onload' }, { name: 'href' }];
  svg.removeAttribute = name => { svg.attributes = svg.attributes.filter(a => a.name !== name); };
  class DOMParser { parseFromString() { return { documentElement: svg, querySelector: () => null }; } }
  const ctx = vm.createContext({ $, state: { project: 'Review' }, DOMParser, store: { get: () => false, set() {} },
    document: { createElement(tag) { created.push(tag); return new Element(tag); }, importNode: node => node },
    api: async url => { calls.push([url]); return { counts: { identified_by_source: { openalex: 59 }, identified_other: {}, screened: 0, included: 3, uncertain: 2 }, svg: '<svg/>' }; },
    post: async (url, body) => { calls.push([url, body]); return { path: '/exports/Review/prisma.svg' }; },
    toast: message => { throw Error(message); } });
  vm.runInContext(source, ctx);
  return { ctx, $, calls, created, svg, malicious, foreign };
}

test('panel renders counts and saves on the server without a download anchor', async () => {
  const { ctx, $, calls, created } = setup();
  ctx.wireReviewReport();
  await ctx.loadReviewReport();
  assert.match($('review-counts').textContent, /Included: 3/);
  assert.equal($('review-summary').textContent, '59 records · 0 screened · 3 included');
  assert.equal($('review-counts').textContent.split('\n').length, 4);
  assert.match($('review-counts').textContent, /uncertain: 2/);
  await $('review-save').onclick();
  const saved = calls.find(c => c[0] === '/api/review/prisma/save');
  assert.equal(saved[1].project, 'Review');
  assert.equal($('review-path').textContent, '/exports/Review/prisma.svg');
  await $('review-folder').onclick();
  assert.equal(calls.find(c => c[0] === '/api/open-exports')[1].project, 'Review');
  assert.ok(!created.includes('a'));
  assert.doesNotMatch(source, /window\.open|\.download\s*=/);
});

test('inline SVG strips scripts, foreign objects, handlers and external references', () => {
  const { ctx, svg, malicious, foreign } = setup();
  assert.equal(ctx.sanitiseReviewSvg('<svg/>'), svg);
  assert.ok(malicious.removed && foreign.removed);
  assert.deepEqual(svg.attributes, []);
});

test('static report follows screening inside results', () => {
  const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');
  const results = html.match(/<section[^>]*id="results"[^>]*>([\s\S]*?)<\/section>/)[1];
  assert.match(results, /id="screen-panel"[\s\S]*?<\/details>\s*<details class="card" id="review-report">/);
  assert.match(results, /id="review-counts" dir="auto"/);
});

test('opening the panel loads the current project and persists its open state', async () => {
  const { ctx, $, calls } = setup();
  const stored = [];
  ctx.store.set = (key, value) => stored.push([key, value]);
  ctx.wireReviewReport();
  assert.equal(calls.length, 0);
  $('review-report').open = true;
  await $('review-report').ontoggle();
  assert.equal(calls[0][0], '/api/review/prisma?project=Review');
  assert.deepEqual(stored, [['review-open', true]]);
  $('review-report').open = false;
  await $('review-report').ontoggle();
  assert.equal(calls.length, 1);
});

test('a response from the previous project cannot replace the current report', async () => {
  const { ctx, $ } = setup();
  let respond;
  ctx.api = () => new Promise(resolve => { respond = resolve; });
  const loading = ctx.loadReviewReport();
  ctx.state.project = 'Another';
  $('review-summary').textContent = 'Current report';
  respond({ counts: {}, svg: '<svg/>' });
  await loading;
  assert.equal($('review-summary').textContent, 'Current report');
});

const references = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8')
  .split('/* ---------- Reference exports ---------- */')[1]?.split('/* ---------- End reference exports ---------- */')[0] || '';

test('reference buttons save included decisions and offer opening the folder without downloads', async () => {
  const elements = {}, calls = [], messages = [], created = [];
  const ctx = vm.createContext({
    $: id => elements[id] ||= {}, state: { project: 'Review' },
    post: async (url, body) => { calls.push([url, body]); return { path: '/exports/references.' + body.format, count: 2 }; },
    toast: (message, action) => messages.push([message, action]),
    document: { createElement: tag => { created.push(tag); return {}; } }
  });
  vm.runInContext(references, ctx);
  ctx.wireReferenceExports();
  for (const [id, format] of [['btn-export-ris', 'ris'], ['btn-export-bib', 'bib']]) {
    await elements[id].onclick();
    assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1))), ['/api/review/references', {
      project: 'Review', format, status: ['included']
    }]);
    assert.match(messages.at(-1)[0], new RegExp('/exports/references.' + format));
    await messages.at(-1)[1]();
    assert.deepEqual(JSON.parse(JSON.stringify(calls.at(-1))), ['/api/open-exports', { project: 'Review' }]);
  }
  assert.ok(!created.includes('a'));
  assert.doesNotMatch(references, /window\.open|download/);
  const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');
  assert.match(html, /id="btn-export-ris"[^>]*title="[^"]*included[^"]*decision/);
  assert.match(html, /id="btn-export-bib"[^>]*title="[^"]*included[^"]*decision/);
});

test('the visible toast folder action accepts pointer input', () => {
  const css = fs.readFileSync('src/litforge/ui/static/app.css', 'utf8');
  assert.match(css, /\.toast\.show #toast-folder\s*\{[^}]*pointer-events:\s*auto/);
});
