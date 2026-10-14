# پروژه‌های مشابه و منابع الهام برای LitForge

> تاریخ تهیه: ۱۴ اکتبر ۲۰۲۶  
> هدف: فهرست پروژه‌های قابل بررسی برای معماری **Local-first Scientific Literature Discovery, Screening & Analysis**. این فهرست به معنی تأیید عملکرد یا سازگاری آزمایش‌شده با RTX 5070 نیست.

## ۱. پروژه‌های مرتبط

| پروژه | مخزن | کاربرد و توضیح کوتاه | استفادهٔ پیشنهادی در LitForge |
|---|---|---|---|
| **ASReview LAB** | https://github.com/asreview/asreview | غربالگری انبوه منابع علمی با یادگیری فعال و مشارکت پژوهشگر | الهام برای active learning، صف مرور و تصمیم‌های انسانی |
| **ragPaper** | https://github.com/i1ight/ragPaper | بازیابی دانش از مقالات و یکپارچگی‌های پژوهشی؛ ویژگی‌های دقیق باید در نسخهٔ انتخابی راستی‌آزمایی شوند | ارزیابی برای RAG، MCP و اتصال به ابزارهای پژوهشی |
| **Docling** | https://github.com/docling-project/docling | تبدیل اسناد، از جمله PDF، به نمایش ساختاریافتهٔ JSON/Markdown؛ پشتیبانی از جدول و layout | موتور اصلی استخراج PDF |
| **GROBID** | https://github.com/kermitt2/grobid | استخراج metadata، بخش‌های مقاله، مراجع و ساختار متون علمی | تکمیل اطلاعات کتاب‌شناختی و فهرست منابع |
| **Universal SLR Assistant** | https://github.com/sadeghanisi/SLR | ورود منابع، حذف تکراری‌ها، غربالگری دومرحله‌ای، استخراج داده و ثبت تصمیم‌ها؛ پشتیبانی از Ollama و endpoint محلی | مرجع مهم برای workflow و audit trail |
| **LitHelper** | https://github.com/RobVoss-AI/LitHelper | ابزار اکتشاف ادبیات علمی و تحلیل ارتباطات استنادی؛ میزان بلوغ و ویژگی‌ها نیازمند بررسی مخزن است | ایده برای Discovery و citation graph |
| **slr-rag-automation** | https://github.com/sariekr/slr-rag-automation | نمونهٔ خودکارسازی مرور نظام‌مند و بازیابی شواهد با RAG | الهام برای synthesis مبتنی بر شواهد |
| **scikgextract-ingest** | https://github.com/sciknoworg/scikgextract-ingest | انتخاب backend مناسب براساس نوع سند، fallback، سنجش کیفیت خروجی و غنی‌سازی با GROBID | الگوی مهم برای ingestion انبوه و کنترل کیفیت |

## ۲. گزینه‌های تکمیلی برای بررسی

| پروژه | مخزن | کاربرد |
|---|---|---|
| **SLR-Engine** | https://github.com/tuirk/SLR-Engine | چرخهٔ کشف، دریافت نسخه‌های مجاز و در دسترس، غربالگری چندمرحله‌ای، citation snowballing و خروجی قابل بازتولید؛ قابل اجرا با coding agent |
| **LightRAG** | https://github.com/HKUDS/LightRAG | موتور RAG با مسیرهای پردازش اسناد و امکان استفاده از parserهای خارجی؛ مناسب بررسی برای لایهٔ retrieval |
| **Docling RAG / docling.rs** | https://github.com/docling-project/docling.rs | مسیر تبدیل سند، chunking، embedding و بازیابی با backendهای قابل‌تعویض |
| **Docling Pipelines** | https://github.com/docling-project/docling-pipelines | ingestion و آماده‌سازی داده در مقیاس بالاتر برای RAG |

## ۳. پیشنهاد انتخاب اجزا

1. **Discovery:** OpenAlex، Crossref و Semantic Scholar API؛ با DOI deduplication، forward/backward citation search و ثبت منبع هر رکورد.
2. **Extraction:** مسیر سریع PyMuPDF برای PDFهای متنی؛ Docling برای layout و جدول؛ GROBID برای metadata و references؛ OCR فقط هنگام نیاز.
3. **Screening:** الهام از ASReview و Universal SLR Assistant؛ مدل محلی کوچک برای پیشنهاد امتیاز و دلیل، با تأیید نهایی پژوهشگر.
4. **Storage & Retrieval:** SQLite + FTS5 و ایندکس embedding محلی؛ کش مشترک براساس هش فایل، با workspace مستقل برای هر پروژه.
5. **Deep Analysis:** بازیابی بخش‌های مستند همراه با شمارهٔ صفحه و ارسال فقط شکل‌ها/جدول‌های ضروری به مدل چندوجهی؛ Codex و Claude Code برای تحلیل پیچیده و توسعهٔ pipeline.
6. **Export:** Markdown + تصاویر/جدول‌های پیوندشده، JSON ساختاریافته، BibTeX/RIS، CSV و گزارش روش‌شناسی.

## ۴. ترتیب ارزیابی پیش از توسعه

- **اول:** روی ۲۰ مقالهٔ متنوع، PyMuPDF/Docling/GROBID و scikgextract-ingest را از نظر سرعت، حفظ فرمول، جدول، caption و ارجاع صفحه مقایسه کن.
- **دوم:** ASReview و Universal SLR Assistant را برای کیفیت غربالگری، ثبت تصمیم‌ها و قابلیت استفادهٔ مجدد از کد ارزیابی کن.
- **سوم:** ragPaper و LightRAG را برای retrieval، اتصال agent و هزینهٔ نگهداری بررسی کن.
- **چهارم:** یک MVP مستقل بساز: `topic/seed DOI → discovery → deduplication → PDF → extraction → screening → Markdown export`.

## ۵. معیارهای بررسی هر مخزن

- مجوز و امکان استفادهٔ مجدد؛ وضعیت نگهداری و تست‌ها
- سازگاری واقعی با Linux، CUDA و RTX 5070 با ۱۲ GB VRAM
- امکان اجرای کاملاً محلی و غیرفعال‌کردن درخواست‌های ابری
- پشتیبانی از batch processing، checkpoint، retry و cache
- دقت استخراج جدول/فرمول/شکل و حفظ provenance در سطح صفحه
- امکان استفاده به‌صورت کتابخانه یا CLI بدون وابستگی به UI
- هزینهٔ محاسباتی، مصرف RAM/VRAM و سرعت روی corpus واقعی

## ۶. اصل معماری

**LitForge باید یک مخزن مستقل و project-based باشد؛ ابزارهای موجود اجزای قابل‌تعویض آن باشند، نه اینکه کل پروژه به fork یکی از آن‌ها وابسته شود.**

ساختار خروجی پیشنهادی:

```text
litforge/
├── projects/
│   └── my-research-topic/
│       ├── project.yaml
│       ├── screening.csv
│       ├── reports/
│       │   ├── literature_map.md
│       │   └── comparison.md
│       └── exports/
├── library/
│   ├── papers.sqlite
│   └── papers/
│       └── <paper-id>/
│           ├── metadata.json
│           ├── paper.md
│           ├── figures/
│           └── tables/
└── src/litforge/
```

## ۷. یادداشت اعتبارسنجی

این سند برای تصمیم معماری و بررسی اولیهٔ مخازن است، نه نتیجهٔ benchmark. قابلیت‌های پروژه‌ها ممکن است با نسخه‌های جدید تغییر کنند؛ پیش از انتخاب dependency، README، LICENSE، آخرین release و issueهای مرتبط را بررسی کن.
