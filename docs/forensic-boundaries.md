# Forensic boundaries

## Precise claim

Custody is a searchable analysis workspace that records local source observations. It is not a validated forensic acquisition platform.

The strongest accurate statement is:

> Custody reads selected source paths, computes SHA-256 observations, indexes derived metadata/content locally, associates those observations with a case, and records a locally hash-linked event history.

## What the import path does

- reads supported source files;
- computes a streaming SHA-256 digest;
- records source path, size, and nanosecond mtime;
- indexes derived text/metadata for retrieval;
- writes local case/evidence/event records.

## What it does not do

- acquire raw sectors or a forensic image;
- use a hardware or software write blocker;
- preserve deleted files, slack space, unallocated space, APFS snapshots, MFT records, or filesystem journals;
- capture a complete metadata/security descriptor set;
- guarantee the source remained unchanged while it was being read;
- create an immutable copy of the original;
- sign the manifest with an independent private key;
- anchor event hashes to an external timestamp/notary;
- prevent a local administrator from modifying SQLite/FAISS/graph files;
- prove legal admissibility in any jurisdiction.

## Source preservation

“Does not modify the source” means the import code does not intentionally rewrite, move, rename, or delete it. It does not mean:

- the source is immutable;
- the source cannot be changed by another process;
- the filesystem metadata is preserved forever;
- the source has been independently acquired;
- the resulting record is automatically admissible.

## Chain-of-custody language

Use:

- “hash-linked local custody event log”;
- “SHA-256 source observation”;
- “case provenance record”;
- “verification observed the source as unchanged/changed at the time of the check.”

Do not use without substantial additional controls:

- “tamper-proof chain of custody”;
- “forensically sound acquisition”;
- “court-ready evidence”;
- “immutable evidence locker”;
- “legally admissible evidence.”

## Recommended operational pairing

For a real investigation, pair Custody with an approved acquisition and evidence-handling process:

1. acquire using validated tooling and a documented write-blocked procedure;
2. record acquisition hashes and independent chain-of-custody records;
3. use Custody on an approved analysis copy or export;
4. preserve the original acquisition and tool logs separately;
5. record Custody version, configuration, model/OCR availability, and manifest hash.

## Investigation record checklist

Record outside the application where required:

- examiner and case identifiers;
- source device/volume identity;
- acquisition tool/version and acquisition time;
- write-blocker details;
- original acquisition hash set;
- Custody build/commit/version;
- enabled integrations and data egress decisions;
- model/OCR versions;
- exact source paths and mount states;
- exported manifest hash;
- exceptions, failed files, and re-verification results.
