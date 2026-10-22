import os
import asyncio
import json
import base64
from functools import lru_cache
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib import error as urlerror
from urllib import request as urlrequest

from fastapi import HTTPException, Header, Depends
from supabase import create_client, Client

try:
    from . import config
except ImportError:
    import config

DEFAULT_ACCESS_STATUS_API_URL = "https://recurdo.lakshitsachdeva.tech/api/access-status"

LOCAL_MODE = (
    os.getenv("RECURDO_LOCAL_MODE", "").strip().lower()
    in {"1", "true", "yes", "on"}
)

SUPABASE_URL = (
    os.getenv("SUPABASE_URL")
    or os.getenv("VITE_SUPABASE_URL")
    or ""
)
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or os.getenv("SUPABASE_SERVICE_KEY")
SUPABASE_PUBLIC_KEY = (
    os.getenv("SUPABASE_ANON_KEY")
    or os.getenv("VITE_SUPABASE_ANON_KEY")
    or ""
)
ACCESS_STATUS_API_URL = (
    os.getenv("ACCESS_STATUS_API_URL")
    or os.getenv("RECURDO_ACCESS_STATUS_API_URL")
    or DEFAULT_ACCESS_STATUS_API_URL
).strip()
ALLOW_LOCAL_DEV_AUTH = (
    LOCAL_MODE
    or os.getenv("RECURDO_ALLOW_LOCAL_DEV_AUTH", "").strip().lower()
    in {"1", "true", "yes", "on"}
)
DIRECT_ACCESS_CONFIGURED = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)
USER_AUTH_CONFIGURED = bool(SUPABASE_URL and (SUPABASE_SERVICE_KEY or SUPABASE_PUBLIC_KEY))
REMOTE_ACCESS_CONFIGURED = bool(ACCESS_STATUS_API_URL)
OFFLINE_AUTH_GRACE_DAYS = int(os.getenv("OFFLINE_AUTH_GRACE_DAYS", "30"))
AUTH_USER_CACHE_TTL_SECONDS = int(os.getenv("AUTH_USER_CACHE_TTL_SECONDS", "45"))
AUTH_CACHE_PATH = config.AUTH_CACHE_PATH
BETA_ACCESS_TIER = os.getenv("RECURDO_DEFAULT_ACCESS_TIER", "beta").strip() or "beta"
BETA_DURATION_DAYS = max(1, int(os.getenv("RECURDO_BETA_DURATION_DAYS", "90")))


class SupabaseHttpError(RuntimeError):
    def __init__(self, status_code: int, body: str):
        super().__init__(body or f"HTTP {status_code}")
        self.status_code = status_code
        self.body = body


class SupabaseNetworkError(RuntimeError):
    pass

@lru_cache(maxsize=1)
def get_supabase() -> Client:
    if not DIRECT_ACCESS_CONFIGURED:
        raise RuntimeError("Supabase auth is not configured")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


def _normalize_email(email: Optional[str]) -> str:
    return str(email or "").strip().lower()


def _merge_metadata(existing: Optional[dict], updates: Optional[dict]) -> dict:
    base = existing if isinstance(existing, dict) else {}
    incoming = updates if isinstance(updates, dict) else {}
    return {
        **base,
        **incoming,
    }


def _compute_beta_window() -> tuple[str, str]:
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(days=BETA_DURATION_DAYS)
    return now.isoformat(), expires_at.isoformat()


def _sync_profile_and_access_grant(user: dict) -> None:
    if not DIRECT_ACCESS_CONFIGURED:
        return

    user_id = str(user.get("id") or "").strip()
    email = _normalize_email(user.get("email"))
    if not user_id or not email:
        return

    try:
        supabase = get_supabase()

        profile_payload = {
            "id": user_id,
            "email": email,
            "name": user.get("name") or email.split("@")[0],
        }
        supabase.table("profiles").upsert(profile_payload, on_conflict="id").execute()

        existing_access = (
            supabase.table("access")
            .select("id")
            .eq("user_id", user_id)
            .limit(1)
            .execute()
        )
        if existing_access.data:
            return

        waitlist_match = (
            supabase.table("waitlist_signups")
            .select("id,email,status,metadata,created_at")
            .eq("email_normalized", email)
            .in_("status", ["approved", "invited"])
            .order("created_at")
            .limit(1)
            .execute()
        )

        if not waitlist_match.data:
            return

        matched_row = waitlist_match.data[0]
        now_iso, expires_at = _compute_beta_window()

        supabase.table("access").insert({
            "user_id": user_id,
            "tier": BETA_ACCESS_TIER,
            "beta_started_at": now_iso,
            "expires_at": expires_at,
            "is_blocked": False,
            "notes": f"approved from waitlist request {matched_row['id']}",
            "updated_at": now_iso,
        }).execute()

        merged_metadata = _merge_metadata(
            matched_row.get("metadata"),
            {
                "granted_user_id": user_id,
                "granted_at": now_iso,
                "provisioned_from_waitlist": True,
            },
        )

        supabase.table("waitlist_signups").update({
            "status": "invited",
            "metadata": merged_metadata,
        }).eq("id", matched_row["id"]).execute()
    except Exception:
        pass

