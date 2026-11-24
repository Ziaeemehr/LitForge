# Design audit of the LitForge interface

Date: 2026-11-24. Method: every main view was opened in the developer browser (`litforge ui`) and captured at
1280×860; `app.css`, `index.html` and the desktop host were read. Items marked **done** were fixed in the same
change as this file; the rest are open, in the order they should be done.

## 1. Copying and sharing results

| Finding | Status |
|---|---|
| Text could not be selected in the desktop window: pywebview's `create_window` defaults to `text_select=False`. | **done**: `text_select=True` in `desktop/host.py` |
| Page zoom (Cmd/Ctrl + / −) was off (`zoomable=False` by default). | **done** |
| Answers, claims, the matrix, gaps and paper details had no way to copy them as text. | **done**: a *Copy* / *Save .md* bar above each, built on `static/share.js` (DOM → Markdown, tested in `tests/ui/share.test.cjs`) |
| Sharing with NotebookLM or another model needs one file, not a folder of notes. Export writes one note per paper. | **done**: every Markdown export also writes `all_papers.md` |

## 2. Navigation and structure

| Finding | Status |
|---|---|
| Nine pages in one flat list with no grouping; the order did not follow the work (find → understand → write). | **done**: groups *Find* (Recents, Library), *Understand* (Ask, Matrix), *More tools* (folded: Claims, Gaps, Workflows), *Write* |
| "Console" says nothing to a new user. | **done**: renamed *Workflows* (view id unchanged) |
| The export toolbar and *Missing PDFs* showed on every page, also where they do nothing (Ask, Write, Settings). | **done**: shown only next to a result list |
| Export showed seven controls at once. | **done**: scope + *Export Markdown* stay; PDFs, versions, Obsidian, math, RIS, BibTeX are under *Options ▾* |
| Snapshots took half of the Claims page though rarely used. | **done**: folded card |
| *Recents* (page) and *Recent* (sidebar list) show the same thing twice under almost the same name. | **done**: page renamed *Search history*; sidebar keeps *Recent* |
| *Delete project* and *Open folder* appear both in the header and in the New search form. | **done**: only in the project menu in the header |
| *Model profile* select repeats on Ask, Claims, Matrix, Gaps and Workflows, always full width. | **done**: Ask and Matrix keep it in the inspector; Claims, Gaps and Workflows show one compact select in the page head |
| Gaps puts its controls outside a card, unlike every other page. | **done** |

## 3. Visual consistency

| Finding | Status |
|---|---|
| Colours are tokens in `:root` with a dark set. The dark set is written twice (media query and `[data-theme]`). | **done**: moved to `theme.css`; still written twice, as CSS needs |
| 13 different font sizes, including 9, 12.5 and 13.5 px. | **done**: macOS scale 11 / 12 / 13 / 15 / 17 / 22 px (`--fs-*`) |
| 11 different corner radii (2–14 px). | **done**: 5 / 8 / 12 px + pill (`--r-*`) |
| Spacing values are ad hoc (3, 5, 7, 9 px …). | partly: 4 px tokens `--s1`…`--s6` exist; existing paddings not yet migrated |
| Section headings inside cards use several sizes and colours (accent-coloured `summary`). | open |
| All-caps labels (sidebar headings, Export label, badges, drawer headings). | **done**: sentence case |
| The orange accent did not match the teal LitForge mark. | **done**: accent is the mark's teal |
| Persian text in answers and titles was laid out left-to-right. | **done**: `unicode-bidi: plaintext` per paragraph |
| Two shadows (`--shadow`, `--float`) already exist. | **done**: menus use `--float` |

## 4. macOS conventions

| Finding | Status |
|---|---|
| The window has no application menu beyond pywebview's default; standard items (Edit → Copy/Paste/Select All, View → Zoom, Window, Help → Guide) and shortcuts are missing. | **done**: File, Appearance, Go, Help; ⌘N, ⌘E, ⌘1–6, ⌘, in the page. pywebview menus cannot show key equivalents or disable items, so shortcuts are named in the titles and Export explains itself when no list is open |
| Settings opens with `,` but not ⌘, as on macOS. | **done** |
| Destructive actions (*Delete project*, *Remove unused papers*) are red links next to ordinary actions. | open: move into a "…" menu or keep only on the detail page |
| Buttons mix filled accent, outlined and text links for actions of the same weight. | open: one primary per page, others outlined |

## Next steps

1. Design tokens (spacing, type, radius) in one `theme.css`, then replace literal values in `app.css`.
2. Native application menu with shortcuts, items disabled when they do not apply (e.g. Export without a result list).
3. Remaining open items in sections 2 and 4.
4. Combined Markdown export for NotebookLM.

Each step: `node --test tests/ui/` and `uv run pytest`, plus screenshots of the changed views.
