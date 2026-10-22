from __future__ import annotations

import asyncio
import unittest

from backend.llm.synthesis import (
    build_community_fallback_summary,
    build_evolution_fallback_summary,
    build_proactive_fallback_briefing,
    build_search_fallback_summary,
    build_temporal_fallback_summary,
    synthesize_community_briefing,
    synthesize_evolution_briefing,
    synthesize_proactive_briefing,
    synthesize_search_briefing,
    synthesize_temporal_briefing,
)


class LLMSynthesisTests(unittest.TestCase):
    def test_search_fallback_summary_mentions_top_results(self) -> None:
        results = [
            {"title": "Seed Deck", "preview_text": "Investor update deck", "source_type": "local"},
            {"title": "Warm Intro Email", "preview_text": "Email to investor", "source_type": "gmail"},
        ]

        summary = build_search_fallback_summary("investor update", results)
        self.assertIn("Seed Deck", summary)
        self.assertIn("Warm Intro Email", summary)

    def test_temporal_fallback_summary_mentions_window(self) -> None:
        payload = {
            "time_window": {
                "start_date": "2026-04-01T00:00:00+00:00",
                "end_date": "2026-04-07T00:00:00+00:00",
            },
            "active_entities": [{"name": "Fundraising", "entity_type": "Concept", "period_mentions": 4}],
            "episodes": [{"title": "Seed Deck", "timestamp": "2026-04-03T12:00:00+00:00"}],
        }

        summary = build_temporal_fallback_summary("fundraising last week", payload)
        self.assertIn("2026-04-01", summary)
        self.assertIn("Fundraising", summary)

    def test_evolution_fallback_summary_mentions_span(self) -> None:
        payload = {
            "episodes": [
                {"title": "Early Notes", "timestamp": "2026-01-01T00:00:00+00:00"},
                {"title": "Latest Deck", "timestamp": "2026-04-01T00:00:00+00:00"},
            ],
            "timeline": [{"valid_from": "2026-02-01T00:00:00+00:00"}],
        }

        summary = build_evolution_fallback_summary("Recurdo", payload)
        self.assertIn("2026-01-01", summary)
        self.assertIn("2026-04-01", summary)

    def test_community_fallback_summary_mentions_top_entities(self) -> None:
        community = {
            "top_entities": [{"name": "Fundraising"}, {"name": "Investor"}, {"name": "Term Sheet"}],
            "episode_count": 6,
            "last_activity": "2026-04-05T00:00:00+00:00",
        }

        summary = build_community_fallback_summary(community)
        self.assertIn("Fundraising", summary)
        self.assertIn("Investor", summary)

    def test_proactive_fallback_briefing_lists_related_items(self) -> None:
        briefing = build_proactive_fallback_briefing(
            "Seed Deck",
            [{"title": "Investor Notes"}, {"title": "Term Sheet"}, {"title": "Warm Intro Email"}],
        )
        self.assertIn("Investor Notes", briefing)
        self.assertIn("Term Sheet", briefing)

    def test_async_briefings_use_heuristic_when_model_disabled(self) -> None:
        search_summary = asyncio.run(
            synthesize_search_briefing(
                "investor update",
                [{"title": "Seed Deck", "preview_text": "Deck", "source_type": "local"}],
                allow_model=False,
            )
        )
        temporal_summary = asyncio.run(
            synthesize_temporal_briefing(
                "fundraising last week",
                {
                    "time_window": {
                        "start_date": "2026-04-01T00:00:00+00:00",
                        "end_date": "2026-04-07T00:00:00+00:00",
                    },
                    "active_entities": [{"name": "Fundraising", "entity_type": "Concept", "period_mentions": 4}],
                    "episodes": [{"title": "Seed Deck", "timestamp": "2026-04-03T12:00:00+00:00"}],
                },
                allow_model=False,
            )
        )
        evolution_summary = asyncio.run(
            synthesize_evolution_briefing(
                "Recurdo",
                {
                    "episodes": [{"title": "Seed Deck", "timestamp": "2026-04-03T12:00:00+00:00"}],
                    "timeline": [{"valid_from": "2026-04-03T12:00:00+00:00"}],
                },
                allow_model=False,
            )
        )
        community_summary = asyncio.run(
            synthesize_community_briefing(
                {
                    "top_entities": [{"name": "Fundraising"}, {"name": "Investor"}],
                    "episode_count": 2,
                    "last_activity": "2026-04-05T00:00:00+00:00",
                },
                allow_model=False,
            )
        )
        proactive_summary = asyncio.run(
            synthesize_proactive_briefing(
                "Seed Deck",
                [{"title": "Investor Notes"}, {"title": "Warm Intro Email"}],
                allow_model=False,
            )
        )

        self.assertEqual(search_summary["provider"], "heuristic")
        self.assertEqual(temporal_summary["provider"], "heuristic")
        self.assertEqual(evolution_summary["provider"], "heuristic")
        self.assertEqual(community_summary["provider"], "heuristic")
        self.assertEqual(proactive_summary["provider"], "heuristic")


if __name__ == "__main__":
    unittest.main()
