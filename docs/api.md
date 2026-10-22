# API surface

The FastAPI app is the source of truth. This page documents the primary local workflow rather than every compatibility endpoint.

## Access

Local SSD mode uses the local-forensics access path. Cloud-authenticated mode may require a bearer token. Do not expose the local mode as a shared network service.

## Health and status

```text
GET /health
GET /ready
GET /auth/status
GET /index/status
POST /index/trigger
GET /cognitive/status
GET /graph/status
GET /llm/status
```

## Search

```text
POST /search
POST /search/briefing
POST /ask
```

Example:

```bash
curl -fsS -X POST http://127.0.0.1:8000/search \
  -H 'content-type: application/json' \
  --data '{"query":"incident response checklist","limit":10}'
```

`/search` accepts:

```json
{
  "query": "incident response checklist",
  "limit": 10,
  "explain": false,
  "case_id": null
}
```

When `explain` is enabled, the response includes debug pipeline/candidate information. Do not share debug responses if they expose sensitive paths or titles.

## Cases

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

Create:

```json
{
  "name": "IR-2026-001",
  "description": "Synthetic fixture investigation",
  "notes": "Do not put credentials here."
}
```

Import:

```json
{
  "paths": ["/path/to/evidence"],
  "hash_algorithm": "sha256"
}
```

Forensic timeline:

```text
GET /cases/{case_id}/forensic-timeline?start_at=<ISO-8601>&end_at=<ISO-8601>&limit=1000
```

This combines case evidence registration records with case-local custody events.
Evidence membership comes from `case_evidence`; records from other cases are not
included. Bounds are inclusive and normalized to UTC.

Natural-language case report:

```text
POST /cases/{case_id}/report
```

```json
{
  "question": "What evidence was recorded and what integrity warnings should I review?",
  "allow_model": false,
  "limit": 10
}
```

The response includes deterministic source citations, SHA-256 values, acquisition
and current verification status, custody context, warnings, and a
`report_sha256`. With `allow_model: true`, the configured local provider may
generate a structured report; model output is accepted only when its source
citations validate. Model-assisted analysis is an investigative aid and does
not determine legal admissibility.

## Folders and items

```text
GET    /folders/discover
GET    /folders/watched
POST   /folders/estimate
POST   /folders/add
DELETE /folders/remove
POST   /folders/open-picker
GET    /items
GET    /items/{item_id}
POST   /items/{item_id}/open
POST   /items/{item_id}/reveal
DELETE /items/{item_id}
```

## API limitations

Not every graph/temporal/items endpoint accepts a case scope. Do not assume case isolation for endpoints not explicitly documented as scoped. Inspect the route signature before adding a forensic UI flow.
