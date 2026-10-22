# Supported formats and extraction behavior

Custody's actual supported set comes from the configured indexer extensions. Historical comments mention additional formats; do not assume an extension is supported until it appears in the configured set and passes a local test.

## Core configured formats

| Extension | Handler | Stored representation | Important limits |
|---|---|---|---|
| `.pdf` | text | extracted text, preview, summary, embeddings | scanned PDFs need a separate OCR path; extraction can be incomplete |
| `.docx` | text | paragraphs and table cells | embedded objects are not guaranteed to be extracted |
| `.txt` | text | decoded text | encoding errors may be recorded as failures |
| `.md` | text | decoded Markdown text | Markdown is indexed as text |
| `.csv` | text | text/table content | generated/raw tables may be down-ranked unless requested |
| `.xlsx` | text | sheet/row/cell text | current extractor limits rows/cells/total text |
| `.pptx` | text | slide shape text | images and speaker notes are not guaranteed |
| `.png` | visual | filename, OCR text when available | no general visual embedding |
| `.jpg`, `.jpeg` | visual | filename, OCR text when available | no general visual embedding |
| `.gif` | visual | filename/OCR path where supported | animation semantics are not guaranteed |
| `.webp` | visual | filename/OCR path where supported | image decoding/OCR depends on installed libraries |

## Not promised by this release

Do not advertise these without adding tests and configuration support:

- legacy binary `.doc`;
- `.xls`;
- `.ppt` or `.key`;
- `.heic`/`.heif`;
- `.bmp`/`.tiff`;
- arbitrary audio/video;
- raw disk images;
- filesystem journals or deleted-file records.

## Skipping rules

An otherwise supported file can still be skipped or deferred because of:

- unsupported extension;
- hidden/excluded directory;
- file age policy during initial scan;
- file-size limit;
- macOS dataless/iCloud state;
- permissions or transient I/O errors;
- duplicate content;
- source replacement or missing path;
- SQLite/index contention;
- background queue state.

The UI and `/index/status` should be treated as the source of truth for a specific run.

## Image search boundary

The current visual pipeline is not “understands every image.” It is primarily:

1. filename fallback;
2. optional local Tesseract OCR;
3. optional Gemini vision/enrichment when explicitly configured.

If OCR and cloud vision are unavailable, an image containing text may only be searchable by filename and any metadata already extracted. Install Tesseract and run a controlled fixture test before relying on screenshot text search.
