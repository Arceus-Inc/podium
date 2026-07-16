"""`decide()` is pure and total — same-tenant allow, cross-tenant deny, never raises."""

from __future__ import annotations

from podium.auth import Actor, Resource, decide


def _actor(workspace_id: str, company_id: str | None = None) -> Actor:
    return Actor(
        workspace_id=workspace_id, company_id=company_id, actor_type="service", actor_id="ak_1"
    )


def test_same_tenant_is_allowed() -> None:
    resource = Resource(kind="company", workspace_id="ws_a", company_id="cmp_1")
    assert decide(_actor("ws_a"), "read", resource) is True


def test_cross_tenant_is_denied() -> None:
    resource = Resource(kind="company", workspace_id="ws_b", company_id="cmp_1")
    assert decide(_actor("ws_a"), "read", resource) is False


def test_company_scoped_actor_cannot_touch_another_company() -> None:
    actor = _actor("ws_a", company_id="cmp_1")
    assert decide(actor, "read", Resource("company", "ws_a", "cmp_1")) is True
    assert decide(actor, "read", Resource("company", "ws_a", "cmp_2")) is False


def test_decide_never_raises_on_empty_input() -> None:
    assert decide(_actor(""), "", Resource("", "x")) is False
