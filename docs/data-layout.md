# Data layout and deletion boundaries

All runtime data should be treated as sensitive. Git ignores `data/`, but ignored does not mean encrypted or automatically wiped.

| Path | Contents | Sensitive | Regenerable | Current deletion boundary |
|---|---|---:|---:|---|
| `data/memory.db` | Items, FTS5, watched files, cases, evidence, custody events, interactions | Yes | Partly | Search reset affects primary DB; case deletion removes case rows/events, not source files |
| `data/text_faiss.index` | Dense index vectors | Yes | Yes | Rebuildable from indexed items/embeddings |
| `data/graph.db` | Entities, episodes, temporal facts, graph relationships | Yes | Usually | Not guaranteed to be removed by primary search reset |
| `data/cognitive_layer.db` | Activation, frames, association/reranking state | Yes | Usually | Not guaranteed to be removed by primary search reset |
| `data/attachments/` | Integration attachments when used | Yes | No | Manual review required |
| `data/gmail_tokens/` | Gmail OAuth material when enabled | Critical | No | Manual revocation/deletion required |
| `data/auth_cache.json` | Local auth cache | Critical | Yes-ish | Delete after stopping the app; revoke upstream credentials if needed |
| `data/user_settings/` | Local settings and aliases | Yes | Yes | Manual review required |
| `.run/` | Logs and PIDs | Yes | Yes | Stop app, then remove if needed |
| `~/.cache/huggingface/` | Downloaded model cache | Possibly | Yes | Outside repository; manual cleanup |

## Case deletion

Deleting a case removes the local case row, case-evidence relationships, and custody-event rows. It does not delete:

- original source files;
- the indexed item in the general memory database;
- all embedding/vector state;
- graph/cognitive records that may refer to the item;
- external copies or backups.

## Full local cleanup procedure

This is an operational procedure, not an application guarantee:

1. Export or preserve any required case manifest.
2. Stop Custody.
3. Revoke optional Gmail/cloud credentials.
4. Delete the intended `data/` directory only after confirming the path.
5. Remove `.run/` if logs/PIDs must be destroyed.
6. Remove model caches separately if required.
7. Check OS trash, snapshots, backup tools, and removable-volume copies.

Do not describe this as secure erasure without a storage-specific erasure process.
