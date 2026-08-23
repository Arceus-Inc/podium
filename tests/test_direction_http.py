"""Canonical HTTP reads over the persisted Horizon direction control-plane state."""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from horizon.generation import CandidateGoal, DirectionBrief
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import podium.db.metadata  # noqa: F401 -- register every product model before application startup
from podium.auth import create_api_key
from podium.companies import create_company
from podium.conductor.company import CompanyConfig, build
from podium.control import ControlPlaneProvider
from podium.control._direction import ProposalView
from podium.main import create_app
from podium.workspaces import create_workspace


def _postgres_dsn(database_url: str) -> str:
    return database_url.replace("+asyncpg", "").replace("://postgres@", "://podium_app@")


@pytest_asyncio.fixture
async def api(
    database_url: str,
    app_sessionmaker: async_sessionmaker[AsyncSession],
) -> AsyncIterator[httpx.AsyncClient]:
    app = create_app()
    app.state.sessionmaker = app_sessionmaker
    app.state.control_provider = ControlPlaneProvider(engine_dsn=_postgres_dsn(database_url))
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        yield client


async def _mint_company(
    sessionmaker: async_sessionmaker[AsyncSession], *, slug: str
) -> tuple[UUID, UUID, str]:
    async with sessionmaker() as session, session.begin():
        workspace = await create_workspace(session, name=slug.upper(), slug=slug)
        company = await create_company(
            session, workspace_id=workspace.id, slug=slug, name=slug.upper()
        )
        _, token = await create_api_key(session, workspace_id=workspace.id, name="direction-reader")
    return workspace.id, company.id, token


async def _mint_empty_company(
    sessionmaker: async_sessionmaker[AsyncSession], *, workspace_id: UUID, slug: str
) -> UUID:
    async with sessionmaker() as session, session.begin():
        company = await create_company(session, workspace_id=workspace_id, slug=slug, name=slug.upper())
    return company.id


def _write_conductor_direction(
    database_url: str,
    workdir: Path,
    company_id: UUID,
    *,
    decisions: int,
    include_proposal: bool,
    activate_first_decision: bool = False,
    reject_first_proposal: bool = False,
) -> tuple[list[str], list[str], list[str]]:
    """Write through the conductor, close it, then let a fresh HTTP plane read the result."""
    graph = build(
        CompanyConfig(
            api_key="test-key",
            base_url="https://example.invalid/openai/v1",
            deployment="gpt-test",
            workdir=workdir,
            company_id=company_id,
            ledger_dsn=_postgres_dsn(database_url),
        )
    )
    try:
        decision_ids: list[str] = []
        goal_ids: list[str] = []
        for number in range(decisions):
            decision = graph.horizon.propose_roadmap(
                f"Make renewal revenue predictable {number}",
                [
                    {
                        "title": f"Improve renewal playbooks {number}",
                        "metric": "Renewal rate",
                        "target": "90%",
                        "score": 0.8,
                    }
                ],
                rationale="Retention is the strongest durable growth signal.",
            )
            decision_ids.append(decision.id)
            goal_ids.append(decision.goal_ids[0])
        if activate_first_decision and decision_ids:
            graph.horizon.approve_roadmap(decision_ids[0])
        proposal_ids: list[str] = []
        if include_proposal:
            for number in range(3):
                proposal = graph.horizon.reconcile(
                    [
                        DirectionBrief(
                            candidate_id=f"candidate_{number}",
                            recommendation=f"Instrument churn risk before renewal {number}",
                            rationale="Early signals make renewal playbooks measurable.",
                            confidence=0.9,
                            risks=["Low coverage"],
                            candidate_goals=[
                                CandidateGoal(
                                    title=f"Ship churn-risk instrumentation {number}",
                                    metric="Coverage",
                                    target="95%",
                                    rationale="Measure the leading indicator.",
                                    score=0.7,
                                )
                            ],
                            evidence_refs=["evidence_1"],
                        )
                    ]
                )[0]
                proposal_ids.append(proposal.id)
        if reject_first_proposal and proposal_ids:
            graph.horizon.reject_proposal(proposal_ids[0], by="reviewer", reason="Not now")
        return decision_ids, goal_ids, proposal_ids
    finally:
        graph.close()


