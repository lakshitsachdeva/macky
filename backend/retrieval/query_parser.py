from __future__ import annotations

import calendar
import logging
import re
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from pydantic import BaseModel, Field


MONTH_NAME_TO_NUMBER = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}

MONTH_PATTERN = re.compile(
    r"\b("
    + "|".join(MONTH_NAME_TO_NUMBER.keys())
    + r")\b",
    re.IGNORECASE,
)
MONTH_DAY_PATTERN = re.compile(
    r"\b("
    + "|".join(MONTH_NAME_TO_NUMBER.keys())
    + r")\s+(\d{1,2})(?:st|nd|rd|th)?(?:,?\s+(\d{4}))?\b",
    re.IGNORECASE,
)
WEEKDAY_NAME_TO_NUMBER = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}
LAST_WEEKDAY_PATTERN = re.compile(
    r"\blast\s+(monday|tuesday|wednesday|thursday|friday|saturday|sunday)\b",
    re.IGNORECASE,
)

AUTHOR_PATTERN = re.compile(
    r"\b(?:sent by|emailed by|shared by|written by|authored by|made by|by|from)\s+"
    r"([A-Za-z][\w.@'-]*(?:\s+[A-Za-z][\w.@'-]+)?)",
    re.IGNORECASE,
)
SENT_RECIPIENT_PATTERN = re.compile(
    r"\bsent\s+(?!by\b|from\b)([A-Za-z][\w.@'-]*(?:\s+[A-Za-z][\w.@'-]+)?)"
    r"(?=\s+(?:on|via|through|over|to|in)\b|\s+(?:mail|gmail|email)\b|$)",
    re.IGNORECASE,
)

POSSESSIVE_PERSON_PATTERN = re.compile(
    r"\b([A-Za-z][\w'-]{1,})(?:'s)\b",
    re.IGNORECASE,
)

CAPITALIZED_PERSON_PATTERN = re.compile(
    r"\b[A-Z][a-z]+(?:\s[A-Z][a-z]+)?\b"
)

POSSESSIVE_STOPWORDS = {
    "what",
    "that",
    "this",
    "there",
    "here",
    "where",
    "when",
    "why",
    "who",
    "how",
    "it",
    "let",
}
QUERY_STYLE_FILLER_WORDS = {
    "a",
    "an",
    "and",
    "by",
    "for",
    "from",
    "got",
    "i",
    "in",
    "is",
    "it",
    "last",
    "me",
    "my",
    "of",
    "on",
    "or",
    "past",
    "recent",
    "recently",
    "show",
    "that",
    "the",
    "this",
    "to",
    "week",
    "with",
    "year",
}

PERSON_TRAILING_STOPWORDS = {
    "today",
    "yesterday",
    "this",
    "last",
    "past",
    "week",
    "month",
    "year",
    "ago",
    "recently",
    "just",
    "few",
    "some",
    "while",
    "back",
    "on",
    "via",
    "through",
    "over",
    "to",
    "in",
    "mail",
    "gmail",
    "email",
}
PERSON_PRONOUN_HINTS = {"me", "him", "her", "them", "us", "you", "it"}
INTENT_ENTITY_STOPWORDS = QUERY_STYLE_FILLER_WORDS | {
    "all",
    "any",
    "about",
    "doc",
    "docs",
    "document",
    "documents",
    "email",
    "emails",
    "everything",
    "file",
    "files",
    "happening",
    "major",
    "project",
    "projects",
    "related",
    "situation",
    "startup",
    "state",
    "status",
    "thing",
    "things",
}
EXACT_LOOKUP_PATTERN = re.compile(
    r"(?:\b#\d+\b|\b[a-z0-9_.-]+\.(?:pdf|docx|doc|xlsx|xls|pptx|csv|txt|md|png|jpg|jpeg)\b)",
    re.IGNORECASE,
)
PERSON_CENTRIC_PATTERN = re.compile(
    r"\b(?:emails?\s+from|sent\s+by|what\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+)?\s+worked\s+on)\b"
)
TEMPORAL_STATE_PATTERN = re.compile(
    r"\b(?:status|working on|worked on|happening with|situation with|state of)\b",
    re.IGNORECASE,
)
ENTITY_LOOKUP_PATTERN = re.compile(
    r"\b(?:everything about|all .*documents|all .*files|related to|about)\b",
    re.IGNORECASE,
)
CAPITALIZED_ENTITY_PATTERN = re.compile(r"\b[A-Z][A-Za-z0-9_-]{2,}(?:\s+[A-Z][A-Za-z0-9_-]{2,})?\b")
WEAK_TIME_PATTERN = re.compile(
    r"\b(?:recent|recently|latest|newest|new|current|updated|just|just now)\b",
    re.IGNORECASE,
)
LATEST_PREFERENCE_PATTERN = re.compile(
    r"\b(?:latest|newest|current|updated|most recent)\b",
    re.IGNORECASE,
)

