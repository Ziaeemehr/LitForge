# Vendored front-end libraries

Copied from [MDviewer](https://github.com/Ziaeemehr/MDviewer) (MIT), which bundles them for
offline use. Each library keeps its own license:

| File | Library | License |
|---|---|---|
| `marked.min.js` | [marked](https://github.com/markedjs/marked) v12.0.2 | MIT |
| `purify.min.js` | [DOMPurify](https://github.com/cure53/DOMPurify) v3.1.6 | Apache-2.0 / MPL-2.0 |
| `katex.min.js`, `katex.min.css`, `fonts/KaTeX_*` | [KaTeX](https://katex.org) | MIT |

The math tokenizer in `../md.js` is adapted from MDviewer's `index.html`.

## Writing Studio

| File | Library | License |
|---|---|---|
| `codemirror.bundle.js` | [CodeMirror 6](https://codemirror.net) (view 6.43.13, state 6.7.6, language 6.12.4, legacy-modes 6.5.4 `stex`, search 6.7.2, commands 6.11.1, autocomplete 6.20.3, @lezer/highlight 1.2.5 `classHighlighter`) with @lezer, style-mod, w3c-keyname, crelt | MIT (`codemirror.LICENSE`) |

Built once with esbuild from pinned packages; see `scripts/build_editor_bundle.md`.

The Persian template's Vazirmatn font (`src/litforge/writing/templates/persian/fonts/`, v33.003) is under the
SIL Open Font License (`OFL.txt` beside it).
