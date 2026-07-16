# podium

The production composition root for the Arceus AI-company engine: `company.build()` assembles the
four engine repos — **dream** (agent harness) + **chorus** (org kernel) + **lattice** (learning
loop) + **horizon** (strategy) — into one wired object graph. This is the ONE production module
allowed to import both chorus and horizon; the four repos never import each other sideways
(CON-001), and the example scripts that used to hand-roll this wiring should import `company`
instead.

```python
from company import CompanyConfig, build

graph = build(CompanyConfig(
    api_key=..., base_url=..., deployment=...,
    workdir=Path(".company"), company_id="acme",
))
graph.org.hire(name="Bex", role="backend_engineer")
graph.horizon.start()
await graph.org.run()
```

`tests/test_company_wiring.py` is the wiring-parity guard: every execution seam (memory writer,
beat runner, landers, governance, delegation ports, shared ledger) is pinned there, so a seam
dropped anywhere in the four repos fails in CI instead of a live run.

Local dev: the four repos are sibling checkouts wired as editable path deps (see
`[tool.uv.sources]`). Run the gate with:

```bash
uv run ruff format . && uv run ruff check . && uv run mypy --strict && uv run pytest -q
```
