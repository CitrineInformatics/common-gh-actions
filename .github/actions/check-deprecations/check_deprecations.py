"""Check for deprecated code that should have been removed."""

import argparse
import ast
import sys
from enum import Enum
from pathlib import Path
from typing import NamedTuple

from packaging.version import InvalidVersion, Version


class Category(Enum):
    """Enum for message filtering."""

    NOTIFICATION = "notification"
    WARNING = "warning"
    ERROR = "error"


class DeprecationReport(NamedTuple):
    """Results of scanning for deprecation issues."""

    errors: list[str]
    warnings: list[str]
    notifications: list[str]


def _validate_deprecation_decorator(
    *,
    decorator: ast.AST,
    current_version: Version,
) -> tuple[Category, str] | None:
    """Validate a single ``@deprecation.deprecated`` decorator.

    Parameters
    ----------
    decorator :
        An AST node from a definition's ``decorator_list``.
    current_version :
        The current project version to compare against.

    Returns
    -------
    tuple[Category, str] | None
        A ``(Category, message)`` pair, or ``None`` if the decorator is
        not a deprecation decorator or passes all checks.
    """
    # A decorator can appear in two AST forms:
    #   @deprecated(...)   -> ast.Call  (func=Name/Attribute, keywords=[...])
    #   @deprecated        -> ast.Name  (bare reference, no parentheses)
    if isinstance(decorator, ast.Call):
        func = decorator.func
        kw_map = {kw.arg: kw.value for kw in decorator.keywords}
    else:
        func = decorator
        kw_map = {}

    # Match @deprecation.deprecated or @deprecated (after import).
    is_deprecation = (
        isinstance(func, ast.Attribute)
        and func.attr == "deprecated"
        and isinstance(func.value, ast.Name)
        and func.value.id == "deprecation"
    ) or (isinstance(func, ast.Name) and func.id == "deprecated")
    if not is_deprecation:
        return None

    # 1. Both required keywords must be present.
    missing = [k for k in ("deprecated_in", "removed_in") if k not in kw_map]
    if missing:
        return (Category.ERROR, "missing required keyword(s): " + ", ".join(missing))

    # 2. Both values must be string literals.  Dynamic expressions can't be evaluated statically.
    non_literal = [
        k
        for k in ("deprecated_in", "removed_in")
        if not isinstance(kw_map[k], ast.Constant)
    ]
    if non_literal:
        return (
            Category.NOTIFICATION,
            ", ".join(non_literal)
            + " can only be evaluated with a string literal, not a dynamic expression",
        )

    # 3. Version string is not formatted correctly.
    deprecated_in = kw_map["deprecated_in"].value
    removed_in = kw_map["removed_in"].value
    try:
        v_deprecated = Version(deprecated_in)
        v_removed = Version(removed_in)
    except InvalidVersion as e:
        return (Category.ERROR, str(e))

    # 4. deprecated_in must be before removed_in.
    if v_deprecated >= v_removed:
        return (
            Category.ERROR,
            f"deprecated_in={deprecated_in!r} is after removed_in={removed_in!r}",
        )

    # 5. Error if the removal version has been reached.
    if current_version >= v_removed:
        return (
            Category.ERROR,
            f"removed_in={removed_in!r}, current version is {current_version}",
        )

    # 6. Warn if the deprecation hasn't taken effect yet.
    if current_version < v_deprecated:
        return (
            Category.WARNING,
            f"deprecated_in={deprecated_in!r} is after current version {current_version}",
        )

    return None


