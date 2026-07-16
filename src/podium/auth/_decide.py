"""The central authorization function: pure, total, allow/deny. It NEVER raises.

The boundary (a router) turns a False into a 403. Keeping this a pure predicate means it is trivial
to unit-test exhaustively and impossible for an authz check to blow up a request.
"""

from __future__ import annotations

from podium.auth._actor import Actor, Resource


def decide(actor: Actor, action: str, resource: Resource) -> bool:
    """Tenant isolation is the M1 rule: an actor may only act within its own workspace, and a
    company-scoped key only on its own company. Richer per-action policy layers on later."""
    same_workspace = actor.workspace_id == resource.workspace_id
    company_ok = (
        actor.company_id is None
        or resource.company_id is None
        or actor.company_id == resource.company_id
    )
    return same_workspace and company_ok
