# راهنمای فارسی معماری سامانهٔ کشف و تحلیل مقالات علمی

**نسخه:** ۱.۱ — ۱۴ اکتبر ۲۰۲۶  
**سند مکمل:** `literature_analysis_architecture_plan.md`  
**هدف:** ساخت یک ابزار مستقل، محلی‌محور و چندپروژه‌ای برای کشف، غربالگری، تحلیل و نگهداری دانش مقالات علمی.

## ۰. هدف و منطق کلی خط لوله

هدف این ابزار آن است که از میان صدها مقالهٔ یافته‌شده، چند ده مقالهٔ واقعاً مرتبط به‌صورت محلی و تقریباً بدون هزینه جدا شوند و فقط همین‌ها با متن تمیز Markdown به مدل‌های بزرگ‌تر سپرده شوند. چهار گام اصلی:

۱. **جست‌وجو و رتبه‌بندی:** رایگان، سریع و روی همین سیستم. امتیاز ارتباط (واژه‌ها، ارجاعات، شباهت معنایی) و امتیاز اهمیت (ارجاع نرمال‌شده، مرکزیت، مجله) ساخته می‌شود.
۲. **غربالگری با مدل کوچک محلی:** مدل محلی (پیش‌فرض `qwen3:4b` روی Ollama) عنوان و چکیده را با پرسش پژوهشی و معیارهای پذیرش و رد پروژه می‌سنجد و مثلاً از ۲۰۰ مقاله حدود ۳۰ مقاله را پیشنهاد می‌کند. تصمیم نهایی با پژوهشگر است.
۳. **خروجی Markdown:** متن تمیز و ساختاریافتهٔ مقالات انتخاب‌شده ساخته می‌شود.
۴. **تحلیل با مدل بزرگ:** فقط روی همان مقالات انتخاب‌شده اجرا می‌شود؛ با Claude، Codex یا یک مدل محلی بزرگ‌تر.

صرفه‌جویی فقط در هزینه نیست؛ کیفیت هم بالاتر می‌رود. مدل بزرگ وقتی چند ده مقالهٔ واقعاً مرتبط را بخواند، بهتر از زمانی تحلیل می‌کند که صدها مقالهٔ پرنویز پیش رویش باشد. متن Markdown تمیز هم نسبت به PDF خام توکن بسیار کمتری مصرف می‌کند.

### خروجی و Obsidian

خروجی Markdown از ابتدا برای Obsidian و Git طراحی شده است و پوشهٔ خروجی را می‌توان مستقیم به‌عنوان vault باز کرد یا در vault موجود گذاشت:

- **یادداشت‌ها:** هر مقاله یک یادداشت جدا در `knowledge_base/` دارد. بخش YAML ابتدای یادداشت عنوان، سال، DOI، وضعیت پردازش و در صورت وجود، تصمیم، نمره و دلیل غربالگری را نگه می‌دارد.
- **پیوندها:** پیوند به PDF و تصاویر نسبی است و در Obsidian کار می‌کند.
- **فهرست:** فایل `README.md` فهرست همهٔ مقالات را با وضعیت و تصمیم غربالگری دارد.

این خروجی **خام** است، نه تحلیل‌شده: اطلاعات کتاب‌شناختی، چکیده و در صورت وجود PDF، متن کامل استخراج‌شده. خلاصه، مقایسه، روش‌شناسی و محدودیت‌ها کار مرحلهٔ دوم (تحلیل عمیق، بخش ۴) است که هنوز ساخته نشده است. تا آن زمان می‌توان پوشهٔ خروجی را در Claude Code باز کرد و تحلیل یادداشت‌ها را از آن خواست.

**وضعیت فعلی:** گام‌های ۱ تا ۳ پیاده‌سازی شده‌اند (غربالگری سطح S1، یعنی عنوان و چکیده). غربالگری متن کامل و تحلیل عمیق خودکار در برنامه است.

