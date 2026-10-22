from __future__ import annotations

import json
import re
from collections import Counter
from typing import Any


STOPWORDS = {
    "the",
    "and",
    "for",
    "with",
    "that",
    "this",
    "from",
    "into",
    "your",
    "have",
    "will",
    "are",
    "was",
    "were",
    "you",
    "our",
    "about",
    "notes",
    "note",
    "file",
    "document",
    "image",
    "screenshot",
    "version",
    "final",
    "draft",
    "copy",
    "new",
    "updated",
    "todo",
    "please",
    "thanks",
    "thank",
    "attached",
    "regards",
    "best",
    "hello",
    "team",
    "dear",
}

QUERY_ENTITY_STOPWORDS = STOPWORDS | {
    "what",
    "when",
    "where",
    "which",
    "who",
    "whom",
    "whose",
    "why",
    "how",
    "did",
    "does",
    "doing",
    "work",
    "working",
    "worked",
    "saved",
    "save",
    "created",
    "modified",
    "opened",
    "accessed",
    "find",
    "show",
    "looking",
    "look",
    "doc",
    "docs",
    "documents",
    "files",
    "major",
    "project",
    "projects",
    "related",
    "associated",
    "similar",
    "thing",
    "things",
    "stuff",
    "jan",
    "january",
    "feb",
    "february",
    "mar",
    "march",
    "apr",
    "april",
    "may",
    "jun",
    "june",
    "jul",
    "july",
    "aug",
    "august",
    "sep",
    "sept",
    "september",
    "oct",
    "october",
    "nov",
    "november",
    "dec",
    "december",
}

ENTITY_BLOCKLIST = STOPWORDS | QUERY_ENTITY_STOPWORDS | {
    "it",
    "no",
    "yes",
    "low",
    "high",
    "print",
    "apply",
    "true",
    "false",
    "none",
    "null",
    "void",
    "help",
    "done",
    "add",
    "show",
    "get",
    "set",
    "run",
    "ok",
    "na",
    "n/a",
    "tbd",
}
CUSTOM_BLOCKLIST: set[str] = {
    # Add document-domain junk terms here after reviewing live extractions.
}
CUSTOM_BLOCKLIST_NORMALIZED = {
    re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", term.casefold())).strip()
    for term in CUSTOM_BLOCKLIST
    if str(term).strip()
}
ALLOWED_SHORT_ENTITIES = {
    "ai",
    "ml",
    "ui",
    "ux",
    "hr",
    "pm",
    "qa",
    "cv",
    "db",
    "os",
}
PERSON_NAME_BLOCKLIST = {
    "accepted",
    "aim",
    "aligned",
    "all",
    "apply",
    "architecture",
    "book",
    "content",
    "contents",
    "course",
    "defect",
    "director",
    "document",
    "documents",
    "executive",
    "feature",
    "features",
    "flight",
    "graph",
    "implementation",
    "introduction",
    "layer",
    "layers",
    "make",
    "minutes",
    "misses",
    "presentation",
    "presentations",
    "project",
    "projects",
    "prompt",
    "prompts",
    "proposal",
    "report",
    "reports",
    "resume",
    "software",
    "summary",
    "system",
    "table",
    "texture",
    "top",
    "workflow",
}
PATHLIKE_ENTITY_RE = re.compile(r"[/\\]|@|https?://|www\.", re.IGNORECASE)
NUMERIC_ENTITY_RE = re.compile(r"^\d+[\d\s.,%+-]*$")

SKILL_TERMS = {
    "python",
    "react",
    "typescript",
    "javascript",
    "node",
    "sql",
    "postgres",
    "docker",
    "kubernetes",
    "aws",
    "ios",
    "android",
    "design",
    "product",
    "ml",
    "ai",
}

