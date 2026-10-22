# Custody

**A macOS-first, local-first evidence intelligence workspace for investigators, defenders, and security researchers.**

Custody turns a folder of documents, screenshots, exports, notes, and reports into a searchable investigation workspace. It combines exact search, local semantic retrieval, provenance records, case scoping, verification, and an auditable event timeline without pretending to be a disk-imaging or legal-admissibility product.

> **Project status:** experimental single-user research software. The current release is designed for local use on macOS and an external SSD. It is not a replacement for validated forensic acquisition tools, write blockers, evidence lockers, or an organization’s legal/forensic SOP.

## Why Custody exists

Traditional file search asks: “What was the filename?”

Investigations ask:

- Which evidence mentions this person, host, domain, hash, or project?
- What changed between two observations?
- Which files belong to this case?
- Can I re-run the query after an acquisition and understand where the result came from?
- Which source files are missing, changed, unreadable, or still waiting to be indexed?

Custody is built around those questions. It keeps the source path visible, records SHA-256 observations, scopes retrieval to a case, and exposes the evidence behind each result.

## What works today

- Local SSD-first operation through `start.sh`.
- FastAPI backend with SQLite/FTS5, FAISS, graph, and cognitive layers.
- React/Vite search interface with a macOS-oriented Tauri shell.
- Exact, lexical, path, temporal, sparse, dense, and graph-assisted retrieval paths.
- Search while indexing, with visible pending/indexing state.
- Case creation and case-scoped search/Ask workflows.
- Recursive evidence discovery for configured supported formats.
- Streaming SHA-256 acquisition records.
- Evidence size, nanosecond mtime, source path, acquisition status, and indexed status.
- Hash-linked local custody events.
- Re-verification that distinguishes `verified`, `changed`, `missing`, and `unreadable` evidence.
- Canonical JSON manifest export with `manifest_sha256`.
- Optional local Tesseract OCR.
- Optional cloud enrichment paths that are disabled by the SSD-first launcher.
- Deterministic fallbacks when the embedding model or LLM is unavailable.

## What it is not

Custody currently does **not** provide:

- sector-level disk or filesystem imaging;
- write-blocked acquisition;
- deleted-file, slack-space, APFS snapshot, MFT, or raw-device recovery;
- a tamper-proof or externally anchored evidence locker;
- a signed acquisition package;
- cryptographic proof that a source file did not change during hashing;
- multi-user case ownership/isolation;
- a legal-admissibility guarantee;
- a validated replacement for Cellebrite, EnCase, FTK, Autopsy, Velociraptor, or similar tools.

Read the full boundary statement in [`docs/forensic-boundaries.md`](docs/forensic-boundaries.md) before using Custody in an investigation.

## Quick start: local SSD mode

### Requirements

- macOS on Apple Silicon is the primary target.
- Python 3.11 or newer.
- Node.js and npm.
- Homebrew is recommended for optional OCR.
- At least enough free SSD space for the embedding model, SQLite databases, and indexes.

### Install dependencies

```bash
cd /path/to/macky

python3.11 -m venv .venv-custody
./.venv-custody/bin/python -m pip install --upgrade pip
./.venv-custody/bin/python -m pip install -r backend/requirements.txt

npm --prefix frontend install
```

Optional image OCR:

```bash
brew install tesseract
./.venv-custody/bin/python -m pip install pytesseract
```

### Start Custody

```bash
./start.sh
```

The launcher starts:

| Service | URL |
|---|---|
| Backend | `http://127.0.0.1:8000` |
| Frontend | `http://127.0.0.1:5173` |
| Readiness | `http://127.0.0.1:8000/ready` |
| Health | `http://127.0.0.1:8000/health` |

Verify the runtime:

```bash
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/auth/status
curl -fsS http://127.0.0.1:8000/index/status
```

The SSD-first launcher deliberately:

- stores runtime state under `data/` beside the source tree;
- uses a local forensic identity;
- disables Supabase and Gemini credentials;
- keeps one shared embedding-model instance to reduce memory pressure;
- does not automatically scan every persisted watched folder at startup;
- waits for explicit folder selection or an explicit index trigger.

Detailed setup and runtime behavior are documented in [`docs/setup-macos.md`](docs/setup-macos.md) and [`docs/runtime-local-ssd.md`](docs/runtime-local-ssd.md).

## The core forensic workflow

```text
case → acquire → hash → index → search → verify → audit → export
```

1. Open **Cases** and create an investigation.
2. Select a file or directory containing evidence.
3. Custody discovers configured supported files recursively.
4. Each source is read and hashed in streaming mode with SHA-256.
5. Custody records path, digest, size, mtime, acquisition time, and indexing status.
6. Search the case with exact terms, natural language, paths, names, or temporal language.
7. Re-verify the source later and inspect changed/missing/unreadable results.
8. Review the hash-linked custody timeline.
9. Export a canonical local manifest for the case.

The acquisition path does not move, rename, rewrite, or delete the original source file. It also does not create an immutable forensic image. That distinction is intentional and documented.