## ۱. تصمیم معماری: مخزن مستقل

**پیشنهاد قطعی: توسعه به‌صورت یک مخزن Git مستقل (Standalone Repository)** با هستهٔ Python، CLI و API داخلی. این سیستم نباید به کد یک پروژهٔ پژوهشی خاص وابسته باشد. هر پروژهٔ پژوهشی یک Workspace با پیکربندی، سؤال‌های پژوهشی، معیارهای انتخاب و گزارش‌های مخصوص خود دارد؛ اما مقالات، فایل‌های PDF و خروجی پردازش اولیه در یک کش مشترک و مبتنی بر شناسهٔ محتوا ذخیره می‌شوند.

مزایا:
- استفادهٔ مجدد برای موضوعات مختلف بدون کپی‌کردن کد.
- پردازش یک‌بارهٔ PDF یکسان، حتی اگر در چند پروژه استفاده شود.
- امکان تعویض parser، مدل زبانی، موتور جست‌وجو و خروجی بدون تغییر معماری اصلی.
- امکان اجرای کاملاً محلی و افزودن سرویس‌های ابری فقط در صورت نیاز.
- نسخه‌بندی و ارزیابی مستقل، تست‌پذیری و بازتولید نتایج.

**نکته:** استقلال پروژه به معنی جداکردن تمام داده‌ها نیست؛ بانک مشترک مقالات باید از عضویت و ارزیابی همان مقاله در پروژه‌های مختلف جدا باشد.

## ۲. چرخهٔ کامل از موضوع تا بانک مقالات

```text
Research topic / question / seed DOI / seed PDF
                  │
                  ▼
       Query planning & expansion
                  │
                  ▼
       Paper discovery adapters
  OpenAlex / Semantic Scholar / Crossref / arXiv
                  │
                  ▼
       Metadata normalization
       DOI / title / authors / year
                  │
                  ▼
   Deduplication + citation chaining
                  │
                  ▼
       Candidate paper library
                  │
                  ▼
   Abstract screening (local small LLM)
                  │
                  ▼
  PDF acquisition (legal/open access/manual)
                  │
                  ▼
 PyMuPDF / Docling / Marker extraction
                  │
        ┌─────────┼──────────┐
        ▼         ▼          ▼
     Text      Tables      Figures
        └─────────┼──────────┘
                  ▼
     Indexed evidence / retrieval
                  │
                  ▼
   Full-text screening + ranking
                  │
                  ▼
       Human review / selection
                  │
                  ▼
   Deep analysis + cross-paper synthesis
                  │
                  ▼
    Markdown reports + SQLite knowledge base
```

### مسیرهای ورود

۱. **موضوع یا سؤال پژوهشی:** تولید چند query تخصصی، جست‌وجو و ایجاد مجموعهٔ نامزدها.  
۲. **مقالهٔ مبنا (Seed Paper):** دریافت DOI، عنوان یا PDF؛ یافتن مقالات استنادشده و استنادکننده، سپس جست‌وجوی مشابهت معنایی.  
۳. **پوشهٔ PDF یا فایل BibTeX/RIS:** واردکردن مجموعهٔ موجود، تطبیق شناسه‌ها و تکمیل فراداده.  
۴. **فهرست دستی DOI/URL:** ورود گروهی و پردازش افزایشی.

## ۳. کشف مقالات مرتبط (Paper Discovery)

### منابع و مسئولیت هر کدام

| منبع | کاربرد | نکتهٔ اجرایی |
|---|---|---|
| OpenAlex | جست‌وجوی موضوعی، فراداده، ارجاعات و آثار مرتبط | محدودیت‌ها و سیاست API بررسی شود |
| Semantic Scholar API | مشابهت مقالات، citation graph و metadata | ممکن است rate limit و کلید API لازم باشد |
| Crossref | اعتبارسنجی DOI و اطلاعات نشر | ابزار تکمیل فراداده، نه موتور جامع ارتباط معنایی |
| arXiv | پیش‌چاپ‌های مرتبط و PDFهای آزاد | تطبیق نسخهٔ پیش‌چاپ با نسخهٔ ژورنالی |
| Unpaywall / لینک‌های Open Access | یافتن نسخهٔ قانونی PDF | دسترسی به متن کامل همیشه تضمین‌شده نیست |

