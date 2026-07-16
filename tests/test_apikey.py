"""API-key hashing is stable, one-way, and fixed-width (never store the raw token)."""

from __future__ import annotations

from podium.auth import generate_token, hash_token


def test_hash_is_deterministic_and_hex() -> None:
    assert hash_token("secret") == hash_token("secret")
    assert hash_token("a") != hash_token("b")
    assert len(hash_token("x")) == 64  # sha256 hex


def test_generated_tokens_are_unique() -> None:
    assert generate_token() != generate_token()