## Search architecture

Custody uses a layered retrieval pipeline rather than one opaque “AI search” call:

1. Query parsing identifies exact markers, file hints, people, source types, and time expressions.
2. SQLite FTS5 searches title, filename, preview, original path, extracted text, summary, and keywords.
3. Dense retrieval uses the local `nomic-ai/nomic-embed-text-v1.5` model when ready.
4. Sparse, filename, path, metadata, temporal, recency, and behavior signals are scored.
5. Graph candidates can add entity and relationship evidence for complex queries.
6. Cognitive reranking is used on non-fast pipelines when available.
7. Case scope is applied before final result serialization.
8. The UI shows provenance and indexing state instead of hiding uncertainty.

Exact marker queries intentionally return an honest empty result instead of unrelated dense neighbors. Search behavior, limitations, and examples are in [`docs/search.md`](docs/search.md).

## Supported files

The current configured ingestion set is narrower than the historical watcher constants. The tested core set is:

- PDF
- DOCX
- TXT
- Markdown
- CSV
- XLSX
- PPTX
- PNG, JPG/JPEG, GIF, WEBP images

Image retrieval is text-derived: filename fallback, optional OCR, and optional cloud vision. It is not a general CLIP-style visual similarity system. Extraction limits, skipped files, dataless files, age rules, and failure states are documented in [`docs/supported-formats.md`](docs/supported-formats.md) and [`docs/indexing.md`](docs/indexing.md).

## Privacy modes

### SSD-first local mode

The recommended development and investigation mode. The launcher clears cloud credentials and keeps indexing/search on the Mac, subject to the operating system, installed libraries, and any separately enabled integration.

### Cloud-authenticated mode

The legacy application path can use Supabase authentication/access checks. This is not the recommended mode for sensitive evidence until the deployment is explicitly threat-modeled and multi-user ownership is implemented.

### Optional enrichment

If a user explicitly configures Gemini or Gmail integrations, data can leave the device. Do not place API keys in a desktop release, and read [`docs/privacy.md`](docs/privacy.md) before enabling them.

## Runtime data layout

```text
data/
├── memory.db                 # primary items, FTS5, watched files, interactions
├── text_faiss.index          # local dense/sparse retrieval index
├── graph.db                  # graph and temporal facts
├── cognitive_layer.db        # activation, frames, associations, reranking state
├── attachments/              # local integration attachments, when used
├── gmail_tokens/             # OAuth material, when Gmail is enabled
├── auth_cache.json           # local auth cache
└── user_settings/            # local user settings
```

All of `data/` is ignored by Git. Treat it as sensitive user evidence metadata, not as a disposable build directory. See [`docs/data-layout.md`](docs/data-layout.md).

## Tests and verification

Backend:

```bash
./.venv-custody/bin/python -m pytest -q backend
```

Frontend:

```bash
npm --prefix frontend run lint
npm --prefix frontend run test:unit
npm --prefix frontend run build
```

Project smoke gate:

```bash
PATH="$PWD/.venv-custody/bin:$PATH" ./scripts/smoke_test.sh
```

The local verification recorded for this public snapshot on **October 21, 2026** was:

```text
Backend: 233 passed, 4 skipped
Smoke suite: 121 passed
Frontend unit tests: 11 passed
Frontend lint: passed
Frontend production build: passed
Live case-scoped search: passed
```

These checks establish software behavior in this workspace. They do not establish legal admissibility, clean-machine packaging, notarization, or forensic-tool validation.

## Documentation map

- [Overview](docs/overview.md)
- [macOS setup](docs/setup-macos.md)
- [SSD runtime](docs/runtime-local-ssd.md)
- [Architecture](docs/architecture.md)
- [Data layout](docs/data-layout.md)
- [Supported formats](docs/supported-formats.md)
- [Indexing](docs/indexing.md)
- [Search](docs/search.md)
- [Cases and provenance](docs/cases.md)
- [Forensic workflow](docs/forensic-workflow.md)
- [Forensic case lab](docs/demo/forensic-case-lab.md)
- [Live demo run](docs/demo/forensic-demo-run-2026-10-22.md)
- [Local forensic case playbooks](docs/demo/local-forensic-case-playbooks.md)
- [Forensic boundaries](docs/forensic-boundaries.md)
- [Privacy](docs/privacy.md)
- [Security model](docs/security.md)
- [API surface](docs/api.md)
- [Testing](docs/testing.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Release engineering](docs/release.md)
- [Development](docs/development.md)

## Security

Please do not publish evidence, credentials, tokens, database files, model caches, or private screenshots in issues or pull requests. Read [`SECURITY.md`](SECURITY.md) for responsible disclosure and local handling rules.

## Contributing

Custody is intentionally conservative about evidence claims. Contributions should preserve source provenance, expose uncertainty, add regression tests, and describe what was actually verified. Read [`CONTRIBUTING.md`](CONTRIBUTING.md).

## License

Custody is released under the Apache License 2.0. See [`LICENSE`](LICENSE).
