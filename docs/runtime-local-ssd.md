# SSD-first local runtime

## What `start.sh` guarantees

The launcher explicitly sets:

```text
RECURDO_LOCAL_MODE=1
RECURDO_ALLOW_LOCAL_DEV_AUTH=1
RECURDO_SHARE_EMBEDDING_MODEL=1
RECURDO_AUTO_INDEX_ON_START=0
RECURDO_DATA_DIR=<repository>/data
WATCHED_DIRS=
GEMINI_API_KEY=
SUPABASE_URL=
SUPABASE_ANON_KEY=
SUPABASE_SERVICE_ROLE_KEY=
```

It also points SQLite, FAISS, graph, cognitive, auth-cache, attachments, and Gmail-token paths into `data/`.

## Optional Gemini key for case analysis

The normal launcher keeps model-assisted analysis disabled unless you opt in from
the case-analysis panel. If you want to enable Gemini locally, create this
ignored file on the SSD:

```text
<repository>/data/backend.env
```

Add one line:

```text
GEMINI_API_KEY=your-real-key-here
```

Do not put the real key in `.env.example`, Git, a desktop bundle, screenshots,
issue reports, or chat. Restart `start.sh` after changing the file. The key is
used only by the local backend; enabling model-assisted case analysis may send
selected evidence excerpts to Gemini, so leave the UI toggle off for local-only
analysis.

## Why automatic startup scanning is disabled

A copied legacy library can contain broad watched folders. Automatically scanning those folders during boot can:

- consume the SSD and CPU before the UI is ready;
- create SQLite write contention;
- make the first search appear hung;
- index personal folders the user did not mean to include in a case.

The local launcher therefore waits for explicit evidence selection or `/index/trigger`. This is a safety and usability default, not a claim that every persisted watched folder has been deleted.

## Health checks

```bash
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/auth/status
curl -fsS http://127.0.0.1:8000/index/status
```

Interpretation:

- `/ready` means the HTTP service is accepting readiness requests.
- `/health` reports indexed counts, model/FAISS state, and integration availability.
- `/auth/status` shows whether local-forensics access is active.
- `/index/status` reports scan phase, pending files, queue size, and layer status.

`is_fully_indexed=false` is not automatically a failure. It can mean files remain pending, were skipped by policy, are inaccessible, or are waiting for a background retry.

## Evidence isolation

Keep the runtime `data/` directory on the intended SSD. Keep source evidence in its original location unless the investigation SOP says otherwise. If the source is on a removable drive, record the volume identity and mount state in the case notes outside the application as part of the investigation procedure.

Custody does not provide a write blocker or an immutable evidence store.

## Logs and temporary state

Runtime logs and PID files live under `.run/` and are ignored by Git. They may contain filenames and error messages. Treat them as sensitive. Do not upload `.run/backend.log` to a public issue without redaction.

## Safe reset

Do not use `rm -rf data` casually. It removes local indexes, case records, custody events, graph state, cognitive state, and possible tokens/attachments. Make an explicit backup or export first, then stop the app before changing runtime state.
