"""Public run DTOs are explicit, immutable, and reject engine-private fields."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from podium.runs.schemas import RunCounts, RunCreate, RunParams


def test_run_create_builds_typed_params_without_optional_fields() -> None:
    params = RunCreate(directive="ship it", idempotency_key="k").params()

    assert isinstance(params, RunParams)
    assert params.model_dump(exclude_none=True) == {"execution_mode": "delivery"}


def test_run_dtos_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        RunParams.model_validate({"execution_mode": "delivery", "engine_private": "secret"})
    with pytest.raises(ValidationError):
        RunCounts.model_validate({"events": 1, "engine_private": 1})
    with pytest.raises(ValidationError):
        RunCreate.model_validate(
            {"directive": "ship it", "idempotency_key": "k", "engine_private": "secret"}
        )