**استراتژی کشف:**

- ابتدا سؤال پژوهشی را به کلیدواژه‌ها، مترادف‌ها، عبارت‌های تخصصی و چند query مستقل تبدیل کن.
- جست‌وجو را در چند منبع اجرا و نتایج را با DOI نرمال‌شده، arXiv ID و سپس تطبیق محتاطانهٔ عنوان/نویسنده/سال ادغام کن.
- از seed paperها یک یا دو دور **Backward/Forward Citation Chaining** انجام بده؛ عمق و سقف نتایج قابل‌تنظیم باشد.
- **Hybrid Ranking:** تطبیق واژگانی + embedding similarity + نشانه‌های موضوعی/روش‌شناختی؛ شمار استناد فقط سیگنال کمکی باشد و به‌تنهایی معیار کیفیت نباشد.
- علت کشف هر مقاله را ثبت کن: query، منبع، seed paper و رابطهٔ citation.
- جست‌وجو باید قابلیت اجرای مجدد و مقایسهٔ نتایج تازه با قبلی را داشته باشد.
- همهٔ نتایج از APIها کامل نیستند؛ coverage و خطاهای هر منبع در گزارش ثبت شوند.

## ۴. غربالگری دومرحله‌ای و تحلیل عمیق

### مرحلهٔ اول: غربالگری انبوه

| سطح | دادهٔ مورد استفاده | هدف |
|---|---|---|
| S0 | عنوان، سال، نوع سند، فراداده | حذف موارد آشکارا خارج از دامنه |
| S1 | عنوان و چکیده | امتیاز ارتباط اولیه با مدل کوچک محلی |
| S2 | مقدمه، نتیجه‌گیری و بخش‌های بازیابی‌شده | بررسی نامزدهای مرزی و موارد فاقد چکیدهٔ کافی |
| S3 | Methods/Results و شواهد صفحه‌دار | تصمیم دقیق‌تر دربارهٔ انتخاب |

هر تصمیم دارای `include`، `exclude` یا `uncertain`، امتیاز چندمعیاره، دلیل، شواهد، نسخهٔ prompt و مدل باشد. **مقالهٔ فاقد PDF نباید به‌اشتباه نامرتبط تلقی شود.** خروجی `uncertain` برای بررسی انسانی ضروری است.

### مرحلهٔ دوم: تحلیل عمیق

برای مقالات منتخب استخراج کن: سؤال پژوهشی، فرضیات، روش، مدل ریاضی، داده‌ها، preprocessing، پارامترها، روش inference، آزمایش‌ها، نتایج کمی، عدم‌قطعیت، محدودیت‌ها و قابلیت بازتولید. جدول‌ها، شکل‌ها و معادلات فقط در صورت نیاز به مدل vision/استدلالی ارسال شوند. مقایسهٔ بین‌مقاله‌ای باید شواهد و اختلاف تعریف معیارها را حفظ کند.

**اصل مهم:** هر ادعای تحلیلی باید به شناسهٔ مقاله و محل شاهد (صفحه، بخش، جدول یا شکل) متصل باشد؛ ادعای بدون شاهد به‌صراحت `unsupported` یا `needs_verification` علامت‌گذاری شود.

## ۵. استفاده از RTX 5070 با ۱۲ گیگابایت VRAM