MONTH_PATTERN = (
    r"(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|jun(?:e)?|jul(?:y)?|"
    r"aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
)
DATE_RE = re.compile(
    rf"\b({MONTH_PATTERN}\s+\d{{1,2}}(?:,\s*\d{{4}})?)\b",
    re.IGNORECASE,
)
AMOUNT_RE = re.compile(
    r"((?:₹|\$|€|£)\s?\d[\d,]*(?:\.\d+)?)",
    re.IGNORECASE,
)
EMAIL_RE = re.compile(
    r"\b([A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,})\b",
    re.IGNORECASE,
)
PROPER_NAME_RE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2})\b"
)
ORGANISATION_RE = re.compile(
    r"\b(?:inc|llc|ltd|corp|corporation|company|university|college|institute|labs|studio|capital|ventures|technologies|tech)\b",
    re.IGNORECASE,
)
INVOICE_NUMBER_RE = re.compile(
    r"\binvoice\s*(?:#|number|no\.?)\s*([A-Z0-9-]+)\b",
    re.IGNORECASE,
)
EXPERIENCE_RE = re.compile(
    r"\b(\d+\+?\s+years?)\b",
    re.IGNORECASE,
)
POSITION_RE = re.compile(
    r"\b(?:position|role|applying for)\s*[:\-]?\s*([A-Za-z][A-Za-z /-]{3,80})",
    re.IGNORECASE,
)
QUESTION_RE = re.compile(r"\?")

FRAME_REGISTRY: dict[str, dict[str, Any]] = {
    "proposal": {
        "isa": "document",
        "slots": {
            "recipient": {"type": "person", "default": None},
            "project_name": {"type": "text", "default": None},
            "proposed_budget": {"type": "amount", "default": None},
            "deadline": {"type": "date", "default": None},
            "status": {
                "type": "status",
                "default": "draft",
                "values": ["draft", "submitted", "accepted", "rejected"],
            },
            "key_asks": {"type": "list", "default": []},
        },
        "triggers": ["proposal", "proposed", "scope of work", "deliverables", "we propose"],
        "scripts": ["client_outreach", "project_kickoff"],
    },
    "invoice": {
        "isa": "financial_document",
        "slots": {
            "vendor": {"type": "person", "default": None},
            "client": {"type": "person", "default": None},
            "amount": {"type": "amount", "default": None},
            "due_date": {"type": "date", "default": None},
            "invoice_number": {"type": "text", "default": None},
            "status": {
                "type": "status",
                "default": "unpaid",
                "values": ["unpaid", "paid", "overdue", "partial"],
            },
        },
        "triggers": ["invoice", "bill to", "amount due", "payment due", "total amount"],
        "scripts": ["vendor_payment", "client_billing"],
    },
    "meeting_notes": {
        "isa": "document",
        "slots": {
            "attendees": {"type": "list", "default": []},
            "date": {"type": "date", "default": None},
            "decisions": {"type": "list", "default": []},
            "action_items": {"type": "list", "default": []},
            "next_meeting": {"type": "date", "default": None},
        },
        "triggers": ["meeting notes", "attendees", "action items", "minutes", "agreed to"],
        "scripts": ["project_meeting", "investor_meeting"],
    },
    "email": {
        "isa": "communication",
        "slots": {
            "sender": {"type": "person", "default": None},
            "recipients": {"type": "list", "default": []},
            "subject": {"type": "text", "default": None},
            "date": {"type": "date", "default": None},
            "requires_response": {
                "type": "status",
                "default": "unknown",
                "values": ["yes", "no", "unknown"],
            },
        },
        "triggers": ["from:", "subject:", "reply", "cc:", "bcc:"],
        "scripts": ["email_thread", "investor_outreach", "client_communication"],
    },
    "pitch_deck": {
        "isa": "document",
        "slots": {
            "company_name": {"type": "text", "default": None},
            "ask_amount": {"type": "amount", "default": None},
            "problem": {"type": "text", "default": None},
            "solution": {"type": "text", "default": None},
            "team": {"type": "list", "default": []},
        },
        "triggers": ["pitch deck", "investor deck", "seed round", "series a", "market opportunity"],
        "scripts": ["fundraising"],
    },
    "contract": {
        "isa": "legal_document",
        "slots": {
            "parties": {"type": "list", "default": []},
            "effective_date": {"type": "date", "default": None},
            "expiry_date": {"type": "date", "default": None},
            "payment_terms": {"type": "text", "default": None},
        },
        "triggers": ["agreement", "contract", "hereby", "whereas", "parties agree", "binding"],
        "scripts": ["vendor_onboarding", "client_onboarding"],
    },
    "resume": {
        "isa": "document",
        "slots": {
            "candidate_name": {"type": "person", "default": None},
            "position_applied": {"type": "text", "default": None},
            "skills": {"type": "list", "default": []},
            "experience_years": {"type": "text", "default": None},
        },
        "triggers": ["resume", "curriculum vitae", " cv ", "work experience", "references available"],
        "scripts": ["hiring_process"],
    },
}

