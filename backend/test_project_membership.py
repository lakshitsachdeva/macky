from __future__ import annotations

from pathlib import Path
import sqlite3
import tempfile
import unittest

from backend.graph.store import GraphStore
from scripts.extract_project_membership import apply_project_membership


def _create_memory_db(path: Path, project_root: Path) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE items (
            id TEXT PRIMARY KEY,
            filename TEXT,
            title TEXT,
            file_type TEXT,
            handler_type TEXT,
            source_type TEXT,
            original_path TEXT,
            sender_name TEXT,
            sender_email TEXT,
            preview_text TEXT,
            extracted_text TEXT,
            summary TEXT,
            keywords TEXT,
            doc_type_guess TEXT,
            first_seen_at TEXT,
            created_at TEXT,
            modified_at TEXT,
            last_opened_at TEXT,
            open_count INTEGER DEFAULT 0,
            total_dwell_seconds INTEGER DEFAULT 0,
            copy_event_count INTEGER DEFAULT 0,
            is_excluded INTEGER DEFAULT 0
        )
        """
    )
    rows = []
    for index in range(3):
        rows.append(
            (
                f"item-{index}",
                f"doc-{index}.md",
                f"Recurdo plan {index}",
                "md",
                "text",
                "local",
                str(project_root / "Desktop" / "Projects" / "Recurdo" / f"doc-{index}.md"),
                None,
                None,
                "Lakshit Sachdeva planning notes",
                (
                    "Lakshit Sachdeva wrote the Recurdo roadmap and Lakshit Sachdeva "
                    "reviewed milestones with Rahul Sharma. "
                    "This text is long enough to keep extraction consistent."
                ),
                "Recurdo planning note",
                '["recurdo","roadmap"]',
                "proposal",
                "2026-04-01T10:00:00+00:00",
                "2026-04-01T10:00:00+00:00",
                "2026-04-01T10:00:00+00:00",
                None,
                0,
                0,
                0,
            )
        )

    conn.executemany(
        """
        INSERT INTO items (
            id, filename, title, file_type, handler_type, source_type, original_path,
            sender_name, sender_email, preview_text, extracted_text, summary, keywords,
            doc_type_guess, first_seen_at, created_at, modified_at, last_opened_at,
            open_count, total_dwell_seconds, copy_event_count, is_excluded
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)
        """,
        rows,
    )
    conn.commit()
    conn.close()


class ProjectMembershipExtractionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.base = Path(self.temp_dir.name)
        self.memory_db = self.base / "memory.db"
        self.graph_db = self.base / "graph.db"
        _create_memory_db(self.memory_db, self.base)

    def test_dry_run_detects_project_folder(self) -> None:
        summary = apply_project_membership(
            memory_db=str(self.memory_db),
            graph_db=str(self.graph_db),
            home_dir=self.base,
            execute=False,
        )

        self.assertEqual(summary["project_folders"], 1)
        self.assertEqual(summary["eligible_project_folders"], 1)
        self.assertEqual(summary["project_entities_created"], 0)

    def test_execute_creates_about_and_contributed_to_links(self) -> None:
        summary = apply_project_membership(
            memory_db=str(self.memory_db),
            graph_db=str(self.graph_db),
            home_dir=self.base,
            execute=True,
        )

        self.assertEqual(summary["project_entities_created"], 1)
        self.assertEqual(summary["about_facts_created"], 3)
        self.assertGreaterEqual(summary["contributed_to_facts_created"], 1)

        store = GraphStore(self.graph_db)
        project = store.get_entity_by_name("Recurdo")
        self.assertIsNotNone(project)
        assert project is not None

        with store.get_connection() as conn:
            about_count = conn.execute(
                """
                SELECT COUNT(*)
                FROM episode_mentions
                WHERE entity_id = ?
                  AND relation_type = 'ABOUT'
                """,
                (project["id"],),
            ).fetchone()[0]
        self.assertEqual(about_count, 3)
