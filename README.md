# common-gh-actions

Shared GitHub Actions and reusable workflows for Citrine's Python repositories.

## Actions

### initialize

Checks out the caller's repository, sets up Python (via `actions/setup-python`), and installs project dependencies using `uv`.

Other Actions (`extract-version`, `check-version-bump`, `check-deprecations`) do **not** include checkout — callers must handle it themselves.  
This is intentional so that build issues do not interfere with other checks.

#### Inputs

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `python_version` | no | *(reads `.python-version`)* | Python version to install |
| `resolution` | no | `""` | Passed to `uv --resolution` (`"highest"` or `"lowest-direct"`) |
| `latest-citrine` | no | `true` | Install citrine from the main branch.  This is skipped if `resolution` is not `"highest"` or if the project itself *is* citrine-python (citrine is an editable dependency) |
| `latest-gemd` | no | `true` | Install gemd from the main branch.  This is skipped if `resolution` is not `"highest"` or if the project itself *is* gemd-python (gemd is an editable dependency) |
| `documentation` | no | `false` | Include the `docs` dependency group in `uv sync` |

### extract-version

Extracts the project version.

The script assumes that you have a `pyproject.toml`.  It support both static and dynamic version strings (both `file` and `attr`), but only supports constant values.  It runs in its own Python 3.12 environment so that build problems do not interfere with checks.

#### Inputs

| Name | Required | Description |
|------|----------|-------------|
| `path` | yes | Path to the project root containing `pyproject.toml` |

#### Outputs

| Name | Description |
|------|-------------|
| `version` | The extracted PEP 440 version string |

### check-version-bump

Verifies that the `pyproject.toml` version on the PR branch is strictly greater than the version on main. Uses `extract-version` internally.

#### Inputs

| Name | Required | Description |
|------|----------|-------------|
| `pr_path` | yes | Path to the PR checkout containing `pyproject.toml` |
| `main_path` | yes | Path to the main checkout containing `pyproject.toml` |

### check-deprecations

Scans Python source files for `@deprecation.deprecated` decorators and `warnings.warn(..., DeprecationWarning)` calls, then checks whether any have passed their removal version. Uses `extract-version` internally.

#### Inputs

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `src` | yes | | Path to the source directory to scan |
| `root` | no | `"."` | Path to the project root containing `pyproject.toml` |

## Workflows

### repo-checks.yml (PR Checks)

Runs standard PR checks: linting, version bump verification, and deprecation scanning.

#### Inputs

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `src` | yes | | Path to the source directory |
| `linter` | no | `"ruff"` | Which linter to run. Must be one of: `ruff`, `flake8`, `black`, `none` |
| `check_version_bump` | no | `true` | Verify the version in `pyproject.toml` has been incremented |
| `check_deprecation` | no | `true` | Check for expired deprecations |

#### Jobs

- **validate-inputs** -- Fails fast if `linter` is not a valid option.
- **linting** -- `ruff check` and `ruff format --check` with the project-pinned version (when `linter` is `ruff`).
- **linting-latest** -- Same checks with the latest `ruff` release (when `linter` is `ruff`).
- **linting-flake8** -- `flake8` lint check (when `linter` is `flake8`).
- **linting-black** -- `black --check` formatting check (when `linter` is `black`).
- **version-bump** -- Compares PR vs. main version (skippable).
- **deprecation-check** -- Scans for expired deprecations (skippable).

### matrix-tests.yml (PR Tests)

Runs unit tests across a Python version / OS matrix, with coverage enforcement.
Expects to run on Python 3.11 and 3.12 at a minimum.
Coverage is only enforced on the pinned Python version.

#### Inputs

| Name | Required | Default | Description |
|------|----------|---------|-------------|
| `src` | yes | | Path to the source directory |
| `skip_38` | no | `true` | Omit Python 3.8 from the matrix |
| `skip_39` | no | `true` | Omit Python 3.9 from the matrix |
| `skip_310` | no | `false` | Omit Python 3.10 from the matrix |
| `include_313` | no | `true` | Include Python 3.13 in the matrix |
| `include_314` | no | `true` | Include Python 3.14 in the matrix |
| `include_315` | no | `false` | Include Python 3.15 in the matrix |
| `coverage_fails_under` | no | `100` | Minimum required coverage percentage |
| `branch_coverage` | no | `false` | Include `--cov-branch` in coverage flags |
| `include_windows` | no | `true` | Include windows-latest in the test matrix |
| `include_macos` | no | `true` | Include macos-latest in the test matrix |

#### Jobs

- **run-tests-default** -- Tests with default dependencies and coverage threshold.
- **run-tests** -- Matrix across Python versions and OSes with `lowest-direct` resolution.
- **run-tests-against-latest** -- Matrix across Python versions and OSes with `highest` resolution, potentially including the main branches of citrine-python and gemd-python.

### deploy-docs.yml (Build and Deploy Docs)

Builds Sphinx documentation and deploys to GitHub Pages.

## Templates

These are workflows that cannot be shared as reusable workflows due to external limitations. Copy them into your repository.

### deploy-template.yml (Deploy to PyPI)

Packages and publishes to PyPI using an API token. Requires a repository secret called `PYPI_API_TOKEN`.

Cannot be a reusable workflow because PyPI's Trusted Publishing uses the *calling* repo's identity. See [pypi/warehouse#11096](https://github.com/pypi/warehouse/issues/11096).

## Development

This repo uses its own shared workflows for CI. To work locally:

```bash
uv sync --dev
uv run ruff check .github/actions tests
uv run ruff format --check .github/actions tests
uv run pytest
```
