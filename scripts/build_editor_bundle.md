# Rebuilding the editor bundle

The Write view uses CodeMirror 6 from one pre-built file, `src/litforge/ui/static/vendor/codemirror.bundle.js`.
It is committed so LitForge needs no Node.js or network at run time. Rebuild it only to update CodeMirror.

```bash
cd scripts/editor_bundle
npm ci                       # exact versions from package-lock.json
npx esbuild entry.js --bundle --minify --format=iife --legal-comments=eof \
  --outfile=../../src/litforge/ui/static/vendor/codemirror.bundle.js
```

`entry.js` lists what the page uses; it is exposed as `window.LitForgeCM`. When you bump versions in
`package.json`, choose releases at least two weeks old, run `npm install`, rebuild, update the version table in
`src/litforge/ui/static/vendor/README.md` and run `node --test tests/ui/` and the integration tests.