ABBREVIATION_MAP: dict[str, tuple[str, ...]] = {
    "grade iv": ("gr 4", "grade 4", "fourth grade", "semester iv"),
    "grade i": ("gr 1", "grade 1", "first grade"),
    "grade ii": ("gr 2", "grade 2"),
    "grade iii": ("gr 3", "grade 3"),
    "grade v": ("gr 5", "grade 5"),
    "gr 4": ("grade iv", "grade 4", "fourth"),
    "sem i": ("semester 1", "semester i", "first semester"),
    "sem ii": ("semester 2",),
    "term i": ("term 1", "first term"),
    "term ii": ("term 2", "second term"),
    "syllabus": ("curriculum", "course outline", "subjects"),
    "resume": ("cv", "curriculum vitae"),
    "invoice": ("bill", "receipt", "payment"),
    "marksheet": ("marks", "grades", "result", "transcript"),
    "finance": (
        "financial",
        "investment",
        "investing",
        "trading",
        "portfolio",
        "stock",
        "ticker",
        "market",
        "quant",
    ),
    "stock": (
        "stocks",
        "finance",
        "financial",
        "investment",
        "investing",
        "trading",
        "portfolio",
        "ticker",
        "market",
        "quant",
    ),
    "stocks": (
        "stock",
        "finance",
        "financial",
        "investment",
        "investing",
        "trading",
        "portfolio",
        "ticker",
        "market",
        "quant",
    ),
    "startup": (
        "venture",
        "product",
        "landing page",
        "mvp",
        "launch",
        "pitch",
    ),
}

AMBIGUOUS_VISUAL_TERMS = {"image"}
NON_VISUAL_IMAGE_CONTEXT_PATTERNS = [
    re.compile(r"\bimage\s+and\s+video\s+processing\b", re.IGNORECASE),
    re.compile(r"\bimage\s+processing\b", re.IGNORECASE),
    re.compile(
        r"\b(?:syllabus|course|chapter|notes?|paper|book|report|thesis|research|resume|cv|"
        r"invoice|receipt|contract|document|readme|curriculum)\b",
        re.IGNORECASE,
    ),
]

FILE_TYPE_RULES = [
    (
        "pdf",
        "text",
        [
            re.compile(r"\bpdf\b", re.IGNORECASE),
        ],
    ),
    (
        "image",
        "visual",
        [
            re.compile(r"\bscreenshots?\b", re.IGNORECASE),
            re.compile(r"\bscreen shots?\b", re.IGNORECASE),
            re.compile(r"\bimage\b", re.IGNORECASE),
            re.compile(r"\bphoto\b", re.IGNORECASE),
            re.compile(r"\bpicture\b", re.IGNORECASE),
        ],
    ),
    (
        "spreadsheet",
        "text",
        [
            re.compile(r"\bspreadsheet\b", re.IGNORECASE),
            re.compile(r"\bexcel\b", re.IGNORECASE),
            re.compile(r"\bcsv\b", re.IGNORECASE),
            re.compile(r"\.xlsx\b", re.IGNORECASE),
        ],
    ),
    (
        "presentation",
        "text",
        [
            re.compile(r"\bslide\b", re.IGNORECASE),
            re.compile(r"\bdeck\b", re.IGNORECASE),
            re.compile(r"\bpresentation\b", re.IGNORECASE),
            re.compile(r"\bppt\b", re.IGNORECASE),
        ],
    ),
    (
        "docx",
        "text",
        [
            re.compile(r"\bdoc\b", re.IGNORECASE),
            re.compile(r"\bword doc\b", re.IGNORECASE),
            re.compile(r"\bword file\b", re.IGNORECASE),
            re.compile(r"\bdocument\b", re.IGNORECASE),
        ],
    ),
    (
        "text",
        "text",
        [
            re.compile(r"\bnote\b", re.IGNORECASE),
            re.compile(r"\bnotes\b", re.IGNORECASE),
            re.compile(r"\bmarkdown\b", re.IGNORECASE),
            re.compile(r"\.md\b", re.IGNORECASE),
        ],
    ),
]

