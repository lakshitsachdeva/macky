from __future__ import annotations

import asyncio
import base64
import hashlib
import html
from importlib.util import find_spec
import json
import logging
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from email.header import decode_header, make_header
from email.utils import getaddresses, parseaddr
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, Callable
from urllib.parse import quote
import uuid

try:
    from .. import config, database
    from ..cognition.store import CognitiveStore
    from ..graph.store import GraphStore, get_store as get_graph_store
    from ..indexer import embedder, enricher, extractor
    from ..llm.extraction import apply_item_knowledge, extract_item_knowledge
except ImportError:
    import config
    import database
    from cognition.store import CognitiveStore
    from graph.store import GraphStore, get_store as get_graph_store
    from indexer import embedder, enricher, extractor
    from llm.extraction import apply_item_knowledge, extract_item_knowledge


logger = logging.getLogger(__name__)

GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_SYNC_QUERY = "-in:chats -category:promotions -category:social -category:updates -category:forums"
GMAIL_DEFAULT_BOOTSTRAP_DAYS = 30
GMAIL_ALLOWED_HISTORICAL_WINDOWS = (30, 90, 180)
GMAIL_SOURCE_TYPES = ("gmail", "gmail_attachment")
GMAIL_ATTACHMENT_MAX_BYTES = 5 * 1024 * 1024
GMAIL_ATTACHMENTS_PER_MESSAGE_LIMIT = 5
GMAIL_ATTACHMENT_SUPPORTED_SUFFIXES = frozenset(config.SUPPORTED_TEXT_EXTENSIONS)
GMAIL_EXCLUDED_LABELS = {
    "SPAM",
    "TRASH",
    "DRAFT",
    "CATEGORY_PROMOTIONS",
    "CATEGORY_SOCIAL",
    "CATEGORY_UPDATES",
    "CATEGORY_FORUMS",
}
GMAIL_ALLOWED_IMPORT_LABELS = {
    "INBOX",
    "SENT",
}
GMAIL_PRIORITY_LABELS = {
    "IMPORTANT",
    "STARRED",
    "CATEGORY_PERSONAL",
}
CLIENT_SECRETS_PATH = config.DATA_DIR / "client_secrets.json"
TOKEN_PATH = config.GMAIL_TOKENS_DIR / "gmail_token.json"
SYNC_STATE_PATH = config.GMAIL_TOKENS_DIR / "sync_state.json"


class GmailConnectorError(RuntimeError):
    pass


class GmailHistoryExpiredError(GmailConnectorError):
    pass


class GmailSyncCancelledError(GmailConnectorError):
    pass


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _has_oauth_flow() -> bool:
    return find_spec("google_auth_oauthlib.flow") is not None


def _read_json(path: Path, *, default: dict[str, Any] | None = None) -> dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise GmailConnectorError(f"Unable to read {path.name}: {exc}") from exc
    if not isinstance(payload, dict):
        raise GmailConnectorError(f"Invalid JSON payload in {path}")
    return payload


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _decode_header_value(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        return str(make_header(decode_header(text))).strip()
    except Exception:
        return text


def _safe_filename(value: str, *, default: str) -> str:
    cleaned = re.sub(r"[^\w .-]+", " ", str(value or "")).strip()
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:96] if cleaned else default


def _decode_base64url(value: str | None) -> str:
    raw = _decode_base64url_bytes(value)
    if not raw:
        return ""
    for encoding in ("utf-8", "latin-1"):
        try:
            return raw.decode(encoding, errors="ignore")
        except Exception:
            continue
    return ""


def _decode_base64url_bytes(value: str | None) -> bytes:
    encoded = str(value or "").strip()
    if not encoded:
        return b""
    padding = "=" * (-len(encoded) % 4)
    try:
        return base64.urlsafe_b64decode(encoded + padding)
    except Exception:
        return b""


def _strip_html(html_text: str) -> str:
    if not html_text:
        return ""
    text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", html_text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    text = html.unescape(text)
    return re.sub(r"\n{3,}", "\n\n", re.sub(r"[ \t]+", " ", text)).strip()


def _extract_body_from_payload(payload: dict[str, Any] | None) -> tuple[str, str]:
    if not payload:
        return "", ""

    plain_parts: list[str] = []
    html_parts: list[str] = []

    def walk(part: dict[str, Any]) -> None:
        mime_type = str(part.get("mimeType") or "").casefold()
        body_data = (part.get("body") or {}).get("data")
        if body_data:
            decoded = _decode_base64url(body_data)
            if mime_type == "text/plain":
                plain_parts.append(decoded)
            elif mime_type == "text/html":
                html_parts.append(decoded)
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(payload)

    plain_text = "\n\n".join(part.strip() for part in plain_parts if part.strip()).strip()
    html_text = "\n\n".join(part.strip() for part in html_parts if part.strip()).strip()
    if not plain_text and (payload.get("body") or {}).get("data"):
        plain_text = _decode_base64url((payload.get("body") or {}).get("data"))
    return plain_text.strip(), _strip_html(html_text)


def _extract_attachment_names(payload: dict[str, Any] | None) -> list[str]:
    return [str(ref.get("filename") or "").strip() for ref in _extract_attachment_refs(payload)]


