<p align="center">
  <img src="src/litforge/ui/static/icon.svg" width="64" alt="">
</p>
<h1 align="center">LitForge</h1>
<p align="center">Find, screen and export scientific papers, on your own computer.<br>
<a href="docs/install.md">Install</a> · <a href="docs/guide.md">Guide</a> · <a href="#فارسی">فارسی</a></p>

![LitForge results: ranked papers with scores, charts and filters](docs/images/results.png)

LitForge turns a paper, a few keywords or a reference list into a ranked reading list, helps you
decide what to keep, and writes Markdown notes with the full text for Obsidian or a language model.
It runs in your browser, but everything stays on your machine.

## Features

- **Find papers** from a DOI, arXiv id, PDF or abstract (references, citing papers, similar meaning),
  from keyword searches (OpenAlex, Crossref, arXiv), or from an Elicit or Zotero export.
- **Ranked by review priority**: relevance to your topic plus importance in the field, and every
  result says why it was found.
- **Mark ✓ / ✗** with the keyboard, in ranges or in bulk; "More like my ✓ papers" searches again.
- **AI screening** (optional): a local model checks titles and abstracts against your criteria.
- **PDFs**: downloads open-access copies, matches your own PDFs to their papers, lists the missing ones.
- **Markdown export** with full text, figures and equations as LaTeX.
- Light and dark themes, a quick guide (press `?`), and a command line for everything.

| Paper details | Dark theme |
|---|---|
| ![Paper details with score breakdown and abstract](docs/images/paper.png) | ![Result list in the dark theme](docs/images/dark.png) |

## Start

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then:

```bash
git clone https://github.com/Ziaeemehr/LitForge.git
cd LitForge
uv sync --extra embed
uv run litforge shortcut     # adds a LitForge icon; double-click it from now on
```

Linux, macOS and Windows: see [docs/install.md](docs/install.md). Scores, screening, the command
line and storage are explained in [docs/guide.md](docs/guide.md).

---

<div dir="rtl">

## فارسی

لیت‌فورج از یک مقاله، چند کلیدواژه یا یک فهرست منابع، فهرستی مرتب از مقاله‌های مرتبط می‌سازد، در
انتخاب مقاله‌ها کمکتان می‌کند و برای مقاله‌های انتخاب‌شده یادداشت‌های Markdown با متن کامل می‌نویسد.
در مرورگر اجرا می‌شود ولی همه‌چیز روی رایانه‌ی خودتان می‌ماند.

### قابلیت‌ها

- **یافتن مقاله** از DOI، شناسه‌ی arXiv، فایل PDF یا چکیده؛ با جستجوی کلیدواژه در OpenAlex و Crossref و arXiv؛ یا از خروجی Elicit و Zotero.
- **مرتب‌سازی بر اساس اولویت مرور**: ترکیب ارتباط با موضوع و اهمیت در حوزه، همراه با دلیل یافته‌شدن هر مقاله.
- **علامت‌گذاری ✓ و ✗** با صفحه‌کلید، به‌صورت بازه‌ای یا گروهی، و جستجوی دوباره از مقاله‌های ✓.
- **غربالگری با هوش مصنوعی** (اختیاری): یک مدل محلی عنوان و چکیده را با معیارهای شما می‌سنجد.
- **مدیریت PDF**: دریافت نسخه‌های دسترسی آزاد، تطبیق PDFهای خودتان و فهرست PDFهای ناموجود.
- **خروجی Markdown** با متن کامل، شکل‌ها و معادله‌ها به LaTeX.
- پوسته‌ی روشن و تاریک، راهنمای سریع (کلید `?`) و خط فرمان برای همه‌ی کارها.

### شروع

ابتدا [uv](https://docs.astral.sh/uv/getting-started/installation/) را نصب کنید، سپس:

</div>

```bash
git clone https://github.com/Ziaeemehr/LitForge.git
cd LitForge
uv sync --extra embed
uv run litforge shortcut
```

<div dir="rtl">

فرمان آخر یک آیکون LitForge می‌سازد؛ از این به بعد فقط روی آن دوبار کلیک کنید. راهنمای نصب در
لینوکس، مک و ویندوز در [docs/install.md](docs/install.md) است.

</div>

## License note

PyMuPDF is AGPL-licensed. Review this before distributing LitForge or derived work.
