const { test } = require('node:test');
const assert = require('node:assert/strict');
const { toMarkdown, slug } = require('../../src/litforge/ui/static/share.js');

const T = s => ({ nodeType: 3, textContent: s });
const E = (tagName, kids = [], attrs = {}) => ({ nodeType: 1, tagName, childNodes: kids, hidden: !!attrs.hidden,
  getAttribute: k => (k in attrs ? attrs[k] : null), textContent: kids.map(k => k.textContent || '').join('') });

test('headings, paragraphs, lists, links and emphasis become Markdown', () => {
  const root = E('DIV', [E('H3', [T('Answer')]), E('P', [T('Alpha '), E('STRONG', [T('beta')]), T(' see '),
    E('A', [T('paper')], { href: 'https://doi.org/1' })]), E('UL', [E('LI', [T('one')]), E('LI', [T('two')])])]);
  assert.equal(toMarkdown(root, 'Q'), '# Q\n\n### Answer\n\nAlpha **beta** see [paper](https://doi.org/1)\n\n- one\n- two\n');
});

test('buttons, hidden nodes and data-share=skip are left out', () => {
  const root = E('DIV', [E('P', [T('keep')]), E('BUTTON', [T('Copy')]), E('P', [T('gone')], { hidden: true }),
    E('P', [T('skip')], { 'data-share': 'skip' })]);
  assert.equal(toMarkdown(root), 'keep\n');
});

test('tables become pipe tables, pipes escaped, short rows padded', () => {
  const tr = (t, cells) => E('TR', cells.map(c => E(t, [T(c)])));
  const root = E('DIV', [E('TABLE', [E('THEAD', [tr('TH', ['Paper', 'Effect'])]), E('TBODY', [tr('TD', ['A|B', 'up']), tr('TD', ['C'])])])]);
  assert.equal(toMarkdown(root), '| Paper | Effect |\n| --- | --- |\n| A\\|B | up |\n| C |  |\n');
});

test('empty content gives an empty string; slug keeps Persian letters', () => {
  assert.equal(toMarkdown(E('DIV', [E('BUTTON', [T('x')])]), 'T'), '');
  assert.equal(slug('Ask: مدل زبانی?'), 'ask-مدل-زبانی');
});