def _access_unavailable(
    tier: str = "access-unavailable",
    *,
    offline_cached: bool = False,
) -> dict:
    return {
        "has_access": False,
        "tier": tier,
        "days_remaining": 0,
        "expires_at": None,
        "is_blocked": False,
        "offline_cached": offline_cached,
        "verification_unavailable": True,
    }


def _normalize_access_info(payload: Optional[dict]) -> dict:
    payload = payload if isinstance(payload, dict) else {}
    return {
        "has_access": bool(payload.get("has_access")),
        "tier": payload.get("tier", "unknown"),
        "days_remaining": payload.get("days_remaining", 0),
        "expires_at": payload.get("expires_at"),
        "is_blocked": bool(payload.get("is_blocked", False)),
        "offline_cached": bool(payload.get("offline_cached", False)),
        "verification_unavailable": bool(payload.get("verification_unavailable", False)),
    }


def get_access_info(user_id: str, token: Optional[str] = None) -> dict:
    if LOCAL_MODE or (ALLOW_LOCAL_DEV_AUTH and not DIRECT_ACCESS_CONFIGURED and not REMOTE_ACCESS_CONFIGURED):
        return {
            "has_access": True,
            "tier": "local-forensics" if LOCAL_MODE else "local-dev",
            "days_remaining": 999,
            "expires_at": None,
            "is_blocked": False,
            "offline_cached": False,
            "verification_unavailable": False,
        }

    access = _get_cached(user_id)
    if not access:
        if not DIRECT_ACCESS_CONFIGURED and not (REMOTE_ACCESS_CONFIGURED and token):
            return _cached_access_record(user_id) or _access_unavailable("access-unconfigured")

        try:
            if DIRECT_ACCESS_CONFIGURED:
                access = _fetch_access_info(user_id)
            else:
                access = _fetch_remote_access_info(token)

            access = _normalize_access_info(access)
            _set_cached(user_id, access)
            _cache_access_record(user_id, access)
        except SupabaseNetworkError:
            access = _cached_access_record(user_id) or _access_unavailable("offline-unverified")
        except SupabaseHttpError as exc:
            if exc.status_code in {401, 403}:
                access = _cached_access_record(user_id) or _access_unavailable("access-unverified")
            else:
                access = _cached_access_record(user_id) or _access_unavailable("access-error")
        except Exception:
            access = _cached_access_record(user_id) or _access_unavailable("access-error")
    return access


def _supabase_api_key(prefer_service: bool = True) -> str:
    if prefer_service and SUPABASE_SERVICE_KEY:
        return SUPABASE_SERVICE_KEY
    if SUPABASE_PUBLIC_KEY:
        return SUPABASE_PUBLIC_KEY
    if SUPABASE_SERVICE_KEY:
        return SUPABASE_SERVICE_KEY
    raise RuntimeError("Missing Supabase API key")


