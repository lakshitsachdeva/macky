#!/usr/bin/env python3
"""
Recurdo auth flow test suite.

Usage:
    python test_auth.py --token YOUR_JWT_TOKEN
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import signal
import socket
import subprocess
import time
import urllib.parse
from pathlib import Path
from typing import Any

import httpx


DEFAULT_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_STARTUP_TIMEOUT_SECONDS = 45.0
DEFAULT_CONCURRENCY = 10

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = BACKEND_DIR.parent
RUN_DIR = PROJECT_ROOT / ".run"
BACKEND_LOG = RUN_DIR / "backend.log"
BACKEND_PID_FILE = RUN_DIR / "backend.pid"
BACKEND_PYTHON = PROJECT_ROOT / "venv311" / "bin" / "python"


def _decode_b64url(data: str) -> bytes:
    padding = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + padding)


def _encode_b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def decode_jwt_payload(token: str) -> dict[str, Any]:
    parts = token.split(".")
    if len(parts) != 3:
        return {}

    try:
        payload = _decode_b64url(parts[1])
        parsed = json.loads(payload.decode("utf-8"))
    except Exception:
        return {}

    return parsed if isinstance(parsed, dict) else {}


def build_expired_token(valid_token: str) -> str:
    parts = valid_token.split(".")
    if len(parts) != 3:
        return f"{valid_token}.expired"

    try:
        payload = decode_jwt_payload(valid_token)
        payload["exp"] = 1
        encoded_payload = _encode_b64url(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        return ".".join((parts[0], encoded_payload, parts[2]))
    except Exception:
        return f"{valid_token}.expired"


def format_detail(detail: Any) -> str:
    if isinstance(detail, (dict, list)):
        return json.dumps(detail, ensure_ascii=True, sort_keys=True)
    return str(detail)


def response_detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except Exception:
        body = response.text.strip()

    return f"status={response.status_code} body={format_detail(body)}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--token",
        required=True,
        help="Valid Supabase JWT to test with.",
    )
    parser.add_argument(
        "--expired-token",
        help=(
            "Optional expired JWT. If omitted, the suite synthesizes an expired-looking "
            "variant from --token and expects the backend to reject it with 401."
        ),
    )
    parser.add_argument(
        "--base-url",
        default=DEFAULT_BASE_URL,
        help=f"Backend base URL. Default: {DEFAULT_BASE_URL}",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help=f"HTTP timeout in seconds. Default: {DEFAULT_TIMEOUT_SECONDS}",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        default=DEFAULT_CONCURRENCY,
        help=f"Number of concurrent auth requests. Default: {DEFAULT_CONCURRENCY}",
    )
    parser.add_argument(
        "--skip-restart",
        action="store_true",
        help="Skip the backend restart scenario.",
    )
    parser.add_argument(
        "--restart-timeout",
        type=float,
        default=DEFAULT_STARTUP_TIMEOUT_SECONDS,
        help=(
            "How long to wait for the backend to shut down and come back after restart. "
            f"Default: {DEFAULT_STARTUP_TIMEOUT_SECONDS}"
        ),
    )
    return parser.parse_args()


def get_backend_port(base_url: str) -> int:
    parsed = urllib.parse.urlparse(base_url)
    if parsed.port is not None:
        return parsed.port
    if parsed.scheme == "https":
        return 443
    return 80


def wait_for_port_state(host: str, port: int, should_be_open: bool, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.5)
            is_open = sock.connect_ex((host, port)) == 0
        if is_open == should_be_open:
            return True
        time.sleep(0.25)
    return False


def wait_for_health(base_url: str, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            response = httpx.get(f"{base_url.rstrip('/')}/health", timeout=2.0)
        except Exception:
            time.sleep(0.5)
            continue

        if response.status_code == 200:
            return True
        time.sleep(0.5)

    return False


def find_backend_pid(port: int) -> int | None:
    try:
        output = subprocess.check_output(
            ["lsof", "-ti", f"tcp:{port}"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except Exception:
        output = ""

    for line in output.splitlines():
        line = line.strip()
        if line.isdigit():
            return int(line)
    return None


def stop_process(pid: int, timeout: float) -> None:
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.25)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return


def start_backend_process(port: int) -> int:
    if not BACKEND_PYTHON.exists():
        raise RuntimeError(f"Missing backend python: {BACKEND_PYTHON}")

    RUN_DIR.mkdir(parents=True, exist_ok=True)
    log_file = open(BACKEND_LOG, "ab")

    try:
        process = subprocess.Popen(
            [str(BACKEND_PYTHON), "-m", "uvicorn", "backend.main:app", "--port", str(port)],
            cwd=PROJECT_ROOT,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        log_file.close()

    BACKEND_PID_FILE.write_text(f"{process.pid}\n", encoding="utf-8")
    return process.pid


def restart_backend(base_url: str, timeout: float) -> tuple[bool, str]:
    parsed = urllib.parse.urlparse(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = get_backend_port(base_url)
    pid = find_backend_pid(port)

    if pid is None:
        return False, f"No process is listening on port {port}."

    stop_process(pid, timeout=timeout / 2)

    if not wait_for_port_state(host, port, should_be_open=False, timeout=timeout / 2):
        return False, f"Backend on port {port} did not stop cleanly."

    new_pid = start_backend_process(port)

    if not wait_for_port_state(host, port, should_be_open=True, timeout=timeout):
        return False, f"Restarted backend pid={new_pid} never opened port {port}."

    if not wait_for_health(base_url, timeout=timeout):
        return False, f"Restarted backend pid={new_pid} never passed /health."

    return True, f"restarted pid {pid} -> {new_pid}"


async def get_auth_status(
    client: httpx.AsyncClient,
    base_url: str,
    token: str | None = None,
) -> httpx.Response:
    headers: dict[str, str] = {}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return await client.get(f"{base_url.rstrip('/')}/auth/status", headers=headers)


async def run_tests(args: argparse.Namespace) -> bool:
    base_url = args.base_url.rstrip("/")
    results: list[tuple[str, bool, Any]] = []
    token_claims = decode_jwt_payload(args.token)
    expired_token = args.expired_token or build_expired_token(args.token)

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        try:
            health = await client.get(f"{base_url}/health")
            results.append(("PRE health endpoint reachable", health.status_code == 200, response_detail(health)))
        except Exception as exc:
            results.append(("PRE health endpoint reachable", False, repr(exc)))
            print_results(results)
            return False

        no_token = await get_auth_status(client, base_url)
        results.append(("T1 no token -> 401", no_token.status_code == 401, response_detail(no_token)))

        garbage = await get_auth_status(client, base_url, "thisisnotavalidjwt")
        results.append(("T2 garbage token -> 401", garbage.status_code == 401, response_detail(garbage)))

        encoded = urllib.parse.quote(args.token, safe="")
        encoded_response = await get_auth_status(client, base_url, encoded)
        results.append(
            ("T3 URL-encoded token -> 200", encoded_response.status_code == 200, response_detail(encoded_response))
        )

        expired = await get_auth_status(client, base_url, expired_token)
        expired_label = "T4 expired token -> 401"
        if args.expired_token:
            expired_label += " (provided token)"
        else:
            expired_label += " (synthetic expired token)"
        results.append((expired_label, expired.status_code == 401, response_detail(expired)))

        valid = await get_auth_status(client, base_url, args.token)
        valid_ok = valid.status_code == 200
        results.append(("T5 valid token -> 200", valid_ok, response_detail(valid)))

        valid_data: dict[str, Any] = {}
        if valid_ok:
            try:
                valid_data = valid.json()
            except Exception as exc:
                results.append(("T5a valid response is JSON", False, repr(exc)))
            else:
                user = valid_data.get("user") or {}
                access = valid_data.get("access") or {}

                expected_user_id = token_claims.get("sub")
                expected_email = token_claims.get("email")

                user_id_ok = bool(user.get("id")) and (
                    expected_user_id is None or user.get("id") == expected_user_id
                )
                results.append(
                    ("T5a valid token -> user.id matches token claims", user_id_ok, format_detail(user))
                )

                email_ok = bool(user.get("email")) and (
                    expected_email is None or user.get("email") == expected_email
                )
                results.append(
                    ("T5b valid token -> user.email matches token claims", email_ok, format_detail(user))
                )

                name_ok = "name" in user
                results.append(("T5c valid token -> user.name field present", name_ok, format_detail(user)))

                has_access_ok = access.get("has_access") is True
                results.append(
                    ("T6 valid token -> check_access returns has_access=true", has_access_ok, format_detail(access))
                )
        else:
            results.append(("T5a valid token -> user.id matches token claims", False, "Skipped because T5 failed"))
            results.append(("T5b valid token -> user.email matches token claims", False, "Skipped because T5 failed"))
            results.append(("T5c valid token -> user.name field present", False, "Skipped because T5 failed"))
            results.append(("T6 valid token -> check_access returns has_access=true", False, "Skipped because T5 failed"))

        tasks = [get_auth_status(client, base_url, args.token) for _ in range(args.concurrency)]
        concurrent_responses = await asyncio.gather(*tasks, return_exceptions=True)
        ok_count = 0
        failures: list[str] = []
        for item in concurrent_responses:
            if isinstance(item, Exception):
                failures.append(repr(item))
                continue
            if item.status_code == 200:
                ok_count += 1
            else:
                failures.append(response_detail(item))
        results.append(
            (
                f"T7 rate {args.concurrency} concurrent valid requests -> all 200",
                ok_count == args.concurrency,
                f"{ok_count}/{args.concurrency} ok; failures={failures or 'none'}",
            )
        )

    if args.skip_restart:
        results.append(("T8 backend restart with cached session -> still works", True, "Skipped by --skip-restart"))
        print_results(results)
        return all(ok for _, ok, _ in results)

    restart_ok, restart_detail = restart_backend(base_url, timeout=args.restart_timeout)
    if not restart_ok:
        results.append(("T8 backend restart with cached session -> still works", False, restart_detail))
        print_results(results)
        return False

    async with httpx.AsyncClient(timeout=args.timeout) as client:
        restarted = await get_auth_status(client, base_url, args.token)
        results.append(
            (
                "T8 backend restart with cached session -> still works",
                restarted.status_code == 200,
                f"{restart_detail}; {response_detail(restarted)}",
            )
        )

    print_results(results)
    return all(ok for _, ok, _ in results)


def print_results(results: list[tuple[str, bool, Any]]) -> None:
    print()
    print("=" * 72)
    print("RECURDO AUTH TEST RESULTS")
    print("=" * 72)

    passed = 0
    failed = 0
    for name, ok, detail in results:
        status = "PASS" if ok else "FAIL"
        print(f"[{status}] {name}")
        if not ok:
            print(f"        got: {format_detail(detail)}")
        passed += int(ok)
        failed += int(not ok)

    print("=" * 72)
    print(f"{passed} passed, {failed} failed")
    print("=" * 72)
    print()


def main() -> int:
    args = parse_args()
    ok = asyncio.run(run_tests(args))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
