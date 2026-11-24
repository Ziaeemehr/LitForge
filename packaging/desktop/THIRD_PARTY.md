# Third-party components in the LitForge installers

The installer itself contains LitForge (AGPL-3.0, see `app/LICENSE`) and one native binary:

| Component | Where | License | Architecture-specific |
|---|---|---|---|
| uv (Astral) | `bin/uv`, `bin/uv.exe` | MIT or Apache-2.0 | yes: one build per target |

Downloaded during setup into the per-user runtime folder, not shipped in the installer:

| Component | Source | License |
|---|---|---|
| CPython 3.12 (python-build-standalone) | uv's managed Python downloads | PSF-2.0 and bundled-library licenses |
| Python packages pinned in `app/uv.lock` (PyMuPDF AGPL-3.0, pydantic MIT, typer MIT, PyYAML MIT, pywebview BSD-3-Clause, ...) | PyPI | as listed in each package's metadata in the runtime |

Renderer, provided by the operating system and not redistributed: Microsoft Edge WebView2 (Windows), WKWebView
(macOS), WebKitGTK 4.1 (Linux, declared as a `.deb` dependency).
