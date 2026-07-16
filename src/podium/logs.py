"""Durable run-log store — transcripts live in files, not Postgres (paperclip's "blobs out" split).

A run's full transcript is appended to one file per run; the DB keeps only `runs.log_ref` (a pointer)
and the events table keeps short excerpts. `excerpt_payload` is the policy that decides what stays in
the row vs. what goes to the file.

DEPLOYMENT CONSTRAINT: the conductor WRITES these files and the api READS them, so the two must share
the `log_dir` filesystem (dev-embedded, or a shared volume). Across hosts without shared storage the
`/logs` endpoint 404s — the deferred object-store (S3) mirror is what removes this constraint; this
local file is the fast-tail primary until then.

ponytail: synchronous file I/O — transcripts are small and appended in short bursts; wrap in
`asyncio.to_thread` only if a single append ever gets large enough to stall the loop.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


class RunLogStore:
    """One append-only UTF-8 file per run under `root`, addressed by run id."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def _path(self, run_id: str) -> Path:
        # Defense-in-depth: run ids are system-minted, but never let one escape the root.
        if "/" in run_id or "\\" in run_id or ".." in run_id:
            raise ValueError(f"unsafe run id: {run_id!r}")
        return self._root / f"{run_id}.log"

    def ref(self, run_id: str) -> str:
        """The pointer stored in `runs.log_ref` (a root-relative path)."""
        return f"{run_id}.log"

    def exists(self, run_id: str) -> bool:
        return self._path(run_id).exists()

    def append(self, run_id: str, text: str) -> None:
        path = self._path(run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(text)

    def read(self, run_id: str) -> str:
        return self._path(run_id).read_text(encoding="utf-8")

    def load(self, run_id: str) -> tuple[bytes, str]:
        """(raw bytes, sha256 hex) in a single read — for serving with an integrity header."""
        data = self._path(run_id).read_bytes()
        return data, hashlib.sha256(data).hexdigest()

    def digest(self, run_id: str) -> tuple[int, str]:
        """(byte length, sha256 hex)."""
        data, sha = self.load(run_id)
        return len(data), sha


def excerpt_payload(
    payload: dict[str, Any], *, max_chars: int
) -> tuple[dict[str, Any], str | None]:
    """Split a text event: return (payload-for-the-row, full-text-for-the-log-or-None).

    A short (or textless) payload passes through unchanged. A long `text` is truncated in the returned
    payload (with a `text_truncated` flag) and its full value is returned to be appended to the log.
    The input is never mutated. Scope: only the `text` field is excerpted (the transcript field of
    `run.text`); other large fields (e.g. a `run.tool_result` body) still land in the row — extend
    here if tool outputs grow large enough to bloat the events table.
    """
    text = payload.get("text")
    if not isinstance(text, str) or len(text) <= max_chars:
        return payload, None
    excerpted = {**payload, "text": text[:max_chars], "text_truncated": True}
    return excerpted, text
