from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any, Iterable


DECAY = 0.5
THRESHOLD = -2.0
NOISE_SIGMA = 0.5
K_RECENT = 10
MIN_ELAPSED_SECONDS = 0.001


def _normalize_datetime(value: datetime | str | int | float) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
    else:
        cleaned = str(value).strip()
        if cleaned.endswith("Z"):
            cleaned = cleaned[:-1] + "+00:00"
        parsed = datetime.fromisoformat(cleaned)

    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _to_unix_seconds(value: datetime | str | int | float) -> float:
    return _normalize_datetime(value).timestamp()


def _coerce_event(event: Any) -> tuple[float, float]:
    if isinstance(event, dict):
        occurred_at = event.get("occurred_at")
        weight = float(event.get("event_weight", 1.0) or 0.0)
        return _to_unix_seconds(occurred_at), weight

    if isinstance(event, (tuple, list)) and len(event) >= 2:
        return _to_unix_seconds(event[0]), float(event[1] or 0.0)

    raise TypeError(f"Unsupported activation event shape: {type(event)!r}")


def compute_base_level(
    access_history: Iterable[Any],
    current_time: datetime | int | float | None = None,
) -> float:
    """
    ACT-R base-level activation.

    B(m) = ln(sum(weight_i * elapsed_i^-d))
    """
    if current_time is None:
        now_ts = time.time()
    else:
        now_ts = _to_unix_seconds(current_time)

    normalized_history = [
        (occurred_at, weight)
        for occurred_at, weight in (_coerce_event(event) for event in access_history)
        if weight > 0
    ]
    if not normalized_history:
        return -float("inf")

    sorted_hist = sorted(normalized_history, key=lambda item: item[0], reverse=True)
    recent = sorted_hist[:K_RECENT]
    older = sorted_hist[K_RECENT:]

    activation_sum = 0.0
    for ts, weight in recent:
        elapsed = max(now_ts - ts, MIN_ELAPSED_SECONDS)
        activation_sum += weight * math.pow(elapsed, -DECAY)

    if older:
        total_older_weight = sum(weight for _, weight in older)
        avg_ts = sum(ts for ts, _ in older) / len(older)
        elapsed = max(now_ts - avg_ts, MIN_ELAPSED_SECONDS)
        activation_sum += total_older_weight * math.pow(elapsed, -DECAY)

    if activation_sum <= 0:
        return -float("inf")
    return math.log(activation_sum)


def compute_base_level_activation(
    events: Iterable[Any],
    *,
    now: datetime | int | float | None = None,
    decay: float = DECAY,
) -> float:
    if decay != DECAY:
        raise ValueError("Custom decay is not supported in the current approximation")
    return compute_base_level(events, current_time=now)


def normalize_activation(base_level: float) -> float:
    if base_level == -float("inf"):
        return 0.0
    return max(0.0, min(1.0, (float(base_level) + 7.0) / 9.0))


def retrieval_probability(activation: float) -> float:
    return 1.0 / (1.0 + math.exp(-(activation - THRESHOLD)))
