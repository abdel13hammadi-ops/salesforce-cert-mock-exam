"""
Deterministic text chunking for official resource ingestion.

Splits normalized UTF-8 plain text or Markdown into paragraph-aware chunks
compatible with ``ingest_resource_version_v1``. No third-party dependencies.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

DEFAULT_TARGET_WORDS_PER_CHUNK = 1200

_WORD_RE = re.compile(r"\S+")


@dataclass(frozen=True)
class ChunkedResource:
    """Chunked resource content ready for resource_ingestion payloads."""

    content_text: str
    content_hash: str
    chunks: List[Dict[str, Any]]


def normalize_line_endings(text: str) -> str:
    """Normalize CRLF and CR line endings to LF."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def sha256_hex(text: str) -> str:
    """Return SHA-256 hex digest of UTF-8 encoded text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def word_count(text: str) -> int:
    """Count whitespace-delimited words."""
    return len(_WORD_RE.findall(text))


def _paragraph_spans(text: str) -> List[Tuple[int, int]]:
    """Return start/end spans for non-empty paragraphs separated by blank lines."""
    spans: List[Tuple[int, int]] = []
    n = len(text)
    i = 0
    while i < n:
        while i < n and text[i] == "\n":
            i += 1
        if i >= n:
            break
        start = i
        while i < n:
            if text[i] == "\n":
                j = i + 1
                while j < n and text[j] == "\n":
                    j += 1
                if j > i + 1:
                    break
            i += 1
        end = i
        if text[start:end].strip():
            spans.append((start, end))
    return spans


def _split_span_by_words(
    text: str,
    start: int,
    end: int,
    target_words: int,
) -> List[Tuple[int, int]]:
    """Split a long paragraph span into word-bounded chunk spans."""
    sub = text[start:end]
    matches = list(_WORD_RE.finditer(sub))
    if not matches:
        raise ValueError("paragraph span contains no words")

    spans: List[Tuple[int, int]] = []
    i = 0
    while i < len(matches):
        j = i
        count = 0
        while j < len(matches) and count < target_words:
            count += 1
            j += 1
        chunk_start = start + matches[i].start()
        chunk_end = start + matches[j - 1].end()
        spans.append((chunk_start, chunk_end))
        i = j
    return spans


def _pack_paragraph_spans(
    text: str,
    paragraph_spans: List[Tuple[int, int]],
    target_words: int,
) -> List[Tuple[int, int]]:
    """Greedy paragraph packing with word-bounded fallback for long paragraphs."""
    chunk_spans: List[Tuple[int, int]] = []
    i = 0
    while i < len(paragraph_spans):
        para_start, para_end = paragraph_spans[i]
        para_words = word_count(text[para_start:para_end])

        if para_words > target_words:
            chunk_spans.extend(
                _split_span_by_words(text, para_start, para_end, target_words)
            )
            i += 1
            continue

        chunk_start = para_start
        chunk_end = para_end
        total_words = para_words
        j = i + 1
        while j < len(paragraph_spans):
            next_start, next_end = paragraph_spans[j]
            next_words = word_count(text[next_start:next_end])
            if total_words + next_words > target_words and total_words > 0:
                break
            chunk_end = next_end
            total_words += next_words
            j += 1

        chunk_spans.append((chunk_start, chunk_end))
        i = j

    return chunk_spans


def chunk_resource_content(
    text: str,
    *,
    target_words_per_chunk: int = DEFAULT_TARGET_WORDS_PER_CHUNK,
) -> ChunkedResource:
    """
    Normalize, hash, and chunk resource text deterministically.

    Raises ValueError when content is empty/whitespace-only or target size is invalid.
    """
    if target_words_per_chunk <= 0:
        raise ValueError("target_words_per_chunk must be > 0")

    normalized = normalize_line_endings(text)
    if not normalized.strip():
        raise ValueError("content must not be empty or whitespace-only")

    paragraph_spans = _paragraph_spans(normalized)
    if not paragraph_spans:
        raise ValueError("content must not be empty or whitespace-only")

    content_hash = sha256_hex(normalized)
    span_chunks = _pack_paragraph_spans(
        normalized,
        paragraph_spans,
        target_words_per_chunk,
    )

    chunks: List[Dict[str, Any]] = []
    for idx, (start, end) in enumerate(span_chunks):
        chunk_text = normalized[start:end]
        if not chunk_text.strip():
            raise ValueError(f"chunk {idx} is empty after splitting")
        chunks.append({
            "chunk_index": idx,
            "chunk_text": chunk_text,
            "content_hash": sha256_hex(chunk_text),
            "start_offset": start,
            "end_offset": end,
        })

    return ChunkedResource(
        content_text=normalized,
        content_hash=content_hash,
        chunks=chunks,
    )
