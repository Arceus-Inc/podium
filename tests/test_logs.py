"""RunLogStore (file per run) + the excerpt policy that keeps big transcripts out of Postgres."""

from __future__ import annotations

from pathlib import Path

from podium.logs import RunLogStore, excerpt_payload


def test_append_read_and_digest(tmp_path: Path) -> None:
    store = RunLogStore(tmp_path)
    assert store.exists("run_1") is False
    store.append("run_1", "hello ")
    store.append("run_1", "world →")  # Unicode: the store is UTF-8
    assert store.read("run_1") == "hello world →"
    assert store.exists("run_1") is True
    size, sha = store.digest("run_1")
    assert size == len("hello world →".encode())
    assert len(sha) == 64  # sha256 hex


def test_ref_is_stable_per_run(tmp_path: Path) -> None:
    store = RunLogStore(tmp_path)
    assert store.ref("run_1") == store.ref("run_1")
    assert store.ref("run_1") != store.ref("run_2")


def test_excerpt_splits_long_text() -> None:
    payload = {"role": "generator", "text": "x" * 500}
    excerpted, full = excerpt_payload(payload, max_chars=100)
    assert full == "x" * 500  # full text returned for the log store
    assert excerpted["text"] == "x" * 100
    assert excerpted["text_truncated"] is True
    assert excerpted["role"] == "generator"  # other keys preserved
    assert payload["text"] == "x" * 500  # original not mutated


def test_excerpt_passes_short_text_through() -> None:
    payload = {"text": "short"}
    excerpted, full = excerpt_payload(payload, max_chars=100)
    assert full is None
    assert excerpted == payload


def test_excerpt_ignores_payloads_without_text() -> None:
    payload = {"status": "ok"}
    assert excerpt_payload(payload, max_chars=100) == (payload, None)
