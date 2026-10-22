from __future__ import annotations

import unittest

from backend.cognition.heuristics import (
    classify_batch,
    classify_frame,
    extract_entities_from_item,
    extract_query_entities,
    infer_frame,
)


class CognitiveHeuristicsTests(unittest.TestCase):
    def test_gmail_items_are_classified_as_email(self) -> None:
        self.assertEqual(
            classify_frame({"source_type": "gmail", "extracted_text": ""}),
            "email",
        )

    def test_doc_type_guess_takes_priority(self) -> None:
        self.assertEqual(
            classify_frame(
                {
                    "source_type": "file",
                    "doc_type_guess": "contract",
                    "extracted_text": "some random content without triggers",
                }
            ),
            "contract",
        )

    def test_pitch_deck_trigger_matches(self) -> None:
        self.assertEqual(
            classify_frame(
                {
                    "source_type": "file",
                    "file_type": "presentation",
                    "extracted_text": "pitch deck investor deck seed round market opportunity",
                    "doc_type_guess": "",
                }
            ),
            "pitch_deck",
        )

    def test_proposal_slots_are_filled_with_defaults_and_extracted_values(self) -> None:
        frame_type, slots, confidence = infer_frame(
            {
                "title": "Bangalore Analytics Proposal",
                "source_type": "file",
                "extracted_text": (
                    "We propose to deliver the Recurdo Analytics Dashboard for XYZ Corporation. "
                    "Scope of work includes data ingestion, visualization, and reporting. "
                    "Deliverables due by March 31, 2026. Budget: ₹8,00,000."
                ),
            }
        )

        self.assertEqual(frame_type, "proposal")
        self.assertGreater(confidence, 0.8)
        self.assertEqual(slots["status"], "draft")
        self.assertEqual(slots["project_name"], "Bangalore Analytics Proposal")
        self.assertEqual(slots["deadline"], "March 31, 2026")
        self.assertEqual(slots["proposed_budget"], "₹8,00,000")
        self.assertIsInstance(slots["key_asks"], list)

    def test_invoice_slots_extract_number_amount_and_client(self) -> None:
        frame_type, slots, _ = infer_frame(
            {
                "title": "Invoice April",
                "source_type": "file",
                "sender_name": "Acme Studio",
                "extracted_text": (
                    "Invoice #1234\nBill To: XYZ Corp\nAmount Due ₹50,000\nPayment due March 20, 2026"
                ),
            }
        )

        self.assertEqual(frame_type, "invoice")
        self.assertEqual(slots["vendor"], "Acme Studio")
        self.assertEqual(slots["client"], "XYZ Corp")
        self.assertEqual(slots["invoice_number"], "1234")
        self.assertEqual(slots["amount"], "₹50,000")
        self.assertEqual(slots["due_date"], "March 20, 2026")

    def test_query_entities_preserve_multiword_concepts(self) -> None:
        entities = extract_query_entities("investor pitch deck for seed round", limit=6)

        self.assertIn("entity:pitch-deck", entities)
        self.assertIn("entity:seed-round", entities)

    def test_query_entities_ignore_question_and_time_words(self) -> None:
        entities = extract_query_entities(
            "what was i working on in february for recurdo",
            limit=6,
        )

        self.assertIn("entity:recurdo", entities)
        self.assertNotIn("entity:what", entities)
        self.assertNotIn("entity:february", entities)
        self.assertFalse(
            any("working" in entity or "february" in entity for entity in entities),
            entities,
        )

    def test_query_entities_ignore_generic_project_words(self) -> None:
        entities = extract_query_entities(
            "what major finance project or related startup was i working on in february",
            limit=6,
        )

        self.assertNotIn("entity:project", entities)
        self.assertNotIn("entity:major", entities)
        self.assertNotIn("entity:related", entities)
        self.assertFalse(any("major" in entity for entity in entities), entities)
        self.assertFalse(any("project" in entity for entity in entities), entities)
        self.assertFalse(any("related" in entity for entity in entities), entities)

    def test_item_entities_include_people_phrases_and_frame(self) -> None:
        entities = extract_entities_from_item(
            {
                "title": "Recurdo Seed Deck",
                "sender_name": "Rahul Mehta",
                "sender_email": "rahul@example.com",
                "keywords": '["fundraising","seed round"]',
                "summary": "Investor deck for the Recurdo fundraising process",
                "extracted_text": "Pitch deck for seed round market opportunity and investor outreach",
                "file_type": "presentation",
            },
            limit=12,
        )

        entity_ids = {entity_id for entity_id, _ in entities}
        self.assertIn("entity:rahul-mehta", entity_ids)
        self.assertIn("entity:seed-round", entity_ids)
        self.assertIn("entity:pitch-deck", entity_ids)

    def test_classify_batch_returns_item_tuples(self) -> None:
        results = classify_batch(
            [
                {
                    "id": "item-1",
                    "source_type": "gmail",
                    "extracted_text": "",
                },
                {
                    "id": "item-2",
                    "source_type": "file",
                    "extracted_text": "meeting notes attendees Lakshit Arjun action items",
                },
            ]
        )

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0][0], "item-1")
        self.assertEqual(results[0][1], "email")
        self.assertEqual(results[1][1], "meeting_notes")


if __name__ == "__main__":
    unittest.main()
