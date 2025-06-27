This is intended to store the GitHub Actions workflows and actions which are shared by Citrine's public Python repos.

## Actions

### Intialize

Sets up the environment in which it's being run. Upgrades pip and installs all dependencies, including the calling repo itself.

#### inputs

- latest: if true, skips installing requirements.txt, instead relying on what's defined in the caller's setup.py install\_requires.

- documentation: if true, also installs the doc\_requirements.txt file.

## Workflows

### Repo Checks (repo-checks.yml)

Kicks off four jobs to perform standard repo checks. Namely:

- linting, using `flake8`,
- checks that the version number in <src>/\_\_version\_\_.py was bumped
- looks for code marked deprecated, using `derp`
- confirms the docs build doesn't throw any warnings

#### inputs

- src: The directory containing the repo's source code.

### Run Tests (run-tests.yml)

Runs the repo's tests against every supported Python version and every supported OS.
It also kicks off a second run of tests in an environment where the dependency versions are the latest supported, instead of what's in requirements.txt.

#### inputs

- src: The directory containing the repo's source code.

### Deploy Documentation (deploy-docs.yml)

Build and deploy the docs using GitHub Pages.

## Templates

These are workflows which for whatever reason can't be shared. As such, we provide what it should look like in your repo.

### Deploy to PyPI (deploy-template.yml)

Package the code and deploy it to PyPI. This requires you have an action secret defined called `PYPI_API_TOKEN`.

See https://github.com/pypi/warehouse/issues/11096 for details on why the PyPI deploy workflow cannot be shared.