def check_deprecation_decorators(
    src: Path, current_version: Version
) -> DeprecationReport:
    """Scan Python files under *src* for ``@deprecation.deprecated`` decorators.

    Parameters
    ----------
    src :
        Path to the source directory to scan.
    current_version :
        The current project version to compare against.

    Returns
    -------
    DeprecationReport
        * **errors** — issues that must be fixed (blocking):
          expired deprecations, missing keywords, ``deprecated_in >= removed_in``.
        * **warnings** — issues to review (non-blocking):
          premature deprecations.
        * **notifications** — informational items (non-blocking):
          non-literal keyword values, ``warnings.warn(..., DeprecationWarning)``.

    Raises
    ------
    FileNotFoundError
        If *src* does not exist or is not a directory.
    """
    if not src.is_dir():
        raise FileNotFoundError(f"Source directory does not exist: {src}")

    messages = {
        Category.NOTIFICATION: [],
        Category.WARNING: [],
        Category.ERROR: [],
    }

    for py_file in sorted(src.rglob("*.py")):
        source = py_file.read_text(encoding="utf-8")
        lines = source.splitlines()
        tree = ast.parse(source, filename=str(py_file))

        # Decorated statements
        for node in ast.walk(tree):
            for decorator in getattr(node, "decorator_list", []):
                result = _validate_deprecation_decorator(
                    decorator=decorator,
                    current_version=current_version,
                )
                if result is not None:
                    category, message = result
                    prefix = f"{py_file}:{node.lineno}: {node.name}"
                    messages[category].append(f"{prefix} - {message}")
                    if category == Category.NOTIFICATION:
                        # AST line numbers are 1-indexed; lines[] is 0-indexed.
                        source_line = lines[decorator.lineno - 1].strip()
                        messages[category][-1] += f"[{source_line}]"

        # Explicit warnings.warn(..., DeprecationWarning) calls require manual review
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            # Match warnings.warn(...) or warn(...) (after from-import).
            func = node.func
            is_warn = (
                isinstance(func, ast.Attribute)
                and func.attr == "warn"
                and isinstance(func.value, ast.Name)
                and func.value.id == "warnings"
            ) or (isinstance(func, ast.Name) and func.id == "warn")
            if not is_warn:
                continue

            # Check if DeprecationWarning appears in any argument (positional or keyword).
            all_args = list(node.args) + [kw.value for kw in node.keywords]
            has_deprecation_warning = any(
                (isinstance(arg, ast.Name) and arg.id == "DeprecationWarning")
                or (isinstance(arg, ast.Attribute) and arg.attr == "DeprecationWarning")
                for arg in all_args
            )
            if not has_deprecation_warning:
                continue

            prefix = f"{py_file}:{node.lineno}"
            source_line = lines[node.lineno - 1].strip()
            messages[Category.NOTIFICATION].append(
                f"{prefix} - Bare DeprecationWarning: {source_line}"
            )

    return DeprecationReport(
        errors=messages[Category.ERROR],
        warnings=messages[Category.WARNING],
        notifications=messages[Category.NOTIFICATION],
    )


def check_deprecations(*, src: Path, current_version: Version) -> None:
    """Check for expired deprecations and exit with code 1 if any found.

    Parameters
    ----------
    src :
        Path to the source directory to scan.
    current_version :
        The current project version to compare against.

    Raises
    ------
    SystemExit
        Exit code 1 if any errors are found.  Errors are printed to stderr.
        Warnings are emitted as ``::warning::`` annotations and notifications
        as ``::notice::`` annotations (both non-blocking).
    """
    report = check_deprecation_decorators(src, current_version)

    for notification in report.notifications:
        print(f"::notice::{notification.strip()}")

    for warning in report.warnings:
        print(f"::warning::{warning.strip()}")

    if report.errors:
        print(
            "Deprecated code past its removal version:\n" + "\n".join(report.errors),
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"No expired deprecations found (current version: {current_version})")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Check for expired deprecation decorators"
    )
    parser.add_argument(
        "--src", type=Path, required=True, help="Path to source directory to scan"
    )
    parser.add_argument(
        "--version", type=Version, required=True, help="Current project version"
    )
    args = parser.parse_args()
    try:
        check_deprecations(src=args.src, current_version=args.version)
    except FileNotFoundError as e:
        print(str(e), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":  # pragma: no cover
    main()
