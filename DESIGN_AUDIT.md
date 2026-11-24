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
| Sharing with NotebookLM or another model needs one file, not a folder of notes. Export writes one note per paper. | open: add an export option "one combined file" (NotebookLM caps the number of sources) |

## 2. Navigation and structure

| Finding | Status |
|---|---|
| Nine pages in one flat list with no grouping; the order did not follow the work (find → understand → write). | **done**: groups *Find* (Recents, Library), *Understand* (Ask, Matrix), *More tools* (folded: Claims, Gaps, Workflows), *Write* |
| "Console" says nothing to a new user. | **done**: renamed *Workflows* (view id unchanged) |
| The export toolbar and *Missing PDFs* showed on every page, also where they do nothing (Ask, Write, Settings). | **done**: shown only next to a result list |
| Export showed seven controls at once. | **done**: scope + *Export Markdown* stay; PDFs, versions, Obsidian, math, RIS, BibTeX are under *Options ▾* |
| Snapshots took half of the Claims page though rarely used. | **done**: folded card |
| *Recents* (page) and *Recent* (sidebar list) show the same thing twice under almost the same name. | open: keep the sidebar list, drop or rename the page |
| *Delete project* and *Open folder* appear both in the header and in the New search form. | open: keep them in one place (header) |
| *Model profile* select repeats on Ask, Claims, Matrix, Gaps and Workflows, always full width. | open: one compact select per page, aligned right in the page head |
| Gaps puts its controls outside a card, unlike every other page. | open |

## 3. Visual consistency

| Finding | Status |
|---|---|
| Colours are tokens in `:root` with a dark set — good. The dark set is written twice (media query and `[data-theme]`). | open: keep it once via a shared selector list |
| 13 different font sizes, including 9, 12.5 and 13.5 px. | open: type scale 11 / 12 / 13 / 15 / 18 / 22 px as tokens |
| 11 different corner radii (2–14 px). | open: 4 / 6 / 10 px + pill |
| Spacing values are ad hoc (3, 5, 7, 9 px …). | open: 4 px grid tokens (`--s1` … `--s6`) |
| Section headings inside cards use several sizes and colours (accent-coloured `summary`). | open |
| Two shadows (`--shadow`, `--float`) already exist; menus added here use their own. | open: use `--float` |

## 4. macOS conventions

| Finding | Status |
|---|---|
| The window has no application menu beyond pywebview's default; standard items (Edit → Copy/Paste/Select All, View → Zoom, Window, Help → Guide) and shortcuts are missing. | open: pywebview `menu=` with File (New search ⌘N, Export ⌘E), Edit, View (Theme, Sidebar ⌘\\, Zoom), Go (pages ⌘1…⌘6), Help (Guide ?) |
| Settings opens with `,` but not ⌘, as on macOS. | open |
| Destructive actions (*Delete project*, *Remove unused papers*) are red links next to ordinary actions. | open: move into a "…" menu or keep only on the detail page |
| Buttons mix filled accent, outlined and text links for actions of the same weight. | open: one primary per page, others outlined |

## Next steps

1. Design tokens (spacing, type, radius) in one `theme.css`, then replace literal values in `app.css`.
2. Native application menu with shortcuts, items disabled when they do not apply (e.g. Export without a result list).
3. Remaining open items in sections 2 and 4.
4. Combined Markdown export for NotebookLM.

Each step: `node --test tests/ui/` and `uv run pytest`, plus screenshots of the changed views.
