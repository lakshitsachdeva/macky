# Local forensic case playbooks

This playbook turns the current local library into four practical investigation
workspaces. The cases are designed around the evidence types already present in
the local index: cloud-architecture PDFs, lab documents, project runbooks,
architecture notes, and infrastructure screenshots.

The automation is safe by design:

- it checks folder existence without opening source files itself;
- Custody performs the normal evidence import and SHA-256 workflow;
- all four cases are case-scoped;
- verification runs after import;
- model-assisted analysis is explicit and opt-in;
- reports are limited to eight focused sources to keep Gemini citations reliable;
- no API key is printed or stored in the repository.

## Create all four cases

Make sure the local app is running:

```bash
./start.sh
```

Create the cases and run the first report for each:

```bash
./.venv-custody/bin/python scripts/create_local_case_playbooks.py --allow-model
```

For fully local deterministic reports:

```bash
./.venv-custody/bin/python scripts/create_local_case_playbooks.py
```

The script writes a local summary to:

```text
data/demo/local-case-playbooks.json
```

That file is ignored by Git.

## CASE-01 — AWS Cloud Architecture Evidence Review

### Purpose

Reconstruct a cloud-architecture assessment as an infrastructure evidence
review. This is the strongest current case for practicing cloud-forensics
thinking: resource topology, deployment stages, screenshots, SQL evidence, and
written assessment artifacts can be reviewed as one bounded case.

### Evidence source

The automation uses the local folder convention:

```text
~/Downloads/AWS_Academy_Cloud_Architecting_Final_Assessment
```

The case includes supported public evidence such as Markdown, PDF, and image
artifacts. Unsupported files are reported rather than silently treated as
evidence.

### Investigation question

```text
Build a cloud-infrastructure evidence timeline. Identify what the artifacts prove, what is only implied, and what integrity or coverage gaps should be reviewed.
```

### Step-by-step in the app

1. Open **Cases**.
2. Select **CASE-01 | AWS Cloud Architecture Evidence Review**.
3. Review the evidence manifest and SHA-256 values.
4. Run **verify evidence**.
5. Open the **forensic timeline**.
6. Review screenshot, report, and infrastructure artifact names.
7. Run the investigation question above.
8. Compare every finding with its numbered source citation.
9. Export the manifest.

### What to look for

- VPC/resource-map evidence;
- EC2 and load-balancer stages;
- target-group health-check evidence;
- SQL import evidence;
- screenshots that show success versus screenshots that only show a page;
- missing timestamps, duplicate-looking artifacts, or incomplete coverage;
- claims in written reports that are not supported by a source artifact.

### Honest limitation

An image filename or indexed metadata cannot prove everything visible inside an
image. Visual OCR is not enabled in the current local runtime, so visual
confirmation remains a human review step.

## CASE-02 — DispatchX Engineering Change Timeline

### Purpose

Use project documentation as engineering-change evidence. This case is useful
for incident response, handoff review, architecture drift, and operational
readiness analysis.

### Evidence source

```text
~/Documents/ChatGPT/New project/DispatchX/docs
```

### Investigation question

```text
Reconstruct the documented engineering timeline. Which decisions, handoffs, and scalability claims are supported by sources, and what should an incident reviewer verify next?
```

### Step-by-step in the app

1. Select **CASE-02 | DispatchX Engineering Change Timeline**.
2. Sort or inspect the manifest by filename and modified time.
3. Run verification.
4. Review the forensic timeline for evidence registration and custody events.
5. Ask the investigation question.
6. Separate documented fact, stated plan, and model inference.
7. Export the report/manifest hashes into the project review notes.

### What to look for

- handoff documents and runbooks;
- architecture decisions;
- scalability assumptions;
- operational gaps;
- whether later documents supersede earlier claims;
- whether a claim has one source or multiple independent sources.

### Best use

This is a strong internal post-incident or project-handoff case. It does not
prove that the documented system actually ran as described; it proves what the
preserved documents record.

## CASE-03 — Cloud Computing Lab Evidence Chain

### Purpose

Build a provenance-aware academic/technical experiment case from a collection
of cloud-computing PDFs. This demonstrates duplicate detection, chronological
review, and source verification at document scale.

### Evidence source

```text
~/Downloads/CC_B102_PDFs
```

### Investigation question

```text
Create a cited inventory of the cloud-computing experiment evidence. Identify the sequence, duplicate-looking artifacts, and any gaps that would matter in a technical review.
```

### Step-by-step in the app

1. Select **CASE-03 | Cloud Computing Lab Evidence Chain**.
2. Review the PDF filenames and recorded sizes.
3. Verify all evidence.
4. Use the timeline to identify acquisition order and custody events.
5. Ask the investigation question.
6. Check whether repeated names represent duplicates, revisions, or separate
   source files.
7. Export the case manifest.

### What to look for

- experiment numbering;
- missing experiment numbers;
- same-named files with different sizes or hashes;
- report/certificate material mixed with experiment evidence;
- whether the available documents support a complete sequence.

### Honest limitation

Filename order is not proof of experiment execution order. Use embedded document
timestamps and human review where the investigation requires stronger chronology.

## CASE-04 — Cloud Infrastructure Screenshot Board

### Purpose

Treat a screenshot collection as a bounded visual-provenance case. This is
useful for reviewing dashboard evidence, cloud console stages, and whether a
submission contains coverage for each infrastructure task.

### Evidence source

```text
~/Downloads/capstone-screenshots
```

### Investigation question

```text
Inventory the screenshot evidence by infrastructure area and explain what can be established from filenames and indexed metadata versus what requires visual or source-level confirmation.
```

### Step-by-step in the app

1. Select **CASE-04 | Cloud Infrastructure Screenshot Board**.
2. Review the image manifest, sizes, mtimes, and hashes.
3. Verify all images.
4. Inspect the forensic timeline.
5. Run the investigation question.
6. Open individual source files manually for visual confirmation.
7. Record which claims are filename-level, metadata-level, or visually verified.

### What to look for

- VPC/resource-map coverage;
- EC2 state and launch-template evidence;
- ALB and target-group health-check evidence;
- SQL import evidence;
- missing task numbers;
- screenshots that may be duplicates or alternate revisions.

### Honest limitation

This case does not make screenshots self-authenticating. A screenshot can be
edited, detached from its original context, or captured at an unknown time.
Custody records its local source digest and metadata; the investigator still
needs context, corroboration, and an appropriate evidence-handling procedure.

## Shared report-review checklist

For every case:

- [ ] The case name identifies the investigation scope.
- [ ] The evidence manifest contains expected files.
- [ ] Every source has a SHA-256 value where acquisition succeeded.
- [ ] Verification has been run after import.
- [ ] `changed`, `missing`, and `unreadable` results were reviewed.
- [ ] The timeline bounds and timezone are understood.
- [ ] Every important report statement has a valid source citation.
- [ ] Model inference is separated from observed evidence.
- [ ] The report hash and manifest hash were preserved.
- [ ] External Gemini processing was explicitly permitted for the selected case.
- [ ] The final interpretation does not claim legal admissibility or intent without
      independent evidence and qualified review.

## Do not rerun blindly

Re-running the playbook is safe for indexing, but each import can append new
case custody events. Use the existing case in the UI for review, and rerun the
script only when you intentionally want a new acquisition/checkpoint event.
