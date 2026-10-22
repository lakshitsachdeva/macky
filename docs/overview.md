# Custody overview

## Product definition

Custody is a local-first evidence intelligence workspace. It indexes user-selected files, builds searchable local metadata and embeddings, and gives an investigator a case-scoped interface for finding, reviewing, verifying, and exporting source observations.

The project has two identities in its history:

- **Custody** is the public forensic-workspace name.
- **Recurdo** is the legacy application name still present in internal modules, environment variables, API comments, and some compatibility paths.

The legacy name is an implementation detail, not a second product or a claim about the maturity of the forensic workflow.

## Intended users

- security researchers reviewing local artifacts;
- incident responders building a searchable working set;
- malware-analysis or threat-intelligence students;
- defenders correlating notes, screenshots, reports, and exported logs;
- developers testing local retrieval and provenance UX;
- investigators who already have a separate acquisition process and need a searchable analysis workspace.

## Intended deployment model

The current target is a **single user on macOS**, preferably with runtime state on an external SSD. The backend binds to loopback in the normal launcher path. The repository also contains legacy cloud-authenticated and landing/admin code, but those paths are not the recommended handling mode for sensitive evidence.

## Maturity statement

Custody should be treated as experimental research software:

- the search pipeline is tested but still heuristic;
- image search is OCR/text-derived rather than general visual similarity;
- the case database is locally mutable;
- case records are not multi-user-owned;
- the source path is observed, not independently acquired into a write-protected image;
- packaging/signing/notarization require maintainer-only validation;
- legal or evidentiary conclusions are outside the project.

## Design principles

### Evidence is read-only by default

The case import path reads and hashes source paths. It does not move, rename, rewrite, or delete them. This is a safety property, not a guarantee that the operating system, another process, or a user cannot change the source.

### Search must be honest

The interface should distinguish:

- no matches;
- matches in a selected case;
- files still waiting to be indexed;
- missing or unreadable source paths;
- dense-model cold start;
- unavailable OCR or enrichment.

It should not fill an exact marker query with unrelated nearest neighbors merely to avoid an empty screen.

### Provenance is visible

Search results should carry enough metadata to answer “where did this come from?” Case views expose source path, digest, size, mtime, acquisition status, indexed status, and custody events.

### Claims must match verification

Documentation and UI copy must say whether a behavior is implemented, configured, observed in a local run, or merely planned.

## High-level capabilities

| Capability | Status |
|---|---|
| Local file indexing | Implemented |
| SQLite FTS5 exact/keyword search | Implemented |
| Local dense retrieval | Implemented when model is available |
| Search while indexing | Implemented with pending-state UI |
| Case registry | Implemented |
| SHA-256 source observation | Implemented |
| Hash-linked custody events | Implemented locally |
| Re-verification | Implemented |
| Canonical manifest export | Implemented |
| Sector-level forensic acquisition | Not implemented |
| Write-blocker integration | Not implemented |
| Multi-user case isolation | Not implemented |
| Signed/external custody anchoring | Not implemented |
| General visual embedding search | Not implemented |

## Repository layout

```text
backend/                  FastAPI app, database, indexing, retrieval, graph, cognition
frontend/                 React/Vite application and Tauri shell
landing/                  Legacy marketing/access application; optional for local mode
scripts/                  Smoke checks and operational utilities
docs/                     Public canonical documentation
start.sh                  SSD-first local launcher
.env.example              Safe placeholder configuration
```
