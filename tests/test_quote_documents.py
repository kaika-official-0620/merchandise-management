"""Regression tests for durable quote numbers in a disposable database."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import datetime
from pathlib import Path
import sqlite3
from tempfile import TemporaryDirectory
from threading import Barrier
from types import SimpleNamespace
import unittest

from quote_documents import allocate_quote_number, ensure_quote_number_schema


class QuoteDocumentsTest(unittest.TestCase):
    def setUp(self):
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / 'quotes.sqlite'
        self.now = datetime(2026, 11, 21, 10, 0)
        self.runtime = SimpleNamespace(DATABASE_URL='', get_db=self.connect)
        with closing(self.connect()) as conn, conn:
            conn.execute('CREATE TABLE user_mitsumori (document_no TEXT, status TEXT)')

    def connect(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def make_quote(self, *, prefix='UM', user_id=42, now=None):
        with closing(self.connect()) as conn, conn:
            number = allocate_quote_number(conn, conn.cursor(), now or self.now,
                                           postgres=False, user_id=user_id, prefix=prefix)
            conn.execute("INSERT INTO user_mitsumori VALUES (?, 'draft')", (number,))
            return number

    def remove_draft(self, number):
        with closing(self.connect()) as conn, conn:
            result = conn.execute("DELETE FROM user_mitsumori WHERE document_no=? AND status='draft'", (number,))
            self.assertEqual(result.rowcount, 1)

    def test_migration_preserves_existing_highest_even_before_first_new_save(self):
        kept = 'UM-202611-42-0011'
        draft = 'UM-202611-42-0023'
        with closing(self.connect()) as conn, conn:
            conn.executemany('INSERT INTO user_mitsumori VALUES (?, ?)', [(kept, 'issued'), (draft, 'draft')])
        ensure_quote_number_schema(self.runtime)
        self.remove_draft(draft)
        ensure_quote_number_schema(self.runtime)  # An app restart keeps its reservation.
        self.assertEqual(self.make_quote(), 'UM-202611-42-0024')
        with closing(self.connect()) as conn:
            self.assertEqual(conn.execute("SELECT document_no FROM user_mitsumori WHERE status='issued'").fetchone()[0], kept)

    def test_customer_and_staff_series_survive_draft_deletion(self):
        ensure_quote_number_schema(self.runtime)
        for prefix, first, second in [('UM', 'UM-202611-42-0001', 'UM-202611-42-0002'),
                                      ('MT', 'MT-20261121-001', 'MT-20261121-002'),
                                      ('KM', 'KM-20261121-001', 'KM-20261121-002')]:
            with self.subTest(prefix=prefix):
                self.assertEqual(self.make_quote(prefix=prefix), first)
                self.remove_draft(first)
                self.assertEqual(self.make_quote(prefix=prefix), second)
        self.assertEqual(self.make_quote(user_id=43), 'UM-202611-43-0001')
        self.assertEqual(self.make_quote(now=datetime(2026, 12, 1)), 'UM-202612-42-0001')

    def test_concurrent_saves_after_deletion_have_distinct_numbers(self):
        ensure_quote_number_schema(self.runtime)
        self.remove_draft(self.make_quote())
        started = Barrier(2, timeout=5)

        def save():
            started.wait()
            return self.make_quote()

        with ThreadPoolExecutor(max_workers=2) as pool:
            saves = [pool.submit(save) for _ in range(2)]
            self.assertEqual({result.result(timeout=10) for result in saves}, {'UM-202611-42-0002', 'UM-202611-42-0003'})

    def test_rolled_back_save_never_creates_duplicate_or_changes_documents(self):
        ensure_quote_number_schema(self.runtime)
        with closing(self.connect()) as conn:
            failed = allocate_quote_number(conn, conn.cursor(), self.now, postgres=False, user_id=42)
            conn.rollback()
        self.assertEqual(self.make_quote(), failed)
        self.remove_draft(failed)
        self.assertEqual(self.make_quote(), 'UM-202611-42-0002')
