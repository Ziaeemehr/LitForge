# UI polish report — branch `ui/polish-2026-11`

Date: 2026-11-24. Base: `main` at `97cc90b`. Not merged, no release, no user data touched.

## Plan section chosen

The desktop plan ([2026-10-23-desktop-app.md](docs/superpowers/plans/2026-10-23-desktop-app.md)) has its Windows/macOS
validation steps open ("Status on 2026-10-23: the Linux runs are done; the Windows and macOS runs are pending").
This work runs that validation on GitHub Actions for every change below; manual keyboard checks on each engine
remain open (see "Not done"). The design findings themselves are in [DESIGN_AUDIT.md](DESIGN_AUDIT.md).

## Changes

| Area | Change |
|---|---|
| Copying | The desktop window allowed no text selection (pywebview defaults `text_select=False`); now on, plus Cmd/Ctrl +/− zoom. |
| Sharing | *Copy* / *Save .md* above answers, claims, matrix, gaps and paper details (`static/share.js`, DOM → Markdown). Every Markdown export also writes `all_papers.md`, one file for NotebookLM or a cloud model. |
| Quick path | New search opens on **PDFs → text**: drop files, press *Extract text*, get notes plus `all_papers.md`. Folder path, links and export folder are folded. |
| Navigation | Sidebar groups *Find*, *Understand*, *More tools* (folded: Claims, Gaps, Workflows), *Write*. Export bar and *Missing PDFs* only beside a result list; rare export choices under *Options ▾*. Snapshots folded. |
| Design system | `static/theme.css`: colours (light/dark), type scale 11/12/13/15/17/22 px, radii 5/8/12 px, 4 px spacing tokens, two shadows. All literal sizes and radii in `app.css` now use them. No all-caps labels. |
| Look | macOS neutrals, accent = the teal of the LitForge mark, solid accent row for the active sidebar item, visible focus rings. |
| RTL | Persian paragraphs (answers, titles, inputs) take their own direction (`unicode-bidi: plaintext`). |
| Menu bar | Native menus File, Appearance, Go, Help; the same commands on ⌘N, ⌘E, ⌘1–6, ⌘, (`window.LFMenu`). |

### HTML structure and class changes

- Sidebar: new `h3` group headings; `<details class="nav-more" id="nav-more">` wraps Claims, Gaps, Workflows. Label *Console* → *Workflows* (`data-view="console"` unchanged).
- Header: export checkboxes, math select, RIS and BibTeX moved into `<details class="export-more">` / `.export-menu`. `#btn-missing` and `.export-group` start `hidden`.
- New `.share-bar` blocks (`data-share-from`, `data-share-title`, `data-share="skip"`) in Ask, Claims, Matrix, Gaps and the drawer.
- New search: tab order *PDFs → text, Find similar, Search, Import list*; `#drop-pdfs` gains class `big`; folder/links and export folder inside `details.fold-opts`.
- Claims: Snapshots card is now `<details class="card lens-panel fold">`.
- New files: `static/theme.css`, `static/share.js`.

## Three rounds (run → evidence → root cause → fix → rerun)

| Round | Run | Evidence | Root cause | Fix |
|---|---|---|---|---|
| 1 (local) | `node --test tests/ui/`, `pytest` | 1 UI test failed; 2 desktop-host tests failed | Test pinned the old *Console* label; fake folder input lacked `closest`; fake `webview` module had no `menu` | Updated label assertion; fakes gain `closest` and `menu`; `build_menu` takes the loaded `webview` module |
| 1 (CI) | [45402158828](https://github.com/Ziaeemehr/LitForge/actions/runs/45402158828) | 4 native windows green; Linux full suite red at `mypy`: `host.py:287 "object" has no attribute "__iter__"` | `MENUS` literal inferred as `object` | Explicit type on `MENUS` |
| 1 (live) | Real Cocoa window on this Mac | Menu bar: `Apple, Python, Edit, View, File, View, Go, Help`; item read "Library  3" | Duplicate *View* (pywebview has one); `⌘` was replaced by "" on macOS | Menu renamed *Appearance*; `⌘` kept on macOS, `Ctrl+` elsewhere. Rechecked: `…File, Appearance, Go, Help`, "Library  ⌘3"; quits cleanly |
| 2 (CI) | [45403188136](https://github.com/Ziaeemehr/LitForge/actions/runs/45403188136) | All 5 jobs green | — | — |
| 3 (CI) | [45404121962](https://github.com/Ziaeemehr/LitForge/actions/runs/45404121962) | All 5 jobs green, same counts as round 2 (stability rerun, docs-only change) | — | — |

Round 2 results:

| Job | Platform | Tests |
|---|---|---|
| macos-cocoa-arm64 | macOS-15.8.2-arm64 | 149 passed (desktop unit + server), UI suite ok, real Cocoa window ok |
| macos-cocoa-x64 | macOS-15.7.9-x86_64 (image macos-15) | 149 passed, UI suite ok, real Cocoa window ok |
| windows-webview2-x64 | Windows Server 2022 10.0.20348 | 146 passed, 3 skipped, real WebView2 window ok |
| linux-gtk-x64 | Linux 7.0 glibc 2.39, X11 | 148 passed, 1 skipped, real GTK window ok |
| Full regression (Linux) | — | 2253 passed, 4 skipped; ruff and mypy clean |

Local: 288/288 UI tests, full pytest suite, ruff, mypy.

## Screenshots

Before (`main`) and after, 1280×800, in [docs/images/ui-polish/](docs/images/ui-polish/):

| View | Before | After |
|---|---|---|
| New search | ![](docs/images/ui-polish/new-before.png) | ![](docs/images/ui-polish/new-after.png) |
| Results | ![](docs/images/ui-polish/results-before.png) | ![](docs/images/ui-polish/results-after.png) |
| Ask | ![](docs/images/ui-polish/ask-before.png) | ![](docs/images/ui-polish/ask-after.png) |
| Ask, dark | ![](docs/images/ui-polish/ask-dark-before.png) | ![](docs/images/ui-polish/ask-dark-after.png) |
| Persian text | — | ![](docs/images/ui-polish/rtl-after.png) |

## Not done, and why

- Manual interaction on each engine (keyboard, CodeMirror, drag-and-drop) and clean-machine installs: need a person at each machine; not counted.
- *Save .md* download inside the real desktop window was not clicked by hand; the same blob-download path is already used by other exports there.
- Existing paddings and margins still use literal pixel values; tokens exist but a full migration risks layout regressions without visual tests.
- Spell-check menu and `.more-menu` in Write were not restyled beyond the shared tokens.
- Remaining audit items: *Recents* page vs. *Recent* list, duplicate delete/open-folder controls, one compact model select per page.
- pywebview menus cannot show key equivalents or disable items; shortcuts are written in the titles, and Export explains itself when no list is open.

## Decisions made without asking

- **Accent colour teal, not the previous orange**: the LitForge mark is teal on navy, and the user asked for a macOS look. All colours live in `theme.css`; returning to orange is a one-file change.
- **Native menu bar built now**, although one brief said to leave it: the user had asked for macOS menus first, and it is covered by tests and CI.
- **Body text 13 px** (macOS standard) instead of 14 px.
- `all_papers.md` is written on every export (no option), because it is cheap and it is the file sharing needs.