def _extract_attachment_refs(payload: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not payload:
        return []

    refs: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(part: dict[str, Any]) -> None:
        filename = _decode_header_value(part.get("filename"))
        body = dict(part.get("body") or {})
        attachment_id = str(body.get("attachmentId") or "").strip()
        inline_data = str(body.get("data") or "").strip()
        size = int(body.get("size") or 0)
        if filename or attachment_id:
            normalized = f"{filename or ''}:{attachment_id or inline_data[:32]}".casefold()
            if normalized not in seen:
                seen.add(normalized)
                refs.append(
                    {
                        "filename": filename or f"attachment-{len(refs) + 1}",
                        "attachment_id": attachment_id,
                        "mime_type": str(part.get("mimeType") or "").strip(),
                        "size": size,
                        "data": inline_data,
                    }
                )
        for child in part.get("parts") or []:
            if isinstance(child, dict):
                walk(child)

    walk(payload)
    return refs


def _normalize_historical_days(days: int | None) -> int | None:
    if days is None:
        return None
    value = max(0, int(days))
    if value == 0:
        return None
    if value not in GMAIL_ALLOWED_HISTORICAL_WINDOWS:
        allowed = ", ".join(str(entry) for entry in GMAIL_ALLOWED_HISTORICAL_WINDOWS)
        raise GmailConnectorError(f"historical_days must be one of: {allowed}")
    return value


def _build_sync_query(*, historical_days: int | None = None, require_attachments: bool = False) -> str:
    terms = [GMAIL_SYNC_QUERY]
    if historical_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=int(historical_days))).strftime("%Y/%m/%d")
        terms.append(f"after:{cutoff}")
    if require_attachments:
        terms.append("has:attachment")
    return " ".join(term for term in terms if term)


