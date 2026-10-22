# Search behavior

## Query modes

Custody accepts ordinary language, filenames, paths, identifiers, people, source hints, and time expressions. The parser classifies intent and removes low-signal words before retrieval.

Examples:

```text
incident response checklist
sha256 abc123...
screenshots containing iit madras
what was I working on last week
files from the evidence export
```

## Retrieval layers

### SQLite FTS5

FTS5 searches:

- title;
- filename;
- preview text;
- original path;
- extracted text;
- summary;
- keywords.

This is the most dependable fallback when the embedding model is cold.

### Dense retrieval

When the local embedding model is ready, FAISS contributes semantic similarity candidates. Dense similarity alone is not treated as proof of an exact marker match.

### Sparse and lexical scoring

Sparse weights, filename overlap, path overlap, content overlap, metadata, and source-type signals help exact and partial matches compete with semantic results.

### Time and graph signals

Temporal queries can add records from a time window. Graph candidates can connect entities, episodes, and relationships for complex questions. Relative-date language is useful but should be checked against the actual index timestamps; it is not a legal timeline generator.

### Case scope

`case_id` is supported by the normal search, search briefing, and Ask flows. A selected case is a hard evidence intersection. The UI avoids silently selecting empty/stale cases and displays the active scope.

## Honest empty results

Exact identifier-style queries require meaningful lexical/path/time/graph evidence. If an invented marker has no evidence, Custody returns an empty result rather than random nearest neighbors.

This is important for investigations: “no match” is a meaningful result, while an unrelated semantic neighbor can create a false lead.

## Indexing state

Search may be available while indexing is incomplete. The overlay shows:

- active scan progress;
- files still pending;
- watched files being prepared;
- scope (`all evidence` or a named case).

If a newly added file is not searchable yet, wait for its indexed status or inspect `/index/status` rather than assuming the query engine failed.

## Temporal language limitation

Relative phrases such as “today,” “last week,” and “last month” currently use implementation-defined rolling windows and should not be interpreted as a formal calendar boundary without checking the exact result timestamps. The temporal test suite uses relative fixtures, but calendar-boundary semantics remain an area for future hardening.

## Ask mode

`⌘↵` sends the question through retrieval and optional synthesis. If no model provider is configured, the deterministic heuristic fallback can still return source-oriented results. Always inspect the cited sources; a generated answer is not independent evidence.
