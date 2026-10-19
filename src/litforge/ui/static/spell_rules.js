/* When a word counts as correctly spelled, given the dictionaries (nspell instances, by language) and the user's
   own words. Shared by the spell-check worker (bundled into vendor/spell.worker.js) and the tests. */
(function (root) {
  'use strict';

  function persianForms(word) {  // Arabic letter forms typed on some keyboards
    return word.replace(/ي/g, 'ی').replace(/ك/g, 'ک');
  }

  var CACHE_LIMIT = 20000;  // distinct words remembered; cleared when full so long sessions do not grow

  function makeChecker(dictionaries, personalWords) {
    var personal = new Set(personalWords || []);
    var cache = new Map();

    function inDictionary(dict, word, lang) {
      if (dict.correct(word)) return true;
      if (lang === 'en') return word !== word.toLowerCase() && dict.correct(word.toLowerCase());
      var plain = persianForms(word);
      if (plain !== word && dict.correct(plain)) return true;
      var parts = plain.split('‌').filter(Boolean);  // compounds: every part a word
      return parts.length > 1 && parts.every(function (p) { return dict.correct(p); });
    }

    function check(word, lang) {
      if (personal.has(word)) return true;
      var dict = dictionaries[lang];
      if (!dict) return true;  // no dictionary for this language: nothing is flagged
      var key = lang + ':' + word;
      if (!cache.has(key)) {
        if (cache.size >= CACHE_LIMIT) cache.clear();
        cache.set(key, inDictionary(dict, word, lang));
      }
      return cache.get(key);
    }
    check.add = function (word) { personal.add(word); };
    check.cacheSize = function () { return cache.size; };
    check.setPersonal = function (words) { personal = new Set(words || []); };
    check.suggest = function (word, lang) {
      var dict = dictionaries[lang];
      if (!dict) return [];
      var base = lang === 'fa' ? persianForms(word) : word, out = [];
      for (var i = 0; i + 1 < base.length; i++) {  // the commonest typo: two neighbours swapped
        var swapped = base.slice(0, i) + base[i + 1] + base[i] + base.slice(i + 2);
        if (swapped !== base && out.indexOf(swapped) < 0 && dict.correct(swapped)) out.push(swapped);
      }
      dict.suggest(base).forEach(function (s) { if (out.indexOf(s) < 0) out.push(s); });
      return out.slice(0, 6);
    };
    return check;
  }

  // Results for the words whose language has a dictionary loaded; the others stay unanswered so the page asks
  // again later (a failed download must not make every word "correct").
  function answer(words, check, loaded) {
    return words.filter(function (w) { return loaded[w.lang]; })
      .map(function (w) { return [w.lang + ':' + w.word, check(w.word, w.lang)]; });
  }

  var api = { makeChecker: makeChecker, answer: answer };
  root.LitForgeSpellRules = api;
  if (typeof module === 'object' && module.exports) module.exports = api;
})(this);