def _message_url(message_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{quote(message_id)}"


def _attachment_url(message_id: str, attachment_id: str, filename: str) -> str:
    token = quote(attachment_id or filename or "attachment")
    return f"https://mail.google.com/mail/u/0/?recurdo_attachment={token}#all/{quote(message_id)}"


def _join_addresses(addresses: list[tuple[str, str]]) -> list[str]:
    formatted: list[str] = []
    seen: set[str] = set()
    for name, email_address in addresses:
        decoded_name = _decode_header_value(name)
        email_value = str(email_address or "").strip()
        label = decoded_name or email_value
        normalized = email_value.casefold() or label.casefold()
        if not label or normalized in seen:
            continue
        seen.add(normalized)
        formatted.append(label)
    return formatted


def _message_label_ids(message: dict[str, Any]) -> set[str]:
    return {
        str(label).strip().upper()
        for label in (message.get("labelIds") or [])
        if str(label or "").strip()
    }


def _has_bulk_mail_headers(headers: dict[str, str]) -> bool:
    precedence = str(headers.get("precedence") or "").strip().casefold()
    auto_submitted = str(headers.get("auto-submitted") or "").strip().casefold()
    return bool(
        headers.get("list-id")
        or headers.get("list-unsubscribe")
        or precedence in {"bulk", "list", "junk"}
        or auto_submitted not in {"", "no"}
    )


def _should_import_message(
    message: dict[str, Any],
    *,
    headers: dict[str, str],
    attachment_names: list[str] | None = None,
    require_attachments: bool = False,
) -> tuple[bool, str | None]:
    label_ids = _message_label_ids(message)
    blocked_labels = sorted(label_ids & GMAIL_EXCLUDED_LABELS)
    if blocked_labels:
        return False, f"excluded labels: {', '.join(blocked_labels)}"

    if label_ids and not (label_ids & (GMAIL_ALLOWED_IMPORT_LABELS | GMAIL_PRIORITY_LABELS)):
        return False, "message is neither inbox, sent, nor priority mail"

    if _has_bulk_mail_headers(headers) and not (label_ids & GMAIL_PRIORITY_LABELS):
        return False, "bulk or mailing-list message"

    if require_attachments and not (attachment_names or []):
        return False, "message has no attachments"

    return True, None


def parse_gmail_message(
    message: dict[str, Any],
    *,
    account_email: str,
    attachment_names: list[str] | None = None,
) -> dict[str, Any]:
    payload = dict(message.get("payload") or {})
    headers = {
        str(header.get("name") or "").casefold(): _decode_header_value(header.get("value"))
        for header in payload.get("headers") or []
        if isinstance(header, dict)
    }
    resolved_attachment_names = list(attachment_names or _extract_attachment_names(payload))

    sender_name, sender_email = parseaddr(headers.get("from", ""))
    sender_name = _decode_header_value(sender_name)
    sender_email = str(sender_email or "").strip().casefold()
    to_addresses = getaddresses([headers.get("to", ""), headers.get("cc", ""), headers.get("bcc", "")])
    recipient_labels = _join_addresses(to_addresses)
    plain_body, html_body = _extract_body_from_payload(payload)
    body_text = plain_body or html_body

    subject = _decode_header_value(headers.get("subject")) or "(no subject)"
    snippet = str(message.get("snippet") or "").strip()
    message_id = str(message.get("id") or "").strip()
    thread_id = str(message.get("threadId") or "").strip()
    history_id = str(message.get("historyId") or "").strip()
    internal_date_ms = int(message.get("internalDate") or 0)
    occurred_at = datetime.fromtimestamp(
        internal_date_ms / 1000 if internal_date_ms else datetime.now(timezone.utc).timestamp(),
        tz=timezone.utc,
    ).isoformat()

    extracted_text = "\n".join(
        part
        for part in [
            f"Subject: {subject}",
            f"From: {headers.get('from', '')}".strip(),
            f"To: {headers.get('to', '')}".strip(),
            f"Cc: {headers.get('cc', '')}".strip() if headers.get("cc") else "",
            f"Date: {headers.get('date', '')}".strip() if headers.get("date") else "",
            (
                f"Attachments: {', '.join(resolved_attachment_names[:10])}"
                if resolved_attachment_names
                else ""
            ),
            "",
            body_text.strip(),
        ]
        if part is not None
    ).strip()

    keywords = [
        "email",
        "gmail",
        subject,
        sender_name or sender_email,
        *(recipient_labels[:6]),
        *(resolved_attachment_names[:6]),
        "has_attachment" if resolved_attachment_names else "",
    ]

    return {
        "message_id": message_id,
        "thread_id": thread_id,
        "history_id": history_id,
        "account_email": str(account_email or "").strip().casefold(),
        "label_ids": sorted(_message_label_ids(message)),
        "subject": subject,
        "sender_name": sender_name or sender_email,
        "sender_email": sender_email,
        "recipient_labels": recipient_labels[:10],
        "attachment_names": resolved_attachment_names[:12],
        "attachment_count": len(resolved_attachment_names),
        "snippet": snippet,
        "body_text": body_text.strip(),
        "extracted_text": extracted_text,
        "preview_text": (snippet or body_text or subject)[:200],
        "summary": snippet or (body_text[:180] if body_text else subject),
        "created_at": occurred_at,
        "modified_at": occurred_at,
        "first_seen_at": occurred_at,
        "filename": f"Email - {_safe_filename(subject, default=message_id or 'message')}.eml",
        "title": subject,
        "original_path": _message_url(message_id),
        "keywords": [entry for entry in keywords if str(entry or "").strip()],
    }


def _merge_keywords(*groups: list[str], limit: int = 30) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for group in groups:
        for value in group:
            keyword = " ".join(str(value or "").split()).strip()
            if not keyword:
                continue
            normalized = keyword.casefold()
            if normalized in seen:
                continue
            seen.add(normalized)
            merged.append(keyword)
            if len(merged) >= limit:
                return merged
    return merged


def _keyword_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(entry).strip() for entry in value if str(entry).strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        payload = json.loads(text)
    except Exception:
        return [text]
    if isinstance(payload, list):
        return [str(entry).strip() for entry in payload if str(entry).strip()]
    return [text]


def _open_memory_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000;")
    return conn


def _count_gmail_items(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        SELECT COUNT(*) AS count
        FROM items
        WHERE COALESCE(is_excluded, 0) = 0
          AND source_type IN ('gmail', 'gmail_attachment')
        """
    ).fetchone()
    return int(row["count"] or 0) if row is not None else 0


class GmailClient:
    def __init__(
        self,
        *,
        client_secrets_path: str | Path | None = None,
        token_path: str | Path | None = None,
        sync_state_path: str | Path | None = None,
        scopes: list[str] | None = None,
    ):
        self.client_secrets_path = Path(client_secrets_path or CLIENT_SECRETS_PATH).expanduser()
        self.token_path = Path(token_path or TOKEN_PATH).expanduser()
        self.sync_state_path = Path(sync_state_path or SYNC_STATE_PATH).expanduser()
        self.scopes = list(scopes or [GMAIL_READONLY_SCOPE])
        self.token_path.parent.mkdir(parents=True, exist_ok=True)
        self.sync_state_path.parent.mkdir(parents=True, exist_ok=True)

    @property
    def oauth_available(self) -> bool:
        return _has_oauth_flow()

    def client_configured(self) -> bool:
        if not self.client_secrets_path.exists():
            return False
        try:
            payload = _read_json(self.client_secrets_path)
        except GmailConnectorError:
            return False
        return "installed" in payload

    def load_sync_state(self) -> dict[str, Any]:
        return _read_json(
            self.sync_state_path,
            default={
                "account_email": "",
                "last_history_id": "",
                "last_sync_at": None,
                "connected_at": None,
                "last_summary": {},
                "policy": {
                    "default_bootstrap_days": GMAIL_DEFAULT_BOOTSTRAP_DAYS,
                    "allowed_historical_days": list(GMAIL_ALLOWED_HISTORICAL_WINDOWS),
                    "attachments_only_supported": True,
                    "attachment_ingestion_supported": True,
                    "attachment_max_bytes": GMAIL_ATTACHMENT_MAX_BYTES,
                    "attachment_supported_suffixes": sorted(GMAIL_ATTACHMENT_SUPPORTED_SUFFIXES),
                    "allowed_labels": sorted(GMAIL_ALLOWED_IMPORT_LABELS | GMAIL_PRIORITY_LABELS),
                    "excluded_labels": sorted(GMAIL_EXCLUDED_LABELS),
                },
            },
        )

    def save_sync_state(self, updates: dict[str, Any]) -> dict[str, Any]:
        current = self.load_sync_state()
        current.update(updates)
        _write_json(self.sync_state_path, current)
        return current

    def clear_saved_state(self) -> None:
        self.token_path.unlink(missing_ok=True)
        self.sync_state_path.unlink(missing_ok=True)

    def load_credentials(self, *, refresh: bool = False) -> Any | None:
        if not self.token_path.exists():
            return None
        try:
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise GmailConnectorError("google-auth is not installed") from exc

        creds = Credentials.from_authorized_user_file(str(self.token_path), self.scopes)
        if refresh and creds and creds.expired and creds.refresh_token:
            try:
                from google.auth.transport.requests import Request
            except ImportError as exc:
                raise GmailConnectorError("google-auth transport support is not installed") from exc
            creds.refresh(Request())
            self.token_path.write_text(creds.to_json(), encoding="utf-8")
        return creds

    def authenticate(self, *, open_browser: bool = True) -> dict[str, Any]:
        if not self.client_configured():
            raise GmailConnectorError(
                f"Gmail client secrets not found at {self.client_secrets_path}"
            )
        if not self.oauth_available:
            raise GmailConnectorError(
                "google-auth-oauthlib is not installed in the active environment"
            )

        from google_auth_oauthlib.flow import InstalledAppFlow

        flow = InstalledAppFlow.from_client_secrets_file(
            str(self.client_secrets_path),
            self.scopes,
        )
        creds = flow.run_local_server(
            host="127.0.0.1",
            port=0,
            open_browser=open_browser,
            authorization_prompt_message="Recurdo is opening Gmail authentication in your browser.",
            success_message="Gmail is connected to Recurdo. You can close this tab now.",
        )
        self.token_path.write_text(creds.to_json(), encoding="utf-8")

        profile = self.get_profile()
        self.save_sync_state(
            {
                "account_email": str(profile.get("emailAddress") or "").strip().casefold(),
                "last_history_id": str(profile.get("historyId") or ""),
                "connected_at": _utc_now_iso(),
            }
        )
        return {
            "account_email": str(profile.get("emailAddress") or "").strip().casefold(),
            "history_id": str(profile.get("historyId") or ""),
        }

    def get_service(self, *, refresh: bool = True) -> Any:
        creds = self.load_credentials(refresh=refresh)
        if creds is None or not getattr(creds, "valid", False):
            raise GmailConnectorError("Gmail is not authenticated yet")

        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise GmailConnectorError("google-api-python-client is not installed") from exc

        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def get_profile(self, *, service: Any | None = None) -> dict[str, Any]:
        svc = service or self.get_service()
        return dict(svc.users().getProfile(userId="me").execute() or {})

    def status(self) -> dict[str, Any]:
        sync_state = self.load_sync_state()
        connected = False
        try:
            creds = self.load_credentials(refresh=False)
            connected = bool(creds and (getattr(creds, "valid", False) or getattr(creds, "refresh_token", None)))
        except Exception:
            connected = False

        return {
            "configured": self.client_configured(),
            "oauthlib_available": self.oauth_available,
            "connected": connected,
            "client_secrets_path": str(self.client_secrets_path),
            "token_path": str(self.token_path),
            "sync_state_path": str(self.sync_state_path),
            "account_email": str(sync_state.get("account_email") or "").strip().casefold(),
            "last_history_id": str(sync_state.get("last_history_id") or ""),
            "last_sync_at": sync_state.get("last_sync_at"),
            "connected_at": sync_state.get("connected_at"),
            "last_summary": dict(sync_state.get("last_summary") or {}),
            "policy": dict(sync_state.get("policy") or {})
            or {
                "default_bootstrap_days": GMAIL_DEFAULT_BOOTSTRAP_DAYS,
                "allowed_historical_days": list(GMAIL_ALLOWED_HISTORICAL_WINDOWS),
                "attachments_only_supported": True,
                "attachment_ingestion_supported": True,
                "attachment_max_bytes": GMAIL_ATTACHMENT_MAX_BYTES,
                "attachment_supported_suffixes": sorted(GMAIL_ATTACHMENT_SUPPORTED_SUFFIXES),
                "allowed_labels": sorted(GMAIL_ALLOWED_IMPORT_LABELS | GMAIL_PRIORITY_LABELS),
                "excluded_labels": sorted(GMAIL_EXCLUDED_LABELS),
            },
        }

    def iter_full_sync_message_ids(
        self,
        *,
        service: Any | None = None,
        query: str = GMAIL_SYNC_QUERY,
        max_messages: int | None = None,
    ) -> list[str]:
        svc = service or self.get_service()
        page_token: str | None = None
        message_ids: list[str] = []

        while True:
            max_results = 100
            if max_messages is not None:
                remaining = max(0, int(max_messages) - len(message_ids))
                if remaining <= 0:
                    break
                max_results = max(1, min(100, remaining))

            response = svc.users().messages().list(
                userId="me",
                q=query,
                maxResults=max_results,
                pageToken=page_token,
                includeSpamTrash=False,
            ).execute()
            for message in response.get("messages") or []:
                message_id = str((message or {}).get("id") or "").strip()
                if message_id:
                    message_ids.append(message_id)
            page_token = response.get("nextPageToken")
            if not page_token:
                break
        return message_ids

    def iter_incremental_message_ids(
        self,
        start_history_id: str,
        *,
        service: Any | None = None,
    ) -> list[str]:
        if not start_history_id:
            return []

        svc = service or self.get_service()
        page_token: str | None = None
        message_ids: list[str] = []
        seen: set[str] = set()

        try:
            from googleapiclient.errors import HttpError
        except ImportError:
            HttpError = Exception

        while True:
            try:
                response = svc.users().history().list(
                    userId="me",
                    startHistoryId=start_history_id,
                    historyTypes=["messageAdded"],
                    pageToken=page_token,
                    maxResults=100,
                ).execute()
            except HttpError as exc:
                if getattr(getattr(exc, "resp", None), "status", None) == 404:
                    raise GmailHistoryExpiredError(
                        "Stored Gmail history cursor expired; a full sync is required."
                    ) from exc
                raise

            for entry in response.get("history") or []:
                for added in entry.get("messagesAdded") or []:
                    message = added.get("message") or {}
                    message_id = str(message.get("id") or "").strip()
                    if message_id and message_id not in seen:
                        seen.add(message_id)
                        message_ids.append(message_id)

            page_token = response.get("nextPageToken")
            if not page_token:
                break

        return message_ids

    def get_message(self, message_id: str, *, service: Any | None = None) -> dict[str, Any]:
        svc = service or self.get_service()
        return dict(
            svc.users().messages().get(
                userId="me",
                id=message_id,
                format="full",
            ).execute()
            or {}
        )

    def get_attachment(
        self,
        message_id: str,
        attachment_id: str,
        *,
        service: Any | None = None,
    ) -> dict[str, Any]:
        svc = service or self.get_service()
        return dict(
            svc.users().messages().attachments().get(
                userId="me",
                messageId=message_id,
                id=attachment_id,
            ).execute()
            or {}
        )


def _gmail_content_hash(account_email: str, message_id: str, thread_id: str) -> str:
    payload = f"gmail:{account_email}:{message_id}:{thread_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _gmail_attachment_content_hash(
    account_email: str,
    message_id: str,
    attachment_id: str,
    filename: str,
    attachment_bytes: bytes,
) -> str:
    payload = (
        f"gmail-attachment:{account_email}:{message_id}:{attachment_id}:{filename}:"
        f"{hashlib.sha256(attachment_bytes).hexdigest()}"
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _attachment_bytes(
    client: GmailClient,
    message_id: str,
    attachment: dict[str, Any],
    *,
    service: Any | None = None,
) -> bytes:
    if attachment.get("data"):
        return _decode_base64url_bytes(str(attachment.get("data") or ""))

    attachment_id = str(attachment.get("attachment_id") or "").strip()
    if not attachment_id:
        return b""
    payload = client.get_attachment(message_id, attachment_id, service=service)
    return _decode_base64url_bytes(str(payload.get("data") or ""))


def _ingest_message(
    conn: sqlite3.Connection,
    parsed: dict[str, Any],
    *,
    graph_store: GraphStore,
    cognitive_store: CognitiveStore,
) -> tuple[str, str]:
    existing = database.get_item_by_path(conn, parsed["original_path"])
    item_id = str(existing["id"]) if existing else str(uuid.uuid4())

    item = {
        "id": item_id,
        "content_hash": _gmail_content_hash(
            parsed["account_email"],
            parsed["message_id"],
            parsed["thread_id"],
        ),
        "title": parsed["title"],
        "filename": parsed["filename"],
        "file_type": "text",
        "handler_type": "text",
        "source_type": "gmail",
        "original_path": parsed["original_path"],
        "created_at": parsed["created_at"],
        "modified_at": parsed["modified_at"],
        "first_seen_at": parsed["first_seen_at"],
        "file_size_bytes": len(parsed["extracted_text"].encode("utf-8")),
        "sender_name": parsed["sender_name"],
        "sender_email": parsed["sender_email"],
        "extracted_text": parsed["extracted_text"],
        "summary": parsed["summary"],
        "preview_text": parsed["preview_text"],
        "doc_type_guess": "email",
        "enrichment_tier": 1 if parsed["extracted_text"] else 0,
        "ocr_pending": 0,
    }

    embed_result = embedder.embed_and_index_text(
        item_id,
        parsed["summary"] or parsed["extracted_text"] or parsed["title"],
        conn,
        chunked=False,
        extra_text=parsed["extracted_text"],
        source_label=parsed["filename"],
        summary_text=parsed["summary"],
    )
    merged_keywords = _merge_keywords(
        list(embed_result.get("keywords") or []),
        list(parsed.get("keywords") or []),
        [parsed["account_email"]],
        [f"thread:{parsed['thread_id']}"] if parsed.get("thread_id") else [],
    )
    item["keywords"] = json.dumps(merged_keywords[:30], ensure_ascii=True)

    if existing is not None:
        update_fields = dict(item)
        update_fields.pop("id", None)
        database.update_item(conn, item_id, update_fields)
        item_state = "updated"
    else:
        inserted_item_id = database.insert_item(conn, item)
        item_id = str(inserted_item_id or item_id)
        item_state = "created" if item_id == str(item["id"]) else "updated"
    conn.commit()

    enrichment = asyncio.run(enricher.enrich_item(item_id, parsed["extracted_text"], parsed["filename"]))
    formatted = enricher.format_for_db(enrichment)
    database.update_item(conn, item_id, formatted)
    refreshed_item = database.get_item_by_id(conn, item_id) or item
    refreshed_item.update(formatted)

    knowledge = asyncio.run(extract_item_knowledge(refreshed_item))
    keyword_fields: dict[str, Any] = {
        "enrichment_tier": max(int(refreshed_item.get("enrichment_tier") or 0), 3),
    }
    if knowledge.get("keywords"):
        keyword_fields["keywords"] = json.dumps(
            _merge_keywords(
                _keyword_list(refreshed_item.get("keywords")),
                list(knowledge["keywords"]),
            ),
            ensure_ascii=True,
        )
    database.update_item(conn, item_id, keyword_fields)
    refreshed_item.update(keyword_fields)
    apply_item_knowledge(
        refreshed_item,
        knowledge,
        graph_store=graph_store,
        cognitive_store=cognitive_store,
    )

    return item_id, item_state


def _ingest_attachment(
    conn: sqlite3.Connection,
    parsed: dict[str, Any],
    attachment: dict[str, Any],
    attachment_bytes: bytes,
    *,
    graph_store: GraphStore,
    cognitive_store: CognitiveStore,
) -> tuple[str | None, str, str | None]:
    raw_filename = str(attachment.get("filename") or "").strip()
    filename = _safe_filename(
        raw_filename,
        default=f"attachment-{attachment.get('attachment_id') or 'file'}",
    )
    suffix = Path(filename).suffix.lower()
    attachment_id = str(attachment.get("attachment_id") or filename).strip()

    if not suffix or suffix not in GMAIL_ATTACHMENT_SUPPORTED_SUFFIXES:
        return None, "skipped", f"unsupported attachment type: {suffix or 'none'}"
    if not attachment_bytes:
        return None, "skipped", "empty attachment"
    if len(attachment_bytes) > GMAIL_ATTACHMENT_MAX_BYTES:
        return None, "skipped", f"attachment exceeds {GMAIL_ATTACHMENT_MAX_BYTES} bytes"

    with TemporaryDirectory(prefix="recurdo-gmail-attachment-") as tmp_dir:
        attachment_path = Path(tmp_dir) / filename
        attachment_path.write_bytes(attachment_bytes)
        extracted = extractor.extract_structured(attachment_path)

    extracted_text = str(extracted.full_text or "").strip()
    summary_text = str(extracted.summary_text or "").strip()
    if extracted.error and not extracted_text:
        return None, "skipped", extracted.error
    if not extracted_text and not summary_text:
        return None, "skipped", extracted.error or "no extractable attachment text"

    account_email = str(parsed["account_email"])
    message_id = str(parsed["message_id"])
    original_path = _attachment_url(message_id, attachment_id, filename)
    existing = database.get_item_by_path(conn, original_path)
    item_id = str(existing["id"]) if existing else str(uuid.uuid4())
    title = extracted.title or Path(filename).stem
    parent_subject = str(parsed.get("subject") or parsed.get("title") or "").strip()
    parent_sender = str(parsed.get("sender_name") or parsed.get("sender_email") or "").strip()
    content_prefix = "\n".join(
        part
        for part in [
            f"Gmail attachment: {filename}",
            f"Parent subject: {parent_subject}" if parent_subject else "",
            f"From: {parent_sender}" if parent_sender else "",
            f"Gmail message: {_message_url(message_id)}",
            "",
        ]
        if part is not None
    ).strip()
    stored_text = f"{content_prefix}\n\n{extracted_text or summary_text}".strip()
    summary = (
        f"Attachment from {parent_subject}: {summary_text}"
        if parent_subject and summary_text
        else summary_text or title
    )

    item = {
        "id": item_id,
        "content_hash": _gmail_attachment_content_hash(
            account_email,
            message_id,
            attachment_id,
            filename,
            attachment_bytes,
        ),
        "title": title,
        "filename": filename,
        "file_type": config.SUPPORTED_TEXT_EXTENSIONS.get(suffix, "text"),
        "handler_type": "text",
        "source_type": "gmail_attachment",
        "original_path": original_path,
        "created_at": parsed["created_at"],
        "modified_at": parsed["modified_at"],
        "first_seen_at": parsed["first_seen_at"],
        "file_size_bytes": len(attachment_bytes),
        "sender_name": parsed["sender_name"],
        "sender_email": parsed["sender_email"],
        "extracted_text": stored_text,
        "summary": summary,
        "preview_text": (extracted.first_page or summary_text or title)[:200],
        "doc_type_guess": config.SUPPORTED_TEXT_EXTENSIONS.get(suffix, "attachment"),
        "enrichment_tier": 1,
        "ocr_pending": 0,
    }

    embed_result = embedder.embed_and_index_text(
        item_id,
        summary_text or extracted_text or title,
        conn,
        chunked=False,
        extra_text=extracted_text,
        source_label=filename,
        summary_text=summary,
    )
    merged_keywords = _merge_keywords(
        list(embed_result.get("keywords") or []),
        list(parsed.get("keywords") or []),
        [filename, Path(filename).stem, parent_subject, account_email, "gmail_attachment", "has_attachment"],
        [f"thread:{parsed['thread_id']}"] if parsed.get("thread_id") else [],
    )
    item["keywords"] = json.dumps(merged_keywords[:30], ensure_ascii=True)

    if existing is not None:
        update_fields = dict(item)
        update_fields.pop("id", None)
        database.update_item(conn, item_id, update_fields)
        item_state = "updated"
    else:
        inserted_item_id = database.insert_item(conn, item)
        item_id = str(inserted_item_id or item_id)
        item_state = "created" if item_id == str(item["id"]) else "updated"
    conn.commit()

    enrichment = asyncio.run(enricher.enrich_item(item_id, stored_text, filename))
    formatted = enricher.format_for_db(enrichment)
    database.update_item(conn, item_id, formatted)
    refreshed_item = database.get_item_by_id(conn, item_id) or item
    refreshed_item.update(formatted)

    knowledge = asyncio.run(extract_item_knowledge(refreshed_item))
    keyword_fields: dict[str, Any] = {
        "enrichment_tier": max(int(refreshed_item.get("enrichment_tier") or 0), 3),
    }
    if knowledge.get("keywords"):
        keyword_fields["keywords"] = json.dumps(
            _merge_keywords(
                _keyword_list(refreshed_item.get("keywords")),
                list(knowledge["keywords"]),
            ),
            ensure_ascii=True,
        )
    database.update_item(conn, item_id, keyword_fields)
    refreshed_item.update(keyword_fields)
    apply_item_knowledge(
        refreshed_item,
        knowledge,
        graph_store=graph_store,
        cognitive_store=cognitive_store,
    )

    return item_id, item_state, None


def sync_gmail(
    *,
    connector: GmailClient | None = None,
    memory_db_path: str | Path | None = None,
    graph_store: GraphStore | None = None,
    cognitive_store: CognitiveStore | None = None,
    full_sync: bool = False,
    historical_days: int | None = None,
    require_attachments: bool = False,
    max_messages: int | None = None,
    progress_callback: Callable[[dict[str, Any]], None] | None = None,
    cancel_requested: Callable[[], bool] | None = None,
) -> dict[str, Any]:
    client = connector or GmailClient()
    graph = graph_store or get_graph_store()
    cognitive = cognitive_store or CognitiveStore()
    memory_path = Path(memory_db_path or config.DB_PATH).expanduser()
    normalized_max_messages = None if max_messages is None else max(0, int(max_messages))
    normalized_historical_days = _normalize_historical_days(historical_days)

    def emit(**payload: Any) -> None:
        if progress_callback is not None:
            progress_callback(payload)

    if not client.client_configured():
        raise GmailConnectorError(f"Missing Gmail client secrets at {client.client_secrets_path}")

    service = client.get_service(refresh=True)
    profile = client.get_profile(service=service)
    account_email = str(profile.get("emailAddress") or "").strip().casefold()
    sync_state = client.load_sync_state()
    start_history_id = str(sync_state.get("last_history_id") or "")
    sync_mode = "incremental"
    effective_historical_days = normalized_historical_days

    if full_sync:
        sync_mode = "historical" if effective_historical_days is not None else "full"
        message_ids = client.iter_full_sync_message_ids(
            service=service,
            query=_build_sync_query(
                historical_days=effective_historical_days,
                require_attachments=require_attachments,
            ),
            max_messages=normalized_max_messages,
        )
    elif effective_historical_days is not None:
        sync_mode = "historical"
        message_ids = client.iter_full_sync_message_ids(
            service=service,
            query=_build_sync_query(
                historical_days=effective_historical_days,
                require_attachments=require_attachments,
            ),
            max_messages=normalized_max_messages,
        )
    elif not start_history_id:
        sync_mode = "bootstrap"
        effective_historical_days = GMAIL_DEFAULT_BOOTSTRAP_DAYS
        message_ids = client.iter_full_sync_message_ids(
            service=service,
            query=_build_sync_query(
                historical_days=effective_historical_days,
                require_attachments=require_attachments,
            ),
            max_messages=normalized_max_messages,
        )
    else:
        try:
            message_ids = client.iter_incremental_message_ids(start_history_id, service=service)
        except GmailHistoryExpiredError:
            sync_mode = "bootstrap"
            effective_historical_days = GMAIL_DEFAULT_BOOTSTRAP_DAYS
            message_ids = client.iter_full_sync_message_ids(
                service=service,
                query=_build_sync_query(
                    historical_days=effective_historical_days,
                    require_attachments=require_attachments,
                ),
                max_messages=normalized_max_messages,
            )
    if normalized_max_messages is not None:
        message_ids = message_ids[:normalized_max_messages]

    emit(
        state="running",
        phase="loading",
        processed=0,
        total=len(message_ids),
        percent=0.0,
        mode=sync_mode,
        full_sync=bool(sync_mode == "full"),
        historical_days=effective_historical_days,
        require_attachments=bool(require_attachments),
        message=(
            f"loaded {len(message_ids)} Gmail messages to process"
            if effective_historical_days is None
            else f"loaded {len(message_ids)} Gmail messages from the last {effective_historical_days} days"
        ),
    )

    processed = 0
    created = 0
    updated = 0
    skipped = 0
    attachments_processed = 0
    attachments_created = 0
    attachments_updated = 0
    attachments_skipped = 0
    indexed_items_total = 0
    item_ids: list[str] = []

    conn = _open_memory_connection(memory_path)
    try:
        for index, message_id in enumerate(message_ids, start=1):
            if cancel_requested is not None and cancel_requested():
                raise GmailSyncCancelledError("gmail sync cancelled")

            raw_message = client.get_message(message_id, service=service)
            payload = dict(raw_message.get("payload") or {})
            attachment_refs = _extract_attachment_refs(payload)
            attachment_names = [
                str(ref.get("filename") or "").strip()
                for ref in attachment_refs
                if str(ref.get("filename") or "").strip()
            ]
            headers = {
                str(header.get("name") or "").casefold(): _decode_header_value(header.get("value"))
                for header in payload.get("headers") or []
                if isinstance(header, dict)
            }
            should_import, skip_reason = _should_import_message(
                raw_message,
                headers=headers,
                attachment_names=attachment_names,
                require_attachments=require_attachments,
            )
            if not should_import:
                skipped += 1
                if index == 1 or index % 10 == 0 or index == len(message_ids):
                    emit(
                        state="running",
                        phase="filtering",
                        processed=index,
                        total=len(message_ids),
                        percent=round((index / max(1, len(message_ids))) * 100, 1),
                        mode=sync_mode,
                        full_sync=bool(sync_mode == "full"),
                        historical_days=effective_historical_days,
                        require_attachments=bool(require_attachments),
                        skipped=skipped,
                        message=f"filtering Gmail message {index:,} / {len(message_ids):,}",
                        detail=skip_reason,
                    )
                continue

            parsed = parse_gmail_message(
                raw_message,
                account_email=account_email,
                attachment_names=attachment_names,
            )
            item_id, item_state = _ingest_message(
                conn,
                parsed,
                graph_store=graph,
                cognitive_store=cognitive,
            )
            item_ids.append(item_id)
            processed += 1
            if item_state == "created":
                created += 1
            else:
                updated += 1

            for attachment in attachment_refs[:GMAIL_ATTACHMENTS_PER_MESSAGE_LIMIT]:
                if cancel_requested is not None and cancel_requested():
                    raise GmailSyncCancelledError("gmail sync cancelled")
                try:
                    attachment_payload = _attachment_bytes(
                        client,
                        parsed["message_id"],
                        attachment,
                        service=service,
                    )
                    attachment_item_id, attachment_state, attachment_skip_reason = _ingest_attachment(
                        conn,
                        parsed,
                        attachment,
                        attachment_payload,
                        graph_store=graph,
                        cognitive_store=cognitive,
                    )
                except Exception as exc:
                    logger.exception(
                        "Unable to ingest Gmail attachment %s from message %s",
                        attachment.get("filename") or attachment.get("attachment_id"),
                        parsed["message_id"],
                    )
                    attachment_item_id = None
                    attachment_state = "skipped"
                    attachment_skip_reason = str(exc)

                if attachment_state == "created":
                    attachments_created += 1
                    attachments_processed += 1
                elif attachment_state == "updated":
                    attachments_updated += 1
                    attachments_processed += 1
                else:
                    attachments_skipped += 1
                    if attachment_skip_reason:
                        logger.info(
                            "Skipped Gmail attachment %s: %s",
                            attachment.get("filename") or attachment.get("attachment_id"),
                            attachment_skip_reason,
                        )
                if attachment_item_id:
                    item_ids.append(attachment_item_id)

            if index == 1 or index % 10 == 0 or index == len(message_ids):
                emit(
                    state="running",
                    phase="ingesting",
                    processed=index,
                    total=len(message_ids),
                    percent=round((index / max(1, len(message_ids))) * 100, 1),
                    mode=sync_mode,
                    full_sync=bool(sync_mode == "full"),
                    historical_days=effective_historical_days,
                    require_attachments=bool(require_attachments),
                    skipped=skipped,
                    attachments_processed=attachments_processed,
                    attachments_skipped=attachments_skipped,
                    message=f"syncing Gmail message {index:,} / {len(message_ids):,}",
                )
        indexed_items_total = _count_gmail_items(conn)
    finally:
        conn.close()

    latest_profile = client.get_profile(service=service)
    summary = {
        "mode": sync_mode,
        "full_sync": bool(sync_mode == "full"),
        "historical_days": effective_historical_days,
        "require_attachments": bool(require_attachments),
        "account_email": account_email,
        "processed": processed,
        "created": created,
        "updated": updated,
        "skipped": skipped,
        "attachments_processed": attachments_processed,
        "attachments_created": attachments_created,
        "attachments_updated": attachments_updated,
        "attachments_skipped": attachments_skipped,
        "message_ids": len(message_ids),
        "indexed_items": indexed_items_total,
        "last_history_id": str(latest_profile.get("historyId") or ""),
        "last_sync_at": _utc_now_iso(),
        "item_ids": item_ids[:25],
    }
    client.save_sync_state(
        {
            "account_email": account_email,
            "last_history_id": summary["last_history_id"],
            "last_sync_at": summary["last_sync_at"],
            "last_summary": {
                key: value
                for key, value in summary.items()
                if key not in {"item_ids"}
            },
        }
    )
    emit(
        state="completed",
        phase="complete",
        processed=processed,
        total=len(message_ids),
        percent=100.0,
        mode=sync_mode,
        full_sync=bool(sync_mode == "full"),
        message="gmail sync complete",
        summary=summary,
    )
    return summary


def cleanup_gmail_items(
    *,
    connector: GmailClient | None = None,
    memory_db_path: str | Path | None = None,
    graph_store: GraphStore | None = None,
    cognitive_store: CognitiveStore | None = None,
    reset_history_cursor: bool = False,
) -> dict[str, Any]:
    client = connector or GmailClient()
    graph = graph_store or get_graph_store()
    cognitive = cognitive_store or CognitiveStore()
    memory_path = Path(memory_db_path or config.DB_PATH).expanduser()

    conn = _open_memory_connection(memory_path)
    try:
        rows = conn.execute(
            """
            SELECT id
            FROM items
            WHERE source_type IN ('gmail', 'gmail_attachment')
              AND COALESCE(is_excluded, 0) = 0
            ORDER BY first_seen_at DESC
            """
        ).fetchall()
        item_ids = [str(row["id"]) for row in rows if row["id"]]
        row_params = [(item_id,) for item_id in item_ids]

        for item_id in item_ids:
            graph.clear_episode_state(item_id)
            cognitive.clear_item_knowledge(item_id, clear_activation_history=True)

        if row_params:
            conn.executemany("DELETE FROM embeddings WHERE item_id = ?", row_params)
            conn.executemany("DELETE FROM events WHERE item_id = ?", row_params)
            conn.executemany("UPDATE items SET is_excluded = 1 WHERE id = ?", row_params)
            conn.commit()

        indexed_items_total = _count_gmail_items(conn)
    finally:
        conn.close()

    if reset_history_cursor:
        client.save_sync_state(
            {
                "last_history_id": "",
                "last_sync_at": None,
                "last_summary": {},
            }
        )

    return {
        "retired": len(item_ids),
        "indexed_items": indexed_items_total,
        "reset_history_cursor": bool(reset_history_cursor),
        "item_ids": item_ids[:25],
    }
