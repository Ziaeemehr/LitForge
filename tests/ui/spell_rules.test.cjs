const { test } = require('node:test');
const assert = require('node:assert/strict');
const { makeChecker } = require('../../src/litforge/ui/static/spell_rules.js');

// Fake dictionaries: a word is correct when it is in the set.
const dict = words => ({ correct: w => words.has(w), suggest: w => [...words].filter(x => x[0] === w[0]).slice(0, 2) });
const en = dict(new Set(['brain', 'model', 'The']));
const fa = dict(new Set(['مدل', 'شخصی', 'سازی', 'شده', 'کتاب', 'یک']));

test('English: a capitalised word is accepted when its lower case is in the dictionary', () => {
  const check = makeChecker({ en, fa }, []);
  assert.equal(check('Brain', 'en'), true);
  assert.equal(check('brian', 'en'), false);
});

test('Persian compounds with a ZWNJ are accepted when every part is a word', () => {
  const check = makeChecker({ en, fa }, []);
  assert.equal(check('شخصی‌سازی‌شده', 'fa'), true);
  assert.equal(check('شخصی‌سازیی', 'fa'), false);
});

test('Arabic letter forms are read as their Persian forms (ي→ی, ك→ک)', () => {
  const check = makeChecker({ en, fa }, []);
  assert.equal(check('كتاب', 'fa'), true);
  assert.equal(check('يك', 'fa'), true);
});

test('the personal dictionary wins and can be extended', () => {
  const check = makeChecker({ en, fa }, ['connectome']);
  assert.equal(check('connectome', 'en'), true);
  assert.equal(check('multistability', 'en'), false);
  check.add('multistability');
  assert.equal(check('multistability', 'en'), true);
});

test('a language without a loaded dictionary never flags words', () => {
  const check = makeChecker({ en }, []);
  assert.equal(check('هرچیزی', 'fa'), true);
});

test('two swapped neighbouring letters are suggested first (nspell missed "the" for "teh")', () => {
  const words = new Set(['the', 'ten', 'brain']);
  const check = makeChecker({ en: { correct: w => words.has(w), suggest: () => ['ten', 'eh'] } }, []);
  assert.deepEqual(check.suggest('teh', 'en'), ['the', 'ten', 'eh']);
  assert.deepEqual(check.suggest('barin', 'en').slice(0, 1), ['brain']);
});

test('the cache is bounded', () => {
  const check = makeChecker({ en: { correct: () => true, suggest: () => [] } }, []);
  for (let i = 0; i < 25000; i++) check('w' + i, 'en');
  assert.ok(check.cacheSize() <= 20000);
});

test('answers are only given for languages whose dictionary is loaded, so they can be asked again', () => {
  const { answer } = require('../../src/litforge/ui/static/spell_rules.js');
  const check = makeChecker({ en: { correct: w => w === 'ok', suggest: () => [] } }, []);
  const words = [{ word: 'ok', lang: 'en' }, { word: 'xx', lang: 'en' }, { word: 'مدل', lang: 'fa' }];
  assert.deepEqual(answer(words, check, { en: true }), [['en:ok', true], ['en:xx', false]]);
});
