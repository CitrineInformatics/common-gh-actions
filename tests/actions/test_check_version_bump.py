"""Tests for .github/actions/check-version-bump/check_version_bump.py"""

import sys

import pytest
from check_version_bump import (  # registered on sys.path via pythonpath in pyproject.toml
    check_version_bump,
    main,
)
from packaging.version import Version


class TestCheckVersionBump:
    """Tests for check_version_bump comparison and output logic."""

    @pytest.mark.parametrize(
        "pr, main_, level",
        [
            pytest.param("2.0.0", "1.0.0", "Major", id="major"),
            pytest.param("2.0.0", "1.9.0", "Major", id="major-precedence"),
            pytest.param("1.1.0", "1.0.0", "Minor", id="minor"),
            pytest.param("1.1.0", "1.0.9", "Minor", id="minor-precedence"),
            pytest.param("1.0.1", "1.0.0", "Patch", id="patch"),
        ],
    )
    def test_success(self, pr, main_, level, capsys):
        check_version_bump(pr_version=Version(pr), main_version=Version(main_))

        captured = capsys.readouterr()
        assert f"{level} bump: {main_} -> {pr}" in captured.out

    @pytest.mark.parametrize(
        "pr, main_",
        [
            pytest.param("1.0.0", "1.0.0", id="equal"),
            pytest.param("0.9.0", "1.0.0", id="lower"),
            pytest.param("1.1.0", "1.2.0", id="main_higher"),
        ],
    )
    def test_failure(self, pr, main_, capsys):
        with pytest.raises(SystemExit) as exc_info:
            check_version_bump(pr_version=Version(pr), main_version=Version(main_))
        assert exc_info.value.code == 1

        captured = capsys.readouterr()
        assert "must be incremented" in captured.err


class TestMain:
    """Tests for the CLI entry point."""

    def test_main_success(self, monkeypatch, capsys):
        pr_version = "1.1.0"
        main_version = "1.0.0"

        monkeypatch.setattr(
            sys,
            "argv",
            [
                "check_version_bump.py",
                "--pr-version",
                pr_version,
                "--main-version",
                "1.0.0",
            ],
        )
        main()

        captured = capsys.readouterr()
        assert pr_version in captured.out
        assert main_version in captured.out

    def test_main_failure(self, monkeypatch):
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "check_version_bump.py",
                "--pr-version",
                "1.0.0",
                "--main-version",
                "1.0.0",
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
