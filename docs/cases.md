# Cases, evidence, and provenance

## Case model

A case is a local investigation workspace with:

- name;
- description;
- status;
- notes;
- timestamps;
- case-evidence relationships;
- custody events.

Cases are not currently owned by a user in the database. Treat the application as single-user/local unless you add an ownership model and authorization checks.

## Evidence import

The UI can import a file or directory. The backend:

1. resolves the requested path;
2. discovers configured supported files recursively;
3. reads each source;
4. streams a SHA-256 digest;
5. records size and nanosecond mtime;
6. indexes the file through the normal pipeline;
7. records a case-evidence relationship;
8. appends an `evidence_acquired` custody event.

Each file returns a status such as `indexed`, `duplicate`, `unsupported`, `missing`, `unreadable`, or `index_error`.

## Verification

Verification re-reads and re-hashes each source path. It compares:

- SHA-256;
- size;
- nanosecond mtime.

Possible statuses:

- `verified`: all observations match;
- `changed`: digest or observed metadata differs;
- `missing`: source path no longer exists;
- `unreadable`: the path exists but cannot be read.

Verification appends a custody event. It does not restore or rewrite the source.

## Case-scoped forensic analysis

The case workspace includes two analysis views:

- **Forensic timeline** combines evidence-registration records and custody events
  in chronological order. Each entry carries case membership, timestamps, source
  metadata, and hash-chain context where available.
- **Natural-language case report** answers a bounded investigation question over
  only the selected case's evidence. The response includes numbered source
  citations, SHA-256 values, acquisition status, current source observation,
  custody context, warnings, and a deterministic report hash.

Model assistance is off by default. When enabled, selected indexed excerpts may
be sent to the configured Gemini provider. The model receives source names and
case-scoped metadata, not absolute local source paths. If no provider is
available, or structured citations fail validation, Custody returns a
deterministic metadata-based fallback instead.

These reports are investigative aids. They do not create a write-blocked image,
an immutable evidence copy, an independent signature, or a legal-admissibility
determination.

## Custody event chain

Each event includes a canonicalized payload and a previous event hash. The event hash is deterministic for the local database sequence. This gives investigators a useful tamper-evident *signal inside the local database*.

It does not provide:

- external notarization;
- a trusted timestamp authority;
- a signature by an independent key;
- protection against a privileged local user rewriting the database;
- protection against source mutation during the initial read.

## Manifest export

Case export includes:

- case metadata;
- evidence manifest rows;
- custody events;
- `manifest_sha256` over the canonical manifest payload.

Save the exported manifest alongside the investigation record according to the organization's evidence-handling procedure. The application does not automatically upload or externally anchor it.

## API routes

The primary case routes are:

```text
GET    /cases
POST   /cases
GET    /cases/{case_id}
PATCH  /cases/{case_id}
DELETE /cases/{case_id}
POST   /cases/{case_id}/evidence/import
GET    /cases/{case_id}/evidence
GET    /cases/{case_id}/timeline
GET    /cases/{case_id}/forensic-timeline
POST   /cases/{case_id}/report
GET    /cases/{case_id}/export
POST   /cases/{case_id}/verify
```

See [`api.md`](api.md) for request shapes and authentication notes.
