# Development guide

## Repository rules

- Keep public documentation in `docs/` and update README links when behavior changes.
- Preserve the local-first, single-user boundary unless a feature explicitly adds ownership/auth isolation.
- Do not claim forensic guarantees that the code does not implement.
- Keep source evidence read-only in import flows.
- Add a regression test for search/indexing/provenance behavior that changes.
- Prefer synthetic fixtures over personal evidence.
- Keep secrets and runtime state outside Git.

## Work on the backend

```bash
./.venv-custody/bin/python -m pytest -q backend
```

Run a syntax check on focused files:

```bash
./.venv-custody/bin/python -m py_compile \
  backend/main.py \
  backend/database.py \
  backend/custody.py \
  backend/retrieval/search.py
```

## Work on the frontend

```bash
npm --prefix frontend run lint
npm --prefix frontend run test:unit
npm --prefix frontend run build
```

## Adding a retrieval change

1. State the user-visible query that currently fails.
2. Reproduce it with a synthetic database/fixture.
3. Identify candidate-pool and scoring behavior.
4. Make the smallest focused change.
5. Add a regression test for the original query and a nearby boundary.
6. Run backend and frontend checks.
7. Record whether the result was verified with a live runtime.

## Adding a case/provenance change

Test:

- case creation/readback;
- import status for supported, missing, unreadable, and unsupported paths;
- source remains byte-for-byte unchanged;
- digest and metadata are recorded;
- case scope is enforced;
- verification detects modification;
- export is deterministic;
- cleanup does not claim to erase unrelated state.

## Generated and private files

Do not add:

- `.env*` except `.env.example`;
- `data/`;
- `.run/`;
- model caches;
- node modules/build outputs;
- local screenshots/evidence;
- personal handoff documents.
