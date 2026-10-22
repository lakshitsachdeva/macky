# macOS setup

## Supported baseline

Custody is macOS-first. The documented baseline is:

- Apple Silicon Mac;
- Python 3.11+;
- Node.js/npm compatible with the checked-in frontend lockfile;
- optional Rust toolchain for Tauri builds;
- optional Homebrew/Tesseract for OCR;
- enough local or external SSD capacity for the model and runtime databases.

The codebase contains cross-platform Python pieces, but folder discovery, native dialogs, tray behavior, packaging, and overlay behavior are macOS-oriented.

## Fresh environment

From the repository root:

```bash
python3.11 --version
node --version
npm --version

python3.11 -m venv .venv-custody
./.venv-custody/bin/python -m pip install --upgrade pip
./.venv-custody/bin/python -m pip install -r backend/requirements.txt
npm --prefix frontend install
```

Optional OCR:

```bash
brew install tesseract
./.venv-custody/bin/python -m pip install pytesseract
```

Do not reuse a broken copied virtual environment. Check the interpreter itself:

```bash
./.venv-custody/bin/python -c 'import sys; print(sys.executable); print(sys.version)'
```

Python 3.9 is not sufficient for the current backend because the code uses `enum.StrEnum`.

## Local configuration

```bash
cp .env.example .env
```

For local SSD mode, leave cloud credentials blank. Never paste service-role credentials or Gemini keys into a public issue, commit, desktop bundle, or screenshot.

## Start

```bash
./start.sh
```

The launcher creates `.run/` and `data/` as needed. It uses the first available Python candidate in this order:

1. `.venv-custody/bin/python`;
2. `venv311/bin/python`;
3. `python3.11` on `PATH`;
4. `python3` on `PATH`.

If the final candidate is Python 3.9, setup is incomplete and the launcher should be repaired rather than used.

## Stop

The launcher records PIDs in `.run/`. Stop the backend/frontend processes using the PIDs shown there or close the development processes from the terminal that started them. Do not delete `data/` while the backend is running.

## First investigation

1. Open the frontend.
2. Create a case in **Cases**.
3. Add a small fixture directory first.
4. Wait for the indexing status to settle.
5. Search for a distinctive marker.
6. Verify the evidence and export the manifest.
7. Only then add a large evidence directory.

This staged approach makes it easier to separate a permissions/indexing problem from a retrieval problem.
