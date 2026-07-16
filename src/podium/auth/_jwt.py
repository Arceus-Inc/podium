"""Per-company JWTs. The signing key is derived per (instance, company) from a master secret, so a
leaked token verifies only under its own company's key — it cannot cross tenants or instances.
"""

from __future__ import annotations

import hashlib
import hmac
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

_ALGORITHM = "HS256"
_ISSUER = "podium"


def derive_signing_key(master_secret: str, instance_id: str, company_id: str) -> str:
    message = f"jwt:{instance_id}:{company_id}".encode()
    return hmac.new(master_secret.encode("utf-8"), message, hashlib.sha256).hexdigest()


def issue_company_token(
    *,
    company_id: str,
    subject: str,
    signing_key: str,
    ttl_seconds: int = 3600,
    issued_at: datetime | None = None,
) -> str:
    issued = issued_at or datetime.now(UTC)
    claims = {
        "iss": _ISSUER,
        "sub": subject,
        "company_id": company_id,
        "iat": issued,
        "exp": issued + timedelta(seconds=ttl_seconds),
    }
    return jwt.encode(claims, signing_key, algorithm=_ALGORITHM)


def verify_company_token(token: str, signing_key: str) -> dict[str, Any]:
    """Decode and verify signature + expiry + issuer. Raises jwt.PyJWTError on any failure."""
    return jwt.decode(token, signing_key, algorithms=[_ALGORITHM], issuer=_ISSUER)
