# common-gh-actions

Shared GitHub Actions and reusable workflows for CitrineInformatics Python repositories.

## Project Structure

- Python helper scripts live inside `.github/actions/*/` (not a standard src/ layout)
- `pyproject.toml` pythonpath maps each action directory so pytest can import them
- `ruff` and `pytest` target `.github/actions/` and `tests/` as source directories
- `local-pr.yml` is CI for this repo itself — all other workflows under `.github/workflows/` are shared via `workflow_call`
- `deploy-template.yml` lives at repo root because it must be copied into consumer repos (PyPI Trusted Publishing limitation)

## Conventions

- Action inputs use kebab-case; workflow inputs use snake_case
- Action directories use kebab-case; Python scripts use matching snake_case names
- Non-blocking jobs use `continue-on-error: true` and a "Non-blocking" name prefix
- Local action refs (`./.github/actions/...`) are preferred over remote refs in reusable workflows — keeps action and workflow versions in sync
- `workflow_call` inputs don't support enums — use a validation job with a `case` statement

## Key Design Decisions

- `initialize` includes checkout because it's consumed by external repos with a pinned tag
- `extract-version`, `check-version-bump`, and `check-deprecations` do NOT include checkout so build issues don't interfere with checks
- `extract-version` installs its own Python 3.12 and `packaging` — independent of the caller's environment
- Self-clobber guard: `uv sync --dev` installs the project as editable; `uv pip show` + `Editable project location` grep distinguishes the project from its dependencies
- User prefers simple solutions over regex-based parsing

## Development

```bash
uv sync --dev
uv run ruff check .github/actions tests
uv run ruff format --check .github/actions tests
uv run pytest
```
