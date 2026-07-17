"""The dev bootstrap door — one click mints a playground (workspace + company + token).

Privileged by nature (workspaces are FORCE-RLS; only the control connection can create them),
so it exists ONLY when the lifespan wired ``app.state.bootstrap_sessionmaker`` — which happens
solely under ``PODIUM_DEV_BOOTSTRAP=1``. Absent that, the route 404s: no privileged door in
production by default, ever.
"""

from __future__ import annotations

import secrets

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from podium.auth import create_api_key
from podium.companies import create_company
from podium.workspaces import create_workspace

router = APIRouter(prefix="/v1/dev", tags=["dev"])


class BootstrapRequest(BaseModel):
    name: str = "playground"


class BootstrapOut(BaseModel):
    workspace_id: str
    company_id: str
    token: str


def _slug(name: str) -> str:
    base = "".join(ch if ch.isalnum() else "-" for ch in name.lower()).strip("-") or "playground"
    return f"{base[:24]}-{secrets.token_hex(3)}"  # collision-proof across repeated clicks


@router.post("/bootstrap", status_code=201, response_model=BootstrapOut)
async def bootstrap(request: Request, body: BootstrapRequest) -> BootstrapOut:
    sessionmaker = getattr(request.app.state, "bootstrap_sessionmaker", None)
    if sessionmaker is None:
        raise HTTPException(status_code=404, detail="not found")  # gate closed — invisible
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name=body.name, slug=_slug(body.name))
        company = await create_company(
            session, workspace_id=workspace.id, slug=_slug(body.name), name=body.name
        )
        _, token = await create_api_key(session, workspace_id=workspace.id, name="dev-bootstrap")
        return BootstrapOut(workspace_id=str(workspace.id), company_id=str(company.id), token=token)


__all__ = ["router"]
