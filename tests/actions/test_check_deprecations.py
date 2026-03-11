"""Tests for .github/actions/check-deprecations/check_deprecations.py"""

import sys
import textwrap
from pathlib import Path

import pytest
from check_deprecations import (  # registered on sys.path via pythonpath in pyproject.toml
    DeprecationReport,
    check_deprecation_decorators,
    check_deprecations,
    main,
)
from packaging.version import Version


def _write_file(path: Path, content: str):
    path.write_text(textwrap.dedent(content), encoding="utf-8")


@pytest.fixture
def src_dir(tmp_path):
    src_dir = tmp_path / "src"
    src_dir.mkdir()
    return src_dir


class TestCheckDeprecationDecorators:
    def test_missing_src_raises(self, tmp_path):
        missing = tmp_path / "nonexistent"
        with pytest.raises(FileNotFoundError, match="does not exist"):
            check_deprecation_decorators(missing, Version("1.0.0"))

    def test_no_files(self, src_dir):
        report = check_deprecation_decorators(src_dir, Version("1.0.0"))
        assert report == DeprecationReport([], [], [])

    def test_no_python_files(self, src_dir):
        (src_dir / "doc.txt").touch()
        (src_dir / "doc.csv").touch()

        report = check_deprecation_decorators(src_dir, Version("1.0.0"))
        assert report == DeprecationReport([], [], [])

    def test_file_outside_src_ignored(self, tmp_path, src_dir):
        """A deprecated file in the project root (outside src_dir) is not scanned."""
        _write_file(
            tmp_path / "outside.py",
            """
            from deprecation import deprecated

            @deprecated(deprecated_in='0.5.0', removed_in='1.0.0')
            def old_func():
                pass
            """,
        )

        report = check_deprecation_decorators(src_dir, Version("1.0.0"))
        assert report == DeprecationReport([], [], [])

    def test_no_deprecations(self, src_dir):
        _write_file(
            src_dir / "file.py",
            """
            def func():
                pass
            """,
        )

        report = check_deprecation_decorators(src_dir, Version("1.0.0"))
        assert report == DeprecationReport([], [], [])

    @pytest.mark.parametrize(
        "decorator_import, decorator",
        [
            (
                "import deprecation",
                "deprecation.deprecated",
            ),
            (
                "from deprecation import deprecated",
                "deprecated",
            ),
        ],
        ids=["dotted", "plain"],
    )
    def test_decorator_forms(self, src_dir, decorator_import, decorator):
        _write_file(
            src_dir / "file.py",
            f"""
            {decorator_import}

            @{decorator}(deprecated_in='0.5.0', removed_in='1.0.0')
            def old_func():
                pass
            """,
        )

        early = check_deprecation_decorators(src_dir, Version("0.1.0"))
        assert len(early.warnings) == 1
        assert early.errors == []

        clean = check_deprecation_decorators(src_dir, Version("0.5.0"))
        assert clean == DeprecationReport([], [], [])

        report = check_deprecation_decorators(src_dir, Version("1.0.0"))
        assert len(report.errors) == 1
        assert report.warnings == []
        assert "old_func" in report.errors[0]
        assert "removed_in='1.0.0'" in report.errors[0]

    @pytest.mark.parametrize(
        "content, name",
        [
            pytest.param(
                """
                from deprecation import deprecated

                @deprecated(deprecated_in='0.5.0', removed_in='1.0.0')
                def old_func():
                    pass
                """,
                "old_func",
                id="function",
            ),
            pytest.param(
                """
                from deprecation import deprecated

                @deprecated(deprecated_in='0.5.0', removed_in='1.0.0')
                async def old_coro():
                    pass
                """,
                "old_coro",
                id="async_function",
            ),
            pytest.param(
                """
                from deprecation import deprecated

                @deprecated(deprecated_in='0.5.0', removed_in='1.0.0')
                class OldClass:
                    pass
                """,
                "OldClass",
                id="class",
            ),
        ],
    )
    def test_definition_types(self, src_dir, content, name):
        _write_file(src_dir / "file.py", content)

        early = check_deprecation_decorators(src_dir, Version("0.1.0"))
        assert len(early.warnings) == 1
        assert early.errors == []

        clean = check_deprecation_decorators(src_dir, Version("0.5.0"))
        assert clean == DeprecationReport([], [], [])

        report = check_deprecation_decorators(src_dir, Version("1.0.0"))
        assert len(report.errors) == 1
        assert report.warnings == []
        assert name in report.errors[0]

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(
                """
                class Foo:
                    @staticmethod
                    def bar():
                        pass
                """,
                id="non_deprecation_decorator",
            ),
            pytest.param(
                """
                def my_decorator(x, **kwargs):
                    def wrapper(f):
                        return f
                    return wrapper

                @my_decorator(deprecated_in='1.0.0', removed_in='1.0.0')
                def func():
                    pass
                """,
                id="unrelated_decorator",
            ),
        ],
    )
    def test_ignored_decorators(self, src_dir, content):
        _write_file(src_dir / "file.py", content)

        report = check_deprecation_decorators(src_dir, Version("1.0.0"))
        assert report == DeprecationReport([], [], [])

    @pytest.mark.parametrize(
        "decorator, missing",
        [
            pytest.param(
                "@deprecated(deprecated_in='0.5.0')",
                "removed_in",
                id="missing_removed_in",
            ),
            pytest.param(
                "@deprecated(removed_in='1.0.0')",
                "deprecated_in",
                id="missing_deprecated_in",
            ),
            pytest.param(
                "@deprecated()",
                "deprecated_in, removed_in",
                id="missing_both",
            ),
            pytest.param(
                "@deprecated",
                "deprecated_in, removed_in",
                id="without_call",
            ),
        ],
    )
    def test_missing_keywords(self, src_dir, decorator, missing):
        _write_file(
            src_dir / "file.py",
            f"""
            from deprecation import deprecated

            {decorator}
            def old_func():
                pass
            """,
        )

        report = check_deprecation_decorators(src_dir, Version("1.0.0"))
        assert len(report.errors) == 1
        assert report.warnings == []
        assert "old_func" in report.errors[0]
        assert "missing required keyword(s)" in report.errors[0]
        assert missing in report.errors[0]

    @pytest.mark.parametrize(
        "decorator, non_literal",
        [
            pytest.param(
                "@deprecated(deprecated_in='0.5.0', removed_in=NEXT_VERSION)",
                "removed_in",
                id="non_constant_removed_in",
            ),
            pytest.param(
                "@deprecated(deprecated_in=WHEN, removed_in='1.0.0')",
                "deprecated_in",
                id="non_constant_deprecated_in",
            ),
            pytest.param(
                "@deprecated(deprecated_in=WHEN, removed_in=NEXT_VERSION)",
                "deprecated_in, removed_in",
                id="non_constant_both",
            ),
        ],
    )
    def test_non_constant_keywords(self, src_dir, decorator, non_literal):
        """Keywords set to variable references produce notifications (only literals can be evaluated)."""
        _write_file(
            src_dir / "file.py",
            f"""
            from deprecation import deprecated

            WHEN = '0.5.0'
            NEXT_VERSION = '2.0.0'

            {decorator}
            def old_func():
                pass
            """,
        )

        report = check_deprecation_decorators(src_dir, Version("1.0.0"))
        assert report.errors == []
        assert report.warnings == []
        assert len(report.notifications) == 1
        assert "old_func" in report.notifications[0]
        assert "a string literal" in report.notifications[0]
        assert non_literal in report.notifications[0]
        # Notifications include the literal source line for quick review
        assert decorator in report.notifications[0]

    def test_deprecated_in_after_removed_in(self, src_dir):
        """deprecated_in > removed_in is logically incoherent and always an error."""
        _write_file(
            src_dir / "file.py",
            """
            from deprecation import deprecated

            @deprecated(deprecated_in='2.0.0', removed_in='1.0.0')
            def confused_func():
                pass
            """,
        )

        report = check_deprecation_decorators(src_dir, Version("0.5.0"))
        assert len(report.errors) == 1
        assert report.warnings == []
        assert "confused_func" in report.errors[0]
        assert "deprecated_in='2.0.0'" in report.errors[0]
        assert "removed_in='1.0.0'" in report.errors[0]

    def test_invalid_version_string(self, src_dir):
        """Malformed version strings are reported as errors."""
        _write_file(
            src_dir / "file.py",
            """
            from deprecation import deprecated

            @deprecated(deprecated_in='1.0.0', removed_in='not-a-version')
            def bad_version_func():
                pass
            """,
        )

        report = check_deprecation_decorators(src_dir, Version("1.0.0"))
        assert len(report.errors) == 1
        assert report.warnings == []
        assert report.notifications == []
        assert "bad_version_func" in report.errors[0]

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(
                """
                import warnings
                warnings.warn("use new_func instead", DeprecationWarning)
                """,
                id="dotted",
            ),
            pytest.param(
                """
                from warnings import warn
                warn("use new_func instead", DeprecationWarning)
                """,
                id="plain",
            ),
            pytest.param(
                """
                import warnings
                warnings.warn("use new_func instead", category=DeprecationWarning)
                """,
                id="keyword_arg",
            ),
        ],
    )
    def test_warn_deprecation_warning(self, src_dir, content):
        """warnings.warn(..., DeprecationWarning) calls produce notifications."""
        _write_file(src_dir / "file.py", content)

        report = check_deprecation_decorators(src_dir, Version("1.0.0"))
        assert report.errors == []
        assert report.warnings == []
        assert len(report.notifications) == 1
        # Notifications include the literal source line for quick review
        assert "warn(" in report.notifications[0]
        assert "DeprecationWarning" in report.notifications[0]

    @pytest.mark.parametrize(
        "content",
        [
            pytest.param(
                """
                import warnings
                warnings.warn("not a deprecation", UserWarning)
                """,
                id="user_warning",
            ),
            pytest.param(
                """
                import warnings
                warnings.warn("bare warning without category")
                """,
                id="no_category",
            ),
        ],
    )
    def test_warn_non_deprecation_ignored(self, src_dir, content):
        """Non-DeprecationWarning warn() calls are not reported."""
        _write_file(src_dir / "file.py", content)

        report = check_deprecation_decorators(src_dir, Version("1.0.0"))
        assert report == DeprecationReport([], [], [])

    def test_multiple_errors_across_files(self, src_dir):
        _write_file(
            src_dir / "a.py",
            """
            from deprecation import deprecated

            @deprecated(deprecated_in='0.1.0', removed_in='0.5.0')
            def func_a():
                pass
            """,
        )
        _write_file(
            src_dir / "b.py",
            """
            from deprecation import deprecated

            @deprecated(deprecated_in='0.5.0', removed_in='1.0.0')
            def func_b():
                pass
            """,
        )
        _write_file(
            src_dir / "c.py",
            """
            from deprecation import deprecated

            @deprecated(deprecated_in='1.0.0', removed_in='1.5.0')
            def func_c():
                pass
            """,
        )

        earliest = check_deprecation_decorators(src_dir, Version("0.1.0"))
        assert earliest.errors == []
        assert len(earliest.warnings) == 2  # b and c are premature

        early = check_deprecation_decorators(src_dir, Version("0.5.0"))
        assert len(early.errors) == 1
        assert len(early.warnings) == 1  # c is premature

        middle = check_deprecation_decorators(src_dir, Version("1.0.0"))
        assert len(middle.errors) == 2
        assert middle.warnings == []

        late = check_deprecation_decorators(src_dir, Version("1.5.0"))
        assert len(late.errors) == 3
        assert late.warnings == []