DOCUMENT_WORDS: dict[str, tuple[str, str | None]] = {
    "book": ("pdf", "book textbook"),
    "textbook": ("pdf", "textbook book"),
    "paper": ("pdf", "paper research"),
    "research": ("pdf", "research paper"),
    "article": ("pdf", "article"),
    "thesis": ("pdf", "thesis dissertation"),
    "report": ("pdf", "report"),
    "invoice": ("pdf", None),
    "receipt": ("pdf", None),
    "contract": ("pdf", None),
    "syllabus": ("pdf", "syllabus curriculum course outline"),
    "resume": ("docx", "resume CV"),
    "cv": ("docx", "resume CV"),
    "notes": ("text", "notes"),
    "readme": ("text", "readme"),
}
IMPLICIT_FILE_TYPE_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "pdf",
        (
            "paper",
            "thesis",
            "report",
            "manual",
            "book",
            "journal",
            "article",
            "publication",
        ),
    ),
    (
        "spreadsheet",
        (
            "budget",
            "revenue",
            "sales",
            "forecast",
            "metrics",
            "data",
            "numbers",
            "accounts",
        ),
    ),
    (
        "presentation",
        (
            "presentation",
            "pitch",
            "deck",
            "slides",
            "talk",
            "keynote",
        ),
    ),
)

SOURCE_TYPE_RULES = [
    (
        "email",
        [
            re.compile(r"\bemail\b", re.IGNORECASE),
            re.compile(r"\bemails\b", re.IGNORECASE),
            re.compile(r"\bgmail\b", re.IGNORECASE),
            re.compile(r"\bmail\b", re.IGNORECASE),
            re.compile(r"\battachment\b", re.IGNORECASE),
            re.compile(r"\bsent me\b", re.IGNORECASE),
            re.compile(r"\bemailed\b", re.IGNORECASE),
            re.compile(
                r"\bsent\s+(?!by\b|from\b)[A-Za-z][\w.@'-]*(?:\s+[A-Za-z][\w.@'-]+)?\s+"
                r"(?:on|via|through|over|to|in)\s+(?:mail|gmail|email)\b",
                re.IGNORECASE,
            ),
        ],
    ),
    (
        "download",
        [
            re.compile(r"\bdownloaded\b", re.IGNORECASE),
            re.compile(r"\bdownload\b", re.IGNORECASE),
        ],
    ),
    (
        "browser",
        [
            re.compile(r"\bbrowser\b", re.IGNORECASE),
            re.compile(r"\bchrome\b", re.IGNORECASE),
            re.compile(r"\btab\b", re.IGNORECASE),
            re.compile(r"\bwebsite\b", re.IGNORECASE),
            re.compile(r"\bvisited\b", re.IGNORECASE),
            re.compile(r"\bbrowsing\b", re.IGNORECASE),
        ],
    ),
    (
        "clipboard",
        [
            re.compile(r"\bclipboard\b", re.IGNORECASE),
        ],
    ),
]

class TimeWindow(BaseModel):
    description: str
    start_date: datetime | None = None
    end_date: datetime | None = None
    center_date: datetime | None = None
    size_days: int = 7


class ParsedQuery(BaseModel):
    original: str
    semantic_text: str
    exact_text: str
    query_tokens: list[str] = Field(default_factory=list)
    file_type: str | None = None
    file_type_strict: bool = False
    handler_type: str | None = None
    source_type: str | None = None
    time_window: TimeWindow | None = None
    time_confidence: str | None = None
    prefer_latest: bool = False
    person_hints: list[str] = Field(default_factory=list)
    is_person_query: bool = False
    query_style: str = "semantic"
    intent_class: str = "SEMANTIC_TOPIC"


class IntentClass(StrEnum):
    ENTITY_LOOKUP = "ENTITY_LOOKUP"
    TEMPORAL_STATE = "TEMPORAL_STATE"
    TEMPORAL_RANGE = "TEMPORAL_RANGE"
    SEMANTIC_TOPIC = "SEMANTIC_TOPIC"
    EXACT_LOOKUP = "EXACT_LOOKUP"
    PERSON_CENTRIC = "PERSON_CENTRIC"


