# Troubleshooting

## Backend will not start

Check Python:

```bash
./.venv-custody/bin/python --version
```

The current backend requires Python 3.11+. Recreate the environment if the interpreter is a broken symlink or reports Python 3.9.

Check the log:

```text
.run/backend.log
```

Redact paths and evidence names before sharing it.

## Port 8000 or 5173 is occupied

Inspect the listener and verify it is actually Custody before reusing it. `start.sh` detects occupied ports but does not cryptographically identify the process.

```bash
lsof -nP -iTCP:8000 -sTCP:LISTEN
lsof -nP -iTCP:5173 -sTCP:LISTEN
```

## Search returns no results

Check in this order:

1. `/health` reports indexed items greater than zero.
2. `/index/status` shows whether files are pending/scanning.
3. The UI scope says `all evidence` or the intended case.
4. The case has a non-zero evidence count.
5. The file extension is configured and the file was not skipped.
6. The source is readable and not macOS dataless.
7. Search a distinctive filename/marker before asking a semantic question.
8. Enable explain mode only locally and inspect candidate pools.

## Search returns unrelated results

For an exact marker, check the query style and whether FTS/path candidates exist. Current exact-query guardrails reject dense-only neighbors. If unrelated results remain, capture a synthetic reproduction and add a regression test rather than tuning scores against personal data.

## Files are pending

Pending files may be waiting for a background lane, model warmup, retry, OCR, permissions, or an explicit scan. Use `/index/status` and `.run/backend.log`. Do not repeatedly start scans against a large source tree.

## Images are not searchable by text

Check:

```bash
tesseract --version
```

Without Tesseract or explicitly enabled vision enrichment, image search may be filename-only. This is expected behavior in local mode.

## Database locked

Stop duplicate backend/indexer processes. Avoid running broad scans while testing search. The SSD launcher disables automatic startup scanning to reduce this failure mode. If a lock persists, stop the app cleanly before inspecting the SQLite files.

## FAISS/model issues

Check `/health` for model/FAISS state. The first query may be slower while the model warms. Do not delete the index while the backend is running. Rebuild only after preserving any required case manifest and following the project’s repair flow.

## Data appears missing after moving the SSD

The database stores source paths. Moving a source volume, changing the macOS account, or losing a mount can make a previously indexed path unavailable. Custody does not silently convert a missing source into a new acquisition. Inspect the evidence manifest and re-mount/re-acquire according to procedure.
