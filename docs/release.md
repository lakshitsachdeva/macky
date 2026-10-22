# Release engineering

## Public source release checklist

- [ ] `.env`, `.env.local`, token directories, runtime databases, logs, and model caches are ignored.
- [ ] No real API key, JWT, service-role key, credential, or private payload is tracked.
- [ ] No personal evidence, screenshots, or database export is tracked.
- [ ] Absolute personal paths are removed from public documentation or clearly synthetic.
- [ ] License and security policy are present.
- [ ] README claims match implementation and test evidence.
- [ ] Backend/frontend checks pass in a clean Python 3.11 environment.
- [ ] `scripts/public_release_check.py` passes.
- [ ] The commit is reviewed before pushing to a public repository.

## Desktop release blockers

Native packaging requires a deliberate maintainer environment:

- Rust/Tauri toolchain;
- Python/PyInstaller sidecar build;
- macOS signing identity;
- notarization credentials and policy;
- updater signing keys;
- safe cloud configuration with no service-role/Gemini secrets embedded.

Do not run a release build from a working directory containing real `.env` credentials unless the script has been audited to prevent propagation. Never ship a Gemini key or Supabase service-role key in a desktop application.

## Current release scope

The public GitHub repository should publish source and documentation, not:

- `data/`;
- `backend/dist/`;
- `frontend/node_modules/`;
- `.venv-custody/` or `venv311/`;
- `.run/`;
- DMGs/apps/build outputs;
- private handoff notes;
- personal screenshots or databases.

## Reproducibility record

For each release, record:

- Git commit;
- OS and architecture;
- Python/Node/Rust versions;
- dependency installation command;
- model/OCR availability;
- test commands and output;
- package checksum if an artifact exists;
- signing/notarization status;
- known limitations.
