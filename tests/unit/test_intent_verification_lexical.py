"""Unit tests for :mod:`patchfrog.intent_verification.lexical` -- the
shared deterministic tokenizer bounded-lexical-overlap matching is built
on (spec section 8: no embeddings, no vector database)."""

from __future__ import annotations

from patchfrog.intent_verification.lexical import meaningful_tokens, tokenize


def test_snake_case_split() -> None:
    assert tokenize("is_duplicate_payment") == {"is", "duplicate", "payment"}


def test_camel_case_split() -> None:
    assert tokenize("RetryWorker") == {"retry", "worker"}


def test_file_path_split() -> None:
    assert tokenize("retry_worker.py") == {"retry", "worker", "py"}


def test_meaningful_tokens_drops_short_and_stopwords() -> None:
    tokens = meaningful_tokens("is_duplicate_payment")
    assert "is" not in tokens  # too short / stopword
    assert "duplicate" in tokens
    assert "payment" in tokens


def test_meaningful_tokens_drops_py_extension_and_test_words() -> None:
    tokens = meaningful_tokens("test_service.py")
    assert "test" not in tokens
    assert "py" not in tokens
    assert "service" in tokens


def test_prose_and_identifier_share_a_token() -> None:
    prose_tokens = meaningful_tokens("Prevent duplicate webhook payment processing")
    identifier_tokens = meaningful_tokens("process_payment")
    assert prose_tokens & identifier_tokens == {"payment"}
