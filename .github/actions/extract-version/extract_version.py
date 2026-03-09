"""Extract the project version from a pyproject.toml, supporting dynamic versions."""

import argparse
import ast
import os
import sys
import tomllib
from pathlib import Path
from typing import Tuple

from packaging.version import InvalidVersion, Version


def file_to_module(file_path: Path, package_root: Path = Path(".")) -> str:
    """Convert a file path (or directory) to a dotted module name.

    The resolution depends on whether the paths are absolute or relative:

    * Both absolute -> ``file_path.relative_to(package_root)``
    * Both relative -> ``file_path.relative_to(package_root)``
    * Relative root, absolute file -> ``file_path.relative_to(package_root.resolve())``
    * Absolute root, relative file -> ``file_path`` is used directly
      (assumed already relative to *package_root*)

    Parameters
    ----------
    file_path :
        Path to a ``.py`` file or a package directory.  ``__init__.py``
        suffixes are stripped so that a package directory and its
        ``__init__.py`` yield the same module name.
    package_root :
        The root directory of the package (where the top-level package
        lives).  Defaults to the current working directory.

    Returns
    -------
    str
        A dotted module name, e.g. ``"pkg.sub.module"``.
    """
    if package_root.is_absolute() and not file_path.is_absolute():
        relative = file_path
    elif not package_root.is_absolute() and file_path.is_absolute():
        relative = file_path.relative_to(package_root.resolve())
    else:
        relative = file_path.relative_to(package_root)

    if relative.name == "__init__.py":
        relative = relative.parent
    else:
        relative = relative.with_suffix("")

    return ".".join(relative.parts)


def module_to_file(module: str, package_root: Path = Path(".")) -> Path:
    """Convert a dotted module name to the corresponding file path.

    Parameters
    ----------
    module :
        A dotted module name, e.g. ``"pkg.sub"`` or ``"pkg.__version__"``.
    package_root :
        The root directory of the package (where the top-level package lives).
        Defaults to the current working directory.

    Returns
    -------
    Path
        The resolved file path.
    """
    file_path = package_root / module.replace(".", "/")

    if file_path.is_dir():
        return file_path / "__init__.py"
    return file_path.with_suffix(".py")


def attr_to_file_and_variable(
    attr: str, package_root: Path = Path(".")
) -> Tuple[Path, str]:
    """Split a setuptools ``attr`` string into a file path and variable name.

    Parameters
    ----------
    attr :
        A dotted ``attr`` string, e.g. ``"pkg.__version__.__version__"``.
    package_root :
        The root directory of the package.  Defaults to the current working
        directory.

    Returns
    -------
    tuple[Path, str]
        ``(file_path, variable)`` — the resolved file and the variable name.

    Raises
    ------
    ValueError
        If *attr* does not contain at least one dot (no module component).
    """
    parts = attr.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(
            f"Cannot resolve attr {attr!r}: expected 'module.attribute' form"
        )
    module_path, variable = parts
    file_path = module_to_file(module_path, package_root)
    return file_path, variable


def file_and_variable_to_attr(
    file_path: Path, variable: str, package_root: Path = Path(".")
) -> str:
    """Reconstruct a setuptools ``attr`` string from a file path and variable name.

    Parameters
    ----------
    file_path :
        Path to the Python file containing the variable.
    variable :
        The variable name within the file.
    package_root :
        The root directory of the package.  Defaults to the current working
        directory.

    Returns
    -------
    str
        A dotted ``attr`` string, e.g. ``"pkg.__version__.__version__"``.
    """
    return f"{file_to_module(file_path, package_root)}.{variable}"


