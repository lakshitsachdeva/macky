# Indexing and ingestion

## Lifecycle

```text
discover → filter → observe → extract → hash/lookup → store → embed → publish status
```

### Discover

Folder selection and watcher scans recursively inspect configured directories. The evidence case importer uses the same supported-file discovery rules and returns per-file statuses.

### Filter

The indexer applies extension, directory, hidden-file, age, size, dataless, and permission rules. A scan count is not the same as an indexed-item count.

### Observe

The pipeline records source timestamps and hashes. Case acquisition uses streaming SHA-256. The general indexer also uses content/path checks for duplicate and replacement handling.

### Extract

Text extractors build title, preview, summary, keywords, and stored extracted text. Large text is chunked for embeddings. Images use filename/OCR/enrichment paths.

### Store

SQLite stores item metadata and FTS5 content. The watched-file table tracks pending, indexed, duplicate, failed, missing, and related states.

### Embed

The local embedding model creates vectors for dense retrieval. Multiple vectors can represent one item because chunked documents and summaries may produce multiple FAISS rows.

### Publish

The backend emits indexing events. The overlay invalidates stale query caches and shows progress/pending state without clearing valid results during normal indexing.

## Initial scan policy

The SSD-first launcher sets `RECURDO_AUTO_INDEX_ON_START=0`. This prevents an old library from scanning broad persisted folders during boot. Explicit evidence import, folder add, or `/index/trigger` starts indexing.

## Pending files

Pending does not necessarily mean “broken.” It can mean:

- the file is waiting for a background lane;
- a model or OCR dependency is warming;
- the file hit a transient SQLite/I/O error;
- the source is dataless or inaccessible;
- the retry loop has not reached it;
- a scan is still in progress.

Use `/index/status`, the UI status line, and backend logs together. Logs can contain source paths and should be redacted before sharing.

## Duplicate handling

Content hashes and path lookups help avoid duplicate item rows. Case evidence remains associated with the source observation and item relationship. A duplicate status does not mean the evidence was discarded from the case; inspect the manifest.

## Source mutation during acquisition

The current implementation captures metadata before and after the hash read, but it is not a sector-level acquisition and does not guarantee the source stayed stable during the entire read. If the post-read observation differs or the investigation requires a validated acquisition, stop and use the organization's acquisition tooling.

## Recommended fixture test

Create a small directory containing:

- one text file with a unique marker;
- one PDF with a unique phrase;
- one screenshot with readable text;
- one unsupported file;
- one missing path in the import request.

Record import statuses, search hits, hashes, verification output, and exported manifest hash. Do not use personal evidence as the first test.
