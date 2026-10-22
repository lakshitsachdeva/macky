# Custody forensic workflow

Custody is a local-first evidence intelligence workspace for organizing,
indexing, verifying, searching, and reviewing investigation material on macOS.
It combines file provenance, SHA-256 evidence records, case-scoped retrieval,
natural-language analysis, and a hash-linked custody event stream.

This page explains the complete workflow as it exists in the current release.
It is intentionally detailed so an investigator, reviewer, or contributor can
understand what the system actually does rather than infer capabilities from
the interface.

## 1. The product in one sentence

> Custody turns selected local files into a case-scoped, searchable evidence
> workspace with reproducible provenance metadata and cited investigative aids.

The important words are **selected**, **case-scoped**, **provenance metadata**,
and **investigative aids**:

- Custody does not silently acquire every disk sector.
- A report is not allowed to search outside its selected case.
- A source record carries a digest, size, nanosecond mtime, path, and status.
- A generated answer is not a legal conclusion or an automatic chain-of-custody guarantee.

## 2. What is implemented

### Evidence and provenance

When evidence is imported, Custody:

1. resolves the requested file or directory;
2. recursively discovers supported files;
3. reads the source without modifying, moving, or renaming it;
4. streams a SHA-256 digest;
5. records byte size and nanosecond modification time;
6. indexes the file through the normal extraction/search pipeline;
7. links the indexed item to the selected case;
8. records an `evidence_acquired` event in the case custody stream.

The original source remains at its original path. The local database stores the
case relationship and metadata; the application does not create a write-blocked
image or claim that the source became immutable.

### Verification

Verification re-reads the source path and compares the current observation with
the acquisition record:

| Status | Meaning |
| --- | --- |
| `verified` | SHA-256, byte size, and nanosecond mtime match the recorded values. |
| `changed` | At least one digest or metadata value differs. |
| `missing` | The recorded source path no longer exists. |
| `unreadable` | The path exists but could not be read. |

Verification appends a new custody event. It does not restore, overwrite, or
silently re-import the source.

### Case scope

Cases are implemented through the `case_evidence` relationship. Search, Ask,
timeline aggregation, and forensic reports use that relationship as their
membership boundary.

This means a report cannot use an indexed item merely because it is semantically
similar. The item must be present in the selected case evidence manifest.

### Custody events

Each case custody event contains:

- event ID;
- case ID;
- optional item ID;
- event type;
- actor;
- occurrence time;
- structured details;
- previous event hash;
- current event hash.

The event hash is computed from canonical JSON and the preceding event hash.
This creates a useful local tamper-evident signal. It is not an external anchor,
trusted timestamp, independent signature, or protection from a privileged local
user who can rewrite the database.

## 3. The forensic timeline

The ordinary custody timeline shows acquisition and verification events. The
forensic timeline adds evidence-registration entries and presents both streams
in one case-scoped order.

Route:

```text
GET /cases/{case_id}/forensic-timeline
```

Optional inclusive UTC bounds:

```text
GET /cases/{case_id}/forensic-timeline?start_at=2026-10-01T00:00:00Z&end_at=2026-10-31T23:59:59Z&limit=1000
```

Each evidence entry can carry:

- evidence source name and path;
- SHA-256;
- byte size;
- nanosecond mtime;
- import timestamp;
- acquisition status;
- indexed title, type, and source metadata;
- created, modified, first-seen, and file-mtime values.

Each custody entry can carry:

- event type and actor;
- occurrence timestamp;
- event details;
- event hash-chain context;
- the associated in-case evidence metadata.

Out-of-case item IDs attached to malformed or stale custody rows are excluded.
Case-level events without an item ID remain visible because they do not identify
an out-of-scope source.

### Timestamp interpretation

The timeline reports explicit ISO-8601 timestamps and normalizes parsed bounds
to UTC. It does not turn a relative phrase such as “last week” into a legal or
calendar-certified period. When a formal investigation needs a precise window,
provide exact start and end timestamps and record the timezone in the case notes.

## 4. Natural-language forensic reports

Route:

```text
POST /cases/{case_id}/report
```

Request:

```json
{
  "question": "What evidence was recorded, what integrity warnings matter, and what should I review next?",
  "allow_model": false,
  "limit": 10
}
```

The report pipeline is deliberately different from conversational `/ask`.
Conversational Ask is designed for helpful memory recall. A forensic report is
designed for bounded review and therefore uses:

- neutral investigative language;
- structured JSON output;
- numbered source citations;
- strict source-index validation;
- source hashes and acquisition status;
- current source observation status;
- custody context;
- warnings and limitations;
- deterministic report hashing.

### Report source records

Each returned source includes, when available:

- `source_index`;
- `item_id`;
- title and filename;
- source name and local path;
- SHA-256;
- acquisition status;
- indexed modified timestamp;
- evidence metadata;
- indexed metadata and excerpt;
- current verification observation;
- related case custody events.

The local UI displays these fields as source citations. Secret-shaped values are
redacted before presentation.

### Deterministic fallback

The report works without a model. With `allow_model: false`, it returns a
deterministic review based on recorded evidence and indexed metadata. This mode
is the default and is appropriate when evidence must remain entirely local.

If model-assisted output is requested but the provider is unavailable, fails,
returns invalid JSON, or returns invalid source indices, Custody falls back to
the deterministic report and records the reason in `generation.fallback_reason`.

