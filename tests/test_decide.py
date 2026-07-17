"""`decide()` is pure and total — same-tenant allow, cross-tenant deny, never raises."""

from __future__ import annotations

import uuid
from uuid import uuid4

from podium.auth import Actor, Resource, decide

_WS_A = uuid4()
_WS_B = uuid4()
_CMP_1 = uuid4()
_CMP_2 = uuid4()


def _actor(workspace_id: uuid.UUID, company_id: uuid.UUID | None = None) -> Actor:
    return Actor(
        workspace_id=workspace_id, company_id=company_id, actor_type="service", actor_id=uuid4()
    )


def test_same_tenant_is_allowed() -> None:
    resource = Resource(kind="company", workspace_id=_WS_A, company_id=_CMP_1)
    assert decide(_actor(_WS_A), "read", resource) is True


def test_cross_tenant_is_denied() -> None:
    resource = Resource(kind="company", workspace_id=_WS_B, company_id=_CMP_1)
    assert decide(_actor(_WS_A), "read", resource) is False


def test_company_scoped_actor_cannot_touch_another_company() -> None:
    actor = _actor(_WS_A, company_id=_CMP_1)
    assert decide(actor, "read", Resource("company", _WS_A, _CMP_1)) is True
    assert decide(actor, "read", Resource("company", _WS_A, _CMP_2)) is False


def test_decide_never_raises_on_degenerate_input() -> None:
    # Total function: empty action/kind and mismatched tenants deny, never raise.
    assert decide(_actor(_WS_A), "", Resource("", _WS_B)) is False
