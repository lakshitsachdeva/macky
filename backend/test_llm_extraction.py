from __future__ import annotations

import asyncio
import unittest

from backend.llm.extraction import extract_item_knowledge


class _FakeLLM:
    def status(self) -> dict[str, object]:
        return {"available": True, "provider": "fake"}

    async def generate(
        self,
        prompt: str,
        *,
        max_tokens: int = 1024,
        temperature: float = 0.1,
    ) -> str:
        del max_tokens, temperature
        if "Extract entities and relationships" in prompt:
            return """
            {
              "entities": [
                {"name": "Acme Ventures", "type": "Organisation", "confidence": 0.91},
                {"name": "Website Revamp", "type": "Project", "confidence": 0.88}
              ],
              "relationships": [
                {"subject": "Acme Ventures", "predicate": "COLLABORATES_WITH", "object": "Website Revamp", "confidence": 0.82}
              ]
            }
            """
        return """
        {
          "recipient": "Acme Ventures",
          "project_name": "Website Revamp",
          "status": "accepted",
          "key_asks": ["redesign", "analytics"]
        }
        """


class LLMExtractionTests(unittest.TestCase):
    def test_heuristic_extraction_returns_frame_entities_and_keywords(self) -> None:
        item = {
            "id": "proposal-1",
            "title": "Website Revamp Proposal",
            "filename": "website-revamp.pdf",
            "file_type": "pdf",
            "handler_type": "text",
            "source_type": "local",
            "summary": "Proposal for Acme Corp redesign project",
            "preview_text": "submitted proposal for Acme Corp",
            "extracted_text": "proposal client: Acme Corp deliverables redesign submitted deadline April 20, 2026",
            "keywords": '["proposal","website revamp"]',
            "first_seen_at": "2026-04-03T10:00:00+00:00",
            "created_at": "2026-04-03T10:00:00+00:00",
            "modified_at": "2026-04-03T10:00:00+00:00",
        }

        knowledge = asyncio.run(extract_item_knowledge(item, allow_model=False))

        self.assertEqual(knowledge["provider"], "heuristic")
        self.assertEqual(knowledge["frame"]["frame_type"], "proposal")
        self.assertGreaterEqual(len(knowledge["graph_entities"]), 2)
        self.assertGreaterEqual(len(knowledge["cognitive_entities"]), 2)
        self.assertTrue(any(fact["predicate"] == "HAS_STATUS" for fact in knowledge["temporal_facts"]))
        self.assertIn("proposal", [keyword.casefold() for keyword in knowledge["keywords"]])

    def test_llm_extraction_merges_entities_slots_and_relationships(self) -> None:
        item = {
            "id": "proposal-2",
            "title": "Client Proposal Draft",
            "filename": "client-proposal.pdf",
            "file_type": "pdf",
            "handler_type": "text",
            "source_type": "local",
            "summary": "Draft proposal for website work",
            "preview_text": "website project for client",
            "extracted_text": "proposal deliverables redesign and analytics draft",
            "keywords": '["proposal"]',
            "first_seen_at": "2026-04-03T10:00:00+00:00",
            "created_at": "2026-04-03T10:00:00+00:00",
            "modified_at": "2026-04-03T10:00:00+00:00",
        }

        knowledge = asyncio.run(
            extract_item_knowledge(
                item,
                allow_model=True,
                llm=_FakeLLM(),
            )
        )

        self.assertEqual(knowledge["provider"], "llm")
        self.assertEqual(knowledge["frame"]["slots"]["recipient"], "Acme Ventures")
        self.assertEqual(knowledge["frame"]["slots"]["status"], "accepted")
        self.assertIn("Acme Ventures", [entity["name"] for entity in knowledge["graph_entities"]])
        self.assertTrue(
            any(
                fact["predicate"] == "COLLABORATES_WITH"
                and fact["subject"] == "Acme Ventures"
                for fact in knowledge["temporal_facts"]
            )
        )
        self.assertIn("Acme Ventures", knowledge["keywords"])


if __name__ == "__main__":
    unittest.main()
