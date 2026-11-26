# Contributing

Thanks for helping improve the harness! This guide gets you from clone to merged PR.

## 1. Set up (about 2 minutes)

```bash
git clone https://github.com/Wareeday/llmops-eval-harness.git && cd llmops-eval-harness
python -m venv .venv && source .venv/bin/activate        # Windows: .venv\Scripts\activate
make install                                              # runtime + dev deps + pre-commit hooks
make check                                                # lint + types + tests (>= 90% coverage)
```

No `make`? Run `pip install -r requirements-dev.txt`, then `ruff check . && ruff format --check . && mypy && pytest`.

## 2. Workflow
1. Open an issue first for anything bigger than a small fix, so we can agree on the approach.
2. Branch from `main`: `feat/<topic>` or `fix/<topic>`.
3. Make focused commits - [Conventional Commits](https://www.conventionalcommits.org) (`feat:`, `fix:`, `docs:`, `test:`, `chore:`) keep the changelog easy.
4. `make check` must pass. Add or update tests - bug fixes need a regression test.
5. Update `docs/` and `CHANGELOG.md` (under **Unreleased**) for user-visible changes.
6. Open a PR using the template. CI runs lint, mypy, tests on Python 3.10-3.13, a dependency audit, the Docker build and the quality gate.

## 3. Project layout

```
app/api/          routes, auth dependency, middleware
app/core/         settings (pydantic-settings), logging
app/models/       pydantic schemas
app/services/     cache, evaluators, target adapter, runner, observability, reporting, telemetry
datasets/         benchmark suites (JSON)
examples/         reference agent implementing the HTTP contract
frontend/         single-file dashboard
tests/            pytest suite (mirrors app/)
docs/             architecture, API, metrics, deployment, integration, dataset authoring
```

## 4. Common contributions

**Add / change test cases** - edit a dataset JSON, then `python -m app.cli --validate-only`. See `docs/DATASET.md`.
Use synthetic identifiers only (never real national IDs, phone numbers or e-mail addresses).

**Add a metric** - implement it in `app/services/evaluators.py`, return it through `EvaluationOutcome`, surface it in
`EvalCaseResult` / `EvalRunSummary`, add a gate threshold in `Settings`, and cover it with table-driven tests
in `tests/test_evaluators.py`. Document formula and limits in `docs/METRICS.md`.

**Add an integration** (new cache/exporter/judge) - keep it optional, failure-tolerant (a broken integration must
never fail an evaluation run) and covered by a fake in the tests.

## 5. Style
- Ruff (lint + format, line length 100) and mypy `--strict`; both run in pre-commit and CI.
- Public functions are typed; comments explain *why*, not *what*.
- Never log secrets; use `SecretStr` for credentials.

## 6. Reporting bugs and security issues
Bugs: use the issue template. Security: **do not open a public issue** - see [SECURITY.md](SECURITY.md).
