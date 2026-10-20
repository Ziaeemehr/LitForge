const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const app = fs.readFileSync('src/litforge/ui/static/app.js', 'utf8');
const html = fs.readFileSync('src/litforge/ui/static/index.html', 'utf8');

function setup() {
  const calls = [], logged = [];
  const ctx = vm.createContext({
    Promise,
    logLine: line => logged.push(line),
    api: async (url, opts) => { calls.push([url, opts.body.name]); return { path: '/up/' + opts.body.name }; },
  });
  vm.runInContext(app.split('/* ---------- Seed PDFs ---------- */')[1]
    .split('/* ---------- End Seed PDFs ---------- */')[0], ctx);
  return { ctx, calls, logged };
}

test('the similar form picks several PDFs', () => {
  assert.match(html, /<input type="file" id="file" accept="application\/pdf" multiple hidden>/);
  assert.match(html, /Drop PDFs here/);
});

test('the same file picked twice is kept once, order kept', () => {
  const { ctx } = setup();
  const a = { name: 'a.pdf', size: 1 }, b = { name: 'b.pdf', size: 2 }, a2 = { name: 'a.pdf', size: 1 };
  const other = { name: 'a.pdf', size: 9 };
  const list = ctx.addSeedPdfs([a], [b, a2, other]);
  assert.deepEqual(list.map(f => f.name + f.size), ['a.pdf1', 'b.pdf2', 'a.pdf9']);
});

test('every PDF is uploaded in order and all paths become seeds', async () => {
  const { ctx, calls, logged } = setup();
  const paths = await ctx.uploadSeedPdfs([{ name: 'a.pdf' }, { name: 'b.pdf' }]);
  assert.deepEqual(Array.from(paths), ['/up/a.pdf', '/up/b.pdf']);
  assert.deepEqual(calls.map(c => c[0]), ['/api/upload?name=a.pdf', '/api/upload?name=b.pdf']);
  assert.deepEqual(logged, ['uploading 1/2 a.pdf', 'uploading 2/2 b.pdf']);
});

test('no PDFs: no upload', async () => {
  const { ctx, calls } = setup();
  assert.deepEqual(Array.from(await ctx.uploadSeedPdfs([])), []);
  assert.equal(calls.length, 0);
});
