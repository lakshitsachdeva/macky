# Testing and verification

## Test philosophy

Tests should prove user-visible investigation outcomes:

- a source can be imported and found;
- a case scope includes only its evidence;
- source changes are detected;
- a missing source is reported honestly;
- an exact marker with no evidence does not return random neighbors;
- indexing state does not make the UI look permanently empty;
- frontend builds and the local runtime starts.

## Backend

Use the project virtual environment:

```bash
./.venv-custody/bin/python -m pytest -q backend
```

Focused suites:

```bash
./.venv-custody/bin/python -m pytest -q \
  backend/test_custody_workflow.py \
  backend/test_database_search.py \
  backend/test_search_resilience.py \
  backend/test_query_intent.py \
  backend/test_temporal_query.py
```

## Frontend

```bash
npm --prefix frontend run lint
npm --prefix frontend run test:unit
npm --prefix frontend run build
```

## Smoke gate

```bash
PATH="$PWD/.venv-custody/bin:$PATH" ./scripts/smoke_test.sh
```

## Live search check

After `./start.sh`:

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  --data '{"query":"unique synthetic marker","limit":5}'
```

For a meaningful end-to-end check, create a temporary synthetic file, import it into a temporary case, search the marker, verify the hash, export the manifest, and clean up the case/source afterward.

## Verification record

Every release note should identify:

- date and OS;
- Python/Node versions;
- whether Tesseract was installed;
- whether the embedding model was warm;
- exact command;
- actual pass/skip/fail output;
- whether the check used synthetic or personal evidence.

## Coverage gaps

The following require future dedicated tests:

- Tauri/Rust integration;
- clean-machine packaged app;
- signed/notarized release;
- multi-user case isolation;
- source mutation while hashing;
- complete deletion of every local artifact;
- hostile document/parser sandboxing;
- exact calendar semantics for relative temporal language.
