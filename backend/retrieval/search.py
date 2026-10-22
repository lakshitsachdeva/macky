from __future__ import annotations

import asyncio
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

try:
    from .. import config, database
    from ..indexer import embedder
    from . import query_parser
except ImportError:
    import config
    import database
    from indexer import embedder
    from retrieval import query_parser


logger = logging.getLogger(__name__)
MIN_TIME_FILTER_RESULTS = 3
NON_TIME_FALLBACK_RESULTS = 5
QUERY_EMBED_TIMEOUT_SECONDS = 2.5
USE_DYNAMIC_SCORING = True
USE_GRAPH_CANDIDATES = True
FILENAME_STOPWORDS = {
    "the",
    "a",
    "an",
    "i",
    "me",
    "my",
    "we",
    "our",
    "is",
    "in",
    "of",
    "to",
    "for",
    "and",
    "or",
    "but",
    "with",
    "by",
    "from",
    "at",
    "on",
    "that",
    "this",
    "it",
    "what",
    "which",
    "who",
    "whom",
    "whose",
    "where",
    "when",
    "why",
    "how",
    "do",
    "does",
    "did",
    "doing",
    "work",
    "working",
    "worked",
    "looking",
    "look",
    "find",
    "show",
    "saved",
    "save",
    "created",
    "modified",
    "opened",
    "accessed",
    "docs",
    "documents",
    "files",
    "be",
    "was",
    "were",
    "are",
    "have",
    "has",
    "had",
    "about",
    "file",
    "document",
}
BROAD_TOPIC_TERMS = {
    "major",
    "project",
    "projects",
    "related",
    "thing",
    "things",
    "stuff",
}
VISUAL_QUERY_STOPWORDS = {
    "image",
    "images",
    "photo",
    "photos",
    "picture",
    "pictures",
    "pic",
    "pics",
    "screenshot",
    "screenshots",
    "jpeg",
    "jpg",
    "png",
    "heic",
    "gif",
    "webp",
}
FTS_STOPWORDS = FILENAME_STOPWORDS | {
    "i",
    "got",
    "just",
    "week",
    "month",
    "year",
    "today",
    "yesterday",
    "last",
    "past",
    "recently",
}
LOW_SIGNAL_GMAIL_SUBJECT_PATTERNS = (
    re.compile(r"\bunsubscribe\b", re.IGNORECASE),
    re.compile(r"\bnewsletter\b", re.IGNORECASE),
    re.compile(r"\bweekly roundup\b", re.IGNORECASE),
    re.compile(r"\bdigest\b", re.IGNORECASE),
    re.compile(r"\bsale\b", re.IGNORECASE),
    re.compile(r"\boffer\b", re.IGNORECASE),
    re.compile(r"\bdiscount\b", re.IGNORECASE),
    re.compile(r"\bpromo\b", re.IGNORECASE),
)
LOW_SIGNAL_GMAIL_SENDER_PATTERNS = (
    re.compile(r"\bno-?reply\b", re.IGNORECASE),
    re.compile(r"\bnewsletter\b", re.IGNORECASE),
    re.compile(r"\bnotifications?\b", re.IGNORECASE),
    re.compile(r"\bupdates?\b", re.IGNORECASE),
    re.compile(r"\bmarketing\b", re.IGNORECASE),
)
FAST_FILENAME_PATTERN = re.compile(
    r"\b[a-z0-9_.-]+\.(?:pdf|docx|doc|xlsx|xls|pptx|csv|txt|md|png|jpg|jpeg)\b",
    re.IGNORECASE,
)
FAST_SIMPLE_NAME_PATTERN = re.compile(
    r"^[A-Za-z][A-Za-z0-9._'-]+\s+[A-Za-z][A-Za-z0-9._'-]+$"
)
RELATIONSHIP_COMPLEXITY_TERMS = (
    "connected to",
    "related to",
    "who",
    "whose",
    "what org",
    "what followed",
    "emails from",
    "sent by",
    "talking to",
    "about",
)
GENERATED_ARTIFACT_PATTERNS = (
    re.compile(r"(?:^|[/\\])validation_artifacts(?:[/\\]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[/\\])artifacts(?:[/\\]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[/\\])logs?(?:[/\\]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[/\\])rows_\d{8}_\d{6}\.csv$", re.IGNORECASE),
    re.compile(r"(?:^|[/\\])monitor_\d{8}_\d{6}\.txt$", re.IGNORECASE),
    re.compile(r"\b(?:metrics|trace|events|logs?)_\d{8}_\d{6}\b", re.IGNORECASE),
)
GENERATED_ARTIFACT_QUERY_TERMS = {
    "artifact",
    "artifacts",
    "csv",
    "row",
    "rows",
    "validation",
    "eval",
    "metrics",
    "trace",
    "event",
    "events",
    "log",
    "logs",
}
RAW_TABLE_QUERY_TERMS = GENERATED_ARTIFACT_QUERY_TERMS | {
    "data",
    "dataset",
    "datasets",
    "spreadsheet",
    "spreadsheets",
    "excel",
    "xlsx",
    "xls",
    "table",
    "tables",
}
RESUME_QUERY_TERMS = {
    "resume",
    "resumes",
    "cv",
    "curriculum",
    "vitae",
    "profile",
    "candidate",
    "internship",
    "job",
    "application",
    "lakshit",
}
ACADEMIC_QUERY_TERMS = {
    "assignment",
    "class",
    "college",
    "course",
    "coursework",
    "experiment",
    "lecture",
    "linear",
    "learning",
    "machine",
    "ml",
    "notes",
    "ppt",
    "practical",
    "regression",
    "sem",
    "semester",
    "slide",
    "slides",
    "sqa",
}
ACADEMIC_COURSEWORK_PATTERNS = (
    re.compile(r"(?:^|[/\\])college(?:[/\\]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[/\\])sem(?:ester)?\s*\d+(?:[/\\]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[/\\])ml(?:[/\\]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[/\\])sqa(?:[/\\]|$)", re.IGNORECASE),
    re.compile(
        r"\b(?:lecture|practical|experiment|assignment|linear regression|machine learning|ml unit)\b",
        re.IGNORECASE,
    ),
)
ACADEMIC_UNIT_PATTERN = re.compile(r"\bunit[\s_-]*(\d+)\b", re.IGNORECASE)
PROJECT_RECALL_QUERY_TERMS = {
    "project",
    "projects",
    "startup",
    "venture",
    "working",
    "worked",
    "work",
}
PROJECT_WORKSPACE_PATTERNS = (
    re.compile(r"(?:^|[/\\])Desktop[/\\]Projects(?:[/\\]|$)", re.IGNORECASE),
    re.compile(r"(?:^|[/\\])Projects(?:[/\\]|$)", re.IGNORECASE),
)
PROJECT_META_QUERY_TERMS = {
    "branch",
    "code",
    "contribute",
    "contributing",
    "contribution",
    "development",
    "git",
    "github",
    "install",
    "readme",
    "repo",
    "setup",
}
PROJECT_META_DOC_PATTERNS = (
    re.compile(r"(?:^|[/\\])CONTRIBUTING\.md$", re.IGNORECASE),
    re.compile(r"\bcontributing\s+to\b", re.IGNORECASE),
)
PROJECT_EXECUTION_META_PATTERNS = (
    re.compile(r"\bnext\s+steps?\b", re.IGNORECASE),
    re.compile(r"\breal\s+next\s+work\b", re.IGNORECASE),
    re.compile(r"\bfrom\s+here\s+to\s+(?:real\s+)?users\b", re.IGNORECASE),
    re.compile(r"\bwhat'?s\s+real\b.*\bwhat'?s\s+next\b", re.IGNORECASE),
    re.compile(r"\bproduction\s+readiness\b", re.IGNORECASE),
    re.compile(r"\baudit(?:\s+fix)?\s+prompts?\b", re.IGNORECASE),
)
GMAIL_SOURCE_TYPES = {"gmail", "gmail_attachment"}
GMAIL_QUERY_SOURCE_TYPES = {"email", "gmail", "gmail_attachment"}
SCORING_WEIGHT_KEYS = (
    "dense",
    "sparse",
    "fts",
    "filename",
    "time",
    "metadata",
    "behavior",
    "recency",
    "graph",
)
STATIC_NORMAL_WEIGHTS = {
    "dense": 0.25,
    "sparse": 0.10,
    "fts": 0.18,
    "filename": 0.12,
    "time": 0.15,
    "metadata": 0.08,
    "behavior": 0.07,
    "recency": 0.05,
    "graph": 0.0,
}
STATIC_TIME_HEAVY_WEIGHTS = {
    "dense": 0.18,
    "sparse": 0.08,
    "fts": 0.15,
    "filename": 0.10,
    "time": 0.35,
    "metadata": 0.08,
    "behavior": 0.05,
    "recency": 0.01,
    "graph": 0.0,
}
STATIC_STRONG_TIME_WEIGHTS = {
    "dense": 0.16,
    "sparse": 0.08,
    "fts": 0.12,
    "filename": 0.08,
    "time": 0.45,
    "metadata": 0.05,
    "behavior": 0.04,
    "recency": 0.02,
    "graph": 0.0,
}
STATIC_IDENTIFIER_WEIGHTS = {
    "dense": 0.12,
    "sparse": 0.10,
    "fts": 0.28,
    "filename": 0.33,
    "time": 0.05,
    "metadata": 0.07,
    "behavior": 0.03,
    "recency": 0.02,
    "graph": 0.0,
}
COMPATIBLE_TYPES: dict[str, tuple[str, ...]] = {
    "pdf": ("document",),
    "spreadsheet": ("csv", "xlsx", "xls"),
    "presentation": ("pptx", "key"),
    "image": ("png", "jpg", "jpeg", "gif", "webp", "heic"),
    "text": ("txt", "md", "rtf"),
    "document": ("docx", "doc", "pdf", "rtf"),
}
FILE_TYPE_ALIASES = {
    "jpeg": "jpg",
    "markdown": "md",
    "presentation": "presentation",
    "spreadsheet": "spreadsheet",
    "document": "document",
    "image": "image",
    "pdf": "pdf",
    "text": "text",
}


