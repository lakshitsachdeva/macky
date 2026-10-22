# Contributing to Custody

Thanks for helping improve Custody. This project values useful search, visible provenance, privacy, reproducibility, and honest forensic boundaries over impressive but unverifiable claims.

## Before opening a change

1. Read [`README.md`](README.md) and the relevant page under [`docs/`](docs/).
2. Check whether the behavior is local single-user, optional cloud, or packaging-specific.
3. Use synthetic fixtures. Do not include personal evidence in a patch.
4. Reproduce the issue with an exact command or UI flow.

## Required checks

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

For search/indexing/custody changes, also run:

```bash
PATH="$PWD/.venv-custody/bin:$PATH" ./scripts/smoke_test.sh
```

Report skipped or unavailable checks explicitly.

## Change expectations

- Add a regression test for a bug when practical.
- Keep source evidence read-only.
- Do not silently broaden cloud data egress.
- Do not add a dependency without explaining why it is necessary.
- Do not weaken case scoping or provenance fields for convenience.
- Use clear error states instead of returning fabricated results.
- Update documentation when supported formats, environment variables, or forensic claims change.

## Commit style

Use conventional prefixes:

```text
feat: add ...
fix: prevent ...
docs: document ...
test: cover ...
refactor: simplify ...
chore: update ...
```

## Pull requests

Include:

- user-visible behavior;
- changed files;
- exact verification commands;
- real output summary;
- known limitations;
- privacy/data-flow impact;
- whether a clean-machine or packaged-app check was unavailable.
