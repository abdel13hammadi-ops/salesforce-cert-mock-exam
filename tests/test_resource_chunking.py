"""
Tests for deterministic official-resource text chunking.
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from workers.resource_chunking import (
    DEFAULT_TARGET_WORDS_PER_CHUNK,
    chunk_resource_content,
    normalize_line_endings,
    sha256_hex,
    word_count,
)


class TestResourceChunking(unittest.TestCase):
    def test_normalize_line_endings_windows_and_unix(self):
        unix = "line one\nline two\n\npara two"
        windows = "line one\r\nline two\r\n\r\npara two"
        cr = "line one\rline two\r\rpara two"
        self.assertEqual(normalize_line_endings(windows), unix)
        self.assertEqual(normalize_line_endings(cr), unix)

    def test_empty_input_rejected(self):
        for value in ("", "   ", "\n\n", "\r\n\r\n"):
            with self.subTest(value=repr(value)):
                with self.assertRaises(ValueError):
                    chunk_resource_content(value)

    def test_target_words_must_be_positive(self):
        with self.assertRaises(ValueError):
            chunk_resource_content("hello world", target_words_per_chunk=0)

    def test_deterministic_output_and_stable_hashes(self):
        text = "Alpha paragraph one.\n\nBeta paragraph two.\n\nGamma paragraph three."
        first = chunk_resource_content(text, target_words_per_chunk=5)
        second = chunk_resource_content(text, target_words_per_chunk=5)
        self.assertEqual(first.content_text, second.content_text)
        self.assertEqual(first.content_hash, second.content_hash)
        self.assertEqual(first.chunks, second.chunks)
        self.assertEqual(first.content_hash, sha256_hex(first.content_text))

    def test_paragraph_aware_splitting_keeps_paragraphs_together(self):
        text = (
            "one two three four five\n\n"
            "six seven eight nine ten\n\n"
            "eleven twelve thirteen fourteen fifteen"
        )
        chunked = chunk_resource_content(text, target_words_per_chunk=12)
        self.assertEqual(len(chunked.chunks), 2)
        self.assertIn("one two three four five", chunked.chunks[0]["chunk_text"])
        self.assertIn("six seven eight nine ten", chunked.chunks[0]["chunk_text"])
        self.assertIn("eleven twelve", chunked.chunks[1]["chunk_text"])

    def test_long_paragraph_fallback_splits_by_words(self):
        words = " ".join(f"w{i}" for i in range(1, 26))
        text = f"{words}\n\nshort tail"
        chunked = chunk_resource_content(text, target_words_per_chunk=10)
        first_words = word_count(chunked.chunks[0]["chunk_text"])
        self.assertLessEqual(first_words, 10)
        self.assertGreater(len(chunked.chunks), 1)
        self.assertTrue(all(chunk["chunk_text"].strip() for chunk in chunked.chunks))

    def test_offsets_reproduce_exact_chunk_text(self):
        text = "First paragraph words here.\n\nSecond paragraph continues here."
        chunked = chunk_resource_content(text, target_words_per_chunk=100)
        for chunk in chunked.chunks:
            start = chunk["start_offset"]
            end = chunk["end_offset"]
            self.assertEqual(
                chunk["chunk_text"],
                chunked.content_text[start:end],
            )
            self.assertEqual(chunk["content_hash"], sha256_hex(chunk["chunk_text"]))

    def test_no_empty_chunks(self):
        text = "A\n\nB\n\nC"
        chunked = chunk_resource_content(text, target_words_per_chunk=1)
        self.assertGreater(len(chunked.chunks), 0)
        for idx, chunk in enumerate(chunked.chunks):
            self.assertTrue(chunk["chunk_text"].strip(), f"chunk {idx} is empty")
            self.assertEqual(chunk["chunk_index"], idx)

    def test_configurable_target_size_changes_chunk_count(self):
        text = "\n\n".join(" ".join(f"word{i}" for i in range(20)) for _ in range(4))
        small = chunk_resource_content(text, target_words_per_chunk=10)
        large = chunk_resource_content(text, target_words_per_chunk=100)
        self.assertGreater(len(small.chunks), len(large.chunks))

    def test_default_target_words_matches_repository_default(self):
        self.assertEqual(DEFAULT_TARGET_WORDS_PER_CHUNK, 1200)

    def test_single_line_without_blank_lines_becomes_one_chunk(self):
        text = "Single paragraph without blank lines."
        chunked = chunk_resource_content(text)
        self.assertEqual(len(chunked.chunks), 1)
        self.assertEqual(chunked.chunks[0]["chunk_index"], 0)


if __name__ == "__main__":
    unittest.main()
