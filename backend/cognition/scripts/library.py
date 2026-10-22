from __future__ import annotations


SCRIPT_LIBRARY = {
    "fundraising": {
        "description": "Raising investment for a startup or project",
        "scenes": [
            {
                "name": "research",
                "frame_types": ["document"],
                "indicators": ["investor list", "vc research", "angellist", "crunchbase"],
            },
            {
                "name": "deck_prep",
                "frame_types": ["pitch_deck"],
                "indicators": ["pitch deck", "investor deck", "slides"],
            },
            {
                "name": "outreach",
                "frame_types": ["email"],
                "indicators": ["intro", "warm intro", "cold email", "investor email"],
            },
            {
                "name": "meetings",
                "frame_types": ["meeting_notes"],
                "indicators": ["investor call", "partner meeting", "due diligence"],
            },
            {
                "name": "closing",
                "frame_types": ["contract"],
                "indicators": ["term sheet", "safe note", "signed", "funding closed"],
            },
        ],
        "typical_duration_days": 90,
    },
    "project_kickoff": {
        "description": "Starting a new project",
        "scenes": [
            {
                "name": "scoping",
                "frame_types": ["proposal", "document"],
                "indicators": ["requirements", "scope", "rfp", "brief"],
            },
            {
                "name": "contracting",
                "frame_types": ["contract"],
                "indicators": ["agreement", "contract", "sow", "nda"],
            },
            {
                "name": "kickoff",
                "frame_types": ["meeting_notes"],
                "indicators": ["kickoff meeting", "first meeting", "introductions"],
            },
            {
                "name": "execution",
                "frame_types": ["meeting_notes", "document"],
                "indicators": ["progress update", "status update", "deliverable", "milestone"],
            },
            {
                "name": "completion",
                "frame_types": ["invoice"],
                "indicators": ["final delivery", "sign off", "completed", "final invoice"],
            },
        ],
        "typical_duration_days": 90,
    },
    "hiring_process": {
        "description": "Hiring someone for a role",
        "scenes": [
            {
                "name": "job_posting",
                "frame_types": ["document"],
                "indicators": ["job description", "we are hiring", "role opening"],
            },
            {
                "name": "screening",
                "frame_types": ["resume"],
                "indicators": ["resume", "cv", "application", "shortlist"],
            },
            {
                "name": "interviews",
                "frame_types": ["meeting_notes"],
                "indicators": ["interview notes", "candidate", "technical round"],
            },
            {
                "name": "offer",
                "frame_types": ["document"],
                "indicators": ["offer letter", "compensation", "salary", "ctc"],
            },
            {
                "name": "onboarding",
                "frame_types": ["document", "email"],
                "indicators": ["onboarding", "first day", "welcome", "orientation"],
            },
        ],
        "typical_duration_days": 45,
    },
}


def humanize_script_name(script_name: str | None) -> str:
    if not script_name:
        return ""
    return str(script_name).replace("_", " ").strip().title()