FRAME_DOC_TYPE_MAP = {
    "invoice": "invoice",
    "contract": "contract",
    "resume": "resume",
    "proposal": "proposal",
    "presentation": "pitch_deck",
    "pitch": "pitch_deck",
    "deck": "pitch_deck",
    "notes": "meeting_notes",
    "meeting": "meeting_notes",
    "email": "email",
}
FRAME_FILE_TYPE_HINTS = {
    "presentation": "pitch_deck",
}
FRAME_CONFIDENCE_DEFAULTS = {
    "document": 0.45,
    "visual_reference": 0.55,
    "proposal": 0.84,
    "invoice": 0.9,
    "meeting_notes": 0.8,
    "email": 0.95,
    "pitch_deck": 0.88,
    "contract": 0.86,
    "resume": 0.86,
}


def _normalize_token(token: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "-", token.casefold()).strip("-")
    return normalized


def _normalize_graph_entity_key(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", value.casefold())).strip()


def is_valid_graph_entity(name: str | None, entity_type: str | None = None) -> bool:
    cleaned = " ".join(str(name or "").split()).strip()
    if not cleaned:
        return False

    normalized = _normalize_graph_entity_key(cleaned)
    if not normalized:
        return False

    if len(normalized) < 2:
        return False
    if normalized in ENTITY_BLOCKLIST:
        return False
    if normalized in CUSTOM_BLOCKLIST_NORMALIZED:
        return False
    if PATHLIKE_ENTITY_RE.search(cleaned):
        return False
    if NUMERIC_ENTITY_RE.fullmatch(cleaned):
        return False

    compact = normalized.replace(" ", "")
    if len(compact) == 2 and compact not in ALLOWED_SHORT_ENTITIES:
        return False

    if str(entity_type or "").strip().casefold() == "misc" and len(normalized) < 4:
        return False

    return True


def is_person_like_entity(
    name: str | None,
    *,
    aliases: list[str] | None = None,
    attributes: dict[str, Any] | None = None,
) -> bool:
    cleaned = " ".join(str(name or "").split()).strip()
    if not is_valid_graph_entity(cleaned, "Person"):
        return False

    alias_values = [str(alias).strip() for alias in (aliases or []) if str(alias).strip()]
    attribute_email = str((attributes or {}).get("email") or "").strip()
    if "@" in cleaned or attribute_email or any("@" in alias for alias in alias_values):
        return True

    token_pairs: list[tuple[str, str]] = []
    for raw_token in cleaned.replace("/", " ").split():
        token = re.sub(r"^[^A-Za-z]+|[^A-Za-z]+$", "", raw_token)
        letters_only = re.sub(r"[^A-Za-z]", "", token)
        if letters_only:
            token_pairs.append((token, letters_only))

    if not 2 <= len(token_pairs) <= 4:
        return False

    for token, letters_only in token_pairs:
        normalized = letters_only.casefold()
        if len(letters_only) < 2:
            return False
        if not token[:1].isupper():
            return False
        if normalized in ENTITY_BLOCKLIST:
            return False
        if normalized in CUSTOM_BLOCKLIST_NORMALIZED:
            return False
        if normalized in PERSON_NAME_BLOCKLIST:
            return False

    return True


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return " ".join(_stringify(part) for part in value)
    return str(value)


def _extract_tokens(*texts: str) -> list[str]:
    tokens: list[str] = []
    for text in texts:
        for token in re.findall(r"[A-Za-z0-9][A-Za-z0-9_@.+/-]*", text or ""):
            normalized = token.casefold().strip("._-")
            if len(normalized) <= 2 and not normalized.isdigit():
                continue
            if normalized in STOPWORDS:
                continue
            if normalized.isdigit():
                continue
            tokens.append(normalized)
    return tokens


def _extract_phrases(text: str, *, min_words: int = 2, max_words: int = 3) -> list[str]:
    words = [token for token in _extract_tokens(text) if token not in STOPWORDS]
    phrases: list[str] = []
    seen: set[str] = set()

    for size in range(max_words, min_words - 1, -1):
        for index in range(len(words) - size + 1):
            phrase = " ".join(words[index:index + size]).strip()
            if len(phrase) < 6 or phrase in seen:
                continue
            seen.add(phrase)
            phrases.append(phrase)
    return phrases


def _extract_query_terms(query: str) -> tuple[list[str], list[str]]:
    tokens = [
        token
        for token in _extract_tokens(query)
        if token not in QUERY_ENTITY_STOPWORDS
    ]
    phrases: list[str] = []
    seen: set[str] = set()
    for size in range(3, 1, -1):
        for index in range(len(tokens) - size + 1):
            phrase = " ".join(tokens[index:index + size]).strip()
            if len(phrase) < 6 or phrase in seen:
                continue
            seen.add(phrase)
            phrases.append(phrase)
    return tokens, phrases


def _parse_keywords(raw_keywords: Any) -> list[str]:
    if raw_keywords is None:
        return []

    if isinstance(raw_keywords, list):
        return [str(entry).strip() for entry in raw_keywords if str(entry).strip()]

    text = str(raw_keywords).strip()
    if not text:
        return []

    if text.startswith("["):
        try:
            parsed = json.loads(text)
        except Exception:
            parsed = None
        if isinstance(parsed, list):
            return [str(entry).strip() for entry in parsed if str(entry).strip()]

    return [
        entry.strip()
        for entry in re.split(r"[,;\n]+", text)
        if entry.strip()
    ]


def _extract_named_phrases(*texts: str, limit: int = 8) -> list[str]:
    phrases: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in PROPER_NAME_RE.findall(text or ""):
            candidate = " ".join(match.split())
            lowered = candidate.casefold()
            if lowered in seen or lowered in STOPWORDS:
                continue
            seen.add(lowered)
            phrases.append(candidate)
            if len(phrases) >= limit:
                return phrases
    return phrases


def _extract_emails(*texts: str) -> list[str]:
    emails: list[str] = []
    seen: set[str] = set()
    for text in texts:
        for match in EMAIL_RE.findall(text or ""):
            normalized = match.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            emails.append(normalized)
    return emails


def _build_frame_content(item: dict[str, Any], *, limit_chars: int = 3000) -> str:
    return " ".join(
        part
        for part in [
            _stringify(item.get("title")),
            _stringify(item.get("filename")),
            _stringify(item.get("summary")),
            _stringify(item.get("preview_text")),
            " ".join(_parse_keywords(item.get("keywords"))),
            _stringify(item.get("extracted_text"))[:limit_chars],
        ]
        if part
    )


def _extract_amount(text: str) -> str | None:
    match = AMOUNT_RE.search(text or "")
    return match.group(1).strip() if match else None


def _extract_date(text: str, *, context_words: tuple[str, ...] = ()) -> str | None:
    haystack = text or ""
    if context_words:
        pattern = re.compile(
            rf"(?:{'|'.join(re.escape(word) for word in context_words)})[^.\n:]*?({DATE_RE.pattern[2:-2]})",
            re.IGNORECASE,
        )
        match = pattern.search(haystack)
        if match:
            return match.group(1).strip()

    match = DATE_RE.search(haystack)
    return match.group(1).strip() if match else None


def _extract_value_after_label(text: str, *labels: str) -> str | None:
    if not text:
        return None
    pattern = re.compile(
        rf"(?:{'|'.join(re.escape(label) for label in labels)})\s*[:\-]?\s*([^\n.;]+)",
        re.IGNORECASE,
    )
    match = pattern.search(text)
    if not match:
        return None
    value = " ".join(match.group(1).split()).strip()
    return value or None


def _extract_status(text: str, values: tuple[str, ...], default: str) -> str:
    lowered = (text or "").casefold()
    for value in values:
        if value in lowered:
            return value
    return default


def _extract_people(text: str, *, limit: int = 6) -> list[str]:
    people = _extract_named_phrases(text or "", limit=limit)
    return people[:limit]


def _extract_list_from_line(text: str, *labels: str, limit: int = 6) -> list[str]:
    value = _extract_value_after_label(text, *labels)
    if not value:
        return []
    entries = [
        entry.strip(" -")
        for entry in re.split(r",|/| and |\n", value)
        if entry.strip(" -")
    ]
    return entries[:limit]


def _frame_defaults(frame_type: str) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for slot_name, slot_spec in FRAME_REGISTRY.get(frame_type, {}).get("slots", {}).items():
        default_value = slot_spec.get("default")
        if isinstance(default_value, list):
            defaults[slot_name] = list(default_value)
        elif isinstance(default_value, dict):
            defaults[slot_name] = dict(default_value)
        else:
            defaults[slot_name] = default_value
    return defaults


def _classify_frame_details(item_data: dict[str, Any]) -> tuple[str, float]:
    source_type = _stringify(item_data.get("source_type")).casefold()
    doc_type_guess = _stringify(item_data.get("doc_type_guess")).casefold()
    file_type = _stringify(item_data.get("file_type")).casefold()
    handler_type = _stringify(item_data.get("handler_type")).casefold()
    content = _build_frame_content(item_data).casefold()

    if source_type == "gmail":
        return "email", FRAME_CONFIDENCE_DEFAULTS["email"]

    for key, frame_name in FRAME_DOC_TYPE_MAP.items():
        if key in doc_type_guess:
            return frame_name, min(0.97, FRAME_CONFIDENCE_DEFAULTS.get(frame_name, 0.82) + 0.04)

    if item_data.get("sender_email") and ("subject:" in content or "from:" in content):
        return "email", 0.92

    if file_type in FRAME_FILE_TYPE_HINTS and any(
        trigger in content for trigger in FRAME_REGISTRY[FRAME_FILE_TYPE_HINTS[file_type]]["triggers"]
    ):
        frame_name = FRAME_FILE_TYPE_HINTS[file_type]
        return frame_name, FRAME_CONFIDENCE_DEFAULTS.get(frame_name, 0.8)

    scores: dict[str, float] = {}
    for frame_name, frame_def in FRAME_REGISTRY.items():
        triggers = frame_def.get("triggers", [])
        trigger_matches = sum(1 for trigger in triggers if trigger in content)
        if trigger_matches <= 0:
            continue

        score = float(trigger_matches)
        if frame_name == "pitch_deck" and file_type == "presentation":
            score += 0.75
        if frame_name == "email" and item_data.get("sender_email"):
            score += 0.75
        scores[frame_name] = score

    if scores:
        frame_name, raw_score = max(scores.items(), key=lambda item: item[1])
        confidence = min(
            0.98,
            FRAME_CONFIDENCE_DEFAULTS.get(frame_name, 0.78) + min(raw_score * 0.03, 0.08),
        )
        return frame_name, confidence

    if handler_type == "visual":
        return "visual_reference", FRAME_CONFIDENCE_DEFAULTS["visual_reference"]

    return "document", FRAME_CONFIDENCE_DEFAULTS["document"]


def classify_frame(item_data: dict[str, Any]) -> str:
    return _classify_frame_details(item_data)[0]


def classify_batch(items: list[dict[str, Any]]) -> list[tuple[str, str, float]]:
    classified: list[tuple[str, str, float]] = []
    for item in items:
        item_id = str(item.get("id") or "")
        if not item_id:
            continue
        frame_type, confidence = _classify_frame_details(item)
        classified.append((item_id, frame_type, confidence))
    return classified


def extract_query_entities(query: str, *, limit: int = 6) -> list[str]:
    weighted_terms: Counter[str] = Counter()
    query_text = _stringify(query)

    query_tokens, query_phrases = _extract_query_terms(query_text)

    for token in query_tokens:
        weighted_terms[token] += 1.0
    for phrase in query_phrases:
        weighted_terms[phrase] += 1.6
    for email in _extract_emails(query_text):
        weighted_terms[email] += 2.0

    lowered_query = query_text.casefold()
    for frame_name, frame_def in FRAME_REGISTRY.items():
        normalized_frame = frame_name.replace("_", " ")
        if normalized_frame in lowered_query:
            weighted_terms[normalized_frame] += 1.5
        for trigger in frame_def.get("triggers", []):
            if trigger in lowered_query:
                weighted_terms[trigger] += 1.8

    entities: list[str] = []
    seen: set[str] = set()
    for term, _ in weighted_terms.most_common(limit * 3):
        entity_id = f"entity:{_normalize_token(term)}"
        if entity_id == "entity:" or entity_id in seen:
            continue
        seen.add(entity_id)
        entities.append(entity_id)
        if len(entities) >= limit:
            break
    return entities


def extract_entities_from_item(item: dict[str, Any], *, limit: int = 12) -> list[tuple[str, float]]:
    weighted_terms: Counter[str] = Counter()
    title = _stringify(item.get("title"))
    filename = _stringify(item.get("filename"))
    summary = _stringify(item.get("summary"))
    preview = _stringify(item.get("preview_text"))
    extracted = _stringify(item.get("extracted_text"))[:2500]
    keywords = _parse_keywords(item.get("keywords"))
    sender_name = _stringify(item.get("sender_name"))
    sender_email = _stringify(item.get("sender_email"))
    doc_type_guess = _stringify(item.get("doc_type_guess"))

    def add_tokens(weight: float, *texts: str) -> None:
        for token in _extract_tokens(*texts):
            weighted_terms[token] += weight

    def add_phrases(weight: float, *texts: str) -> None:
        for text in texts:
            for phrase in _extract_phrases(text):
                weighted_terms[phrase] += weight

    add_tokens(3.0, sender_name, sender_email)
    add_tokens(2.8, title, filename)
    add_phrases(2.4, title, summary, " ".join(keywords))
    add_tokens(2.1, " ".join(keywords), doc_type_guess)
    add_tokens(1.8, summary)
    add_tokens(1.2, preview)
    add_tokens(0.8, extracted)

    if sender_name:
        weighted_terms[sender_name] += 3.4

    for keyword in keywords:
        if keyword:
            weighted_terms[keyword] += 2.6

    for phrase in _extract_named_phrases(sender_name, title, summary, preview, extracted):
        weighted_terms[phrase] += 2.0

    for email in _extract_emails(sender_email, preview, extracted):
        weighted_terms[email] += 2.4

    inferred_frame = classify_frame(item)
    if inferred_frame not in {"document", "visual_reference"}:
        weighted_terms[inferred_frame.replace("_", " ")] += 4.2

    entities: list[tuple[str, float]] = []
    seen: set[str] = set()
    for term, score in weighted_terms.most_common(limit * 3):
        entity_id = f"entity:{_normalize_token(term)}"
        if entity_id == "entity:" or entity_id in seen:
            continue
        seen.add(entity_id)
        entities.append((entity_id, min(1.0, 0.2 + score / 4.5)))
        if len(entities) >= limit:
            break
    return entities


def _guess_graph_entity_type(name: str, *, default: str = "Concept") -> str:
    cleaned = " ".join(str(name or "").split()).strip()
    if not cleaned:
        return default
    lowered = cleaned.casefold()
    if "@" in lowered:
        return "Person"
    if ORGANISATION_RE.search(cleaned):
        return "Organisation"
    parts = cleaned.split()
    if len(parts) >= 2 and all(part[:1].isupper() for part in parts if part):
        return "Person"
    if lowered.startswith("invoice "):
        return "Document"
    return default


def extract_graph_entities_from_item(
    item: dict[str, Any],
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    frame_type, slots, _ = infer_frame(item)
    title = _stringify(item.get("title") or item.get("filename"))
    summary = _stringify(item.get("summary"))
    preview = _stringify(item.get("preview_text"))
    extracted = _stringify(item.get("extracted_text"))[:2500]
    sender_name = _stringify(item.get("sender_name"))
    sender_email = _stringify(item.get("sender_email"))
    keywords = _parse_keywords(item.get("keywords"))

    candidates: list[tuple[str, str, float, list[str], dict[str, Any]]] = []

    def add_candidate(
        name: str | None,
        entity_type: str,
        weight: float,
        *,
        aliases: list[str] | None = None,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        cleaned = " ".join(str(name or "").split()).strip()
        if not is_valid_graph_entity(cleaned, entity_type):
            return
        candidates.append(
            (
                cleaned,
                entity_type,
                float(weight),
                list(aliases or []),
                dict(attributes or {}),
            )
        )

    if sender_name:
        sender_aliases = [sender_email] if sender_email else []
        sender_attributes = {"email": sender_email} if sender_email else {}
        add_candidate(sender_name, "Person", 3.8, aliases=sender_aliases, attributes=sender_attributes)
    elif sender_email:
        add_candidate(sender_email, "Person", 3.4, attributes={"email": sender_email})

    if title:
        add_candidate(title, "Document", 2.8)

    for keyword in keywords[:8]:
        add_candidate(keyword, "Concept", 2.1)

    for phrase in _extract_named_phrases(title, summary, preview, extracted, limit=8):
        add_candidate(phrase, _guess_graph_entity_type(phrase), 2.4)

    for phrase in _extract_phrases(" ".join([title, summary, " ".join(keywords)]), min_words=2, max_words=3)[:6]:
        add_candidate(phrase, _guess_graph_entity_type(phrase), 1.8)

    if frame_type == "proposal":
        add_candidate(slots.get("recipient"), _guess_graph_entity_type(str(slots.get("recipient") or "")), 3.2)
        add_candidate(slots.get("project_name"), "Project", 3.0)
    elif frame_type == "invoice":
        add_candidate(slots.get("vendor"), _guess_graph_entity_type(str(slots.get("vendor") or "")), 3.2)
        add_candidate(slots.get("client"), _guess_graph_entity_type(str(slots.get("client") or "")), 3.0)
        invoice_number = str(slots.get("invoice_number") or "").strip()
        if invoice_number:
            add_candidate(f"Invoice {invoice_number}", "Document", 2.8)
    elif frame_type == "meeting_notes":
        for attendee in list(slots.get("attendees") or [])[:6]:
            add_candidate(str(attendee), "Person", 2.8)
    elif frame_type == "email":
        add_candidate(slots.get("sender"), "Person", 3.2, aliases=[sender_email] if sender_email else [])
        for recipient in list(slots.get("recipients") or [])[:6]:
            add_candidate(str(recipient), _guess_graph_entity_type(str(recipient)), 2.4)
    elif frame_type == "pitch_deck":
        add_candidate(slots.get("company_name"), _guess_graph_entity_type(str(slots.get("company_name") or ""), default="Organisation"), 3.4)
        for member in list(slots.get("team") or [])[:6]:
            add_candidate(str(member), "Person", 2.4)
    elif frame_type == "contract":
        for party in list(slots.get("parties") or [])[:6]:
            add_candidate(str(party), _guess_graph_entity_type(str(party)), 2.8)
    elif frame_type == "resume":
        add_candidate(slots.get("candidate_name"), "Person", 3.2)
        add_candidate(slots.get("position_applied"), "Concept", 2.4)

    if frame_type not in {"document", "visual_reference"}:
        add_candidate(frame_type.replace("_", " "), "Concept", 2.2)

    deduped: dict[str, dict[str, Any]] = {}
    for name, entity_type, weight, aliases, attributes in candidates:
        key = _normalize_graph_entity_key(name)
        if not key:
            continue
        existing = deduped.get(key)
        confidence = min(1.0, 0.25 + (weight / 4.0))
        if existing is None or weight > float(existing["score"]):
            deduped[key] = {
                "name": name,
                "entity_type": entity_type,
                "aliases": [alias for alias in aliases if str(alias).strip()],
                "attributes": attributes,
                "score": weight,
                "confidence": confidence,
            }
        else:
            existing["aliases"] = list(
                dict.fromkeys(
                    [*(existing.get("aliases") or []), *(alias for alias in aliases if str(alias).strip())]
                )
            )
            existing["attributes"].update(attributes)

    ranked = sorted(
        deduped.values(),
        key=lambda entity: (float(entity["score"]), len(str(entity["name"]))),
        reverse=True,
    )
    return ranked[:limit]


def infer_frame(item: dict[str, Any]) -> tuple[str, dict[str, Any], float]:
    frame_type, confidence = _classify_frame_details(item)
    content = _build_frame_content(item)
    lowered = content.casefold()

    slots = _frame_defaults(frame_type)
    slots.update(
        {
            "title": item.get("title") or item.get("filename"),
            "source_type": item.get("source_type"),
            "file_type": item.get("file_type"),
            "sender_name": item.get("sender_name"),
            "sender_email": item.get("sender_email"),
        }
    )

    if frame_type == "proposal":
        slots["recipient"] = (
            _extract_value_after_label(content, "recipient", "client")
            or _extract_value_after_label(content, "for")
            or slots.get("recipient")
        )
        slots["project_name"] = item.get("title") or slots.get("project_name")
        slots["proposed_budget"] = _extract_amount(content) or slots.get("proposed_budget")
        slots["deadline"] = _extract_date(content, context_words=("deadline", "due", "deliverables due by")) or slots.get("deadline")
        slots["status"] = _extract_status(content, ("submitted", "accepted", "rejected"), slots["status"])
        asks = _extract_list_from_line(content, "scope of work includes", "deliverables", limit=5)
        if asks:
            slots["key_asks"] = asks

    elif frame_type == "invoice":
        slots["vendor"] = item.get("sender_name") or _extract_value_after_label(content, "from", "vendor") or slots.get("vendor")
        slots["client"] = _extract_value_after_label(content, "bill to", "client", "to") or slots.get("client")
        slots["amount"] = _extract_amount(content) or slots.get("amount")
        slots["due_date"] = _extract_date(content, context_words=("due date", "amount due", "payment due", "due")) or slots.get("due_date")
        match = INVOICE_NUMBER_RE.search(content)
        slots["invoice_number"] = match.group(1) if match else slots.get("invoice_number")
        slots["status"] = _extract_status(content, ("paid", "overdue", "partial"), slots["status"])

    elif frame_type == "meeting_notes":
        attendees = _extract_list_from_line(content, "attendees", "participants", limit=8)
        if not attendees:
            attendees = _extract_people(content, limit=6)
        slots["attendees"] = attendees or slots["attendees"]
        slots["date"] = _extract_date(content, context_words=("meeting", "date", "on")) or slots.get("date")
        slots["action_items"] = _extract_list_from_line(content, "action items", "next steps", limit=6) or slots["action_items"]
        decisions = _extract_list_from_line(content, "decisions", "agreed to", limit=6)
        if decisions:
            slots["decisions"] = decisions
        slots["next_meeting"] = _extract_date(content, context_words=("next meeting",)) or slots.get("next_meeting")

    elif frame_type == "email":
        slots["sender"] = item.get("sender_name") or item.get("sender_email") or slots.get("sender")
        recipients = _extract_list_from_line(content, "to", "cc", limit=8)
        if not recipients:
            recipients = _extract_emails(content)
        slots["recipients"] = recipients[:8] if recipients else slots["recipients"]
        slots["subject"] = item.get("title") or _extract_value_after_label(content, "subject") or slots.get("subject")
        slots["date"] = (
            item.get("created_at")
            or item.get("first_seen_at")
            or _extract_date(content, context_words=("sent", "date"))
            or slots.get("date")
        )
        requires_response = slots["requires_response"]
        if any(phrase in lowered for phrase in ("please reply", "let me know", "can you", "could you", "need your response")) or QUESTION_RE.search(content):
            requires_response = "yes"
        elif any(phrase in lowered for phrase in ("fyi", "no action needed", "resolved")):
            requires_response = "no"
        slots["requires_response"] = requires_response

    elif frame_type == "pitch_deck":
        names = _extract_named_phrases(item.get("title") or "", content, limit=4)
        slots["company_name"] = names[0] if names else (item.get("title") or slots.get("company_name"))
        slots["ask_amount"] = _extract_amount(content) or slots.get("ask_amount")
        slots["problem"] = _extract_value_after_label(content, "problem") or slots.get("problem")
        slots["solution"] = _extract_value_after_label(content, "solution") or slots.get("solution")
        team = _extract_list_from_line(content, "team", "founders", limit=6)
        if not team:
            team = _extract_people(content, limit=4)
        slots["team"] = team or slots["team"]

    elif frame_type == "contract":
        parties = _extract_list_from_line(content, "between", "parties", limit=6)
        if not parties:
            parties = _extract_people(content, limit=4)
        slots["parties"] = parties or slots["parties"]
        slots["effective_date"] = _extract_date(content, context_words=("effective", "effective date")) or slots.get("effective_date")
        slots["expiry_date"] = _extract_date(content, context_words=("expiry", "expires", "termination", "end date")) or slots.get("expiry_date")
        slots["payment_terms"] = _extract_value_after_label(content, "payment terms", "net") or slots.get("payment_terms")

    elif frame_type == "resume":
        names = _extract_named_phrases(item.get("title") or "", content, limit=3)
        slots["candidate_name"] = names[0] if names else slots.get("candidate_name")
        position = POSITION_RE.search(content)
        slots["position_applied"] = position.group(1).strip() if position else slots.get("position_applied")
        skills = [token for token in _extract_tokens(content) if token in SKILL_TERMS]
        if skills:
            slots["skills"] = list(dict.fromkeys(skills))[:8]
        experience = EXPERIENCE_RE.search(content)
        slots["experience_years"] = experience.group(1) if experience else slots.get("experience_years")

    return frame_type, slots, confidence


def infer_desired_frame(query: str) -> str | None:
    lowered = _stringify(query).casefold()
    for frame_name, frame_def in FRAME_REGISTRY.items():
        if frame_name.replace("_", " ") in lowered:
            return frame_name
        if any(trigger in lowered for trigger in frame_def.get("triggers", [])):
            return frame_name
    return None
