# Custody forensic case lab

This lab is a safe demonstration of the current application. It uses the
project’s own public documentation as synthetic evidence, so it shows the
complete acquisition, verification, timeline, and natural-language report flow
without requiring personal evidence.

## Learning objectives

After completing the lab, you should be able to:

- create a local case;
- import a directory or selected files;
- identify an evidence SHA-256 record;
- verify a source and interpret changed/missing/unreadable states;
- read the combined forensic timeline;
- ask a case-scoped natural-language question;
- validate the answer against numbered source citations;
- understand when model assistance sends excerpts to Gemini;
- export a reproducible case manifest.

## Prerequisites

From the repository root:

```bash
./.venv-custody/bin/python --version
node --version
```

Start the local app:

```bash
./start.sh
```

Confirm readiness:

```bash
curl -fsS http://127.0.0.1:8000/ready
curl -fsS http://127.0.0.1:8000/health
```

The app should be available at:

```text
http://localhost:5173
```

## Option A: browser walkthrough

1. Open **Cases**.
2. Create a case named `Custody Forensic Demo - Documentation Self-Audit`.
3. In the evidence path box, add these paths, one per line:

   ```text
   README.md
   docs/cases.md
   docs/forensic-boundaries.md
   docs/privacy.md
   ```

4. Click **import evidence**.
5. Confirm the evidence manifest shows four files and SHA-256 values.
6. Click **verify evidence**.
7. Review the **forensic timeline**.
8. Ask:

   ```text
   What provenance controls are present, what integrity warnings should I review, and what limitations must an investigator record?
   ```

9. Leave **allow model-assisted analysis** off for a local-only report.
10. Review the findings and numbered source citations.
11. If you have explicitly approved Gemini processing for these public docs,
    enable the model toggle and run the same question again.
12. Export the case manifest.

## Option B: one-command live demo

The repository includes an end-to-end driver:

```bash
./.venv-custody/bin/python scripts/run_forensic_demo.py
```

That runs local deterministic analysis. To test the configured Gemini path on
the public documentation excerpts:

```bash
./.venv-custody/bin/python scripts/run_forensic_demo.py --allow-model
```

The command prints only a safe summary. It does not print environment-file
contents or source-path contents. The full report is stored in the ignored file:

```text
data/demo/forensic-demo-report.json
```

Example summary shape:

```json
{
  "case_name": "Custody Forensic Demo - Documentation Self-Audit",
  "evidence": {"imported": 4, "failed": 0},
  "verification": {"verified": 4, "changed": 0, "missing": 0, "unreadable": 0},
  "timeline": {"evidence_count": 4, "custody_event_count": 4},
  "report": {
    "source_count": 4,
    "provider": "heuristic",
    "model_used": false,
    "fallback_used": true
  }
}
```

When Gemini is configured and reachable, the model fields should change to
`provider: "gemini"`, `model_used: true`, and `fallback_used: false`, unless
structured output validation rejects the provider response.

## What to inspect

### Evidence manifest

Check that each source has:

- filename;
- original path;
- SHA-256;
- size;
- nanosecond mtime;
- acquisition status.

### Verification

All four public documentation sources should verify when they have not changed.
If you edit one after import, its result should become `changed`.

### Forensic timeline

The timeline should include evidence-registration entries and custody events. It
is scoped to the demo case, not the entire indexed library.

### Report

The report should include:

- a summary;
- source-cited findings;
- limitations;
- provider/fallback metadata;
- warnings, if any;
- source hashes and current verification state;
- a report SHA-256.

## Controlled mutation exercise

To demonstrate integrity detection safely:

1. Run the demo once.
2. Add a harmless line to one of the demo Markdown files.
3. Run **verify evidence** again.
4. Observe `changed` for that source.
5. Revert the documentation change if desired.

Do not perform this exercise on real investigation evidence.

## Interpretation

This lab demonstrates provenance-aware local review, not forensic acquisition.
The report can say that a recorded digest matches or differs. It cannot say that
the source is legally admissible, that no other copy exists, or that a person’s
intent has been proven.