class SearchResult(BaseModel):
    item_id: str
    item: dict[str, Any]
    dense_score: float = 0.0
    sparse_score: float = 0.0
    visual_score: float = 0.0
    fts_score: float = 0.0
    filename_score: float = 0.0
    content_score: float = 0.0
    path_score: float = 0.0
    time_score: float = 0.0
    metadata_score: float = 0.0
    behavior_score: float = 0.0
    recency_score: float = 0.0
    graph_score: float = 0.0
    base_score: float = 0.0
    cognitive_score: float = 0.0
    final_score: float = 0.0
    intent_class: str | None = None
    source_pool: str = "base"
    graph_path: list[str] = Field(default_factory=list)
    complexity_score: float = 0.0
    pipeline: str = "standard"
    use_graph: bool = True
    use_temporal_subgraph: bool = False
    cognitive_breakdown: dict[str, Any] = Field(default_factory=dict)
    frame_type: str | None = None
    matched_fields: list[str] = Field(default_factory=list)


def _clamp_score(value: float) -> float:
    return max(0.0, min(1.0, float(value)))


def parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None

    cleaned = value.strip()
    if cleaned.endswith("Z"):
        cleaned = cleaned[:-1] + "+00:00"

    try:
        parsed = datetime.fromisoformat(cleaned)
    except ValueError:
        return None

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _normalize_dt(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _window_center(window: query_parser.TimeWindow | None) -> datetime | None:
    if window is None:
        return None

    center = _normalize_dt(window.center_date)
    if center is not None:
        return center

    start = _normalize_dt(window.start_date)
    end = _normalize_dt(window.end_date)

    if start is not None and end is not None:
        return start + (end - start) / 2
    return end or start


def _passes_time_filter(item: dict[str, Any], window: query_parser.TimeWindow | None) -> bool:
    if window is None:
        return True
    parsed_times = [
        parse_iso(value)
        for value in [
            item.get("first_seen_at"),
            item.get("modified_at"),
            item.get("created_at"),
        ]
        if value
    ]
    parsed_times = [value for value in parsed_times if value is not None]
    if not parsed_times:
        return False

    start = _normalize_dt(window.start_date)
    end = _normalize_dt(window.end_date)
    if start is not None and end is not None:
        tolerance_days = max(1.0, window.size_days * 1.5)
        for timestamp in parsed_times:
            if start <= timestamp <= end:
                return True
            if timestamp < start:
                days_off = (start - timestamp).total_seconds() / 86400
            else:
                days_off = (timestamp - end).total_seconds() / 86400
            if days_off <= tolerance_days:
                return True
        return False

    center = _window_center(window)
    if center is None:
        return True

    tolerance_days = max(1.0, window.size_days * 1.5)
    timestamps = [
        value for value in parsed_times if value is not None
    ]

    for parsed in timestamps:
        days_off = abs((parsed - center).total_seconds()) / 86400
        if days_off <= tolerance_days:
            return True

    return False


def _time_fit(item: dict[str, Any], window: query_parser.TimeWindow | None) -> float:
    if window is None:
        return 0.5

    timestamps = [
        item.get("first_seen_at"),
        item.get("modified_at"),
        item.get("created_at"),
    ]
    parsed_times = [parse_iso(value) for value in timestamps if value]
    parsed_times = [value for value in parsed_times if value is not None]
    if not parsed_times:
        return 0.0

    best = 0.0
    start = _normalize_dt(window.start_date)
    end = _normalize_dt(window.end_date)
    if start is not None and end is not None:
        for timestamp in parsed_times:
            if start <= timestamp <= end:
                best = max(best, 1.0)
                continue

            # Use fractional-day precision. The prior `.days` attribute truncates
            # toward zero on a timedelta, which silently inverted rank between
            # `_passes_time_filter` (which uses total_seconds/86400) and this
            # scorer for items 1.5–2.999 days outside the window.
            if timestamp < start:
                days_off = (start - timestamp).total_seconds() / 86400.0
            else:
                days_off = (timestamp - end).total_seconds() / 86400.0

            if days_off <= 2.0:
                best = max(best, 0.7)
            elif days_off <= float(window.size_days):
                best = max(best, 0.3)
            else:
                best = max(best, 0.0)
        return best

    center = _window_center(window)
    if center is None:
        return 0.5

    for timestamp in parsed_times:
        days_off = abs((timestamp - center).total_seconds()) / 86400
        if days_off <= window.size_days:
            best = max(best, 1.0)
        elif days_off <= max(2.0, window.size_days * 1.5):
            best = max(best, 0.7)
        elif days_off <= max(float(window.size_days), 7.0):
            best = max(best, 0.3)

    return best


def _metadata_match(item: dict[str, Any], parsed: query_parser.ParsedQuery) -> float:
    hits = 0.0
    total = 0.0

    if parsed.file_type:
        total += 1.0
        hits += 1.0 if item.get("file_type") == parsed.file_type else 0.0

    if parsed.source_type:
        total += 1.0
        item_source = str(item.get("source_type") or "").strip().casefold()
        parsed_source = str(parsed.source_type or "").strip().casefold()
        source_matches = (
            item_source == parsed_source
            or (item_source in GMAIL_SOURCE_TYPES and parsed_source in GMAIL_QUERY_SOURCE_TYPES)
        )
        hits += 1.0 if source_matches else 0.0

    if parsed.person_hints:
        total += 2.0
        searchable = " ".join(
            filter(
                None,
                [
                    (item.get("sender_name") or "").casefold(),
                    (item.get("sender_email") or "").casefold(),
                    (item.get("filename") or "").casefold(),
                    (item.get("title") or "").casefold(),
                    str(item.get("extracted_text") or "")[:500].casefold(),
                    str(item.get("keywords") or "").casefold(),
                ],
            )
        )
        for hint in parsed.person_hints:
            if hint.casefold() in searchable:
                hits += 2.0
                break

    return hits / total if total > 0 else 0.5


def _is_low_signal_gmail_item(item: dict[str, Any]) -> bool:
    source_type = str(item.get("source_type") or "").strip().casefold()
    if source_type not in GMAIL_SOURCE_TYPES:
        return False

    subject = " ".join(
        filter(
            None,
            [
                str(item.get("title") or "").strip(),
                str(item.get("filename") or "").strip(),
                str(item.get("summary") or "").strip(),
            ],
        )
    )
    sender = " ".join(
        filter(
            None,
            [
                str(item.get("sender_name") or "").strip(),
                str(item.get("sender_email") or "").strip(),
            ],
        )
    )
    preview = str(item.get("preview_text") or "").strip()
    searchable = " ".join(filter(None, [subject, sender, preview]))

    if any(pattern.search(searchable) for pattern in LOW_SIGNAL_GMAIL_SUBJECT_PATTERNS):
        return True
    if any(pattern.search(sender) for pattern in LOW_SIGNAL_GMAIL_SENDER_PATTERNS):
        return True

    return False


def _gmail_has_attachment(item: dict[str, Any]) -> bool:
    if str(item.get("source_type") or "").strip().casefold() == "gmail_attachment":
        return True
    raw_keywords = item.get("keywords")
    if isinstance(raw_keywords, list):
        return any(str(entry or "").strip().casefold() == "has_attachment" for entry in raw_keywords)
    return "has_attachment" in str(raw_keywords or "").casefold()


def _source_type_bias(item: dict[str, Any], parsed: query_parser.ParsedQuery) -> float:
    source_type = str(item.get("source_type") or "").strip().casefold()
    if source_type not in GMAIL_SOURCE_TYPES:
        return 1.0

    low_signal = _is_low_signal_gmail_item(item)
    has_attachment = _gmail_has_attachment(item)
    attachment_item = source_type == "gmail_attachment"
    important = (
        float(item.get("importance_score") or 0.0) >= 0.72
        or int(item.get("open_count") or 0) > 0
    )
    explicit_email_query = str(parsed.source_type or "").strip().casefold() in GMAIL_QUERY_SOURCE_TYPES

    if explicit_email_query:
        return 0.9 if low_signal else 1.0
    if attachment_item:
        if parsed.file_type or parsed.handler_type:
            return 0.70 if not low_signal else 0.24
        return 0.38 if not low_signal else 0.18
    if parsed.person_hints:
        if important:
            return 0.96 if has_attachment else 0.92
        return 0.42 if low_signal else 0.72
    if parsed.file_type or parsed.handler_type:
        if important:
            return 0.62 if has_attachment else 0.55
        return 0.16 if low_signal else 0.38
    if important:
        return 0.86 if has_attachment else 0.78
    return 0.18 if low_signal else 0.60 if has_attachment else 0.52


def _generated_artifact_bias(item: dict[str, Any], parsed: query_parser.ParsedQuery) -> float:
    query_tokens = _meaningful_tokens(parsed.original, stopwords=FILENAME_STOPWORDS)
    if (
        parsed.query_style == "identifier"
        or parsed.file_type == "csv"
        or query_tokens & GENERATED_ARTIFACT_QUERY_TERMS
    ):
        return 1.0

    searchable = " ".join(
        filter(
            None,
            [
                str(item.get("title") or ""),
                str(item.get("filename") or ""),
                str(item.get("original_path") or ""),
            ],
        )
    )
    if not searchable:
        return 1.0

    if any(pattern.search(searchable) for pattern in GENERATED_ARTIFACT_PATTERNS):
        return 0.24
    if str(item.get("filename") or "").casefold().endswith(".csv") and not (
        query_tokens & RAW_TABLE_QUERY_TERMS
    ):
        return 0.55
    return 1.0


def _resume_noise_bias(item: dict[str, Any], parsed: query_parser.ParsedQuery) -> float:
    query_tokens = _meaningful_tokens(parsed.original, stopwords=FILENAME_STOPWORDS)
    if query_tokens & RESUME_QUERY_TERMS or parsed.person_hints:
        return 1.0

    searchable = " ".join(
        filter(
            None,
            [
                str(item.get("title") or ""),
                str(item.get("filename") or ""),
                str(item.get("original_path") or ""),
                str(item.get("doc_type_guess") or ""),
                str(item.get("keywords") or ""),
            ],
        )
    ).casefold()
    searchable = re.sub(r"[._/-]+", " ", searchable)
    if re.search(r"\b(?:resume|curriculum vitae|cv)\b", searchable):
        return 0.55
    return 1.0


def _is_project_recall_query(parsed: query_parser.ParsedQuery) -> bool:
    query_tokens = _meaningful_tokens(parsed.original, stopwords=set())
    if query_tokens & PROJECT_RECALL_QUERY_TERMS:
        return True

    lowered = parsed.original.casefold()
    return bool(
        re.search(
            r"\b(?:working\s+on|worked\s+on|building|built|making|made)\b",
            lowered,
        )
    )


def _academic_coursework_bias(item: dict[str, Any], parsed: query_parser.ParsedQuery) -> float:
    query_tokens = _meaningful_tokens(parsed.original, stopwords=FILENAME_STOPWORDS)
    if query_tokens & ACADEMIC_QUERY_TERMS:
        return 1.0

    searchable = " ".join(
        filter(
            None,
            [
                str(item.get("title") or ""),
                str(item.get("filename") or ""),
                str(item.get("original_path") or ""),
                str(item.get("keywords") or ""),
            ],
        )
    )
    if not searchable:
        return 1.0

    if any(pattern.search(searchable) for pattern in ACADEMIC_COURSEWORK_PATTERNS):
        return 0.48 if _is_project_recall_query(parsed) else 0.72
    return 1.0


def _project_workspace_bias(item: dict[str, Any], parsed: query_parser.ParsedQuery) -> float:
    if not _is_project_recall_query(parsed):
        return 1.0

    path_value = str(item.get("original_path") or "")
    if not path_value:
        return 1.0

    if any(pattern.search(path_value) for pattern in PROJECT_WORKSPACE_PATTERNS):
        topical_evidence = max(
            _content_score(item, parsed),
            _filename_score(item, parsed),
            _path_score(item, parsed),
        )
        if topical_evidence < 0.20:
            return 1.0
        return 1.14
    return 1.0


def _academic_unit_lookup_bias(item: dict[str, Any], parsed: query_parser.ParsedQuery) -> float:
    if parsed.query_style != "identifier":
        return 1.0

    lookup_text = parsed.exact_text or parsed.semantic_text or parsed.original
    query_match = ACADEMIC_UNIT_PATTERN.search(lookup_text or "")
    if query_match is None:
        return 1.0

    query_unit = query_match.group(1)
    searchable = " ".join(
        filter(
            None,
            [
                str(item.get("filename") or ""),
                str(item.get("title") or ""),
                str(item.get("preview_text") or "")[:400],
                str(item.get("summary") or "")[:400],
            ],
        )
    )
    unit_matches = [match.group(1) for match in ACADEMIC_UNIT_PATTERN.finditer(searchable)]
    if query_unit in unit_matches:
        return 1.18
    if unit_matches:
        return 0.48
    return 0.64


def _project_meta_doc_bias(item: dict[str, Any], parsed: query_parser.ParsedQuery) -> float:
    if not _is_project_recall_query(parsed):
        return 1.0
    query_tokens = _meaningful_tokens(parsed.original, stopwords=FILENAME_STOPWORDS)
    if query_tokens & PROJECT_META_QUERY_TERMS:
        return 1.0
    if any(pattern.search(parsed.original) for pattern in PROJECT_EXECUTION_META_PATTERNS):
        return 1.0

    searchable = " ".join(
        filter(
            None,
            [
                str(item.get("title") or ""),
                str(item.get("filename") or ""),
                str(item.get("original_path") or ""),
            ],
        )
    )
    if any(pattern.search(searchable) for pattern in PROJECT_META_DOC_PATTERNS):
        return 0.52
    if any(pattern.search(searchable) for pattern in PROJECT_EXECUTION_META_PATTERNS):
        return 0.42
    return 1.0


def _visual_asset_bias(item: dict[str, Any], parsed: query_parser.ParsedQuery) -> float:
    if parsed.handler_type == "visual":
        return 1.0
    if not _is_project_recall_query(parsed):
        return 1.0
    if item.get("handler_type") == "visual":
        return 0.56
    return 1.0


def _dense_only_visual_false_positive_bias(
    item: dict[str, Any],
    parsed: query_parser.ParsedQuery,
    *,
    dense_score: float,
    fts_score: float,
    filename_score: float,
    content_score: float,
    path_score: float,
    has_visual_lexical_anchor: bool,
) -> float:
    if item.get("handler_type") != "visual":
        return 1.0
    if parsed.file_type != "image" and parsed.handler_type != "visual":
        return 1.0
    if not has_visual_lexical_anchor:
        return 1.0

    topical_tokens = _topic_query_tokens(parsed)
    if len(topical_tokens) < 2:
        return 1.0

    lexical_evidence = max(
        _clamp_score(fts_score),
        _clamp_score(filename_score),
        _clamp_score(content_score),
        _clamp_score(path_score),
    )
    if lexical_evidence >= 0.15:
        return 1.0
    if dense_score < 0.35:
        return 0.45
    if len(topical_tokens) >= 3 and lexical_evidence <= 0.02:
        return 0.12
    return 0.28


def _meaningful_tokens(*texts: str, stopwords: set[str]) -> set[str]:
    tokens: set[str] = set()
    for text in texts:
        for word in re.findall(r"[A-Za-z0-9]+", text or ""):
            normalized = word.casefold()
            if len(normalized) <= 2 and not normalized.isdigit():
                continue
            if normalized in stopwords:
                continue
            tokens.add(normalized)
    return tokens


def _drop_broad_topic_terms(tokens: set[str]) -> set[str]:
    specific_tokens = {token for token in tokens if token not in BROAD_TOPIC_TERMS}
    return specific_tokens or tokens


def _query_lookup_tokens(parsed: query_parser.ParsedQuery) -> set[str]:
    tokens = _drop_broad_topic_terms(
        _meaningful_tokens(
            parsed.exact_text,
            parsed.semantic_text,
            *parsed.person_hints,
            stopwords=FILENAME_STOPWORDS,
        )
    )
    if parsed.file_type == "image" or parsed.handler_type == "visual":
        filtered = {token for token in tokens if token not in VISUAL_QUERY_STOPWORDS}
        if filtered:
            return filtered
    return tokens


def _path_query_tokens(parsed: query_parser.ParsedQuery) -> set[str]:
    return _query_lookup_tokens(parsed)


def _topic_query_tokens(parsed: query_parser.ParsedQuery) -> set[str]:
    return _query_lookup_tokens(parsed)


def _filename_score(item: dict[str, Any], parsed: query_parser.ParsedQuery) -> float:
    lookup_text = parsed.exact_text or parsed.semantic_text or parsed.original
    if not lookup_text:
        return 0.0

    query_tokens = _drop_broad_topic_terms(
        _meaningful_tokens(
            lookup_text,
            parsed.semantic_text,
            *parsed.person_hints,
            stopwords=FILENAME_STOPWORDS,
        )
    )

    if not query_tokens:
        return 0.0

    filename_str = re.sub(r"[._\\-]", " ", item.get("filename", "")).lower()
    title_str = re.sub(r"[._\\-]", " ", (item.get("title") or "")).lower()
    filename_tokens = set(re.findall(r"[a-z0-9]+", filename_str))
    title_tokens = set(re.findall(r"[a-z0-9]+", title_str))
    file_tokens = filename_tokens | title_tokens

    if not filename_tokens and not title_tokens:
        return 0.0

    filename_overlap = len(query_tokens & filename_tokens) / len(query_tokens) if filename_tokens else 0.0
    title_overlap = len(query_tokens & title_tokens) / len(query_tokens) if title_tokens else 0.0
    combined_overlap = len(query_tokens & file_tokens) / len(query_tokens)

    score = max(
        combined_overlap * 1.4,
        filename_overlap * 1.8,
        title_overlap * 1.7,
    )

    if len(query_tokens) >= 2 and filename_tokens and query_tokens.issubset(filename_tokens):
        score = max(score, 0.95)
    elif len(query_tokens) >= 2 and title_tokens and query_tokens.issubset(title_tokens):
        score = max(score, 0.90)
    elif len(query_tokens) >= 3:
        # A near-match such as "PROJECT_DEEP_DIVE.md" should not look perfect
        # for "recurdo complete deep dive" when the exact titled file exists.
        score = min(score, 0.88)

    return min(1.0, score)


def _content_score(item: dict[str, Any], parsed: query_parser.ParsedQuery) -> float:
    query_tokens = _query_lookup_tokens(parsed)
    if not query_tokens:
        return 0.0

    searchable = " ".join(
        filter(
            None,
            [
                item.get("title") or "",
                item.get("summary") or "",
                item.get("keywords") or "",
                str(item.get("preview_text") or "")[:1000],
                str(item.get("extracted_text") or "")[:4000],
            ],
        )
    )
    content_tokens = _meaningful_tokens(searchable, stopwords=FILENAME_STOPWORDS)
    if not content_tokens:
        return 0.0

    overlap = query_tokens & content_tokens
    score = len(overlap) / len(query_tokens)
    return min(1.0, score * 1.5)


def _sparse_query_tokens(parsed: query_parser.ParsedQuery) -> list[str]:
    topic_tokens = sorted(_topic_query_tokens(parsed))
    return topic_tokens or list(parsed.query_tokens)


def _recent_visual_bonus(item: dict[str, Any], visual_score: float, recency_score: float) -> float:
    if item.get("handler_type") != "visual":
        return 0.0
    if visual_score < 0.65:
        return 0.0
    if recency_score < 0.45:
        return 0.0
    return min(0.06, 0.04 * visual_score + 0.03 * recency_score)


def _ranking_evidence_bonus(
    item: dict[str, Any],
    *,
    content_score: float,
    path_score: float,
    fts_score: float,
    recency_score: float = 0.0,
) -> float:
    bonus = 0.0

    if content_score > 0.0:
        bonus += 0.10 * content_score
    if path_score > 0.0:
        bonus += 0.04 * path_score

    # OCR-backed screenshots and posters often have great lexical evidence
    # but weak dense similarity. Give them a bounded boost so they can compete
    # with nearby document hits instead of getting buried.
    if item.get("handler_type") == "visual":
        visual_text_strength = max(content_score, fts_score)
        if visual_text_strength >= 0.45:
            bonus += 0.16 * visual_text_strength
        bonus += _recent_visual_bonus(item, visual_text_strength, recency_score)

    return min(0.22, bonus)


def _path_score(item: dict[str, Any], parsed: query_parser.ParsedQuery) -> float:
    query_tokens = _path_query_tokens(parsed)
    if not query_tokens:
        return 0.0

    path_value = str(item.get("original_path") or "")
    if not path_value:
        return 0.0

    path_tokens = _meaningful_tokens(
        path_value.replace("/", " ").replace("\\", " "),
        stopwords=FILENAME_STOPWORDS,
    )
    if not path_tokens:
        return 0.0

    overlap = query_tokens & path_tokens
    if not overlap:
        return 0.0

    score = len(overlap) / len(query_tokens)
    return min(1.0, score * 1.5)


def _behavior(item: dict[str, Any]) -> float:
    open_count = int(item.get("open_count") or 0)
    dwell_seconds = int(item.get("total_dwell_seconds") or 0)
    copy_events = int(item.get("copy_event_count") or 0)

    return min(
        1.0,
        min(0.4, open_count * 0.05)
        + min(0.3, (dwell_seconds / 3600) * 0.3)
        + min(0.3, copy_events * 0.1),
    )


def _recency(item: dict[str, Any]) -> float:
    last_value = item.get("last_opened_at") or item.get("first_seen_at")
    last_timestamp = parse_iso(last_value)
    if last_timestamp is None:
        return 0.1

    days = (datetime.now(timezone.utc) - last_timestamp).days
    return max(0.0, 1.0 - days / 90)


def _fts_token_query(*texts: str, require_all: bool = False) -> list[str]:
    ordered_queries: list[str] = []
    seen_queries: set[str] = set()

    for text in texts:
        tokens: list[str] = []
        seen_tokens: set[str] = set()
        for token in re.findall(r"[A-Za-z0-9]+", text or ""):
            normalized = token.casefold()
            if len(normalized) < 2:
                continue
            if normalized in FTS_STOPWORDS:
                continue
            if normalized in seen_tokens:
                continue
            seen_tokens.add(normalized)
            tokens.append(normalized)

        if not tokens:
            continue

        if len(set(tokens) - BROAD_TOPIC_TERMS) > 0:
            tokens = [token for token in tokens if token not in BROAD_TOPIC_TERMS]

        if require_all:
            # Identifier-like queries are usually filenames, UUIDs, or exact
            # markers. An OR query makes a non-existent marker such as
            # `zzzz-no-such-term-123` match every document containing the
            # generic word `term`. Require every meaningful token instead.
            queries = [" ".join(tokens[:12])]
        else:
            queries = [
                " OR ".join(tokens[:12]),
                " ".join(tokens[:6]),
            ]
        for query in queries:
            cleaned = query.strip()
            if not cleaned or cleaned in seen_queries:
                continue
            seen_queries.add(cleaned)
            ordered_queries.append(cleaned)

    return ordered_queries


def _normalized_lookup_text(value: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[._\\/-]", " ", value.casefold())).strip()


def exact_filename_bonus(query: str, filename: str) -> float:
    """
    Returns a multiplier (1.0 = no bonus, 1.3 = 30% boost).
    """
    q_lower = _normalized_lookup_text(query)
    f_lower = _normalized_lookup_text(filename)

    if not q_lower or not f_lower:
        return 1.0

    if q_lower in f_lower:
        return 1.45

    tokens = [token for token in q_lower.split() if token not in FILENAME_STOPWORDS]
    if not tokens:
        tokens = q_lower.split()
    if not tokens:
        return 1.0

    if len(tokens) >= 2 and all(token in f_lower for token in tokens):
        return 1.38

    matches = sum(1 for token in tokens if token in f_lower)
    if matches / len(tokens) >= 0.75:
        return 1.12

    return 1.0


def _exact_match_bonus(item: dict[str, Any], parsed: query_parser.ParsedQuery) -> float:
    lookup_text = parsed.exact_text or parsed.semantic_text or parsed.original
    filename_bonus = exact_filename_bonus(lookup_text, item.get("filename") or "")
    title_bonus = exact_filename_bonus(lookup_text, item.get("title") or "")
    bonus = max(filename_bonus, title_bonus)
    filename_score = _clamp_score(_filename_score(item, parsed))

    if parsed.query_style == "identifier":
        # Cap identifier bonus so that `final_score * bonus` does not saturate
        # every strong filename match to exactly 1.0 — doing so collapsed the
        # secondary sort order (two perfect-filename hits became ties rather
        # than being disambiguated by dense/content/recency evidence).
        if filename_score >= 0.95:
            bonus = max(bonus, 1.35)
        elif filename_score >= 0.80:
            bonus = max(bonus, 1.22)

    return bonus


def _weights_for_query(parsed: query_parser.ParsedQuery) -> dict[str, float]:
    if not USE_DYNAMIC_SCORING:
        if parsed.time_confidence == "strong" and parsed.time_window and parsed.time_window.start_date:
            return dict(STATIC_STRONG_TIME_WEIGHTS)
        if parsed.time_window and parsed.time_window.start_date:
            return dict(STATIC_TIME_HEAVY_WEIGHTS)
        if parsed.query_style == "identifier":
            return dict(STATIC_IDENTIFIER_WEIGHTS)
        return dict(STATIC_NORMAL_WEIGHTS)
    raw_weights = query_parser.QueryIntentClassifier.get_weights(parsed.intent_class)
    total = sum(float(raw_weights.get(key, 0.0) or 0.0) for key in SCORING_WEIGHT_KEYS)
    if total <= 0:
        total = 1.0
    return {
        key: float(raw_weights.get(key, 0.0) or 0.0) / total
        for key in SCORING_WEIGHT_KEYS
    }


def _relationship_complexity_hit(query_text: str) -> bool:
    lowered = query_text.casefold()
    return any(term in lowered for term in RELATIONSHIP_COMPLEXITY_TERMS)


def compute_query_complexity(query: str, parsed: query_parser.ParsedQuery) -> float:
    score = 0.2
    if parsed.is_person_query or parsed.person_hints:
        score += 0.15
    if parsed.time_window is not None:
        score += 0.15
    salient_tokens = [
        token
        for token in parsed.query_tokens
        if len(token) >= 4 and token.casefold() not in FILENAME_STOPWORDS
    ]
    if len(set(salient_tokens)) >= 3:
        score += 0.15
    if _relationship_complexity_hit(query):
        score += 0.20
    if parsed.intent_class in {
        query_parser.IntentClass.PERSON_CENTRIC.value,
        query_parser.IntentClass.ENTITY_LOOKUP.value,
    }:
        score += 0.20
    elif parsed.intent_class == query_parser.IntentClass.TEMPORAL_STATE.value:
        score += 0.10
    return min(1.0, score)


def _is_fast_path_query(query: str, parsed: query_parser.ParsedQuery) -> bool:
    lowered = query.strip()
    terms = [token for token in re.findall(r"[a-z0-9_.-]+", lowered.casefold()) if token]
    if parsed.query_style == "identifier":
        return True
    if FAST_FILENAME_PATTERN.search(lowered):
        return True
    if len(terms) < 3 and not _relationship_complexity_hit(lowered):
        return True
    if len(terms) == 2 and FAST_SIMPLE_NAME_PATTERN.fullmatch(lowered):
        return True
    return False


def _resolve_query_pipeline(query: str, parsed: query_parser.ParsedQuery) -> dict[str, Any]:
    complexity = compute_query_complexity(query, parsed)
    if _is_fast_path_query(query, parsed):
        return {
            "complexity_score": round(min(complexity, 0.25), 3),
            "pipeline": "fast",
            "use_graph": False,
            "use_temporal_subgraph": False,
        }
    if complexity < 0.3:
        return {
            "complexity_score": round(complexity, 3),
            "pipeline": "fast",
            "use_graph": False,
            "use_temporal_subgraph": False,
        }
    if complexity <= 0.7:
        return {
            "complexity_score": round(complexity, 3),
            "pipeline": "standard",
            "use_graph": True,
            "use_temporal_subgraph": False,
        }
    return {
        "complexity_score": round(complexity, 3),
        "pipeline": "complex",
        "use_graph": True,
        "use_temporal_subgraph": bool(parsed.time_window is not None),
    }


def _apply_pipeline_to_weights(weights: dict[str, float], *, use_graph: bool) -> dict[str, float]:
    adjusted = dict(weights)
    if use_graph:
        return adjusted

    adjusted["graph"] = 0.0
    remaining_total = sum(adjusted.get(key, 0.0) for key in SCORING_WEIGHT_KEYS if key != "graph")
    if remaining_total <= 0:
        return adjusted
    for key in SCORING_WEIGHT_KEYS:
        if key == "graph":
            continue
        adjusted[key] = float(adjusted.get(key, 0.0) or 0.0) / remaining_total
    return adjusted


def _final_weighted_score(
    weights: dict[str, float],
    *,
    dense_score: float,
    sparse_score: float,
    fts_score: float,
    filename_score: float,
    time_score: float,
    metadata_score: float,
    behavior_score: float,
    recency_score: float,
    graph_score: float,
) -> float:
    return (
        weights["dense"] * dense_score
        + weights["sparse"] * sparse_score
        + weights["fts"] * fts_score
        + weights["filename"] * filename_score
        + weights["time"] * time_score
        + weights["metadata"] * metadata_score
        + weights["behavior"] * behavior_score
        + weights["recency"] * recency_score
        + weights["graph"] * graph_score
    )


def _topical_evidence_score(
    *,
    dense_score: float,
    fts_score: float,
    filename_score: float,
    content_score: float,
    path_score: float,
) -> float:
    return max(
        _clamp_score(dense_score),
        _clamp_score(fts_score),
        _clamp_score(filename_score),
        _clamp_score(content_score),
        _clamp_score(path_score),
    )


def _is_time_only_match(
    parsed: query_parser.ParsedQuery,
    *,
    dense_score: float,
    fts_score: float,
    filename_score: float,
    content_score: float,
    path_score: float,
) -> bool:
    if parsed.time_window is None:
        return False
    if not _topic_query_tokens(parsed):
        return False
    return (
        _topical_evidence_score(
            dense_score=dense_score,
            fts_score=fts_score,
            filename_score=filename_score,
            content_score=content_score,
            path_score=path_score,
        )
        < 0.08
    )


def _time_filter_rescue_candidates(
    items: dict[str, dict[str, Any]],
    parsed: query_parser.ParsedQuery,
    text_dense_candidates: dict[str, float],
    fts_candidates: dict[str, float],
    limit: int = NON_TIME_FALLBACK_RESULTS,
) -> dict[str, dict[str, Any]]:
    if parsed.time_confidence == "strong":
        return {}
    if parsed.query_style not in {"identifier", "mixed"}:
        return {}

    scored_candidates: list[tuple[float, str, dict[str, Any]]] = []
    for item_id, item in items.items():
        filename_score = _clamp_score(_filename_score(item, parsed))
        bonus = _exact_match_bonus(item, parsed)
        fts_score = _clamp_score(fts_candidates.get(item_id, 0.0))
        if filename_score < 0.6 and bonus <= 1.0 and fts_score < 0.85:
            continue

        score = _base_relevance_score(item, parsed, text_dense_candidates, fts_candidates) * bonus
        scored_candidates.append((score, item_id, item))

    scored_candidates.sort(key=lambda entry: entry[0], reverse=True)
    return {
        item_id: item
        for _score, item_id, item in scored_candidates[:limit]
    }


def _freshness_timestamp(item: dict[str, Any]) -> datetime | None:
    for key in ("modified_at", "created_at", "first_seen_at", "last_opened_at"):
        parsed = parse_iso(item.get(key))
        if parsed is not None:
            return parsed
    return None


def _normalize_file_type(file_type: str | None) -> str | None:
    if not file_type:
        return None
    normalized = str(file_type).casefold().strip()
    if not normalized:
        return None
    return FILE_TYPE_ALIASES.get(normalized, normalized)


def apply_type_preference(
    score: float,
    item_type: str | None,
    query_type: str | None,
    is_strict: bool,
) -> float:
    normalized_query_type = _normalize_file_type(query_type)
    normalized_item_type = _normalize_file_type(item_type)

    if not normalized_query_type:
        return score

    if normalized_item_type == normalized_query_type:
        multiplier = 1.20 if is_strict else 1.10
    elif normalized_item_type in {
        _normalize_file_type(value)
        for value in COMPATIBLE_TYPES.get(normalized_query_type, ())
    }:
        multiplier = 1.05
    else:
        multiplier = 0.85 if is_strict else 1.0

    return min(1.0, score * multiplier)


def _type_match_rank(item_type: str | None, query_type: str | None) -> int:
    normalized_query_type = _normalize_file_type(query_type)
    normalized_item_type = _normalize_file_type(item_type)
    if not normalized_query_type:
        return 0
    if normalized_item_type == normalized_query_type:
        return 2
    compatible_types = {
        _normalize_file_type(value)
        for value in COMPATIBLE_TYPES.get(normalized_query_type, ())
    }
    if normalized_item_type in compatible_types:
        return 1
    return 0


def _has_strong_lexical_file_type_match(
    item: dict[str, Any],
    parsed: query_parser.ParsedQuery,
    *,
    fts_score: float = 0.0,
) -> bool:
    """Keep a strong natural-language hit even when its metadata type differs."""
    if parsed.query_style == "identifier" or not _topic_query_tokens(parsed):
        return False

    title_and_filename_score = _clamp_score(_filename_score(item, parsed))
    content_score = _clamp_score(_content_score(item, parsed))
    return (
        title_and_filename_score >= 0.80
        or content_score >= 0.80
        or _clamp_score(fts_score) >= 0.80
    )


def _apply_type_tie_break(
    results: list[SearchResult],
    parsed: query_parser.ParsedQuery,
) -> list[SearchResult]:
    if not parsed.file_type or len(results) < 2:
        return results

    reordered = results[:]
    changed = True
    while changed:
        changed = False
        for index in range(1, len(reordered)):
            previous = reordered[index - 1]
            current = reordered[index]
            previous_rank = _type_match_rank(previous.item.get("file_type"), parsed.file_type)
            current_rank = _type_match_rank(current.item.get("file_type"), parsed.file_type)
            if current_rank <= previous_rank:
                continue
            if current.final_score + 0.05 < previous.final_score:
                continue
            reordered[index - 1], reordered[index] = current, previous
            changed = True
    return reordered


def _base_relevance_score(
    item: dict[str, Any],
    parsed: query_parser.ParsedQuery,
    text_dense_candidates: dict[str, float],
    fts_candidates: dict[str, float],
) -> float:
    dense_score = _clamp_score(text_dense_candidates.get(item["id"], 0.0))
    sparse_score = _clamp_score(
        embedder.compute_sparse_score(_sparse_query_tokens(parsed), item.get("sparse_weights"))
    )
    fts_score = _clamp_score(fts_candidates.get(item["id"], 0.0))
    filename_score = _clamp_score(_filename_score(item, parsed))
    content_score = _clamp_score(_content_score(item, parsed))
    path_score = _clamp_score(_path_score(item, parsed))
    metadata_score = _clamp_score(_metadata_match(item, parsed))
    score = _final_weighted_score(
        _weights_for_query(parsed),
        dense_score=dense_score,
        sparse_score=sparse_score,
        fts_score=fts_score,
        filename_score=filename_score,
        time_score=0.5,
        metadata_score=metadata_score,
        behavior_score=0.0,
        recency_score=0.0,
    )
    score += _ranking_evidence_bonus(
        item,
        content_score=content_score,
        path_score=path_score,
        fts_score=fts_score,
        recency_score=0.0,
    )
    return _clamp_score(score)


def _matched_fields(
    item_id: str,
    parsed: query_parser.ParsedQuery,
    dense_score: float,
    sparse_score: float,
    visual_score: float,
    fts_score: float,
    filename_score: float,
    content_score: float,
    path_score: float,
    time_score: float,
    metadata_score: float,
    behavior_score: float,
    recency_score: float,
    recent_ids: set[str],
) -> list[str]:
    fields: list[str] = []

    if dense_score > 0.05:
        fields.append("dense")
    if sparse_score > 0.05:
        fields.append("sparse")
    if visual_score > 0.05:
        fields.append("visual")
    if fts_score > 0.05:
        fields.append("fts")
    if filename_score > 0.3:
        fields.append("filename")
    if content_score > 0.3:
        fields.append("content")
    if path_score > 0.3:
        fields.append("path")
    if parsed.time_window is not None and time_score > 0.0:
        fields.append("time")
    if (
        (parsed.file_type or parsed.source_type or parsed.person_hints)
        and metadata_score > 0.5
    ):
        fields.append("metadata")
    if behavior_score > 0.1:
        fields.append("behavior")
    if item_id in recent_ids or recency_score > 0.5:
        fields.append("recency")

    return fields


async def search(
    query: str,
    limit: int = 10,
    *,
    return_debug: bool = False,
    case_id: str | None = None,
) -> list[SearchResult] | tuple[list[SearchResult], dict[str, Any]]:
    total_started = time.perf_counter()
    parsed = query_parser.parse_query(query)
    route = _resolve_query_pipeline(query, parsed)
    parse_elapsed_ms = (time.perf_counter() - total_started) * 1000.0
    def _empty_debug(candidate_pool_sizes: dict[str, int] | None = None) -> tuple[list[SearchResult], dict[str, Any]]:
        return (
            [],
            {
                "query": query,
                "intent_class": parsed.intent_class,
                "pipeline": route["pipeline"],
                "complexity_score": float(route["complexity_score"]),
                "use_graph": bool(route["use_graph"]),
                "use_temporal_subgraph": bool(route["use_temporal_subgraph"]),
                "seed_entities_in_graph": [],
                "candidate_pool_sizes": candidate_pool_sizes or {
                    "dense": 0,
                    "fts": 0,
                    "path": 0,
                    "time": 0,
                    "graph": 0,
                    "recent": 0,
                    "combined": 0,
                },
                "latency_ms": {
                    "total": round((time.perf_counter() - total_started) * 1000.0, 2),
                    "parse": round(parse_elapsed_ms, 2),
                    "embedding": 0.0,
                    "candidate_generation": 0.0,
                    "scoring": 0.0,
                    "cognitive_rerank": 0.0,
                },
                "ranked_results": [],
            },
        )
    intent_profile = query_parser.QueryIntentClassifier.get_weights(parsed.intent_class)
    logger.info(
        "Query classified as %s, graph_weight=%.2f, pipeline=%s, complexity=%.2f",
        parsed.intent_class,
        float(intent_profile.get("graph", 0.0) or 0.0),
        route["pipeline"],
        float(route["complexity_score"]),
    )
    model_pool_status = embedder.get_model_pool_status()
    query_pool_ready = bool(model_pool_status.get("query_pool_initialized"))

    dense_vec = None
    embed_started = time.perf_counter()
    if not query_pool_ready:
        embedder.ensure_model_pool_warmup_started()
        logger.info(
            "Skipping dense query embedding for %r while query model pool warms",
            query,
        )
    else:
        try:
            dense_vec, _ = await asyncio.wait_for(
                embedder.embed_query_async(parsed.semantic_text),
                timeout=QUERY_EMBED_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "Dense query embedding timed out for %r after %.1fs; falling back to lexical search",
                query,
                QUERY_EMBED_TIMEOUT_SECONDS,
            )
            embedder.ensure_model_pool_warmup_started()
        except Exception as exc:
            logger.warning("Dense query embedding failed for %r: %s", query, exc)
            embedder.ensure_model_pool_warmup_started()
    embed_elapsed_ms = (time.perf_counter() - embed_started) * 1000.0

    conn = database.get_connection()
    try:
        candidate_started = time.perf_counter()
        case_item_ids = (
            database.get_case_item_ids(conn, case_id) if case_id is not None else None
        )

        def _case_allows(item_id: str) -> bool:
            return case_item_ids is None or item_id in case_item_ids

        faiss_rows = conn.execute(
            """
            SELECT faiss_id, item_id, embedding_type
            FROM embeddings
            WHERE embedding_type IN ('dense', 'summary')
              AND faiss_id IS NOT NULL
            """
        ).fetchall()
        faiss_id_map = {
            int(row["faiss_id"]): (
                str(row["item_id"]),
                str(row["embedding_type"] or "dense"),
            )
            for row in faiss_rows
        }

        text_dense_candidates: dict[str, float] = {}
        if dense_vec is not None:
            try:
                # Each item can be represented by multiple FAISS rows (dense
                # chunks + summary). Searching k=40 often returned only ~15-20
                # unique item_ids because duplicates absorbed slots; overfetch
                # to k=80 and dedup so the dense pool is ~40 distinct items.
                dense_k = max(80, len(case_item_ids) * 2) if case_item_ids is not None else 80
                faiss_ids, dense_scores = embedder.get_text_index().search(
                    dense_vec, k=dense_k
                )
                for faiss_id, score in zip(faiss_ids, dense_scores):
                    faiss_row = faiss_id_map.get(faiss_id)
                    if not faiss_row:
                        continue
                    item_id, embedding_type = faiss_row
                    if not _case_allows(item_id):
                        continue
                    normalized_score = _clamp_score(score)
                    if embedding_type == "summary":
                        normalized_score *= embedder.SUMMARY_EMBED_WEIGHT
                        # Re-clamp as a defensive measure: SUMMARY_EMBED_WEIGHT
                        # is <=1.0 today but we do not want a future tuning
                        # change to leak scores >1.0 into the rest of the pipeline.
                        normalized_score = _clamp_score(normalized_score)
                    if (
                        item_id not in text_dense_candidates
                        or normalized_score > text_dense_candidates[item_id]
                    ):
                        text_dense_candidates[item_id] = normalized_score
            except Exception as exc:
                logger.warning("Dense FAISS search failed for %r: %s", query, exc)

        fts_candidates: dict[str, float] = {}
        fts_source_texts = [parsed.exact_text, parsed.semantic_text]
        if parsed.time_window is None:
            fts_source_texts.append(parsed.original)
        fts_queries = _fts_token_query(
            *fts_source_texts,
            require_all=parsed.query_style == "identifier",
        )
        for fts_query in fts_queries:
            try:
                fts_results = database.search_fts(
                    conn,
                    fts_query,
                    limit=20,
                    case_id=case_id,
                )
            except sqlite3.Error as exc:
                logger.warning("FTS search failed for %r: %s", fts_query, exc)
                continue
            for row in fts_results:
                item_id = row["id"]
                score = _clamp_score(row.get("bm25_score", 0.0))
                if item_id not in fts_candidates or score > fts_candidates[item_id]:
                    fts_candidates[item_id] = score

        path_candidates: dict[str, float] = {}
        path_tokens = sorted(_path_query_tokens(parsed))[:8]
        if path_tokens and route["pipeline"] != "fast":
            where_clause = " OR ".join("lower(original_path) LIKE ?" for _ in path_tokens)
            like_values = [f"%{token}%" for token in path_tokens]
            try:
                case_scope = ""
                path_params: list[Any] = list(like_values)
                if case_id is not None:
                    case_scope = """
                      AND EXISTS (
                          SELECT 1 FROM case_evidence
                          WHERE case_evidence.case_id = ?
                            AND case_evidence.item_id = items.id
                      )
                    """
                    path_params.append(case_id)
                rows = conn.execute(
                    f"""
                    SELECT *
                    FROM items
                    WHERE is_excluded = 0
                      AND ({where_clause})
                      {case_scope}
                    LIMIT 80
                    """,
                    path_params,
                ).fetchall()
            except sqlite3.Error as exc:
                logger.warning("Path search failed for %r: %s", query, exc)
            else:
                for row in rows:
                    item = dict(row)
                    score = _clamp_score(_path_score(item, parsed))
                    if score <= 0.0:
                        continue
                    item_id = item["id"]
                    if item_id not in path_candidates or score > path_candidates[item_id]:
                        path_candidates[item_id] = score

        time_candidates: set[str] = set()
        if (
            parsed.time_window
            and parsed.time_window.start_date
            and parsed.time_window.end_date
            and route["pipeline"] != "fast"
        ):
            try:
                time_scope = ""
                time_params: list[Any] = [
                    parsed.time_window.start_date.isoformat(),
                    parsed.time_window.end_date.isoformat(),
                    parsed.time_window.start_date.isoformat(),
                    parsed.time_window.end_date.isoformat(),
                    parsed.time_window.start_date.isoformat(),
                    parsed.time_window.end_date.isoformat(),
                ]
                if case_id is not None:
                    time_scope = """
                      AND EXISTS (
                          SELECT 1 FROM case_evidence
                          WHERE case_evidence.case_id = ?
                            AND case_evidence.item_id = items.id
                      )
                    """
                    time_params.append(case_id)
                rows = conn.execute(
                    f"""
                    SELECT id
                    FROM items
                    WHERE is_excluded = 0
                      AND (
                        (first_seen_at IS NOT NULL AND first_seen_at >= ? AND first_seen_at <= ?)
                        OR (created_at IS NOT NULL AND created_at >= ? AND created_at <= ?)
                        OR (modified_at IS NOT NULL AND modified_at >= ? AND modified_at <= ?)
                      )
                      {time_scope}
                    ORDER BY COALESCE(modified_at, created_at, first_seen_at) DESC
                    LIMIT 160
                    """,
                    time_params,
                ).fetchall()
            except sqlite3.Error as exc:
                logger.warning("Time-window candidate search failed for %r: %s", query, exc)
            else:
                time_candidates = {str(row["id"]) for row in rows}

        graph_candidates: dict[str, float] = {}
        graph_paths: dict[str, list[str]] = {}
        if (
            USE_GRAPH_CANDIDATES
            and route["use_graph"]
            and float(intent_profile.get("graph", 0.0) or 0.0) > 0.09
        ):
            try:
                try:
                    from ..graph.candidates import get_generator
                except ImportError:
                    from graph.candidates import get_generator

                for candidate in get_generator().generate(query, parsed=parsed, n=max(limit * 4, 20)):
                    item_id = str(candidate.get("episode_id") or "")
                    if not item_id or not _case_allows(item_id):
                        continue
                    graph_candidates[item_id] = max(
                        graph_candidates.get(item_id, 0.0),
                        _clamp_score(float(candidate.get("graph_score") or 0.0)),
                    )
                    graph_paths[item_id] = list(candidate.get("graph_path") or [])[:10]
            except Exception:
                logger.exception("Graph candidate generation failed for query=%r", query)

        # Exact identifiers need a lexical/path/graph anchor. A dense model
        # always returns nearest neighbours, even for an invented marker, and
        # surfacing those neighbours is worse than an honest empty result.
        if (
            parsed.query_style == "identifier"
            and not fts_candidates
            and not path_candidates
            and not time_candidates
            and not graph_candidates
        ):
            if return_debug:
                return _empty_debug(
                    candidate_pool_sizes={
                        "dense": len(text_dense_candidates),
                        "fts": 0,
                        "path": 0,
                        "time": 0,
                        "graph": 0,
                        "recent": 0,
                        "combined": len(text_dense_candidates),
                    }
                )
            return []

        recent_ids: set[str] = set()
        if parsed.time_window is not None:
            recent = database.get_recent_items(conn, days=14, limit=20, case_id=case_id)
            recent_ids = {row["id"] for row in recent}

        all_item_ids = (
            set(text_dense_candidates)
            | set(fts_candidates)
            | set(path_candidates)
            | set(time_candidates)
            | set(graph_candidates)
            | set(recent_ids)
        )
        if not all_item_ids:
            if return_debug:
                return _empty_debug()
            return []

        placeholders = ", ".join("?" for _ in all_item_ids)
        rows = conn.execute(
            f"""
            SELECT items.*, sparse.sparse_weights AS sparse_weights
            FROM items
            LEFT JOIN embeddings AS sparse
              ON sparse.item_id = items.id
             AND sparse.embedding_type = 'sparse_json'
            WHERE items.id IN ({placeholders})
              AND items.is_excluded = 0
              AND (
                  ? IS NULL
                  OR EXISTS (
                      SELECT 1 FROM case_evidence
                      WHERE case_evidence.case_id = ?
                        AND case_evidence.item_id = items.id
                  )
              )
            """,
            [*list(all_item_ids), case_id, case_id],
        ).fetchall()
        candidate_items = {row["id"]: dict(row) for row in rows}
        provenance = database.get_item_provenance(conn, candidate_items)
        for item_id, item in candidate_items.items():
            item.update(provenance.get(item_id, {}))
        # Pre-parse sparse_weights JSON once per candidate. The scoring loop
        # previously ran json.loads() on every iteration inside
        # compute_sparse_score, which for ~500-item result sets was the single
        # largest Python-time cost in the hot path.
        for _cand_id, _cand in candidate_items.items():
            _cand["sparse_weights"] = embedder.parse_sparse_weights(
                _cand.get("sparse_weights")
            )
        if not candidate_items:
            if return_debug:
                return _empty_debug(
                    candidate_pool_sizes={
                        "dense": len(text_dense_candidates),
                        "fts": len(fts_candidates),
                        "path": len(path_candidates),
                        "time": len(time_candidates),
                        "graph": len(graph_candidates),
                        "recent": len(recent_ids),
                        "combined": len(all_item_ids),
                    }
                )
            return []

        filtered_items: dict[str, dict[str, Any]] = {}
        for item_id, item in candidate_items.items():
            if parsed.handler_type and item.get("handler_type") != parsed.handler_type:
                continue
            if parsed.file_type and parsed.file_type_strict and item.get("file_type") != parsed.file_type:
                if not _has_strong_lexical_file_type_match(
                    item,
                    parsed,
                    fts_score=fts_candidates.get(item_id, 0.0),
                ):
                    continue
            filtered_items[item_id] = item

        if not filtered_items:
            if return_debug:
                return _empty_debug(
                    candidate_pool_sizes={
                        "dense": len(text_dense_candidates),
                        "fts": len(fts_candidates),
                        "path": len(path_candidates),
                        "time": len(time_candidates),
                        "graph": len(graph_candidates),
                        "recent": len(recent_ids),
                        "combined": len(all_item_ids),
                    }
                )
            return []
        candidate_elapsed_ms = (time.perf_counter() - candidate_started) * 1000.0

        all_items = filtered_items
        time_priority_ids: set[str] = set()
        strong_time_soft_order = False
        topic_tokens = _topic_query_tokens(parsed)

        if parsed.time_window and parsed.time_window.start_date:
            tw = parsed.time_window

            def item_in_window(item: dict[str, Any]) -> bool:
                timestamps_to_check = [
                    item.get("first_seen_at"),
                    item.get("created_at"),
                    item.get("modified_at"),
                ]
                for ts_str in timestamps_to_check:
                    if not ts_str:
                        continue
                    try:
                        timestamp = parse_iso(ts_str)
                        if timestamp is None:
                            continue
                        if tw.start_date <= timestamp <= tw.end_date:
                            return True
                    except Exception:
                        continue
                return False

            in_window = {
                item_id: item
                for item_id, item in all_items.items()
                if item_in_window(item)
            }

            logger.info(
                "Time filter '%s': %s/%s items in window (%s to %s)",
                tw.description,
                len(in_window),
                len(all_items),
                tw.start_date.date(),
                tw.end_date.date(),
            )

            if parsed.time_confidence == "strong":
                if topic_tokens and len(in_window) > 0:
                    time_priority_ids = set(in_window)
                    strong_time_soft_order = True
                elif len(in_window) >= MIN_TIME_FILTER_RESULTS:
                    all_items = in_window
                    time_priority_ids = set(in_window)
                elif len(in_window) > 0:
                    time_priority_ids = set(in_window)
                    strong_time_soft_order = True
            elif len(in_window) > 0:
                time_priority_ids = set(in_window)
            else:
                logger.warning(
                    "Time filter found 0 results for '%s'",
                    tw.description,
                )

        scoring_started = time.perf_counter()
        weights = _apply_pipeline_to_weights(
            _weights_for_query(parsed),
            use_graph=bool(route["use_graph"]),
        )
        # Compute query-side sparse-scoring inputs once. They are independent
        # of the candidate being scored; recomputing per-candidate wasted CPU
        # on string lowercasing and list construction for every result.
        sparse_query_tokens_cached = _sparse_query_tokens(parsed)
        sparse_query_tokens_normalized = list(
            dict.fromkeys(
                token.lower() for token in sparse_query_tokens_cached if token
            )
        )
        has_visual_lexical_anchor = False
        if parsed.file_type == "image" or parsed.handler_type == "visual":
            for item_id, item in all_items.items():
                if item.get("handler_type") != "visual":
                    continue
                anchor_evidence = max(
                    _clamp_score(fts_candidates.get(item_id, 0.0)),
                    _clamp_score(_filename_score(item, parsed)),
                    _clamp_score(_content_score(item, parsed)),
                    _clamp_score(_path_score(item, parsed)),
                )
                if anchor_evidence >= 0.45:
                    has_visual_lexical_anchor = True
                    break

        results: list[SearchResult] = []

        for item_id, item in all_items.items():
            dense_score = _clamp_score(text_dense_candidates.get(item_id, 0.0))
            fts_score = _clamp_score(fts_candidates.get(item_id, 0.0))
            sparse_score = _clamp_score(
                embedder.compute_sparse_score(
                    sparse_query_tokens_cached,
                    item.get("sparse_weights"),
                    normalized_query_tokens=sparse_query_tokens_normalized,
                )
            )
            time_score = _clamp_score(_time_fit(item, parsed.time_window))
            metadata_score = _clamp_score(_metadata_match(item, parsed))
            filename_score = _clamp_score(_filename_score(item, parsed))
            content_score = _clamp_score(_content_score(item, parsed))
            graph_score = _clamp_score(graph_candidates.get(item_id, 0.0))
            path_score = max(
                _clamp_score(path_candidates.get(item_id, 0.0)),
                _clamp_score(_path_score(item, parsed)),
            )
            visual_score = (
                _clamp_score(max(content_score, fts_score))
                if item.get("handler_type") == "visual"
                else 0.0
            )
            behavior_score = _clamp_score(_behavior(item))
            recency_score = _clamp_score(_recency(item))
            time_only_match = _is_time_only_match(
                parsed,
                dense_score=dense_score,
                fts_score=fts_score,
                filename_score=filename_score,
                content_score=content_score,
                path_score=path_score,
            )
            weighted_time_score = (
                min(time_score, 0.05)
                if parsed.time_confidence == "strong" and time_only_match
                else time_score
            )

            final_score = _final_weighted_score(
                weights,
                dense_score=dense_score,
                sparse_score=sparse_score,
                fts_score=fts_score,
                filename_score=filename_score,
                time_score=weighted_time_score,
                metadata_score=metadata_score,
                behavior_score=behavior_score,
                recency_score=recency_score,
                graph_score=graph_score,
            )

            if item_id in recent_ids:
                final_score = min(1.0, final_score * 1.1)

            if parsed.time_confidence == "strong" and time_priority_ids:
                if item_id in time_priority_ids:
                    final_score = min(1.0, final_score * 1.25)
                elif strong_time_soft_order:
                    final_score *= 0.55
            elif parsed.time_confidence == "weak" and time_priority_ids:
                if item_id in time_priority_ids:
                    final_score = min(1.0, final_score * 1.15)
                elif len(time_priority_ids) >= MIN_TIME_FILTER_RESULTS:
                    final_score *= 0.92

            exact_bonus = _exact_match_bonus(item, parsed)
            if parsed.query_style == "identifier":
                # Matched-cap values so identifier queries with strong filename
                # + FTS evidence still get a clear edge, but do not fully
                # saturate final_score to 1.0 (which would destroy tie-breaking
                # between two equally strong filename hits).
                if filename_score >= 0.5 and fts_score >= 0.95:
                    exact_bonus = max(exact_bonus, 1.32)
                elif filename_score >= 0.5 and fts_score >= 0.80:
                    exact_bonus = max(exact_bonus, 1.22)

            final_score = min(1.0, final_score * exact_bonus)
            if filename_score >= 0.95:
                final_score = min(
                    1.0,
                    final_score + (0.10 if parsed.query_style == "identifier" else 0.06),
                )
            elif parsed.query_style == "identifier" and filename_score >= 0.80:
                final_score = min(1.0, final_score + 0.06)
            final_score = apply_type_preference(
                final_score,
                item.get("file_type"),
                parsed.file_type,
                parsed.file_type_strict,
            )
            final_score = min(1.0, final_score * _project_workspace_bias(item, parsed))
            if parsed.time_confidence == "strong" and time_only_match:
                # For queries like "what was I working on in February for Recurdo",
                # the date should narrow relevant hits, not let random February files win.
                final_score *= 0.18
            final_score = min(
                1.0,
                final_score
                + _ranking_evidence_bonus(
                    item,
                    content_score=content_score,
                    path_score=path_score,
                    fts_score=fts_score,
                    recency_score=recency_score,
                ),
            )
            # Keep source/noise guardrails last so late evidence bonuses cannot
            # resurrect Gmail imports, generated logs, or unrelated coursework.
            final_score *= _source_type_bias(item, parsed)
            final_score *= _generated_artifact_bias(item, parsed)
            final_score *= _resume_noise_bias(item, parsed)
            final_score *= _academic_coursework_bias(item, parsed)
            final_score *= _academic_unit_lookup_bias(item, parsed)
            final_score *= _project_meta_doc_bias(item, parsed)
            final_score *= _visual_asset_bias(item, parsed)
            final_score *= _dense_only_visual_false_positive_bias(
                item,
                parsed,
                dense_score=dense_score,
                fts_score=fts_score,
                filename_score=filename_score,
                content_score=content_score,
                path_score=path_score,
                has_visual_lexical_anchor=has_visual_lexical_anchor,
            )

            results.append(
                SearchResult(
                    item_id=item_id,
                    item=item,
                    dense_score=dense_score,
                    sparse_score=sparse_score,
                    visual_score=visual_score,
                    fts_score=fts_score,
                    filename_score=filename_score,
                    content_score=content_score,
                    path_score=path_score,
                    time_score=time_score,
                    metadata_score=metadata_score,
                    behavior_score=behavior_score,
                    recency_score=recency_score,
                    graph_score=graph_score,
                    final_score=_clamp_score(final_score),
                    intent_class=parsed.intent_class,
                    source_pool=(
                        "graph"
                        if item_id in graph_candidates
                        and item_id not in text_dense_candidates
                        and item_id not in fts_candidates
                        and item_id not in path_candidates
                        else (
                            "hybrid"
                            if item_id in graph_candidates
                            else "base"
                        )
                    ),
                    graph_path=graph_paths.get(item_id, []),
                    complexity_score=float(route["complexity_score"]),
                    pipeline=str(route["pipeline"]),
                    use_graph=bool(route["use_graph"]),
                    use_temporal_subgraph=bool(route["use_temporal_subgraph"]),
                    matched_fields=_matched_fields(
                        item_id=item_id,
                        parsed=parsed,
                        dense_score=dense_score,
                        sparse_score=sparse_score,
                        visual_score=visual_score,
                        fts_score=fts_score,
                        filename_score=filename_score,
                        content_score=content_score,
                        path_score=path_score,
                        time_score=time_score,
                        metadata_score=metadata_score,
                        behavior_score=behavior_score,
                        recency_score=recency_score,
                        recent_ids=recent_ids,
                    ),
                )
            )

        results.sort(key=lambda result: result.final_score, reverse=True)
        results = _apply_type_tie_break(results, parsed)

        if parsed.prefer_latest and results:
            semantic_head = sorted(
                results[:10],
                key=lambda result: (
                    result.dense_score
                    + result.fts_score
                    + result.filename_score
                    + result.content_score
                    + result.path_score
                ),
                reverse=True,
            )[:5]
            semantic_head = [
                result
                for result in semantic_head
                if (
                    result.dense_score
                    + result.fts_score
                    + result.filename_score
                    + result.content_score
                    + result.path_score
                )
                >= 1.0
                or max(
                    result.dense_score,
                    result.fts_score,
                    result.filename_score,
                    result.content_score,
                    result.path_score,
                )
                >= 0.45
            ]
            if semantic_head:
                freshest = max(
                    semantic_head,
                    key=lambda result: (
                        _freshness_timestamp(result.item) or datetime.min.replace(tzinfo=timezone.utc),
                        result.final_score,
                    ),
                )
                results = [freshest] + [
                    result for result in results if result.item_id != freshest.item_id
                ]
        scoring_elapsed_ms = (time.perf_counter() - scoring_started) * 1000.0

        cognitive_start = datetime.now(timezone.utc)
        cognitive_elapsed_ms = 0.0
        if route["pipeline"] != "fast":
            try:
                try:
                    from ..cognition.reranker import rerank_results
                except ImportError:
                    from cognition.reranker import rerank_results

                results = await asyncio.to_thread(rerank_results, results, query)
                elapsed_ms = (datetime.now(timezone.utc) - cognitive_start).total_seconds() * 1000
                cognitive_elapsed_ms = elapsed_ms
                logger.info(
                    "Cognitive rerank added %.1fms for query=%r results=%s",
                    elapsed_ms,
                    query,
                    len(results),
                )
            except Exception:
                logger.exception("Cognitive rerank failed for query=%r", query)
        final_results = results[:limit]

        if not return_debug:
            return final_results

        latency_ms = {
            "total": round((time.perf_counter() - total_started) * 1000.0, 2),
            "parse": round(parse_elapsed_ms, 2),
            "embedding": round(embed_elapsed_ms, 2),
            "candidate_generation": round(candidate_elapsed_ms, 2),
            "scoring": round(scoring_elapsed_ms, 2),
            "cognitive_rerank": round(cognitive_elapsed_ms, 2),
        }
        seed_entities_in_graph = sorted(
            {
                str(path[0]).strip()
                for path in graph_paths.values()
                if isinstance(path, list) and path and str(path[0]).strip()
            }
        )[:10]
        pool_breakdown = {
            "dense": len(text_dense_candidates),
            "fts": len(fts_candidates),
            "path": len(path_candidates),
            "time": len(time_candidates),
            "graph": len(graph_candidates),
            "recent": len(recent_ids),
            "combined": len(all_item_ids),
        }
        ranked = []
        for result in final_results[:10]:
            ranked.append(
                {
                    "id": result.item_id,
                    "title": str(
                        result.item.get("title")
                        or result.item.get("filename")
                        or result.item_id
                    ),
                    "source_pool": result.source_pool,
                    "graph_path": list(result.graph_path or []),
                    "scoring": {
                        "vector": round(float(result.dense_score or 0.0), 3),
                        "fts": round(float(result.fts_score or 0.0), 3),
                        "graph": round(float(result.graph_score or 0.0), 3),
                        "time": round(float(result.time_score or 0.0), 3),
                        "behavior": round(float(result.behavior_score or 0.0), 3),
                        "final": round(float(result.final_score or 0.0), 3),
                    },
                }
            )
        debug = {
            "query": query,
            "intent_class": parsed.intent_class,
            "pipeline": route["pipeline"],
            "complexity_score": float(route["complexity_score"]),
            "use_graph": bool(route["use_graph"]),
            "use_temporal_subgraph": bool(route["use_temporal_subgraph"]),
            "seed_entities_in_graph": seed_entities_in_graph,
            "candidate_pool_sizes": pool_breakdown,
            "latency_ms": latency_ms,
            "ranked_results": ranked,
        }
        return final_results, debug
    finally:
        conn.close()