- **پردازش اولیهٔ PDF:** ترجیحاً parserهای محلی بدون LLM؛ PyMuPDF برای مسیر سریع، Docling برای ساختار و جدول‌های پیچیده؛ OCR فقط برای صفحات نیازمند آن.
- **غربالگری انبوه:** مدل instruction کوچک کوانتیزه‌شده (ابتدا کلاس 3B–4B؛ در صورت نیاز 7B–8B با تنظیم context و quantization) با خروجی JSON ساختاریافته.
- **Embeddings:** مدل embedding چندزبانه/علمی سبک، قابل اجرا روی CPU یا GPU؛ اندازه‌گیری کیفیت بازیابی روی مجموعهٔ آزمایشی واقعی.
- **Vision:** فقط برای شکل‌های انتخاب‌شده؛ اگر کیفیت مدل vision محلی کافی نبود، ارجاع محدود به مدل قوی‌تر.
- **تحلیل عمیق:** می‌تواند از API یا Codex/Claude Code برای کارهای تخصصی و توسعه استفاده کند؛ این ابزارهای کدنویسی را با موتور پردازش خودکار مقالات اشتباه نگیریم.
- **مدیریت حافظه:** مدل‌ها را به‌صورت متوالی بارگذاری کن، نه هم‌زمان؛ batch size، context length و KV cache را benchmark کن. ظرفیت اسمی ۱۲ گیگابایت به معنی تضمین اجرای همهٔ مدل‌های 8B با هر طول context نیست.

**فاز صفر اجباری:** روی ۲۰–۳۰ مقالهٔ نماینده (دوستونه، فرمول‌دار، جدول‌دار، اسکن‌شده) سرعت، دقت، VRAM و کیفیت خروجی را برای parserها و مدل‌ها مقایسه کن. انتخاب نهایی ابزار باید بر اساس نتایج این benchmark باشد.

## ۶. داده‌ها، ساختار مخزن و تفکیک پروژه‌ها

```text
literature-workbench/
├── pyproject.toml
├── README.md
├── docs/
│   ├── architecture.md
│   └── evaluation.md
├── src/litwork/
│   ├── discovery/       # OpenAlex, S2, Crossref, arXiv
│   ├── acquisition/     # OA links, manual imports
│   ├── parsing/         # PDF -> structured content
│   ├── indexing/        # FTS5, embeddings, FAISS
│   ├── screening/       # rubric, local LLM, review
│   ├── analysis/        # deep analysis, synthesis
│   ├── export/          # Markdown, BibTeX, CSV, JSON
│   ├── storage/         # SQLite, content-addressed cache
│   ├── llm/             # model adapters, prompts, cache
│   └── cli.py
├── tests/
└── data/                # gitignored
    ├── library/
    │   ├── papers.sqlite
    │   ├── pdfs/
    │   ├── parsed/
    │   ├── figures/
    │   └── tables/
    └── projects/
        ├── project_alpha/
        │   ├── project.yaml
        │   ├── decisions/
        │   └── exports/
        └── project_beta/
```

### مدل دادهٔ پیشنهادی

- `papers`: شناسهٔ پایدار، DOI، arXiv ID، عنوان، سال، نویسندگان، چکیده، وضعیت دسترسی.
- `paper_versions`: نسخهٔ PDF، checksum، parser و نسخهٔ parser.
- `paper_relations`: استنادها و رابطه‌های مرتبط.
- `discovery_runs` و `discovery_hits`: query، منبع، زمان، رتبه و منشأ کشف.
- `projects` و `project_papers`: عضویت مقاله در پروژه.
- `screening_decisions`: rubric، امتیاز، وضعیت، مدل، prompt، شواهد.
- `chunks` و `assets`: متن صفحه‌دار، جدول، شکل، caption، مسیر فایل.
- `analysis_runs` و `claims`: گزارش‌ها، ادعاها و شواهد.
- `jobs`: صف کار، وضعیت، خطا، retry و زمان اجرا.

برای ایندکس برداری، FAISS مناسب است؛ برای جست‌وجوی واژگانی و metadata، SQLite + FTS5 کافی است. اگر بعدها حجم یا نیاز چندکاربری افزایش یافت، می‌توان لایهٔ ذخیره‌سازی را عوض کرد.

