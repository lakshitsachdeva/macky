# Security model and threat model

## Security posture

Custody has useful local-development controls:

- loopback backend binding in the normal runtime;
- explicit local mode;
- request models with field/length/control-character validation;
- local cloud-integration disablement in `start.sh`;
- ignored runtime data and environment files;
- atomic-ish FAISS save paths and SQLite WAL configuration;
- visible indexing and evidence status.

These controls do not make the application a hardened multi-user service.

## Threat model

### In scope

- accidental cloud use during local evidence analysis;
- stale watched folders causing unintended indexing;
- search returning unrelated dense neighbors for exact markers;
- credentials accidentally committed or bundled;
- local runtime files being copied into a public release;
- missing/changed source evidence;
- a local process reaching the loopback API.

### Out of scope for the current release

- hostile root/admin users on the Mac;
- a compromised Python/Node/Rust toolchain;
- kernel/filesystem compromise;
- malicious evidence exploiting every parser dependency;
- multi-tenant isolation;
- secure erasure from SSDs, snapshots, backups, or trash;
- legal validation or courtroom admissibility.

## Secure operating rules

1. Run only on a trusted local account.
2. Keep the backend on loopback; do not expose port 8000 to a LAN.
3. Use an isolated SSD/runtime directory for sensitive work.
4. Keep cloud integrations disabled unless explicitly approved.
5. Treat SQLite, FAISS, graph, cognitive DBs, logs, and model caches as sensitive.
6. Use a separate acquisition and write-blocking process for original media.
7. Run fixture tests before indexing real evidence.
8. Redact logs before sharing.
9. Never paste tokens or evidence into GitHub issues.
10. Pin/review dependencies before a production or investigative deployment.

## Known release blockers

- plaintext local auth/session material remains possible;
- case ownership is not multi-user scoped;
- desktop release configuration must reject secret propagation;
- source mutation during hashing is not cryptographically prevented;
- no packaged clean-machine/notarization evidence is included;
- parser hardening for hostile documents is not a substitute for sandboxing.

## Reporting a vulnerability

See [`SECURITY.md`](../SECURITY.md). Do not include live evidence or credentials. Prefer a minimal synthetic fixture and a private report.
