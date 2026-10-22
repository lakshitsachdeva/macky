#!/usr/bin/env python3
"""Run a safe, reproducible Custody forensic-case demonstration.

The demo uses repository documentation as synthetic/public evidence. It does not
read environment files or print credentials. With ``--allow-model`` it sends
only the selected public-document excerpts to the configured Gemini provider.
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEMO_CASE_NAME = "Custody Forensic Demo - Documentation Self-Audit"
DEMO_EVIDENCE = (
    ROOT / "README.md",
    ROOT / "docs" / "cases.md",
    ROOT / "docs" / "forensic-boundaries.md",
    ROOT / "docs" / "privacy.md",
)


def request_json(
    base_url: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> Any:
    body = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=body,
        headers=headers,
        method=method,
    )
    try:
        with urllib.request.urlopen(request, timeout=90) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {path} failed with HTTP {exc.code}: {detail[:500]}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Unable to reach Custody at {base_url}: {exc.reason}") from exc
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Custody returned non-JSON data for {method} {path}") from exc


def find_or_create_case(base_url: str) -> dict[str, Any]:
    cases = request_json(base_url, "GET", "/cases")
    if isinstance(cases, list):
        for case in cases:
            if str(case.get("name") or "").strip() == DEMO_CASE_NAME:
                return case

    created = request_json(
        base_url,
        "POST",
        "/cases",
        {
            "name": DEMO_CASE_NAME,
            "description": "Synthetic/public documentation review demonstrating case-scoped forensic analysis.",
            "notes": (
                "Demo evidence is repository documentation. This is an investigative-aid workflow, "
                "not a legal-admissibility validation."
            ),
        },
    )
    if not isinstance(created, dict) or not created.get("id"):
        raise RuntimeError("Custody created a demo case without an id")
    return created


def summarize_import(payload: dict[str, Any]) -> dict[str, Any]:
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


def write_full_report(output_path: Path, payload: dict[str, Any]) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def run_demo(base_url: str, *, allow_model: bool, output_path: Path | None) -> dict[str, Any]:
    missing = [str(path.relative_to(ROOT)) for path in DEMO_EVIDENCE if not path.is_file()]
    if missing:
        raise RuntimeError(f"Demo evidence is missing: {', '.join(missing)}")

    case = find_or_create_case(base_url)
    case_id = str(case["id"])
    import_payload = request_json(
        base_url,
        "POST",
        f"/cases/{case_id}/evidence/import",
        {"paths": [str(path) for path in DEMO_EVIDENCE], "hash_algorithm": "sha256"},
    )
    verification = request_json(base_url, "POST", f"/cases/{case_id}/verify")
    timeline = request_json(base_url, "GET", f"/cases/{case_id}/forensic-timeline?limit=1000")
    report = request_json(
        base_url,
        "POST",
        f"/cases/{case_id}/report",
        {
            "question": (
                "Review this case as a cyber-forensics documentation self-audit. "
                "What evidence and provenance controls are present, what integrity warnings matter, "
                "and what limitations must an investigator record?"
            ),
            "allow_model": allow_model,
            "limit": 10,
        },
    )
    exported = request_json(base_url, "GET", f"/cases/{case_id}/export")

    if output_path is not None:
        write_full_report(output_path, report)

    generation = report.get("generation") if isinstance(report.get("generation"), dict) else {}
    result = {
        "demo_date": date.today().isoformat(),
        "case_id": case_id,
        "case_name": case.get("name") or DEMO_CASE_NAME,
        "evidence": summarize_import(import_payload if isinstance(import_payload, dict) else {}),
        "verification": {
            key: verification.get(key, 0)
            for key in ("verified", "changed", "missing", "unreadable")
        },
        "timeline": {
            "entries": timeline.get("entry_count", len(timeline.get("entries", []))),
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
        "manifest_sha256": exported.get("manifest_sha256") if isinstance(exported, dict) else None,
        "full_report_path": str(output_path) if output_path is not None else None,
    }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--allow-model",
        action="store_true",
        help="allow Gemini/model-assisted analysis of the public demo excerpts",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "data" / "demo" / "forensic-demo-report.json",
        help="ignored local path for the full report JSON",
    )
    args = parser.parse_args()

    try:
        result = run_demo(args.base_url, allow_model=args.allow_model, output_path=args.output)
    except RuntimeError as exc:
        print(f"FORENSIC DEMO FAILED: {exc}", file=sys.stderr)
        return 1

    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
