import unittest

from backend.retrieval import query_parser
from backend.retrieval.search import (
    _academic_coursework_bias,
    _academic_unit_lookup_bias,
    _content_score,
    _dense_only_visual_false_positive_bias,
    _generated_artifact_bias,
    _filename_score,
    _is_time_only_match,
    _metadata_match,
    _ranking_evidence_bonus,
    _recent_visual_bonus,
    _resume_noise_bias,
    _project_meta_doc_bias,
    _project_workspace_bias,
    _source_type_bias,
    _topic_query_tokens,
    _visual_asset_bias,
)


class SearchRankingTests(unittest.TestCase):
    def test_content_score_includes_extracted_text(self):
        parsed = query_parser.parse_query("iit madras")
        item = {
            "title": "IMG_8664",
            "summary": None,
            "keywords": '["ssp","iitm","summer"]',
            "preview_text": "Internship opportunity at Indian Institute of Technology, Madras",
            "extracted_text": (
                "Internship opportunity at Indian Institute of Technology, Madras "
                "Details at https://ssp.iitm.ac.in"
            ),
        }

        self.assertGreaterEqual(_content_score(item, parsed), 0.70)

    def test_image_query_tokens_ignore_visual_request_words(self):
        parsed = query_parser.parse_query("that image that i had about that iit madras internshop")
        topic_tokens = _topic_query_tokens(parsed)

        self.assertIn("iit", topic_tokens)
        self.assertIn("madras", topic_tokens)
        self.assertIn("internshop", topic_tokens)
        self.assertNotIn("image", topic_tokens)
        self.assertNotIn("about", topic_tokens)
        self.assertNotIn("had", topic_tokens)

    def test_visual_items_receive_bounded_ocr_bonus(self):
        bonus = _ranking_evidence_bonus(
            {"handler_type": "visual"},
            content_score=0.75,
            path_score=0.0,
            fts_score=0.68,
        )

        self.assertGreaterEqual(bonus, 0.18)
        self.assertLessEqual(bonus, 0.22)

    def test_recent_visual_bonus_rewards_recent_ocr_heavy_images(self):
        bonus = _recent_visual_bonus(
            {"handler_type": "visual"},
            visual_score=0.78,
            recency_score=0.82,
        )

        self.assertGreater(bonus, 0.05)
        self.assertLessEqual(bonus, 0.06)

    def test_recent_visual_bonus_does_not_affect_old_or_weak_matches(self):
        self.assertEqual(
            _recent_visual_bonus(
                {"handler_type": "visual"},
                visual_score=0.50,
                recency_score=0.90,
            ),
            0.0,
        )

    def test_metadata_match_treats_email_queries_as_gmail_source(self):
        parsed = query_parser.parse_query("gmail")
        item = {
            "source_type": "gmail",
            "sender_name": "",
            "sender_email": "",
            "filename": "Email - Seed Deck Follow Up.eml",
            "title": "Seed Deck Follow Up",
            "extracted_text": "",
            "keywords": "[]",
        }

        self.assertEqual(_metadata_match(item, parsed), 1.0)

    def test_topic_tokens_ignore_temporal_question_words(self):
        parsed = query_parser.parse_query("what was i working on in february for recurdo")

        self.assertEqual(_topic_query_tokens(parsed), {"recurdo"})

    def test_finance_startup_recall_does_not_become_spreadsheet_search(self):
        parsed = query_parser.parse_query(
            "what major finance project or startup was i working on in february"
        )

        self.assertIsNone(parsed.file_type)
        self.assertIsNone(parsed.handler_type)
        topic_tokens = _topic_query_tokens(parsed)
        self.assertIn("finance", topic_tokens)
        self.assertIn("startup", topic_tokens)
        self.assertIn("trading", topic_tokens)
        self.assertIn("landing", topic_tokens)
        self.assertNotIn("project", topic_tokens)
        self.assertNotIn("major", topic_tokens)

    def test_finance_startup_expansion_beats_generic_project_match(self):
        parsed = query_parser.parse_query(
            "what major finance project or startup was i working on in february"
        )
        athair_item = {
            "title": "ATHAIR — Coming Soon",
            "summary": "",
            "keywords": '["athair","single-ticker analysis","portfolio","quant"]',
            "preview_text": "",
            "extracted_text": "Single-ticker analysis with quant metrics, trade input, portfolio history, and investment thesis.",
        }
        generic_project_item = {
            "title": "Risk Log",
            "summary": "",
            "keywords": '["risk","project","implementation"]',
            "preview_text": "",
            "extracted_text": "Risk log for a class project implementation plan.",
        }

        self.assertGreater(_content_score(athair_item, parsed), _content_score(generic_project_item, parsed))

    def test_stocks_project_query_expands_finance_terms_without_related_noise(self):
        parsed = query_parser.parse_query(
            "the stocks related project i was working on in february"
        )

        self.assertIsNone(parsed.file_type)
        self.assertIsNone(parsed.handler_type)
        topic_tokens = _topic_query_tokens(parsed)
        self.assertIn("stocks", topic_tokens)
        self.assertIn("finance", topic_tokens)
        self.assertIn("trading", topic_tokens)
        self.assertIn("portfolio", topic_tokens)
        self.assertNotIn("related", topic_tokens)
        self.assertNotIn("project", topic_tokens)

    def test_stock_project_recall_penalizes_unasked_coursework(self):
        parsed = query_parser.parse_query(
            "the stocks related project i was working on in february"
        )
        item = {
            "title": "LINEAR REGRESSION",
            "filename": "linear regression.pptx",
            "original_path": "/Users/me/Desktop/college/sem 6/ML/linear regression.pptx",
            "keywords": "[]",
        }

        self.assertLess(_academic_coursework_bias(item, parsed), 0.6)

    def test_explicit_coursework_search_keeps_coursework(self):
        parsed = query_parser.parse_query("linear regression ppt")
        item = {
            "title": "LINEAR REGRESSION",
            "filename": "linear regression.pptx",
            "original_path": "/Users/me/Desktop/college/sem 6/ML/linear regression.pptx",
            "keywords": "[]",
        }

        self.assertEqual(_academic_coursework_bias(item, parsed), 1.0)

    def test_identifier_unit_lookup_prefers_matching_unit(self):
        parsed = query_parser.parse_query("unit 5 program security pdf")
        item = {
            "title": "Welcome to -",
            "filename": "Unit-5-Program Security.pdf",
            "preview_text": "Unit 5 : Program Security",
            "summary": "Unit 5 : Program Security",
        }

        self.assertGreater(_academic_unit_lookup_bias(item, parsed), 1.0)

    def test_identifier_unit_lookup_penalizes_other_units(self):
        parsed = query_parser.parse_query("unit 5 program security pdf")
        item = {
            "title": "Welcome to -",
            "filename": "Unit-1-Introduction.pdf",
            "preview_text": "Unit 1 : Introduction",
            "summary": "Unit 1 : Introduction",
        }

        self.assertLess(_academic_unit_lookup_bias(item, parsed), 0.6)

    def test_identifier_unit_lookup_penalizes_general_course_pdfs_without_unit(self):
        parsed = query_parser.parse_query("unit 5 program security pdf")
        item = {
            "title": "Mukesh Patel School of Technology Management and Engineering",
            "filename": "Course Policy-Cyber Security 2025-26.pdf",
            "preview_text": "Course Policy for Cyber Security program and semester details",
            "summary": "Course Policy for Cyber Security program and semester details",
        }

        self.assertLess(_academic_unit_lookup_bias(item, parsed), 0.7)

    def test_stock_project_recall_penalizes_unasked_ml_lectures(self):
        parsed = query_parser.parse_query(
            "the stocks related project i was working on in february"
        )
        item = {
            "title": "Introduction to ML",
            "filename": "ML unit 1.pptx",
            "original_path": "/Users/me/Downloads/ML unit 1.pptx",
            "keywords": "[]",
        }

        self.assertLess(_academic_coursework_bias(item, parsed), 0.6)

    def test_project_recall_boosts_project_workspace_docs(self):
        parsed = query_parser.parse_query(
            "the stocks related project i was working on in february"
        )
        item = {
            "title": "Rate limits and what accuracy means",
            "filename": "RATE_LIMITS_AND_ACCURACY.md",
            "original_path": "/Users/me/Desktop/Projects/mat-system/swarm-alpha/RATE_LIMITS_AND_ACCURACY.md",
            "summary": "Stock trading project with portfolio accuracy and market rate limits",
            "keywords": '["stock","trading","portfolio","market"]',
        }

        self.assertGreater(_project_workspace_bias(item, parsed), 1.0)

    def test_project_recall_does_not_boost_unrelated_project_workspace_docs(self):
        parsed = query_parser.parse_query(
            "the stocks related project i was working on in february"
        )
        item = {
            "title": "Contributing to drift",
            "filename": "CONTRIBUTING.md",
            "original_path": "/Users/me/Desktop/Projects/intent2model/CONTRIBUTING.md",
            "summary": "How to contribute development setup code style",
            "keywords": '["contributing","repo","branch","setup"]',
        }

        self.assertEqual(_project_workspace_bias(item, parsed), 1.0)

    def test_project_recall_penalizes_unasked_contributing_docs(self):
        parsed = query_parser.parse_query(
            "the stocks related project i was working on in february"
        )
        item = {
            "title": "Contributing to drift",
            "filename": "CONTRIBUTING.md",
            "original_path": "/Users/me/Desktop/Projects/intent2model/CONTRIBUTING.md",
        }

        self.assertLess(_project_meta_doc_bias(item, parsed), 0.7)

    def test_explicit_contributing_search_keeps_contributing_docs(self):
        parsed = query_parser.parse_query("contributing setup for drift project")
        item = {
            "title": "Contributing to drift",
            "filename": "CONTRIBUTING.md",
            "original_path": "/Users/me/Desktop/Projects/intent2model/CONTRIBUTING.md",
        }

        self.assertEqual(_project_meta_doc_bias(item, parsed), 1.0)

    def test_project_recall_penalizes_unasked_roadmap_docs(self):
        parsed = query_parser.parse_query("what was i working on in february for recurdo")
        item = {
            "title": "RECURDO — FROM HERE TO REAL USERS",
            "filename": "recurdo_from_here_to_users.md",
            "original_path": "/Users/me/Desktop/Projects/MemorySearch/recurdo_from_here_to_users.md",
        }

        self.assertLess(_project_meta_doc_bias(item, parsed), 0.5)

    def test_explicit_roadmap_search_keeps_roadmap_docs(self):
        parsed = query_parser.parse_query("recurdo from here to users")
        item = {
            "title": "RECURDO — FROM HERE TO REAL USERS",
            "filename": "recurdo_from_here_to_users.md",
            "original_path": "/Users/me/Desktop/Projects/MemorySearch/recurdo_from_here_to_users.md",
        }

        self.assertEqual(_project_meta_doc_bias(item, parsed), 1.0)

    def test_project_recall_penalizes_unasked_visual_assets(self):
        parsed = query_parser.parse_query(
            "the stocks related project i was working on in february"
        )
        item = {
            "title": "lakshit-face",
            "filename": "lakshit-face.png",
            "handler_type": "visual",
            "original_path": "/Users/me/Desktop/Projects/portfolio-lakshit/lakshit-face.png",
        }

        self.assertLess(_visual_asset_bias(item, parsed), 0.7)

    def test_explicit_image_project_search_keeps_visual_assets(self):
        parsed = query_parser.parse_query("image from stocks project in february")
        item = {
            "title": "athair",
            "filename": "athair.png",
            "handler_type": "visual",
            "original_path": "/Users/me/Desktop/Projects/documentation mat/athair.png",
        }

        self.assertEqual(_visual_asset_bias(item, parsed), 1.0)

    def test_dense_only_visual_false_positive_bias_penalizes_irrelevant_image_when_anchor_exists(self):
        parsed = query_parser.parse_query("that image that i had about that iit madras internshop")
        item = {
            "title": "e47e39ea-e452-454f-9cfc-615d8fdb0ad4",
            "filename": "e47e39ea-e452-454f-9cfc-615d8fdb0ad4.jpeg",
            "handler_type": "visual",
        }

        self.assertLess(
            _dense_only_visual_false_positive_bias(
                item,
                parsed,
                dense_score=0.54,
                fts_score=0.0,
                filename_score=0.0,
                content_score=0.0,
                path_score=0.0,
                has_visual_lexical_anchor=True,
            ),
            0.2,
        )

    def test_dense_only_visual_false_positive_bias_does_not_fire_without_anchor(self):
        parsed = query_parser.parse_query("that image that i had about that iit madras internshop")
        item = {
            "title": "vacation photo",
            "filename": "vacation.jpg",
            "handler_type": "visual",
        }

        self.assertEqual(
            _dense_only_visual_false_positive_bias(
                item,
                parsed,
                dense_score=0.54,
                fts_score=0.0,
                filename_score=0.0,
                content_score=0.0,
                path_score=0.0,
                has_visual_lexical_anchor=False,
            ),
            1.0,
        )

    def test_broad_memory_recall_penalizes_generated_artifacts(self):
        parsed = query_parser.parse_query(
            "what major finance project or startup was i working on in february"
        )
        item = {
            "title": "rows_20260220_151939",
            "filename": "rows_20260220_151939.csv",
            "original_path": "/Users/me/project/validation_artifacts/rows_20260220_151939.csv",
        }

        self.assertLess(_generated_artifact_bias(item, parsed), 0.5)

    def test_explicit_artifact_search_keeps_generated_artifacts(self):
        parsed = query_parser.parse_query("rows_20260220_151939 csv")
        item = {
            "title": "rows_20260220_151939",
            "filename": "rows_20260220_151939.csv",
            "original_path": "/Users/me/project/validation_artifacts/rows_20260220_151939.csv",
        }

        self.assertEqual(_generated_artifact_bias(item, parsed), 1.0)

    def test_broad_memory_recall_penalizes_generated_logs(self):
        parsed = query_parser.parse_query(
            "the stocks related project i was working on in february"
        )
        item = {
            "title": "monitor_20260223_220152",
            "filename": "monitor_20260223_220152.txt",
            "original_path": "/Users/me/Desktop/Projects/mat-system/swarm-alpha/logs/monitor_20260223_220152.txt",
        }

        self.assertLess(_generated_artifact_bias(item, parsed), 0.5)

    def test_explicit_log_search_keeps_generated_logs(self):
        parsed = query_parser.parse_query("monitor log 20260223")
        item = {
            "title": "monitor_20260223_220152",
            "filename": "monitor_20260223_220152.txt",
            "original_path": "/Users/me/Desktop/Projects/mat-system/swarm-alpha/logs/monitor_20260223_220152.txt",
        }

        self.assertEqual(_generated_artifact_bias(item, parsed), 1.0)

    def test_broad_memory_recall_penalizes_raw_csv_data(self):
        parsed = query_parser.parse_query(
            "what major finance project or startup was i working on in february"
        )
        item = {
            "title": "SandP_stocks_data_2008_2023",
            "filename": "SandP_stocks_data_2008_2023.csv",
            "original_path": "/Users/me/Downloads/swarm-data/SandP_stocks_data_2008_2023.csv",
        }

        self.assertLess(_generated_artifact_bias(item, parsed), 0.7)

    def test_explicit_data_search_keeps_csv_data(self):
        parsed = query_parser.parse_query("SandP stocks csv data")
        item = {
            "title": "SandP_stocks_data_2008_2023",
            "filename": "SandP_stocks_data_2008_2023.csv",
            "original_path": "/Users/me/Downloads/swarm-data/SandP_stocks_data_2008_2023.csv",
        }

        self.assertEqual(_generated_artifact_bias(item, parsed), 1.0)

    def test_broad_project_recall_penalizes_resume_files(self):
        parsed = query_parser.parse_query(
            "what major finance project or startup was i working on in february"
        )
        item = {
            "title": "Lakshit Sachdeva",
            "filename": "Lakshit_Resume_Final.docx",
            "original_path": "/Users/me/Downloads/Lakshit_Resume_Final.docx",
            "doc_type_guess": "",
            "keywords": "[]",
        }

        self.assertLess(_resume_noise_bias(item, parsed), 0.7)

    def test_explicit_resume_search_keeps_resume_files(self):
        parsed = query_parser.parse_query("lakshit resume")
        item = {
            "title": "Lakshit Sachdeva",
            "filename": "Lakshit_Resume_Final.docx",
            "original_path": "/Users/me/Downloads/Lakshit_Resume_Final.docx",
            "doc_type_guess": "",
            "keywords": "[]",
        }

        self.assertEqual(_resume_noise_bias(item, parsed), 1.0)

    def test_content_score_ignores_temporal_words_for_topic_time_query(self):
        parsed = query_parser.parse_query("what was i working on in february for recurdo")

        self.assertEqual(
            _content_score(
                {
                    "title": "February notes",
                    "summary": "February planning notes",
                    "keywords": "[]",
                    "preview_text": "",
                    "extracted_text": "",
                },
                parsed,
            ),
            0.0,
        )
        self.assertGreater(
            _content_score(
                {
                    "title": "Recurdo plan",
                    "summary": "Recurdo memory architecture",
                    "keywords": "[]",
                    "preview_text": "",
                    "extracted_text": "",
                },
                parsed,
            ),
            0.9,
        )

    def test_strong_time_query_detects_time_only_match_when_topic_present(self):
        parsed = query_parser.parse_query("what was i working on in february for recurdo")

        self.assertTrue(
            _is_time_only_match(
                parsed,
                dense_score=0.0,
                fts_score=0.0,
                filename_score=0.0,
                content_score=0.0,
                path_score=0.0,
            )
        )
        self.assertFalse(
            _is_time_only_match(
                parsed,
                dense_score=0.0,
                fts_score=0.0,
                filename_score=0.6,
                content_score=0.0,
                path_score=0.0,
            )
        )

    def test_metadata_match_treats_email_queries_as_gmail_attachment_source(self):
        parsed = query_parser.parse_query("gmail")
        item = {
            "source_type": "gmail_attachment",
            "sender_name": "",
            "sender_email": "",
            "filename": "seed-deck.pdf",
            "title": "Seed Deck",
            "extracted_text": "",
            "keywords": "[]",
        }

        self.assertEqual(_metadata_match(item, parsed), 1.0)

    def test_source_type_bias_penalizes_low_signal_gmail_in_generic_search(self):
        parsed = query_parser.parse_query("seed deck")
        item = {
            "source_type": "gmail",
            "title": "Weekly newsletter",
            "filename": "Email - Weekly newsletter.eml",
            "summary": "unsubscribe any time",
            "preview_text": "unsubscribe from these product updates",
            "sender_name": "Product Updates",
            "sender_email": "newsletter@example.com",
            "importance_score": 0.5,
            "open_count": 0,
        }

        self.assertLessEqual(_source_type_bias(item, parsed), 0.18)

    def test_source_type_bias_preserves_gmail_for_explicit_email_queries(self):
        parsed = query_parser.parse_query("email about seed deck")
        item = {
            "source_type": "gmail",
            "title": "Seed Deck Follow Up",
            "filename": "Email - Seed Deck Follow Up.eml",
            "summary": "Quick follow up on the seed deck",
            "preview_text": "Can you review the seed deck this week?",
            "sender_name": "Rahul Sharma",
            "sender_email": "rahul@acme.com",
            "importance_score": 0.72,
            "open_count": 0,
        }

        self.assertGreaterEqual(_source_type_bias(item, parsed), 0.9)
        self.assertEqual(
            _recent_visual_bonus(
                {"handler_type": "text"},
                visual_score=0.90,
                recency_score=0.90,
            ),
            0.0,
        )

    def test_source_type_bias_prefers_attachment_gmail_for_explicit_email_search(self):
        parsed = query_parser.parse_query("email seed deck")
        regular_item = {
            "source_type": "gmail",
            "title": "Seed Deck Follow Up",
            "filename": "Email - Seed Deck Follow Up.eml",
            "summary": "Quick follow up on the seed deck",
            "preview_text": "Can you review the seed deck this week?",
            "sender_name": "Rahul Sharma",
            "sender_email": "rahul@acme.com",
            "importance_score": 0.72,
            "open_count": 0,
            "keywords": '["seed deck"]',
        }
        attachment_item = {
            **regular_item,
            "keywords": '["seed deck","has_attachment","seed-deck.pdf"]',
        }

        self.assertGreaterEqual(_source_type_bias(attachment_item, parsed), _source_type_bias(regular_item, parsed))
        self.assertGreaterEqual(_source_type_bias(attachment_item, parsed), 0.9)

    def test_source_type_bias_keeps_gmail_attachment_low_for_generic_search(self):
        parsed = query_parser.parse_query("recurdo architecture")
        item = {
            "source_type": "gmail_attachment",
            "title": "Seed Deck",
            "filename": "seed-deck.pdf",
            "summary": "Investor seed deck attachment",
            "preview_text": "Seed deck for partner meeting",
            "sender_name": "Rahul Sharma",
            "sender_email": "rahul@acme.com",
            "importance_score": 0.76,
            "open_count": 0,
            "keywords": '["seed deck","has_attachment"]',
        }

        self.assertLessEqual(_source_type_bias(item, parsed), 0.40)

    def test_filename_score_keeps_partial_title_match_below_exact_filename_match(self):
        parsed = query_parser.parse_query("recurdo complete deep dive")
        partial_item = {
            "filename": "PROJECT_DEEP_DIVE.md",
            "title": "Recurdo / Memory Engine: Full Deep Dive",
        }
        exact_item = {
            "filename": "Recurdo_Complete_Deep_Dive.md",
            "title": "Recurdo - The Definitive Technical & Product Deep Dive",
        }

        self.assertGreater(_filename_score(exact_item, parsed), _filename_score(partial_item, parsed))
        self.assertLess(_filename_score(partial_item, parsed), 0.90)


if __name__ == "__main__":
    unittest.main()
