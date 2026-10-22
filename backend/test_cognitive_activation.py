from __future__ import annotations

from datetime import datetime, timezone
import unittest

from backend.cognition.activation import compute_base_level_activation, normalize_activation


class BaseLevelActivationTests(unittest.TestCase):
    def test_empty_history_returns_zero(self) -> None:
        self.assertEqual(compute_base_level_activation([]), -float("inf"))

    def test_recent_event_scores_higher_than_old_event(self) -> None:
        now = datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc)

        recent = compute_base_level_activation(
            [(datetime(2026, 4, 2, 11, 0, tzinfo=timezone.utc), 1.0)],
            now=now,
        )
        old = compute_base_level_activation(
            [(datetime(2026, 3, 20, 12, 0, tzinfo=timezone.utc), 1.0)],
            now=now,
        )

        self.assertGreater(recent, old)

    def test_more_events_raise_activation(self) -> None:
        now = datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc)

        single = compute_base_level_activation(
            [(datetime(2026, 4, 2, 11, 30, tzinfo=timezone.utc), 1.0)],
            now=now,
        )
        repeated = compute_base_level_activation(
            [
                (datetime(2026, 4, 2, 11, 30, tzinfo=timezone.utc), 1.0),
                (datetime(2026, 4, 1, 10, 0, tzinfo=timezone.utc), 1.0),
                (datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc), 1.0),
            ],
            now=now,
        )

        self.assertGreater(repeated, single)

    def test_zero_weight_events_are_ignored(self) -> None:
        now = datetime(2026, 4, 2, 12, 0, tzinfo=timezone.utc)
        score = compute_base_level_activation(
            [
                (datetime(2026, 4, 2, 11, 59, tzinfo=timezone.utc), 0.0),
                (datetime(2026, 4, 2, 11, 58, tzinfo=timezone.utc), 0.0),
            ],
            now=now,
        )

        self.assertEqual(score, -float("inf"))

    def test_normalize_activation_bounds_values(self) -> None:
        self.assertEqual(normalize_activation(-float("inf")), 0.0)
        self.assertGreater(normalize_activation(1.0), normalize_activation(-2.0))


if __name__ == "__main__":
    unittest.main()