### Model-assisted mode

With `allow_model: true`, the backend uses the configured local provider order.
For the current public release, Gemini is the optional external provider.

The model receives:

- the investigation question;
- source names;
- item IDs;
- SHA-256 values;
- acquisition and verification status;
- timestamps;
- selected indexed excerpts;
- case-scoped custody details.

The model prompt intentionally does **not** send absolute local source paths.
The local report still returns paths to the investigator, but the external model
only needs stable source names and provenance identifiers to cite its material.

The UI toggle is off by default because enabling it may send evidence-derived
excerpts to Gemini. The user must make that egress decision for each analysis.

## 5. A real local use case: documentation self-audit

The repository includes a reproducible live demo that uses public project
documentation as synthetic evidence. This is useful for showing the full flow
without importing personal files or real incident material.

The demo treats these files as evidence:

```text
README.md
```

The resulting case demonstrates:

1. case creation;
2. evidence import;
3. streaming SHA-256 acquisition records;
4. normal indexing;
5. case-local verification;
6. a combined forensic timeline;
7. a natural-language report;
8. source citations and warnings;
9. a case manifest hash.

Run it while the local app is up:

```bash
./start.sh
./.venv-custody/bin/python scripts/run_forensic_demo.py --allow-model
```

The script does not read or print environment-file contents. With model mode
enabled, only the selected public documentation excerpts are eligible for the
Gemini request. The full JSON report is written to the ignored local path:

```text
data/demo/forensic-demo-report.json
```

For a fully local run, omit `--allow-model`:

```bash
./.venv-custody/bin/python scripts/run_forensic_demo.py
```

The script reuses the named demo case if it already exists, so repeating the
demo does not create a new case every time.

## 6. Investigation workflow for real evidence

### Before acquisition

- Confirm the intended SSD and source volume.
- Record the source volume identity and mount state in the case notes.
- Decide whether the source is a working copy or an original.
- Confirm the app is bound to loopback and local mode is enabled.
- Decide whether model-assisted analysis is allowed for this evidence.
- Do not treat Custody as a write blocker or forensic imaging utility.

### Acquire

1. Open **Cases**.
2. Create a descriptive case ID/name.
3. Import a file or folder path.
4. Wait for the import result.
5. Confirm the evidence manifest contains expected filenames, sizes, mtimes,
   and SHA-256 values.
6. Export the manifest if an external investigation record is required.

### Validate

1. Run **verify evidence**.
2. Review every `changed`, `missing`, and `unreadable` result.
3. Inspect the custody timeline.
4. Do not ignore a changed source merely because the indexed text still looks
   familiar.
5. Preserve the exported manifest according to the organization’s procedure.

### Analyze

1. Start with model assistance off.
2. Ask a bounded question, such as:

   ```text
   Which files support the recorded handoff event, and what integrity warnings are present?
   ```

3. Review the source citations rather than trusting the prose alone.
4. Compare cited hashes and statuses with the evidence manifest.
5. Enable Gemini only when external excerpt processing is permitted.
6. Record the report SHA-256 and the exact question used.

### Report

A defensible internal report should preserve:

- case name and ID;
- exact question;
- generation mode and provider;
- report SHA-256;
- manifest SHA-256;
- evidence source list;
- source hashes and verification statuses;
- timeline bounds and timezone;
- warnings and limitations;
- the operator’s interpretation separate from observed records.

## 7. Data flow and privacy

### Local-only mode

The standard launcher sets:

```text
RECURDO_LOCAL_MODE=1
GEMINI_API_KEY=""
GEMINI_VISION_ENABLED=0
SUPABASE_URL=""
SUPABASE_SERVICE_ROLE_KEY=""
RECURDO_AUTO_INDEX_ON_START=0
```

The runtime databases and indexes live under `data/` on the selected SSD.

### Optional Gemini configuration

Keep the real key only in the ignored file:

```text
data/backend.env
```

```env
GEMINI_API_KEY=your_real_key_here
```

Restart the backend after editing the file. Never place the real key in:

- `.env.example`;
- Git;
- frontend source;
- a Tauri bundle;
- screenshots;
- issues or pull requests;
- chat messages.

The API status endpoint reports provider availability but never returns the key.

## 8. What this is not

Custody does not currently provide:

- raw disk or memory acquisition;
- write-blocked collection;
- deleted/unallocated-sector recovery;
- a cryptographically immutable evidence vault;
- independent signatures or external trusted timestamps;
- multi-user case ownership isolation;
- automated legal-admissibility decisions;
- guaranteed detection of every source mutation during an initial read;
- a substitute for a trained forensic examiner or organizational SOP.

The correct claim is that Custody is an evidence-aware local review workspace
with provenance and investigative-analysis features.

## 9. Useful API calls

```bash
curl -fsS http://127.0.0.1:8000/health
curl -fsS http://127.0.0.1:8000/llm/status
curl -fsS http://127.0.0.1:8000/cases
curl -fsS http://127.0.0.1:8000/index/status
```

After creating a case, the principal workflow is:

```text
POST /cases/{case_id}/evidence/import
POST /cases/{case_id}/verify
GET  /cases/{case_id}/forensic-timeline
POST /cases/{case_id}/report
GET  /cases/{case_id}/export
```

See [`docs/api.md`](api.md) for request shapes and [`docs/cases.md`](cases.md)
for the evidence/provenance model.
