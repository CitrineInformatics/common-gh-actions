"""Check that the PR version is strictly greater than main."""

import argparse
import sys

from packaging.version import Version


def check_version_bump(*, pr_version: Version, main_version: Version) -> None:
    """Compare two versions and print the bump level.

    Parameters
    ----------
    pr_version :
        The version on the pull-request branch.
    main_version :
        The version on the main branch.

    Raises
    ------
    SystemExit
        Exit code 1 if *pr_version* is not strictly greater than
        *main_version*.  On success, prints the bump level
        (Major / Minor / Patch) to stdout.
    """
    if pr_version <= main_version:
        print(
            f"Version must be incremented: main is {main_version}, PR is {pr_version}",
            file=sys.stderr,
        )
        sys.exit(1)

    # Determine the significance of the bump.  The checks are ordered from
    # most to least significant; once a higher component has increased, the
    # lower components are irrelevant (e.g. 1.0.0 → 2.0.0 is "Major" even
    # if minor/patch also changed).
    if pr_version.release[0] > main_version.release[0]:
        level = "Major"
    elif pr_version.release[1] > main_version.release[1]:
        level = "Minor"
    else:
        level = "Patch"

    print(f"{level} bump: {main_version} -> {pr_version}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Verify version bump")
    parser.add_argument("--pr-version", type=Version, required=True)
    parser.add_argument("--main-version", type=Version, required=True)
    args = parser.parse_args()
    check_version_bump(pr_version=args.pr_version, main_version=args.main_version)


if __name__ == "__main__":  # pragma: no cover
    main()