class TestCheckDeprecations:
    def test_success(self, src_dir, capsys):
        _write_file(
            src_dir / "file.py",
            """
            def func():
                pass
            """,
        )
        check_deprecations(src=src_dir, current_version=Version("1.0.0"))

        captured = capsys.readouterr()
        assert "No expired deprecations" in captured.out
        assert "1.0.0" in captured.out

    def test_failure(self, src_dir, capsys):
        _write_file(
            src_dir / "file.py",
            """
            from deprecation import deprecated

            @deprecated(deprecated_in='0.5.0', removed_in='1.0.0')
            def func():
                pass
            """,
        )

        with pytest.raises(SystemExit) as exc_info:
            check_deprecations(src=src_dir, current_version=Version("1.0.0"))
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "past its removal version" in captured.err

    def test_warnings_emitted(self, src_dir, capsys):
        """Warnings are printed as ::warning annotations but do not cause failure."""
        _write_file(
            src_dir / "file.py",
            """
            from deprecation import deprecated

            @deprecated(deprecated_in='2.0.0', removed_in='3.0.0')
            def future_func():
                pass
            """,
        )

        check_deprecations(src=src_dir, current_version=Version("1.0.0"))

        captured = capsys.readouterr()
        assert "::warning::" in captured.out
        assert "future_func" in captured.out
        assert "No expired deprecations" in captured.out

    def test_notifications_emitted(self, src_dir, capsys):
        """Notifications are printed as ::notice annotations but do not cause failure."""
        _write_file(
            src_dir / "file.py",
            """
            import warnings
            warnings.warn("old API", DeprecationWarning)
            """,
        )

        check_deprecations(src=src_dir, current_version=Version("1.0.0"))

        captured = capsys.readouterr()
        assert "::notice::" in captured.out
        assert "DeprecationWarning" in captured.out
        assert "No expired deprecations" in captured.out


class TestMain:
    def test_main_success(self, src_dir, monkeypatch, capsys):
        _write_file(
            src_dir / "file.py",
            """
            def func():
                pass
            """,
        )

        monkeypatch.setattr(
            sys,
            "argv",
            ["check_deprecations.py", "--src", str(src_dir), "--version", "1.0.0"],
        )
        main()

        captured = capsys.readouterr()
        assert "No expired deprecations" in captured.out

    def test_main_failure(self, src_dir, monkeypatch):
        _write_file(
            src_dir / "file.py",
            """
            from deprecation import deprecated

            @deprecated(deprecated_in='0.5.0', removed_in='1.0.0')
            def old_func():
                pass
            """,
        )

        monkeypatch.setattr(
            sys,
            "argv",
            ["check_deprecations.py", "--src", str(src_dir), "--version", "1.0.0"],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    def test_main_missing_src(self, tmp_path, monkeypatch, capsys):
        """main() prints the error to stderr and exits 1 when src doesn't exist."""
        monkeypatch.setattr(
            sys,
            "argv",
            [
                "check_deprecations.py",
                "--src",
                str(tmp_path / "nonexistent"),
                "--version",
                "1.0.0",
            ],
        )

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "does not exist" in captured.err
