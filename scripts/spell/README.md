# Spell-check worker and dictionaries

The Write view checks spelling in a Web Worker so loading the Persian dictionary (about 1.5 s) never blocks typing.

```bash
cd scripts/spell
npm ci                                    # nspell 2.1.5, dictionary-en 4.0.0, dictionary-fa 2.0.0
../editor_bundle/node_modules/.bin/esbuild worker.js --bundle --minify --format=iife --legal-comments=eof \
  --outfile=../../src/litforge/ui/static/vendor/spell.worker.js
for l in en fa; do mkdir -p ../../src/litforge/ui/static/vendor/dict/$l
  cp node_modules/dictionary-$l/index.aff node_modules/dictionary-$l/index.dic node_modules/dictionary-$l/license \
    ../../src/litforge/ui/static/vendor/dict/$l/; done
```

`spell_rules.js` (in the static folder) decides when a word counts as correct; it is bundled into the worker and
tested with `node --test tests/ui/spell_rules.test.cjs`.
