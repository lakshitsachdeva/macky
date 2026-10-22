# Privacy and data flows

## Local-first does not mean “nothing can ever leave the device”

The SSD-first launcher disables cloud credentials and Gemini enrichment for its process. The repository also contains optional integrations. Privacy claims must describe the actual launch mode and enabled integrations.

## Data inventory

| Data | Local location/processing | Possible external destination |
|---|---|---|
| Source files | Original source paths; read by indexer | None in local mode; optional enrichment can change this |
| Extracted text | `data/memory.db`, embeddings/indexes | Gemini if explicitly enabled for enrichment |
| Image bytes | Local extractor/OCR path | Gemini vision if explicitly enabled |
| Query text | Request/UI and interaction storage | No documented sync in local mode |
| Case metadata | `data/memory.db` | None in local mode |
| Custody events | `data/memory.db` | None in local mode |
| Auth/session data | local cache/session files | Supabase when cloud auth is enabled |
| Gmail tokens/messages | `data/gmail_tokens/` and connector state | Gmail APIs when enabled |
| Updates | Tauri updater metadata/downloads | Configured update endpoint |
| Model cache | Hugging Face cache, often outside repo | Download endpoint during first setup |

## Local mode checklist

Before handling sensitive evidence:

- confirm `RECURDO_LOCAL_MODE=1` in the launched process;
- confirm Supabase and Gemini environment variables are empty;
- confirm Gmail is not connected;
- confirm the selected `data/` directory is on the intended volume;
- confirm the backend binds only to loopback;
- disable or restrict automatic update behavior if the environment requires it;
- record the installed build and enabled integrations.

## Desktop secret rules

- Never embed a Gemini key in a desktop bundle.
- Never ship a Supabase service-role key to a client.
- Never commit `.env`, `.env.local`, token directories, auth caches, or runtime databases.
- For local Gemini use, keep the key in ignored `data/backend.env`; never put it in the repository or a frontend build.
- Prefer the macOS Keychain for future credential storage instead of plaintext JSON.
- Redact absolute personal paths, email addresses, file names, and evidence excerpts from public bug reports.

## Personal data in the index

Indexing a folder can capture names, email addresses, identifiers, document contents, screenshots, and metadata. The user—not the application—must decide whether a folder is appropriate for a case and whether the resulting database can be retained or shared.
