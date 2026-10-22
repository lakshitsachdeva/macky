from __future__ import annotations

import unittest

from backend.retrieval import query_parser
from backend.retrieval.search import (
    _apply_pipeline_to_weights,
    _resolve_query_pipeline,
    _weights_for_query,
)


class QueryIntentTests(unittest.TestCase):
    def test_person_centric_classification(self) -> None:
        parsed = query_parser.parse_query("emails from Rahul")
        self.assertEqual(parsed.intent_class, query_parser.IntentClass.PERSON_CENTRIC.value)

    def test_temporal_state_classification(self) -> None:
        parsed = query_parser.parse_query("what was the status of Recurdo in January")
        self.assertEqual(parsed.intent_class, query_parser.IntentClass.TEMPORAL_STATE.value)

    def test_temporal_range_classification(self) -> None:
        parsed = query_parser.parse_query("files from last week")
        self.assertEqual(parsed.intent_class, query_parser.IntentClass.TEMPORAL_RANGE.value)

    def test_exact_lookup_classification(self) -> None:
        parsed = query_parser.parse_query("invoice #1234")
        self.assertEqual(parsed.intent_class, query_parser.IntentClass.EXACT_LOOKUP.value)

    def test_semantic_topic_classification(self) -> None:
        parsed = query_parser.parse_query("machine learning transformers")
        self.assertEqual(parsed.intent_class, query_parser.IntentClass.SEMANTIC_TOPIC.value)

    def test_entity_lookup_classification(self) -> None:
        parsed = query_parser.parse_query("everything about the Bangalore project")
        self.assertEqual(parsed.intent_class, query_parser.IntentClass.ENTITY_LOOKUP.value)

    def test_search_weights_normalize_non_graph_components(self) -> None:
        parsed = query_parser.parse_query("emails from Rahul")
        weights = _weights_for_query(parsed)
        self.assertAlmostEqual(sum(weights.values()), 1.0, places=6)
        self.assertLess(weights["time"], weights["fts"])

    def test_fast_pipeline_for_filename_query(self) -> None:
        query = "resume_lakshit_iitg.pdf"
        parsed = query_parser.parse_query(query)
        route = _resolve_query_pipeline(query, parsed)
        self.assertEqual(route["pipeline"], "fast")
        self.assertFalse(route["use_graph"])

    def test_complex_pipeline_for_relationship_temporal_query(self) -> None:
        query = "who was i talking to about fundraising in march"
        parsed = query_parser.parse_query(query)
        route = _resolve_query_pipeline(query, parsed)
        self.assertEqual(route["pipeline"], "complex")
        self.assertTrue(route["use_graph"])
        self.assertTrue(route["use_temporal_subgraph"])

    def test_pipeline_weight_adjustment_zeroes_graph_when_disabled(self) -> None:
        parsed = query_parser.parse_query("emails from Rahul")
        raw_weights = _weights_for_query(parsed)
        adjusted = _apply_pipeline_to_weights(raw_weights, use_graph=False)
        self.assertAlmostEqual(sum(adjusted.values()), 1.0, places=6)
        self.assertEqual(adjusted["graph"], 0.0)

    def test_mail_phrase_detects_email_source_and_relaxes_file_type_strictness(self) -> None:
        parsed = query_parser.parse_query("the presentation i sent anannya on mail")
        self.assertEqual(parsed.source_type, "email")
        self.assertEqual(parsed.file_type, "presentation")
        self.assertFalse(parsed.file_type_strict)

    def test_sent_mail_phrase_extracts_person_hint_and_person_centric_intent(self) -> None:
        parsed = query_parser.parse_query("sent anannya on mail")
        self.assertIn("anannya", [hint.casefold() for hint in parsed.person_hints])
        self.assertEqual(parsed.source_type, "email")
        self.assertEqual(parsed.intent_class, query_parser.IntentClass.PERSON_CENTRIC.value)

    def test_standalone_uppercase_i_is_not_a_roman_numeral(self) -> None:
        parsed = query_parser.parse_query(
            "what was I working on in February for Recurdo"
        )

        self.assertNotIn("1", parsed.semantic_text.split())
        self.assertNotEqual(parsed.query_style, "identifier")

    def test_contextual_uppercase_i_still_expands_as_roman_numeral(self) -> None:
        self.assertIn("1", query_parser.expand_query("semester I security").split())


if __name__ == "__main__":
    unittest.main()
