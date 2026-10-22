# Live forensic demo run — October 22, 2026

This is the recorded result of running the Custody demo against the local
backend on October 22, 2026. The demo used public repository documentation as
synthetic evidence, not personal files or a real incident.

## Command

From the repository root:

```bash
./start.sh
./.venv-custody/bin/python scripts/run_forensic_demo.py --allow-model
```

The script used the local case:

```text
Custody Forensic Demo - Documentation Self-Audit
```

The full response was written to the ignored local path:

```text
data/demo/forensic-demo-report.json
```

No API key or environment-file content was printed.

## Observed result

| Check | Result |
| --- | ---: |
| Evidence files requested | 4 |
| Evidence files discovered | 4 |
| Evidence files imported | 4 |
| Import failures | 0 |
| Verified sources | 4 |
| Changed sources | 0 |
| Missing sources | 0 |
| Unreadable sources | 0 |
| Forensic timeline entries | 20 |
| Case evidence records | 4 |
| Custody events | 16 |
| Report source records | 4 |
| Generated findings | 7 |
| Report warnings | 0 |

The second run reported `already_indexed` for all four files because the files
were already present in the local index. That is expected and demonstrates that
case import can reuse indexed content while still recording the case-specific
relationship and custody events.

## Gemini connectivity result

The first model attempt correctly detected that the configured legacy model was
no longer served and fell back safely. The runtime default was then updated to
the currently served `gemini-3.8-flash` endpoint, the backend was restarted,
and the demo was run again.

Final model result:

```text
provider: gemini
model_used: true
fallback_used: false
warning_count: 0
```

The API status endpoint reported provider availability without returning the
key itself.

## Report hashes

The run returned:

```text
report_sha256:   853c21ba47cee5638c3ab10263a7bfe9578c83fef90183e17bbfcb298fe40c07
manifest_sha256: 601cd8287d367097183bf4ed8d90e2438a1ad6832a16273272a03e0dfec11dc0
```

These hashes identify the exact report and manifest payloads produced during
this run. They do not provide an external trusted timestamp or make the case
legally admissible.

## What the model found

The generated self-audit identified these implemented controls:

- streaming SHA-256 acquisition;
- file size and nanosecond mtime tracking;
- case-evidence linking;
- local hash-linked custody events;
- re-hash verification with `verified`, `changed`, `missing`, and `unreadable`
  states;
- source citations and report limitations;
- local-first storage with explicit optional external integrations.

It also correctly identified the major limitations:

- no write blocker;
- no raw disk imaging;
- no deleted/unallocated/slack-space preservation;
- no independent signatures or external timestamp anchoring;
- no protection from a privileged local administrator rewriting the local store;
- single-user/local ownership assumptions;
- optional Gemini, Supabase, and Gmail data egress.

## Why this is a useful demo

This run proves the complete vertical slice without pretending that a document
self-audit is a real incident investigation:

```text
local files
  → case evidence import
  → SHA-256 + size + mtime
  → indexed/searchable records
  → verification
  → case-scoped timeline
  → natural-language report
  → cited findings + warnings + reproducible hashes
```

For a real investigation, replace the public documentation with an approved
working copy of evidence and follow the organization’s acquisition, retention,
review, and reporting procedures.