class QueryIntentClassifier:
    WEIGHT_PROFILES: dict[IntentClass, dict[str, float]] = {
        IntentClass.ENTITY_LOOKUP: {
            "dense": 0.30,
            "sparse": 0.08,
            "fts": 0.15,
            "filename": 0.07,
            "time": 0.05,
            "metadata": 0.10,
            "behavior": 0.10,
            "recency": 0.05,
            "graph": 0.35,
        },
        IntentClass.PERSON_CENTRIC: {
            "dense": 0.20,
            "sparse": 0.08,
            "fts": 0.20,
            "filename": 0.05,
            "time": 0.05,
            "metadata": 0.10,
            "behavior": 0.10,
            "recency": 0.05,
            "graph": 0.40,
        },
        IntentClass.TEMPORAL_STATE: {
            "dense": 0.15,
            "sparse": 0.06,
            "fts": 0.10,
            "filename": 0.04,
            "time": 0.35,
            "metadata": 0.10,
            "behavior": 0.05,
            "recency": 0.05,
            "graph": 0.30,
        },
        IntentClass.TEMPORAL_RANGE: {
            "dense": 0.25,
            "sparse": 0.08,
            "fts": 0.20,
            "filename": 0.07,
            "time": 0.38,
            "metadata": 0.07,
            "behavior": 0.07,
            "recency": 0.05,
            "graph": 0.05,
        },
        IntentClass.EXACT_LOOKUP: {
            "dense": 0.10,
            "sparse": 0.05,
            "fts": 0.30,
            "filename": 0.25,
            "time": 0.10,
            "metadata": 0.10,
            "behavior": 0.10,
            "recency": 0.05,
            "graph": 0.05,
        },
        IntentClass.SEMANTIC_TOPIC: {
            "dense": 0.45,
            "sparse": 0.08,
            "fts": 0.20,
            "filename": 0.05,
            "time": 0.05,
            "metadata": 0.07,
            "behavior": 0.05,
            "recency": 0.05,
            "graph": 0.05,
        },
    }

    @classmethod
    def classify(
        cls,
        query: str,
        parsed: ParsedQuery | None = None,
    ) -> IntentClass:
        parsed_query = parsed or parse_query(query)
        original = str(parsed_query.original or query)
        exact_text = str(parsed_query.exact_text or original)
        semantic_text = str(parsed_query.semantic_text or original)

        if parsed_query.query_style == "identifier" or EXACT_LOOKUP_PATTERN.search(exact_text):
            return IntentClass.EXACT_LOOKUP

        if PERSON_CENTRIC_PATTERN.search(original) or (
            parsed_query.is_person_query and parsed_query.person_hints
        ) or (
            parsed_query.source_type == "email" and parsed_query.person_hints
        ):
            return IntentClass.PERSON_CENTRIC

        has_temporal = parsed_query.time_window is not None and parsed_query.time_window.start_date is not None
        has_entity_reference = cls._has_entity_reference(original, semantic_text, parsed_query)

        if has_temporal and TEMPORAL_STATE_PATTERN.search(original) and has_entity_reference:
            return IntentClass.TEMPORAL_STATE

        if has_temporal and not has_entity_reference:
            return IntentClass.TEMPORAL_RANGE

        if has_entity_reference and ENTITY_LOOKUP_PATTERN.search(original):
            return IntentClass.ENTITY_LOOKUP

        return IntentClass.SEMANTIC_TOPIC

    @classmethod
    def get_weights(cls, intent: IntentClass | str) -> dict[str, float]:
        try:
            normalized_intent = intent if isinstance(intent, IntentClass) else IntentClass(str(intent))
        except ValueError:
            normalized_intent = IntentClass.SEMANTIC_TOPIC
        return dict(cls.WEIGHT_PROFILES[normalized_intent])

    @classmethod
    def _has_entity_reference(
        cls,
        original_query: str,
        semantic_text: str,
        parsed: ParsedQuery,
    ) -> bool:
        if parsed.person_hints:
            return True
        if CAPITALIZED_ENTITY_PATTERN.search(original_query):
            return True
        focus_tokens = cls._focus_tokens(semantic_text)
        lower_query = original_query.casefold()
        if parsed.time_window and 1 <= len(focus_tokens) <= 2:
            if any(marker in lower_query for marker in (" for ", " of ", " with ", " about ")):
                return True
        return bool(focus_tokens) and (
            ENTITY_LOOKUP_PATTERN.search(original_query) is not None
        )

    @classmethod
    def _focus_tokens(cls, semantic_text: str) -> list[str]:
        tokens = []
        for token in re.findall(r"[A-Za-z0-9]+", semantic_text):
            normalized = token.casefold()
            if len(normalized) < 3:
                continue
            if normalized in INTENT_ENTITY_STOPWORDS:
                continue
            if normalized in MONTH_NAME_TO_NUMBER:
                continue
            tokens.append(normalized)
        return tokens