def _supabase_headers(token: Optional[str] = None, *, prefer_service: bool = True) -> dict[str, str]:
    api_key = _supabase_api_key(prefer_service=prefer_service)
    headers = {
        "apikey": api_key,
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    elif prefer_service and SUPABASE_SERVICE_KEY:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _supabase_get_json(url: str, headers: dict[str, str]) -> dict:
    request = urlrequest.Request(url, headers=headers)
    try:
        with urlrequest.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise SupabaseHttpError(exc.code, body or str(exc)) from exc
    except urlerror.URLError as exc:
        raise SupabaseNetworkError(str(exc.reason or exc)) from exc
    except TimeoutError as exc:
        raise SupabaseNetworkError(str(exc)) from exc


def _supabase_post_json(url: str, payload: dict, headers: dict[str, str]) -> dict:
    request = urlrequest.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            **headers,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urlrequest.urlopen(request, timeout=10) as response:
            return json.loads(response.read().decode("utf-8"))
    except urlerror.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise SupabaseHttpError(exc.code, body or str(exc)) from exc
    except urlerror.URLError as exc:
        raise SupabaseNetworkError(str(exc.reason or exc)) from exc
    except TimeoutError as exc:
        raise SupabaseNetworkError(str(exc)) from exc


def _auth_cache_default() -> dict:
    return {
        "users": {},
        "access": {},
    }


def _read_auth_cache() -> dict:
    try:
        payload = json.loads(AUTH_CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            return {
                "users": payload.get("users") if isinstance(payload.get("users"), dict) else {},
                "access": payload.get("access") if isinstance(payload.get("access"), dict) else {},
            }
    except Exception:
        pass
    return _auth_cache_default()


def _write_auth_cache(data: dict) -> None:
    try:
        AUTH_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        AUTH_CACHE_PATH.write_text(
            json.dumps(data, ensure_ascii=True, indent=2, sort_keys=True),
            encoding="utf-8",
        )
    except Exception:
        pass


def _cache_timestamp_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _cache_now() -> datetime:
    return datetime.now(timezone.utc)


def _is_cache_fresh(verified_at: str | None) -> bool:
    if not verified_at:
        return False

    try:
        parsed = datetime.fromisoformat(verified_at.replace("Z", "+00:00"))
    except Exception:
        return False

    return (_cache_now() - parsed).days <= OFFLINE_AUTH_GRACE_DAYS


def _decode_jwt_payload(token: str) -> dict:
    parts = token.split(".")
    if len(parts) != 3:
        return {}

    try:
        payload = parts[1]
        padding = "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload + padding)
        parsed = json.loads(decoded.decode("utf-8"))
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _token_cache_expiry(token: str, ttl_seconds: int) -> Optional[datetime]:
    if ttl_seconds <= 0:
        return None

    expires_at = _cache_now() + timedelta(seconds=ttl_seconds)
    claims = _decode_jwt_payload(token)
    token_exp = claims.get("exp")
    if token_exp is None:
        return expires_at

    try:
        token_expiry = datetime.fromtimestamp(int(token_exp), tz=timezone.utc)
    except Exception:
        return expires_at

    return min(expires_at, token_expiry)


def _cache_user_record(user: dict) -> None:
    user_id = str(user.get("id") or "").strip()
    if not user_id:
        return

    data = _read_auth_cache()
    data["users"][user_id] = {
        "id": user_id,
        "email": user.get("email") or "",
        "name": user.get("name") or "",
        "verified_at": _cache_timestamp_now(),
    }
    _write_auth_cache(data)


def _cached_user_from_token(token: str) -> Optional[dict]:
    claims = _decode_jwt_payload(token)
    user_id = str(claims.get("sub") or "").strip()
    if not user_id:
        return None

    cached = _read_auth_cache().get("users", {}).get(user_id)
    if not isinstance(cached, dict) or not _is_cache_fresh(cached.get("verified_at")):
        return None

    return {
        "id": user_id,
        "email": cached.get("email") or claims.get("email") or "",
        "name": cached.get("name") or "",
        "token": token,
        "offline_cached": True,
    }


def _cache_access_record(user_id: str, access: dict) -> None:
    if not user_id:
        return

    data = _read_auth_cache()
    data["access"][user_id] = {
        "has_access": bool(access.get("has_access")),
        "tier": access.get("tier", "unknown"),
        "days_remaining": access.get("days_remaining", 0),
        "expires_at": access.get("expires_at"),
        "is_blocked": bool(access.get("is_blocked", False)),
        "verified_at": _cache_timestamp_now(),
    }
    _write_auth_cache(data)


def _cached_access_record(user_id: str) -> Optional[dict]:
    cached = _read_auth_cache().get("access", {}).get(user_id)
    if not isinstance(cached, dict) or not _is_cache_fresh(cached.get("verified_at")):
        return None

    return {
        "has_access": bool(cached.get("has_access")),
        "tier": cached.get("tier", "unknown"),
        "days_remaining": cached.get("days_remaining", 0),
        "expires_at": cached.get("expires_at"),
        "is_blocked": bool(cached.get("is_blocked", False)),
        "offline_cached": True,
        "verification_unavailable": False,
    }


def _fetch_supabase_user(token: str) -> dict:
    auth_url = f"{SUPABASE_URL.rstrip('/')}/auth/v1/user"
    return _supabase_get_json(auth_url, _supabase_headers(token, prefer_service=False))


def _fetch_access_info(user_id: str) -> dict:
    rpc_url = f"{SUPABASE_URL.rstrip('/')}/rest/v1/rpc/check_access"
    return _normalize_access_info(_supabase_post_json(
        rpc_url,
        {"p_user_id": user_id},
        _supabase_headers(),
    ))


def _fetch_remote_access_info(token: Optional[str]) -> dict:
    if not token or not ACCESS_STATUS_API_URL:
        raise SupabaseNetworkError("Missing access status verifier")

    payload = _supabase_get_json(
        ACCESS_STATUS_API_URL,
        {
            "Accept": "application/json",
            "Authorization": f"Bearer {token}",
        },
    )
    if isinstance(payload.get("access"), dict):
        return _normalize_access_info(payload.get("access"))
    return _normalize_access_info(payload)


def _is_invalid_or_expired_auth_error(exc: SupabaseHttpError) -> bool:
    body = (exc.body or "").lower()
    return exc.status_code in {401, 403} and (
        "bad_jwt" in body
        or "token is expired" in body
        or "invalid jwt" in body
        or "jwt" in body
    )

# -- Simple 60-second in-memory cache so we don't hit
#    Supabase on every single API call -----------------
_cache: dict = {}
CACHE_TTL = 60  # seconds
_user_cache: dict[str, tuple[dict, datetime]] = {}

def _get_cached(user_id: str) -> Optional[dict]:
    if user_id not in _cache:
        return None
    result, ts = _cache[user_id]
    if (_cache_now() - ts).seconds > CACHE_TTL:
        del _cache[user_id]
        return None
    return result

def _set_cached(user_id: str, result: dict):
    _cache[user_id] = (result, _cache_now())


def _get_cached_verified_user(token: str) -> Optional[dict]:
    cached = _user_cache.get(token)
    if not cached:
        return None

    user, expires_at = cached
    if _cache_now() >= expires_at:
        _user_cache.pop(token, None)
        return None

    return {
        **user,
        "token": token,
    }


def _set_cached_verified_user(token: str, user: dict) -> None:
    expires_at = _token_cache_expiry(token, AUTH_USER_CACHE_TTL_SECONDS)
    if not expires_at or expires_at <= _cache_now():
        return

    _user_cache[token] = (
        {
            "id": user.get("id"),
            "email": user.get("email"),
            "name": user.get("name"),
        },
        expires_at,
    )


def _clear_cached_verified_user(token: str) -> None:
    _user_cache.pop(token, None)


def _clear_cached_verified_users_for_user(user_id: str) -> None:
    tokens_to_remove = [
        token
        for token, (cached_user, _expires_at) in _user_cache.items()
        if cached_user.get("id") == user_id
    ]
    for token in tokens_to_remove:
        _user_cache.pop(token, None)

def bust_cache(user_id: str):
    """Call this if you ever extend/block a user mid-session."""
    _cache.pop(user_id, None)
    _clear_cached_verified_users_for_user(user_id)
    data = _read_auth_cache()
    data.get("access", {}).pop(user_id, None)
    data.get("users", {}).pop(user_id, None)
    _write_auth_cache(data)


# -- Step 1: Validate JWT, return user ------------------
async def get_current_user(
    authorization: Optional[str] = Header(None)
) -> dict:
    """
    Validates Bearer JWT from Authorization header.
    Returns: { id, email, name, token }
    Raises 401 if missing or invalid.
    """
    if LOCAL_MODE or (ALLOW_LOCAL_DEV_AUTH and not USER_AUTH_CONFIGURED):
        return {
            "id": "local-dev-user",
            "email": "local@custody.app" if LOCAL_MODE else "local@recurdo.app",
            "name": "Local Forensics Workspace" if LOCAL_MODE else "Local Developer",
            "token": "local-dev-token",
        }

    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail={
                "code":    "AUTH_REQUIRED",
                "message": "Please log in to use Recurdo.",
            },
            headers={"X-Auth-Required": "true"}
        )

    token = authorization.split(" ", 1)[1]
    cached_user = _get_cached_verified_user(token)
    if cached_user:
        return cached_user

    if not USER_AUTH_CONFIGURED:
        cached_user = _cached_user_from_token(token)
        if cached_user:
            return cached_user

        raise HTTPException(status_code=503, detail={
            "code": "AUTH_UNAVAILABLE",
            "message": "Desktop auth is not configured right now."
        })

    try:
        # Supabase token verification uses a synchronous urllib request with a
        # 10-second timeout. Running it inline would block the FastAPI event
        # loop on every cold-cache auth check; pushing it to the default
        # thread pool keeps /search and other requests responsive while one
        # request is stuck on Supabase network I/O.
        user = await asyncio.to_thread(_fetch_supabase_user, token)
        if not user:
            raise HTTPException(status_code=401, detail={
                "code":    "INVALID_TOKEN",
                "message": "Session expired. Please log in again."
            })
        normalized_user = {
            "id":    user.get("id"),
            "email": user.get("email"),
            "name":  (user.get("user_metadata") or {}).get("full_name"),
            "token": token,
        }
        # The access-grant sync also hits Supabase synchronously — offload it
        # so authentication latency variance does not leak into the event loop.
        await asyncio.to_thread(_sync_profile_and_access_grant, normalized_user)
        _cache_user_record(normalized_user)
        _set_cached_verified_user(token, normalized_user)
        return normalized_user
    except SupabaseHttpError as exc:
        if _is_invalid_or_expired_auth_error(exc):
            cached_user = _cached_user_from_token(token)
            if cached_user:
                return cached_user

            _clear_cached_verified_user(token)
            raise HTTPException(status_code=401, detail={
                "code":    "INVALID_TOKEN",
                "message": "Session expired. Please log in again."
            })

        cached_user = _cached_user_from_token(token)
        if cached_user:
            return cached_user

        raise HTTPException(status_code=503, detail={
            "code": "AUTH_UNAVAILABLE",
            "message": "Unable to verify your session right now."
        })
    except SupabaseNetworkError:
        cached_user = _cached_user_from_token(token)
        if cached_user:
            return cached_user

        raise HTTPException(status_code=503, detail={
            "code": "AUTH_OFFLINE",
            "message": "Sign in online once before using Recurdo offline."
        })
    except HTTPException:
        raise
    except Exception:
        _clear_cached_verified_user(token)
        cached_user = _cached_user_from_token(token)
        if cached_user:
            return cached_user

        raise HTTPException(status_code=401, detail={
            "code":    "AUTH_ERROR",
            "message": "Authentication failed. Please log in again."
        })


# -- Step 2: Check beta access --------------------------
async def require_access(
    user: dict = Depends(get_current_user)
) -> dict:
    """
    Checks if this user has active beta access.
    Uses check_access() Postgres function.
    Raises 403 with { code, message, days_remaining } if not.
    Returns merged user + access info if allowed.
    """
    user_id = user["id"]

    # get_access_info hits Supabase synchronously on cache miss; run it off
    # the event loop so one slow access-check does not stall every concurrent
    # /search request behind it.
    access = await asyncio.to_thread(get_access_info, user_id, user.get("token"))

    if access.get("verification_unavailable"):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "ACCESS_UNAVAILABLE",
                "message": "Unable to verify your access right now.",
            },
        )

    if not access.get("has_access"):
        raise HTTPException(
            status_code=403,
            detail={
                "code": "ACCESS_BLOCKED" if access.get("is_blocked") else "BETA_EXPIRED",
                "message": (
                    "Your access has been revoked."
                    if access.get("is_blocked") else
                    "Your beta access has ended. Stay tuned for the full launch."
                ),
                "tier":           access.get("tier"),
                "days_remaining": 0,
                "expires_at":     access.get("expires_at"),
            }
        )

    return {
        **user,
        "tier":           access.get("tier"),
        "days_remaining": access.get("days_remaining"),
        "expires_at":     access.get("expires_at"),
    }


# -- Usage tracking (no limits, just counting) ----------
def track_file_indexed(user_id: str, new_total: int):
    if not DIRECT_ACCESS_CONFIGURED:
        return
    try:
        supabase = get_supabase()
        today    = datetime.now(timezone.utc).date().isoformat()
        supabase.table("usage").upsert({
            "user_id":     user_id,
            "date":        today,
            "files_total": new_total,
            "updated_at":  datetime.now(timezone.utc).isoformat()
        }, on_conflict="user_id,date").execute()
    except Exception:
        pass  # never let tracking crash the app

def track_search_done(user_id: str):
    if not DIRECT_ACCESS_CONFIGURED:
        return
    try:
        supabase = get_supabase()
        today    = datetime.now(timezone.utc).date().isoformat()
        supabase.rpc("increment_search_count", {
            "p_user_id": user_id,
            "p_date":    today
        }).execute()
    except Exception:
        pass