def _resolve_attr(package_root: Path, attr: str) -> str:
    """Resolve a setuptools dynamic version ``attr`` to its string value.

    Parameters
    ----------
    package_root :
        The root directory containing the package.
    attr :
        A setuptools ``attr`` string, e.g. ``"pkg.__version__.__version__"``.

    Returns
    -------
    str
        The resolved version string.

    Raises
    ------
    FileNotFoundError
        If the file referenced by *attr* does not exist.
    ValueError
        If the target variable is not a string literal or is not found.
    """
    file_path, variable = attr_to_file_and_variable(attr, package_root)

    if not file_path.exists():
        raise FileNotFoundError(
            f"Cannot resolve attr {attr!r}: {file_path} does not exist"
        )
    tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == variable:
                    if isinstance(node.value, ast.Constant) and isinstance(
                        node.value.value, str
                    ):
                        return node.value.value
                    raise ValueError(
                        f"Cannot resolve attr {attr!r}: "
                        f"{variable} in {file_path} is not a string literal"
                    )

    raise ValueError(
        f"Cannot resolve attr {attr!r}: {variable} not found in {file_path}"
    )


def extract_version(project_root: Path) -> Version:
    """Extract the project version from a project root directory.

    Supports static versions (``[project] version = "X.Y.Z"``) and
    setuptools dynamic versions (``attr`` and ``file`` forms).

    Parameters
    ----------
    project_root :
        Path to the project root directory containing ``pyproject.toml``.

    Returns
    -------
    Version
        The extracted project version.

    Raises
    ------
    ValueError
        If the version cannot be determined from ``pyproject.toml``.
    InvalidVersion
        If the resolved version string is not PEP 440 compliant.
        A note is added to the exception indicating the source
        location (static field, attr, or file).
    """
    with open(project_root / "pyproject.toml", "rb") as f:
        pyproject = tomllib.load(f)

    project = pyproject["project"]

    # Static version
    if "version" in project:
        try:
            return Version(project["version"])
        except InvalidVersion as e:
            e.add_note(f"in [project] version of {project_root / 'pyproject.toml'}")
            raise

    # Dynamic version
    if "version" not in project.get("dynamic", []):
        raise ValueError(
            f"pyproject.toml in {project_root} has no static version "
            f"and 'version' is not listed in dynamic"
        )

    dynamic_cfg = pyproject.get("tool", {}).get("setuptools", {}).get("dynamic", {})
    version_cfg = dynamic_cfg.get("version")
    if version_cfg is None:
        raise ValueError(
            f"pyproject.toml in {project_root} declares dynamic version "
            f"but [tool.setuptools.dynamic] version is not configured"
        )

    if "attr" in version_cfg:
        # The package root may differ from the project root.
        where = (
            pyproject.get("tool", {})
            .get("setuptools", {})
            .get("packages", {})
            .get("find", {})
            .get("where")
        )
        if where:
            package_root = project_root / where[0]
        else:
            package_root = project_root
        raw = _resolve_attr(package_root, version_cfg["attr"])
        try:
            return Version(raw)
        except InvalidVersion as e:
            e.add_note(f"resolved from attr {version_cfg['attr']!r}")
            raise

    if "file" in version_cfg:
        # The file path in TOML is always relative to the project root.
        version_file = project_root / version_cfg["file"]
        raw = version_file.read_text(encoding="utf-8").strip()
        try:
            return Version(raw)
        except InvalidVersion as e:
            e.add_note(f"read from {version_file}")
            raise

    raise ValueError(
        f"pyproject.toml in {project_root} has [tool.setuptools.dynamic] version "
        f"but it contains neither 'attr' nor 'file'"
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Extract project version from pyproject.toml"
    )
    parser.add_argument("path", type=Path, help="Path to the project root directory")
    args = parser.parse_args()

    try:
        version = extract_version(args.path)
    except (ValueError, FileNotFoundError) as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)

    print(f"Extracted version: {version}")

    # Write to GITHUB_OUTPUT if running in Actions
    github_output = os.environ.get("GITHUB_OUTPUT")
    if github_output:
        with open(github_output, "a") as f:
            f.write(f"version={version}\n")


if __name__ == "__main__":  # pragma: no cover
    main()