def _make_window(
    description: str,
    days_back_start: int,
    days_back_end: int = 0,
    size_days: int = 7,
) -> TimeWindow:
    now = datetime.now(timezone.utc)
    start = now - timedelta(days=days_back_start)
    end = now - timedelta(days=days_back_end)
    center = start + (end - start) / 2
    return TimeWindow(
        description=description,
        start_date=start,
        end_date=end,
        center_date=center,
        size_days=size_days,
    )


def _make_datetime_window(
    description: str,
    start_date: datetime,
    end_date: datetime,
    size_days: int = 7,
) -> TimeWindow:
    start = start_date.astimezone(timezone.utc)
    end = end_date.astimezone(timezone.utc)
    center = start + (end - start) / 2
    return TimeWindow(
        description=description,
        start_date=start,
        end_date=end,
        center_date=center,
        size_days=size_days,
    )


def _make_day_window(description: str, day: datetime) -> TimeWindow:
    start = day.replace(hour=0, minute=0, second=0, microsecond=0)
    end = day.replace(hour=23, minute=59, second=59, microsecond=999999)
    return _make_datetime_window(description, start, end, size_days=1)


def _strip_phrases(text: str, phrases: list[str]) -> str:
    cleaned = text
    for phrase in sorted({phrase for phrase in phrases if phrase}, key=len, reverse=True):
        cleaned = re.sub(re.escape(phrase), " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _phrase_present(text: str, phrase: str) -> bool:
    pattern = re.compile(rf"(?<!\w){re.escape(phrase)}(?!\w)", re.IGNORECASE)
    return bool(pattern.search(text))


def expand_roman(text: str) -> str:
    roman_map = {
        "I": "1",
        "II": "2",
        "III": "3",
        "IV": "4",
        "V": "5",
        "VI": "6",
    }
    expanded_terms: list[str] = []
    seen: set[str] = set()
    contextual_singletons = {"grade", "gr", "sem", "semester", "term"}

    for match in re.finditer(r"\b(I|II|III|IV|V|VI)\b", text, re.IGNORECASE):
        roman = match.group(1)
        normalized_roman = roman.upper()
        if normalized_roman not in roman_map:
            continue

        if normalized_roman == "I":
            prefix_tokens = re.findall(r"[A-Za-z]+", text[: match.start()])
            if not prefix_tokens or prefix_tokens[-1].casefold() not in contextual_singletons:
                continue

        arabic = roman_map[normalized_roman]
        if arabic not in seen:
            seen.add(arabic)
            expanded_terms.append(arabic)

    return " ".join(expanded_terms)


def expand_query(semantic_text: str) -> str:
    base = re.sub(r"\s+", " ", (semantic_text or "").strip())
    if not base:
        return base

    expansion_terms: list[str] = []
    seen: set[str] = set()
    text_lower = base.casefold()

    for key, expansions in ABBREVIATION_MAP.items():
        if not _phrase_present(text_lower, key.casefold()):
            continue
        for term in expansions:
            normalized = term.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            expansion_terms.append(term)

    roman_expansion = expand_roman(base)
    if roman_expansion:
        for term in roman_expansion.split():
            normalized = term.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            expansion_terms.append(term)

    if not expansion_terms:
        return base

    return f"{base} {' '.join(expansion_terms)}".strip()


def _detect_file_type(query: str) -> tuple[str | None, str | None, list[str]]:
    matched_phrases: list[str] = []

    for file_type, handler_type, patterns in FILE_TYPE_RULES:
        for pattern in patterns:
            match = pattern.search(query)
            if match:
                matched_text = match.group(0)
                if (
                    file_type == "image"
                    and matched_text.casefold() in AMBIGUOUS_VISUAL_TERMS
                    and any(
                        context_pattern.search(query)
                        for context_pattern in NON_VISUAL_IMAGE_CONTEXT_PATTERNS
                    )
                ):
                    continue
                matched_phrases.append(matched_text)
                return file_type, handler_type, matched_phrases

    return None, None, matched_phrases


def _detect_document_words(
    query: str,
) -> tuple[str | None, str | None, list[str], list[str]]:
    file_type: str | None = None
    handler_type: str | None = None
    removal_phrases: list[str] = []
    boost_terms: list[str] = []
    seen_boost_terms: set[str] = set()

    for word, (mapped_file_type, boost_keyword) in DOCUMENT_WORDS.items():
        pattern = re.compile(rf"\b{re.escape(word)}\b", re.IGNORECASE)
        if not pattern.search(query):
            continue

        if file_type is None:
            file_type = mapped_file_type
            handler_type = "text"

        if boost_keyword:
            removal_phrases.append(word)
            for token in boost_keyword.split():
                normalized = token.casefold()
                if normalized not in seen_boost_terms:
                    seen_boost_terms.add(normalized)
                    boost_terms.append(token)

    return file_type, handler_type, removal_phrases, boost_terms


def _detect_source_type(query: str) -> tuple[str | None, list[str]]:
    matched_phrases: list[str] = []

    for source_type, patterns in SOURCE_TYPE_RULES:
        for pattern in patterns:
            match = pattern.search(query)
            if match:
                matched_phrases.append(match.group(0))
                return source_type, matched_phrases

    return None, matched_phrases


def _infer_implicit_file_type(query: str) -> tuple[str | None, str | None]:
    lower_query = query.casefold()
    for file_type, hints in IMPLICIT_FILE_TYPE_HINTS:
        if any(re.search(rf"\b{re.escape(hint)}\b", lower_query) for hint in hints):
            return file_type, "text"
    return None, None


def _detect_time_window(query: str) -> tuple[TimeWindow | None, list[str], str | None, bool]:
    window: TimeWindow | None = None
    removal_phrases: list[str] = []
    confidence: str | None = None
    prefer_latest = False
    now = datetime.now(timezone.utc)

    if match := re.search(r"\b(?:two weeks ago|2 weeks ago)\b", query, re.IGNORECASE):
        window = _make_window("2 weeks ago", 14, 7, 7)
        removal_phrases = [match.group(0)]
        confidence = "strong"
    elif match := re.search(r"\byesterday\b", query, re.IGNORECASE):
        window = _make_window("yesterday", 2, 1, 1)
        removal_phrases = [match.group(0)]
        confidence = "strong"
    elif match := re.search(r"\btoday\b", query, re.IGNORECASE):
        window = _make_window("today", 1, 0, 1)
        removal_phrases = [match.group(0)]
        confidence = "strong"
    elif match := re.search(r"\bthis morning\b", query, re.IGNORECASE):
        today = now.replace(hour=0, minute=0, second=0, microsecond=0)
        window = _make_datetime_window(
            "this morning",
            today,
            today.replace(hour=11, minute=59, second=59, microsecond=999999),
            size_days=1,
        )
        removal_phrases = [match.group(0)]
        confidence = "strong"
    elif match := re.search(r"\b(?:an|a|one)\s+hour\s+ago\b|\b(\d+)\s+hours?\s+ago\b", query, re.IGNORECASE):
        hours = 1
        if match.group(1):
            hours = max(1, int(match.group(1)))
        center = now - timedelta(hours=hours)
        window = _make_datetime_window(
            match.group(0),
            center - timedelta(hours=2),
            center + timedelta(hours=2),
            size_days=1,
        )
        removal_phrases = [match.group(0)]
        confidence = "strong"
    elif match := LAST_WEEKDAY_PATTERN.search(query):
        weekday_name = match.group(1).casefold()
        weekday = WEEKDAY_NAME_TO_NUMBER[weekday_name]
        days_ago = (now.weekday() - weekday) % 7 or 7
        target_day = now - timedelta(days=days_ago)
        window = _make_day_window(match.group(0), target_day)
        removal_phrases = [match.group(0)]
        confidence = "strong"
    elif match := re.search(
        r"\b(?:this week|last week|past week|a week ago)\b",
        query,
        re.IGNORECASE,
    ):
        window = _make_window("last week", 7, 0, 7)
        removal_phrases = [match.group(0)]
        confidence = "strong"
    elif match := re.search(
        r"\b(?:this month|last month|past month)\b",
        query,
        re.IGNORECASE,
    ):
        window = _make_window("last month", 30, 0, 30)
        removal_phrases = [match.group(0)]
        confidence = "strong"
    elif match := MONTH_DAY_PATTERN.search(query):
        phrase = match.group(0)
        month_number = MONTH_NAME_TO_NUMBER[match.group(1).casefold()]
        day_number = int(match.group(2))
        year_number = int(match.group(3)) if match.group(3) else now.year
        try:
            target_day = datetime(year_number, month_number, day_number, tzinfo=timezone.utc)
        except ValueError:
            target_day = None
        if target_day is not None:
            if target_day > now and match.group(3) is None:
                target_day = target_day.replace(year=year_number - 1)
            window = _make_day_window(phrase, target_day)
            removal_phrases = [phrase]
            confidence = "strong"
    elif match := re.search(r"\b(?:recently|just now|just)\b", query, re.IGNORECASE):
        window = _make_window("recently", 7, 0, 7)
        removal_phrases = [match.group(0)]
        confidence = "weak"
    elif match := re.search(r"\b(?:a few days ago|few days)\b", query, re.IGNORECASE):
        window = _make_window("few days ago", 5, 0, 5)
        removal_phrases = [match.group(0)]
        confidence = "strong"
    elif match := re.search(
        r"\b(?:a while ago|some time ago)\b",
        query,
        re.IGNORECASE,
    ):
        window = _make_window("a while back", 60, 0, 60)
        removal_phrases = [match.group(0)]
        confidence = "weak"
    elif match := re.search(r"\b(?:this year|last year)\b", query, re.IGNORECASE):
        window = _make_window("this year", 365, 0, 365)
        removal_phrases = [match.group(0)]
        confidence = "strong"
    elif match := WEAK_TIME_PATTERN.search(query):
        window = _make_window("recent", 7, 0, 7)
        removal_phrases = [match.group(0)]
        confidence = "weak"
        prefer_latest = bool(LATEST_PREFERENCE_PATTERN.search(query))
    else:
        month_match = MONTH_PATTERN.search(query)
        if month_match:
            phrase = month_match.group(0)
            month_number = MONTH_NAME_TO_NUMBER[phrase.lower()]
            year = now.year if month_number <= now.month else now.year - 1
            _, last_day = calendar.monthrange(year, month_number)
            start_date = datetime(year, month_number, 1, tzinfo=timezone.utc)
            end_date = datetime(
                year,
                month_number,
                last_day,
                23,
                59,
                59,
                tzinfo=timezone.utc,
            )
            center_date = start_date + (end_date - start_date) / 2
            window = TimeWindow(
                description=phrase,
                start_date=start_date,
                end_date=end_date,
                center_date=center_date,
                size_days=31,
            )
            removal_phrases = [phrase]
            confidence = "strong"

    prefer_latest = prefer_latest or bool(LATEST_PREFERENCE_PATTERN.search(query))
    return window, removal_phrases, confidence, prefer_latest


def _detect_person_hints(query: str) -> tuple[list[str], list[str]]:
    hints: list[str] = []
    removal_phrases: list[str] = []
    seen: set[str] = set()

    def _add_hint(raw_hint: str, removal_phrase: str | None = None) -> None:
        words = raw_hint.strip(" ,.;:!?").split()
        while words and words[-1].casefold() in PERSON_TRAILING_STOPWORDS:
            words.pop()
        hint = " ".join(words)
        if not hint:
            return
        if hint.casefold() in PERSON_PRONOUN_HINTS:
            return
        normalized = hint.casefold()
        if normalized not in seen:
            seen.add(normalized)
            hints.append(hint)
        if removal_phrase:
            raw_cleaned = raw_hint.strip(" ,.;:!?")
            if raw_cleaned and removal_phrase.endswith(raw_cleaned):
                removal_phrase = removal_phrase[: -len(raw_cleaned)] + hint
            removal_phrases.append(removal_phrase)

    for match in AUTHOR_PATTERN.finditer(query):
        hint = match.group(1).strip(" ,.;:!?")
        _add_hint(hint, match.group(0))

    for match in SENT_RECIPIENT_PATTERN.finditer(query):
        hint = match.group(1).strip(" ,.;:!?")
        _add_hint(hint, match.group(0))

    for match in POSSESSIVE_PERSON_PATTERN.finditer(query):
        hint = match.group(1)
        if hint.casefold() in POSSESSIVE_STOPWORDS:
            continue
        _add_hint(hint, match.group(0))

    for match in CAPITALIZED_PERSON_PATTERN.finditer(query):
        prefix = query[: match.start()].rstrip()
        if not prefix or prefix.endswith((".", "!", "?")):
            continue

        hint = match.group(0)
        _add_hint(hint)

    return hints, removal_phrases


def detect_query_style(query: str) -> str:
    """
    Returns: 'identifier' | 'semantic' | 'mixed'

    Identifier queries look like titles, codes, versions, or specific lookups.
    Semantic queries look like descriptive natural language.
    Mixed queries carry signals of both.
    """
    cleaned = query.strip()
    if not cleaned:
        return "semantic"

    tokens = re.findall(r"[A-Za-z0-9._-]+", cleaned)
    meaningful_words = [
        token
        for token in tokens
        if token.casefold() not in QUERY_STYLE_FILLER_WORDS
    ]
    lower_q = cleaned.casefold()

    has_numbers = bool(re.search(r"\b\d+\b", cleaned))
    has_roman_numeral = bool(
        re.search(r"\b(?:II|III|IV|V|VI|VII|VIII|IX|X)\b", cleaned, re.IGNORECASE)
        or re.search(
            r"\b(?:grade|gr|sem|semester|term)\s+I\b",
            cleaned,
            re.IGNORECASE,
        )
    )
    has_mixed_case = any(
        any(ch.isupper() for ch in token[1:])
        or (
            index > 0
            and token[:1].isupper()
            and any(ch.islower() for ch in token[1:])
        )
        for index, token in enumerate(tokens)
        if len(token) > 2
    )
    has_alphanum_code = bool(
        re.search(r"\b[a-z]{1,}\d+[a-z0-9.]*\b|\b\d+[a-z]{1,}[a-z0-9.]*\b", cleaned, re.IGNORECASE)
    ) or bool(re.search(r"\bv?\d+(?:\.\d+)+\b", lower_q))
    is_short = len(meaningful_words) <= 3
    has_file_ext_word = bool(
        re.search(r"\b(?:pdf|xlsx|docx|pptx|csv|txt|png|jpg|jpeg)\b", lower_q)
    )

    identifier_score = (
        (2 if has_numbers else 0)
        + (2 if has_roman_numeral else 0)
        + (1 if has_mixed_case else 0)
        + (3 if has_alphanum_code else 0)
        + (1 if is_short else 0)
        + (1 if has_file_ext_word else 0)
    )

    if identifier_score >= 3:
        return "identifier"
    if identifier_score >= 1:
        return "mixed"
    return "semantic"


def tokenize_query(query: str) -> list[str]:
    clean = re.sub(r"[^\w\s]", " ", query.lower())
    tokens: list[str] = []
    for token in clean.split():
        if len(token) > 2 or any(char.isdigit() for char in token):
            tokens.append(token)
    return tokens


def parse_query(query: str) -> ParsedQuery:
    q = query.lower().strip()
    remaining = query

    file_type, handler_type, file_phrases = _detect_file_type(q)
    file_type_strict = file_type is not None
    doc_file_type, doc_handler_type, doc_phrases, boost_terms = _detect_document_words(q)
    if file_type is None:
        file_type = doc_file_type
        handler_type = doc_handler_type
        file_type_strict = False
    if file_type is None:
        implicit_file_type, implicit_handler_type = _infer_implicit_file_type(q)
        if implicit_file_type is not None:
            file_type = implicit_file_type
            handler_type = implicit_handler_type
            file_type_strict = False
    source_type, source_phrases = _detect_source_type(q)
    if source_type == "email" and file_type_strict:
        file_type_strict = False
    time_window, time_phrases, time_confidence, prefer_latest = _detect_time_window(query)
    person_query = _strip_phrases(query, time_phrases)
    person_hints, person_phrases = _detect_person_hints(person_query)
    exact_text = _strip_phrases(remaining, source_phrases + time_phrases + person_phrases)

    phrases_to_remove = file_phrases + doc_phrases + source_phrases + time_phrases + person_phrases
    semantic_text = _strip_phrases(remaining, phrases_to_remove)

    if boost_terms:
        semantic_text = " ".join([*boost_terms, semantic_text]).strip()
        semantic_text = re.sub(r"\s+", " ", semantic_text).strip()

    if len(semantic_text.strip()) < 3 and not person_hints:
        semantic_text = query.strip()

    core_semantic_text = semantic_text
    semantic_text = expand_query(semantic_text)
    exact_text = exact_text.strip() or core_semantic_text.strip() or query.strip()
    query_style = detect_query_style(exact_text or query)

    semantic_word_count = len([part for part in core_semantic_text.split() if part.strip()])
    is_person_query = bool(person_hints) and semantic_word_count < 3

    if time_window is not None and time_window.start_date and time_window.end_date:
        logging.debug(
            "Time window: %s from %s to %s",
            time_window.description,
            time_window.start_date.date(),
            time_window.end_date.date(),
        )

    parsed = ParsedQuery(
        original=query,
        semantic_text=semantic_text,
        exact_text=exact_text,
        query_tokens=tokenize_query(query),
        file_type=file_type,
        file_type_strict=file_type_strict,
        handler_type=handler_type,
        source_type=source_type,
        time_window=time_window,
        time_confidence=time_confidence,
        prefer_latest=prefer_latest,
        person_hints=person_hints,
        is_person_query=is_person_query,
        query_style=query_style,
    )
    parsed.intent_class = QueryIntentClassifier.classify(query, parsed).value
    return parsed
