#!/usr/bin/env python3
"""Create practical forensic case playbooks from the current local library.

This script inspects only path existence and lets Custody perform the normal
evidence import/verification flow. It never opens or prints source-file content.
Model-assisted reports are opt-in because selected excerpts may be sent to
Gemini when ``--allow-model`` is used.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


BASE_URL = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[1]


def request_json(method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{BASE_URL}{path}", data=body, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Unable to reach Custody at {BASE_URL}: {exc.reason}") from exc


def case_id(case: dict[str, Any]) -> str:
    value = case.get("id") or case.get("case_id")
    if not value:
        raise RuntimeError("Custody returned a case without an id")
    return str(value)


def get_or_create_case(spec: dict[str, Any], existing: list[dict[str, Any]]) -> dict[str, Any]:
    for case in existing:
        if str(case.get("name") or "").strip() == spec["name"]:
            return case
    created = request_json(
        "POST",
        "/cases",
        {
            "name": spec["name"],
            "description": spec["description"],
            "notes": spec["notes"],
        },
    )
    if not isinstance(created, dict):
        raise RuntimeError(f"Invalid case response for {spec['name']}")
    return created


def compact_import(payload: dict[str, Any]) -> dict[str, Any]:
    summary_raw = payload.get("summary")
    summary: dict[str, Any] = summary_raw if isinstance(summary_raw, dict) else {}
    return {
        "requested": payload.get("requested", 0),
        "discovered": payload.get("discovered", 0),
        "imported": summary.get("imported", 0),
        "skipped": summary.get("skipped", 0),
        "failed": summary.get("failed", 0),
        "by_status": summary.get("by_status", {}),
    }


def build_specs(home: Path) -> list[dict[str, Any]]:
    return [
        {
            "name": "CASE-01 | AWS Cloud Architecture Evidence Review",
            "path": home / "Downloads" / "AWS_Academy_Cloud_Architecting_Final_Assessment",
            "description": "Review cloud-architecture assessment evidence as a case-scoped infrastructure investigation.",
            "notes": "Focus: VPC, EC2, load balancing, SQL evidence, screenshots, sequence, and missing proof.",
            "question": "Build a cloud-infrastructure evidence timeline. Identify what the artifacts prove, what is only implied, and what integrity or coverage gaps should be reviewed.",
        },
        {
            "name": "CASE-02 | DispatchX Engineering Change Timeline",
            "path": home / "Documents" / "ChatGPT" / "New project" / "DispatchX" / "docs",
            "description": "Reconstruct engineering evolution and operational handoffs from the DispatchX documentation set.",
            "notes": "Focus: chronology, architecture decisions, handoff claims, scalability risks, and operational readiness.",
            "question": "Reconstruct the documented engineering timeline. Which decisions, handoffs, and scalability claims are supported by sources, and what should an incident reviewer verify next?",
        },
        {
            "name": "CASE-03 | Cloud Computing Lab Evidence Chain",
            "path": home / "Downloads" / "CC_B102_PDFs",
            "description": "Create a provenance-aware academic cloud-computing evidence case from experiment PDFs.",
            "notes": "Focus: experiment sequence, repeated artifacts, missing/duplicate evidence, and verification state.",
            "question": "Create a cited inventory of the cloud-computing experiment evidence. Identify the sequence, duplicate-looking artifacts, and any gaps that would matter in a technical review.",
        },
        {
            "name": "CASE-04 | Cloud Infrastructure Screenshot Board",
            "path": home / "Downloads" / "capstone-screenshots",
            "description": "Review screenshot evidence from cloud infrastructure tasks as a bounded visual-provenance case.",
            "notes": "Focus: VPC, EC2, ALB, target-group health checks, SQL import evidence, and filename-level coverage.",
            "question": "Inventory the screenshot evidence by infrastructure area and explain what can be established from filenames and indexed metadata versus what requires visual or source-level confirmation.",
        },
    ]


def run(allow_model: bool) -> list[dict[str, Any]]:
    existing_payload = request_json("GET", "/cases")
    existing = existing_payload if isinstance(existing_payload, list) else []
    results: list[dict[str, Any]] = []

    for spec in build_specs(Path.home()):
        path = Path(spec["path"])
        if not path.is_dir():
            results.append({"name": spec["name"], "status": "missing_folder"})
            continue

        case = get_or_create_case(spec, existing)
        current_case_id = case_id(case)
        import_payload = request_json(
            "POST",
            f"/cases/{current_case_id}/evidence/import",
            {"paths": [str(path)], "hash_algorithm": "sha256"},
        )
        verification = request_json("POST", f"/cases/{current_case_id}/verify")
        timeline = request_json(
            "GET", f"/cases/{current_case_id}/forensic-timeline?limit=5000"
        )
        report = request_json(
            "POST",
            f"/cases/{current_case_id}/report",
            # Keep the source set bounded so Gemini receives a focused,
            # citation-friendly case packet instead of an oversized prompt.
            {"question": spec["question"], "allow_model": allow_model, "limit": 8},
        )
        generation = report.get("generation") if isinstance(report.get("generation"), dict) else {}
        results.append(
            {
                "name": spec["name"],
                "case_id": current_case_id,
                "status": "ready",
                "import": compact_import(import_payload),
                "verification": {
                    key: verification.get(key, 0)
                    for key in ("verified", "changed", "missing", "unreadable")
                },
                "timeline": {
                    "entries": timeline.get("entry_count", 0),
                    "evidence_count": timeline.get("evidence_count", 0),
                    "custody_event_count": timeline.get("custody_event_count", 0),
                },
                "report": {
                    "source_count": report.get("source_count", 0),
                    "finding_count": len((report.get("report") or {}).get("findings") or []),
                    "warning_count": len(report.get("warnings") or []),
                    "provider": report.get("provider") or generation.get("provider"),
                    "model_used": bool(generation.get("model_used")),
                    "fallback_used": bool(generation.get("fallback_used")),
                    "report_sha256": report.get("report_sha256"),
                },
            }
        )
        existing.append(case)

    output = ROOT / "data" / "demo" / "local-case-playbooks.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2) + "\n", encoding="utf-8")
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--allow-model",
        action="store_true",
        help="allow Gemini analysis of selected excerpts; otherwise use deterministic fallback",
    )
    args = parser.parse_args()
    try:
        print(json.dumps(run(bool(args.allow_model)), indent=2))
    except RuntimeError as exc:
        print(f"CASE PLAYBOOK CREATION FAILED: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