## ۷. Markdown-first: خروجی‌های نهایی

Markdown باید **فرمت اصلی قابل‌خواندن و قابل‌انتقال** باشد، نه منبع حقیقت برای وضعیت پردازش. داده‌های ساختاریافته و رابطه‌ها در SQLite/JSON ذخیره شوند.

```text
exports/project_alpha/
├── README.md
├── bibliography.bib
├── screening_summary.md
├── screening_decisions.csv
├── research_landscape.md
├── comparison_tables.md
├── knowledge_base/
│   ├── paper_DOI_slug.md
│   └── paper_arxiv_slug.md
├── deep_analysis/
│   ├── paper_A.md
│   └── paper_B.md
└── assets/
    ├── paper_A_fig_2.png
    └── paper_B_table_1.html
```

### قالب هر فایل مقاله

```markdown
---
paper_id: "doi:10.xxxx/example"
title: "..."
year: 2025
doi: "10.xxxx/example"
project: "project_alpha"
screening_status: "include"
---

# Paper title

## Research question
...

## Methods and models
...

## Key findings
...

## Evidence
- Claim: ...
- Source: page 7, Figure 2

![Figure 2](../assets/paper_A_fig_2.png)

## Limitations
...

## Links
- DOI: https://doi.org/10.xxxx/example
```

- جدول‌های ساده به Markdown و جدول‌های دارای merged cells به HTML یا تصویر لینک‌شده تبدیل شوند.
- فرمول‌ها با LaTeX و تصویر مرجع در صورت عدم‌قطعیت استخراج ذخیره شوند.
- مسیر assetها **نسبی** باشد تا خروجی در Obsidian و Git قابل‌انتقال بماند.

## ۸. CLI پیشنهادی

```bash
litwork project create project_alpha --topic "EEG responses to rhythmic auditory stimulation"
litwork discover project_alpha --sources openalex,s2,crossref,arxiv
litwork discover project_alpha --seed-doi "10.xxxx/example" --citations both
litwork import project_alpha ./papers/
litwork screen project_alpha --stage abstract --model local
litwork acquire project_alpha --open-access-only
litwork parse project_alpha --parser auto
litwork index project_alpha
litwork screen project_alpha --stage fulltext --model local
litwork review project_alpha
litwork analyze project_alpha --selected --mode deep
litwork synthesize project_alpha
litwork export project_alpha --format markdown --obsidian
```

این دستورها **قرارداد پیشنهادی رابط** هستند و هنوز پیاده‌سازی نشده‌اند.

## ۹. ترتیب توسعه و معیارهای پذیرش

| فاز | خروجی اجرایی | معیار پذیرش |
|---|---|---|
| ۰. Benchmark | مجموعهٔ تست و گزارش عملکرد | مقایسهٔ parser/LLM روی PDF واقعی و ثبت VRAM |
| ۱. Core Library | دیتابیس، cache، import و CLI | واردکردن مجدد بدون ایجاد تکرار |
| ۲. Discovery | جست‌وجوی چندمنبعی و citation graph | ثبت منشأ هر نتیجه، dedup و مدیریت rate limit |
| ۳. Parsing | Markdown، جدول، شکل، صفحه | ارجاع درست به صفحه و حفظ assetها |
| ۴. Retrieval | FTS5 + embeddings | ارزیابی Recall@K روی پرسش‌های نمونه |
| ۵. Screening | JSON schema + rubric + review | وضعیت uncertain، دلایل و شواهد قابل‌ردیابی |
| ۶. Deep Analysis | گزارش تک‌مقاله‌ای | پیوند هر claim به شاهد و بررسی انسانی |
| ۷. Synthesis/Export | بانک Markdown و مقایسه‌ها | لینک‌های نسبی سالم و بازشدن در Obsidian |
| ۸. Hardening | تست، logging، resume، docs | ادامهٔ کار پس از خطا بدون پردازش تکراری |

