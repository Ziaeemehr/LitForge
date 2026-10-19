// LitForge spell-check worker: loads Hunspell dictionaries with nspell and answers check/suggest messages.
// Built into src/litforge/ui/static/vendor/spell.worker.js (see scripts/spell/README.md).
import nspell from 'nspell';
import rules from '../../src/litforge/ui/static/spell_rules.js';

const dictionaries = {};
const loading = {};
let personal = [];
const check = rules.makeChecker(dictionaries, personal);

function load(lang) {
  if (!loading[lang]) {
    const base = '/static/vendor/dict/' + lang + '/';
    loading[lang] = Promise.all(['index.aff', 'index.dic'].map((name) => fetch(base + name).then((r) => {
      if (!r.ok) throw new Error(name + ': HTTP ' + r.status);
      return r.text();
    }))).then(([aff, dic]) => { dictionaries[lang] = nspell(aff, dic); })
      .catch((e) => { postMessage({ type: 'error', lang, message: String(e && e.message || e) }); });
  }
  return loading[lang];
}

self.onmessage = async (event) => {
  const m = event.data || {};
  if (m.type === 'personal') { personal = m.words || []; check.setPersonal(personal); return; }
  const langs = [...new Set((m.words || []).map((w) => w.lang).concat(m.lang ? [m.lang] : []))];
  await Promise.all(langs.map(load));
  if (m.type === 'check') {
    postMessage({ type: 'checked', id: m.id, results: m.words.map((w) => [w.lang + ':' + w.word, check(w.word, w.lang)]) });
  } else if (m.type === 'suggest') {
    postMessage({ type: 'suggested', id: m.id, list: check.suggest(m.word, m.lang) });
  }
};