def _change_cursor_item_statuses(
    database_url: str,
    workdir: Path,
    company_id: UUID,
    *,
    decision_id: str,
    proposal_id: str,
) -> None:
    """Change both cursor rows through Horizon's public facade between HTTP page reads."""
    graph = build(
        CompanyConfig(
            api_key="test-key",
            base_url="https://example.invalid/openai/v1",
            deployment="gpt-test",
            workdir=workdir,
            company_id=company_id,
            ledger_dsn=_postgres_dsn(database_url),
        )
    )
    try:
        graph.horizon.approve_roadmap(decision_id)
        graph.horizon.reject_proposal(proposal_id, by="reviewer", reason="Cursor state changed")
    finally:
        graph.close()


def _write_strategy_records(
    database_url: str, workdir: Path, company_id: UUID, *, count: int
) -> list[str]:
    """Author one public Horizon roadmap containing enough goals to exercise the snapshot bound."""
    graph = build(
        CompanyConfig(
            api_key="test-key",
            base_url="https://example.invalid/openai/v1",
            deployment="gpt-test",
            workdir=workdir,
            company_id=company_id,
            ledger_dsn=_postgres_dsn(database_url),
        )
    )
    try:
        decision = graph.horizon.propose_roadmap(
            "Exercise the bounded strategy snapshot",
            [
                {
                    "title": f"Strategy record {number}",
                    "metric": "Coverage",
                    "target": "100%",
                    "score": 0.5,
                }
                for number in range(count)
            ],
        )
        return list(decision.goal_ids)
    finally:
        graph.close()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def test_direction_reads_are_persistent_and_expose_only_horizon_view_fields(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    workspace_id, company_id, token = await _mint_company(sessionmaker, slug="direction-live")
    decision_ids, goal_ids, proposal_ids = _write_conductor_direction(
        database_url, tmp_path / "conductor", company_id, decisions=1, include_proposal=True
    )
    base = f"/v1/workspaces/{workspace_id}/companies/{company_id}"

    decisions = await api.get(f"{base}/decisions", headers=_headers(token))
    assert decisions.status_code == 200
    body = decisions.json()
    assert set(body) == {"data", "meta", "links"}
    assert body["meta"] == {"next_cursor": None, "has_more": False}
    assert body["links"] == {"self": f"{base}/decisions?limit=50", "next": None}
    assert body["data"] == [
        {
            "id": decision_ids[0],
            "statement": "Make renewal revenue predictable 0",
            "status": "proposed",
            "owner": None,
            "rationale": "Retention is the strongest durable growth signal.",
            "goal_ids": [goal_ids[0]],
        }
    ]

    proposals = await api.get(f"{base}/direction-proposals?limit=1", headers=_headers(token))
    assert proposals.status_code == 200
    proposal_body = proposals.json()
    assert set(proposal_body) == {"data", "meta", "links"}
    assert proposal_body["data"][0]["id"] == proposal_ids[0]
    assert proposal_body["data"][0]["status"] == "proposed"
    assert proposal_body["data"][0]["decision_statement"] == "Instrument churn risk before renewal 0"
    assert proposal_body["data"][0]["brief"]["candidate_id"] == "candidate_0"
    created_at = datetime.fromisoformat(proposal_body["data"][0]["created_at"].replace("Z", "+00:00"))
    assert created_at.tzinfo is not None
    assert created_at.utcoffset() == UTC.utcoffset(created_at)
    proposal_cursor = proposal_body["meta"]["next_cursor"]
    assert isinstance(proposal_cursor, str)
    assert proposal_body["links"] == {
        "self": f"{base}/direction-proposals?limit=1",
        "next": f"{base}/direction-proposals?cursor={proposal_cursor}&limit=1",
    }
    next_proposals = await api.get(
        f"{base}/direction-proposals?cursor={proposal_cursor}&limit=1", headers=_headers(token)
    )
    assert next_proposals.status_code == 200
    assert [item["id"] for item in proposal_body["data"] + next_proposals.json()["data"]] == proposal_ids[:2]
    assert next_proposals.json()["meta"]["has_more"] is True

    final_cursor = next_proposals.json()["meta"]["next_cursor"]
    assert isinstance(final_cursor, str)
    final_proposals = await api.get(
        f"{base}/direction-proposals?cursor={final_cursor}&limit=1", headers=_headers(token)
    )
    assert final_proposals.status_code == 200
    assert [item["id"] for item in final_proposals.json()["data"]] == proposal_ids[2:]
    assert final_proposals.json()["meta"] == {"next_cursor": None, "has_more": False}


async def test_direction_lists_page_without_skips_or_duplicates_and_empty_company_is_enveloped(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    workspace_id, company_id, token = await _mint_company(sessionmaker, slug="direction-pages")
    decision_ids, _, _ = _write_conductor_direction(
        database_url, tmp_path / "conductor", company_id, decisions=3, include_proposal=False
    )
    base = f"/v1/workspaces/{workspace_id}/companies/{company_id}"

    first = await api.get(f"{base}/decisions?limit=2", headers=_headers(token))
    assert first.status_code == 200
    first_body = first.json()
    assert [item["id"] for item in first_body["data"]] == decision_ids[:2]
    assert first_body["meta"]["has_more"] is True
    cursor = first_body["meta"]["next_cursor"]
    assert isinstance(cursor, str)

    second = await api.get(f"{base}/decisions?cursor={cursor}&limit=2", headers=_headers(token))
    assert second.status_code == 200
    second_body = second.json()
    assert [item["id"] for item in second_body["data"]] == decision_ids[2:]
    assert second_body["meta"] == {"next_cursor": None, "has_more": False}
    paged_ids = [item["id"] for item in first_body["data"] + second_body["data"]]
    assert paged_ids == decision_ids
    assert len(paged_ids) == len(set(paged_ids))

    empty_company_id = await _mint_empty_company(
        sessionmaker, workspace_id=workspace_id, slug="direction-empty"
    )
    empty = await api.get(
        f"/v1/workspaces/{workspace_id}/companies/{empty_company_id}/decisions",
        headers=_headers(token),
    )
    assert empty.status_code == 200
    assert empty.json() == {
        "data": [],
        "meta": {"next_cursor": None, "has_more": False},
        "links": {"self": f"/v1/workspaces/{workspace_id}/companies/{empty_company_id}/decisions?limit=50", "next": None},
    }
    empty_proposals = await api.get(
        f"/v1/workspaces/{workspace_id}/companies/{empty_company_id}/direction-proposals",
        headers=_headers(token),
    )
    assert empty_proposals.status_code == 200
    assert empty_proposals.json() == {
        "data": [],
        "meta": {"next_cursor": None, "has_more": False},
        "links": {
            "self": f"/v1/workspaces/{workspace_id}/companies/{empty_company_id}/direction-proposals?limit=50",
            "next": None,
        },
    }


async def test_direction_read_validation_and_detail_etag(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    workspace_id, company_id, token = await _mint_company(sessionmaker, slug="direction-etag")
    decision_ids, _, _ = _write_conductor_direction(
        database_url, tmp_path / "conductor", company_id, decisions=2, include_proposal=False
    )
    base = f"/v1/workspaces/{workspace_id}/companies/{company_id}"

    page = await api.get(f"{base}/decisions?limit=1", headers=_headers(token))
    cursor = page.json()["meta"]["next_cursor"]
    assert isinstance(cursor, str)
    tampered = cursor[:-1] + ("A" if cursor[-1] != "A" else "B")
    for path in (
        f"{base}/decisions?cursor=not-a-cursor",
        f"{base}/decisions?cursor={tampered}",
        f"{base}/decisions?limit=0",
        f"{base}/decisions?limit=201",
        f"{base}/decisions?status=unknown",
        f"{base}/decisions?status=proposed&status=active",
        f"{base}/decisions?limit=1&limit=2",
        f"{base}/direction-proposals?unexpected=true",
        f"{base}/direction-proposals?status=active",
        f"{base}/direction-proposals?status=proposed&status=rejected",
        f"{base}/decisions/{decision_ids[0]}?unexpected=true",
        f"{base}/decisions/{'a' * 256}",
    ):
        response = await api.get(path, headers=_headers(token))
        assert response.status_code == 422
        assert response.headers["content-type"].startswith("application/problem+json")

    first = await api.get(f"{base}/decisions/{decision_ids[0]}", headers=_headers(token))
    assert first.status_code == 200
    assert set(first.json()) == {"data", "links"}
    assert first.json()["links"] == {"self": f"{base}/decisions/{decision_ids[0]}"}
    assert first.headers["etag"].startswith('"')
    matched = await api.get(
        f"{base}/decisions/{decision_ids[0]}",
        headers={**_headers(token), "If-None-Match": first.headers["etag"]},
    )
    assert matched.status_code == 304
    assert matched.headers["etag"] == first.headers["etag"]
    assert matched.content == b""


async def test_direction_status_filters_page_after_filtering_and_preserve_links(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    workspace_id, company_id, token = await _mint_company(sessionmaker, slug="direction-status")
    decision_ids, _, proposal_ids = _write_conductor_direction(
        database_url,
        tmp_path / "conductor",
        company_id,
        decisions=3,
        include_proposal=True,
        activate_first_decision=True,
        reject_first_proposal=True,
    )
    base = f"/v1/workspaces/{workspace_id}/companies/{company_id}"

    first_decisions = await api.get(
        f"{base}/decisions?status=proposed&limit=1", headers=_headers(token)
    )
    assert first_decisions.status_code == 200
    decision_body = first_decisions.json()
    assert [item["id"] for item in decision_body["data"]] == [decision_ids[1]]
    decision_cursor = decision_body["meta"]["next_cursor"]
    assert isinstance(decision_cursor, str)
    assert decision_body["links"] == {
        "self": f"{base}/decisions?status=proposed&limit=1",
        "next": f"{base}/decisions?status=proposed&cursor={decision_cursor}&limit=1",
    }

    first_proposals = await api.get(
        f"{base}/direction-proposals?status=proposed&limit=1", headers=_headers(token)
    )
    assert first_proposals.status_code == 200
    proposal_body = first_proposals.json()
    assert [item["id"] for item in proposal_body["data"]] == [proposal_ids[1]]
    proposal_cursor = proposal_body["meta"]["next_cursor"]
    assert isinstance(proposal_cursor, str)
    assert proposal_body["links"]["next"] == (
        f"{base}/direction-proposals?status=proposed&cursor={proposal_cursor}&limit=1"
    )

    _change_cursor_item_statuses(
        database_url,
        tmp_path / "cursor-status-change",
        company_id,
        decision_id=decision_ids[1],
        proposal_id=proposal_ids[1],
    )

    next_decisions = await api.get(decision_body["links"]["next"], headers=_headers(token))
    assert next_decisions.status_code == 200
    assert [item["id"] for item in next_decisions.json()["data"]] == [decision_ids[2]]
    assert next_decisions.json()["meta"] == {"next_cursor": None, "has_more": False}

    next_proposals = await api.get(proposal_body["links"]["next"], headers=_headers(token))
    assert next_proposals.status_code == 200
    assert [item["id"] for item in next_proposals.json()["data"]] == [proposal_ids[2]]


async def test_strategy_snapshot_reads_typed_horizon_state_and_rejects_queries(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    workspace_id, company_id, token = await _mint_company(sessionmaker, slug="direction-strategy")
    _, goal_ids, _ = _write_conductor_direction(
        database_url, tmp_path / "conductor", company_id, decisions=2, include_proposal=False
    )
    path = f"/v1/workspaces/{workspace_id}/companies/{company_id}/strategy"

    response = await api.get(path, headers=_headers(token))
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"data", "meta", "links"}
    assert body["meta"] == {"truncated": False, "total": 2, "limit": 200}
    assert body["links"] == {"self": path}
    assert [item["goal_id"] for item in body["data"]] == goal_ids
    assert [item["title"] for item in body["data"]] == [
        "Improve renewal playbooks 0",
        "Improve renewal playbooks 1",
    ]
    assert body["data"][0]["metric"] == "Renewal rate"
    assert body["data"][0]["target"] == "90%"

    truncated_company_id = await _mint_empty_company(
        sessionmaker, workspace_id=workspace_id, slug="direction-strategy-bounded"
    )
    all_goal_ids = _write_strategy_records(
        database_url,
        tmp_path / "bounded-conductor",
        truncated_company_id,
        count=201,
    )
    truncated_path = (
        f"/v1/workspaces/{workspace_id}/companies/{truncated_company_id}/strategy"
    )
    truncated = await api.get(truncated_path, headers=_headers(token))
    assert truncated.status_code == 200
    truncated_body = truncated.json()
    assert truncated_body["meta"] == {"truncated": True, "total": 201, "limit": 200}
    assert len(truncated_body["data"]) == 200
    assert [item["goal_id"] for item in truncated_body["data"]] == all_goal_ids[:200]
    assert all_goal_ids[200] not in {item["goal_id"] for item in truncated_body["data"]}
    assert truncated_body["links"] == {"self": truncated_path}

    for query in ("?limit=1", "?view=full&view=compact"):
        invalid = await api.get(f"{path}{query}", headers=_headers(token))
        assert invalid.status_code == 422
        assert invalid.headers["content-type"].startswith("application/problem+json")


def test_proposal_view_requires_utc_timestamps_and_serializes_them() -> None:
    def proposal_view(*, created_at: datetime, decided_at: datetime | None) -> ProposalView:
        return ProposalView(
            id="proposal_1",
            status="proposed",
            brief=None,
            decision_statement="Ship a direction read API",
            decision_rationale="Users need durable control-plane reads.",
            created_at=created_at,
            decided_by=None,
            decided_at=decided_at,
            linked_decision_id=None,
            note="",
        )

    with pytest.raises(ValidationError, match="timestamp must be UTC"):
        proposal_view(created_at=datetime(2026, 8, 9, 10), decided_at=None)
    with pytest.raises(ValidationError, match="timestamp must be UTC"):
        proposal_view(
            created_at=datetime(2026, 8, 9, 10, tzinfo=UTC),
            decided_at=datetime(2026, 8, 9, 11, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        )

    view = proposal_view(
        created_at=datetime(2026, 8, 9, 10, tzinfo=UTC),
        decided_at=datetime(2026, 8, 9, 11, tzinfo=UTC),
    )
    assert view.model_dump(mode="json")["created_at"] == "2026-08-09T10:00:00Z"
    assert view.model_dump(mode="json")["decided_at"] == "2026-08-09T11:00:00Z"


async def test_direction_missing_and_foreign_tenant_resources_are_opaque_404s(
    database_url: str,
    sessionmaker: async_sessionmaker[AsyncSession],
    api: httpx.AsyncClient,
    tmp_path: Path,
) -> None:
    workspace_id, company_id, token = await _mint_company(sessionmaker, slug="direction-owner")
    decision_ids, _, _ = _write_conductor_direction(
        database_url, tmp_path / "conductor", company_id, decisions=1, include_proposal=False
    )
    foreign_workspace_id, _, foreign_token = await _mint_company(sessionmaker, slug="direction-foreign")
    base = f"/v1/workspaces/{workspace_id}/companies/{company_id}"

    missing = await api.get(f"{base}/decisions/missing", headers=_headers(token))
    foreign = await api.get(
        f"{base}/decisions/{decision_ids[0]}", headers=_headers(foreign_token)
    )
    foreign_strategy = await api.get(f"{base}/strategy", headers=_headers(foreign_token))
    assert foreign_workspace_id != workspace_id
    for response in (missing, foreign, foreign_strategy):
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("application/problem+json")
        assert response.json()["detail"] == "resource not found"