**اولویت MVP:** ورود موضوع یا DOI → کشف مقالات → حذف تکراری‌ها → غربالگری عنوان/چکیده → واردکردن PDF منتخب → استخراج → خروجی Markdown. تحلیل عمیق و رابط گرافیکی پس از تثبیت این مسیر اضافه شوند.

## ۱۰. نقش Codex و Claude Code

پروژه را با issueها و milestoneهای کوچک توسعه بده. برای هر فاز از عامل کدنویسی بخواه:

1. قرارداد داده و API ماژول را قبل از کدنویسی مشخص کند.
2. ابتدا تست‌های واحد و چند fixture PDF واقعی/مصنوعی بسازد.
3. تغییرات را کوچک و مستقل نگه دارد؛ schema migration و سازگاری عقب‌رو را لحاظ کند.
4. خروجی قابل‌اندازه‌گیری شامل زمان، مصرف VRAM، تعداد توکن، خطا و کیفیت ارائه دهد.
5. هر فاز را با یک مثال end-to-end و دستور اجرای دقیق تحویل دهد.

نمونهٔ دستور برای coding agent:

> Implement Phase 1 of the literature-workbench architecture. Build the SQLite schema, content-addressed PDF cache, project workspace isolation, PDF import CLI, idempotent ingestion, structured logging, and pytest coverage. Do not implement LLM inference yet. Provide migrations, a runnable example, and an acceptance checklist.

## ۱۱. خطاهای رایج و تصمیم‌های پیشگیرانه

- **ارسال کل PDF به مدل برای همهٔ مقالات:** پرهزینه؛ ابتدا retrieval و screening انجام شود.
- **خلاصه‌سازی و حذف متن اصلی:** اطلاعات علمی از دست می‌رود؛ متن استخراج‌شدهٔ کامل حفظ شود.
- **اتکا به OCR برای همهٔ صفحات:** کند و گاه کم‌دقت؛ OCR انتخابی باشد.
- **ادغام نادرست دو مقاله با عنوان مشابه:** DOI و شناسه‌ها در اولویت باشند؛ تطبیق احتمالی قابل‌بررسی باشد.
- **اعتماد به پاسخ بدون شاهد LLM:** همهٔ claimها دارای provenance باشند.
- **رتبه‌بندی صرفاً براساس citation count:** مقالات جدید و حوزه‌های کم‌استناد را ناعادلانه حذف می‌کند.
- **وابستگی به یک سرویس discovery:** پوشش ناقص می‌شود؛ adapterهای چندمنبعی و log خطا لازم است.
- **بارگذاری هم‌زمان چند مدل GPU:** روی ۱۲ GB احتمال OOM بالا می‌رود؛ زمان‌بندی ترتیبی و fallback به CPU.
- **اختلاط دادهٔ مشترک با تصمیم پروژه:** ارزیابی هر مقاله باید مستقل برای هر پروژه ثبت شود.

## ۱۲. جمع‌بندی اجرایی

**توصیهٔ نهایی:** یک مخزن مستقل با موتور مشترک و Workspaceهای پروژه‌ای بساز. کشف مقالات را به‌عنوان ورودی اصلی سیستم در نظر بگیر؛ داده‌های PDF و استخراج‌شده را یک بار ذخیره کن؛ غربالگری حجیم را به مدل محلی کوچک بسپار؛ تحلیل عمیق را فقط روی مجموعهٔ محدود انجام بده؛ و نتیجه را در SQLite + Markdown با ارجاعات دقیق و تصاویر/جدول‌های مرتبط نگه دار.

**گام بعدی:** اجرای فاز صفر روی ۲۰–۳۰ مقالهٔ نماینده، سپس پیاده‌سازی MVP تا مرحلهٔ خروجی Markdown. این سند مکمل طرح فنی انگلیسی است و می‌تواند در `docs/architecture-fa.md` مخزن قرار گیرد.
