"""Per-company JWT: keys are derived per (instance, company); a token can't cross either boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import jwt
import pytest

from podium.auth import derive_signing_key, issue_company_token, verify_company_token

_MASTER = "master-secret"
_INSTANCE = "inst_1"


def test_key_derivation_is_deterministic_and_isolated() -> None:
    assert derive_signing_key(_MASTER, _INSTANCE, "cmp_a") == derive_signing_key(
        _MASTER, _INSTANCE, "cmp_a"
    )
    assert derive_signing_key(_MASTER, _INSTANCE, "cmp_a") != derive_signing_key(
        _MASTER, _INSTANCE, "cmp_b"
    )
    # Same company, different instance → different key (instance isolation).
    assert derive_signing_key(_MASTER, "inst_1", "cmp_a") != derive_signing_key(
        _MASTER, "inst_2", "cmp_a"
    )


def test_token_round_trips_under_its_own_company_key() -> None:
    key = derive_signing_key(_MASTER, _INSTANCE, "cmp_a")
    token = issue_company_token(company_id="cmp_a", subject="emp_1", signing_key=key)
    claims = verify_company_token(token, key)
    assert claims["company_id"] == "cmp_a"
    assert claims["sub"] == "emp_1"


def test_token_does_not_verify_under_another_companys_key() -> None:
    key_a = derive_signing_key(_MASTER, _INSTANCE, "cmp_a")
    key_b = derive_signing_key(_MASTER, _INSTANCE, "cmp_b")
    token = issue_company_token(company_id="cmp_a", subject="emp_1", signing_key=key_a)
    with pytest.raises(jwt.InvalidSignatureError):
        verify_company_token(token, key_b)


def test_expired_token_is_rejected() -> None:
    key = derive_signing_key(_MASTER, _INSTANCE, "cmp_a")
    issued = datetime.now(UTC) - timedelta(hours=2)
    token = issue_company_token(
        company_id="cmp_a", subject="emp_1", signing_key=key, ttl_seconds=1, issued_at=issued
    )
    with pytest.raises(jwt.ExpiredSignatureError):
        verify_company_token(token, key)
