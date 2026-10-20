const { test } = require('node:test');
const assert = require('node:assert/strict');
const sym = require('../../src/litforge/ui/static/latex_symbols.js');

test('every symbol has a command, a glyph, a group and search words; commands are unique', () => {
  const seen = new Set();
  for (const s of sym.symbols) {
    assert.match(s.command, /^\\/); assert.ok(s.glyph && s.group && s.words, JSON.stringify(s));
    assert.ok(!seen.has(s.command), 'duplicate ' + s.command); seen.add(s.command);
  }
  assert.ok(sym.symbols.length > 150);
});

test('search finds by name, command, group and glyph; a command prefix comes first', () => {
  assert.equal(sym.search('alpha')[0].command, '\\alpha');
  assert.equal(sym.search('\\leq')[0].command, '\\leq');
  assert.ok(sym.search('less or equal').some(s => s.command === '\\leq'));
  assert.ok(sym.search('arrows').length >= 10);
  assert.equal(sym.search('∑')[0].command, '\\sum');
  assert.equal(sym.search('in')[0].command.slice(0, 3), '\\in');
  assert.equal(sym.search('').length, sym.symbols.length);
  assert.deepEqual(sym.search('nothing like this'), []);
});
