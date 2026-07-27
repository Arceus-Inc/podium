"""Focused live e2e for the one-mind-one-ledger thesis.

Bootstraps a company, submits ONE formation run (the CEO's beat), auto-approves the workforce plan, and
then inspects horizon's own store: did the CEO — the only mind — AUTHOR the roadmap (a proposed decision
whose goals carry a metric + target), held deterministically in horizon (the ledger)? No hardcoded
roadmap, no operator. Prints a step-by-step trace and a final verdict.

Env: PODIUM_BASE (default http://127.0.0.1:8901), PODIUM_WORKDIR (where horizon's decisions.json lands),
OPERATOR_LEDGER_DSN / PODIUM_ENGINE_LEDGER_DSN (asyncpg, to read authored goals).
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from pathlib import Path

import asyncpg
import httpx

BASE = os.environ.get("PODIUM_BASE", "http://127.0.0.1:8901")
_ROOT = Path(__file__).resolve().parent.parent
WORKDIR = Path(os.environ.get("PODIUM_WORKDIR", str(_ROOT / ".podium" / "workdir")))
DSN = os.environ.get(
    "OPERATOR_LEDGER_DSN",
    os.environ.get("PODIUM_ENGINE_LEDGER_DSN", "postgresql://postgres@127.0.0.1:5432/podium"),
)
DEADLINE_S = int(os.environ.get("E2E_DEADLINE_S", "1200"))

MISSION = (
    "Found and operate 'Lumen', a calm-productivity startup. Build a small suite of privacy-first web "
    "apps that help people focus: a distraction-free markdown notes app, a Pomodoro focus timer, and a "
    "daily habit tracker, on a shared calm design system. Staff the company to design, build, and test "
    "these in parallel, with a lead coordinating the team."
)


def log(msg: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as http:
        # 1) bootstrap a fresh company
        r = await http.post("/v1/dev/bootstrap", json={"name": "lumen"})
        r.raise_for_status()
        d = r.json()
        ws, co, token = d["workspace_id"], d["company_id"], d["token"]
        headers = {"Authorization": f"Bearer {token}"}
        log(f"bootstrapped company {co}")

        # 2) submit ONE formation run — the CEO proposes the workforce AND the roadmap
        body = {
            "directive": MISSION,
            "idempotency_key": str(uuid.uuid4()),
            "execution_mode": "formation",
        }
        r = await http.post(f"/v1/companies/{co}/runs", headers=headers, json=body)
        r.raise_for_status()
        rid = r.json()["id"]
        log(f"submitted formation run {rid}")

        # 3) watch it; auto-approve any workforce plan the CEO proposes
        approved: set[str] = set()
        status = "running"
        deadline = time.time() + DEADLINE_S
        while time.time() < deadline:
            await asyncio.sleep(5.0)
            plans = (
                await http.get(f"/v1/workspaces/{ws}/companies/{co}/plans", headers=headers)
            ).json() or []
            for p in plans:
                if p.get("status") == "proposed" and p["id"] not in approved:
                    approved.add(p["id"])
                    await http.post(
                        f"/v1/workspaces/{ws}/companies/{co}/plans/{p['id']}/approve",
                        headers=headers,
                    )
                    log(f"approved workforce plan {p['id'][:8]} (+{len(p.get('employees', []))} hires)")
            st = (await http.get(f"/v1/companies/{co}/runs/{rid}", headers=headers)).json()
            if st["status"] != status:
                status = st["status"]
                log(f"formation status -> {status}")
            if status in ("succeeded", "failed", "canceled", "timed_out"):
                break

        log(f"formation ended: {status}")

    # 4) inspect horizon's own store — did the CEO author a roadmap?
    return await _inspect(co)


async def _inspect(co: str) -> int:
    log("=== ONE MIND, ONE LEDGER — inspection ===")
    ok = True

    dec_path = WORKDIR / co / "decisions.json"
    # The roadmap is written server-side during the beat; its decisions.json flush can lag a moment
    # behind the run flipping to `succeeded`. Poll briefly so the verdict reflects settled state, not a
    # read that raced the write.
    decisions: dict = {}
    for _ in range(30):
        if dec_path.exists():
            decisions = json.loads(dec_path.read_text(encoding="utf-8"))
            if any(d.get("status") in ("proposed", "active") for d in decisions.values()):
                break
        await asyncio.sleep(0.5)
    log(f"horizon decisions.json: {len(decisions)} decision(s) at {dec_path}")
    for dec in decisions.values():
        log(
            f"  decision [{dec.get('id')}] status={dec.get('status')} "
            f"goals={len(dec.get('goal_ids', []))} :: {dec.get('statement', '')[:70]}"
        )

    strat_path = WORKDIR / co / "strategy.json"
    strategy = {}
    if strat_path.exists():
        strategy = json.loads(strat_path.read_text(encoding="utf-8"))
    authored = [s for s in strategy.values() if s.get("decision_id")]
    log(f"horizon strategy.json: {len(strategy)} goal record(s), {len(authored)} decision-linked")
    for s in authored:
        log(
            f"  goal '{s.get('title', '')[:44]}' metric={s.get('metric')!r} "
            f"target={s.get('target')!r} score={s.get('score')}"
        )

    # cross-check the authored goals landed in the chorus ledger too
    conn = await asyncpg.connect(DSN)
    try:
        rows = await conn.fetch(
            "select title,status from goal where company_id=$1 order by created_at", uuid.UUID(co)
        )
        log(f"chorus ledger: {len(rows)} goal row(s)")
        for row in rows[:12]:
            log(f"  [{row['status']}] {row['title'][:60]}")
    finally:
        await conn.close()

    # verdict
    proposed = [d for d in decisions.values() if d.get("status") in ("proposed", "active")]
    goals_have_metrics = authored and all(s.get("metric") and s.get("target") for s in authored)
    log("--- VERDICT ---")
    if proposed and authored and goals_have_metrics:
        log("PASS: the CEO (one mind) AUTHORED a roadmap held in horizon (one ledger) —")
        log(f"      {len(proposed)} decision(s), {len(authored)} measurable goal(s). No hardcoded list.")
    else:
        ok = False
        log("FAIL: no CEO-authored roadmap found in horizon.")
        if not proposed:
            log("      - no proposed/active decision in decisions.json")
        if not authored:
            log("      - no decision-linked goals in strategy.json")
        if authored and not goals_have_metrics:
            log("      - authored goals are missing metric/target")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
