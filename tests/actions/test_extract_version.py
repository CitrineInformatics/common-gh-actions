"""Tests for .github/actions/extract-version/extract_version.py"""

import sys
import textwrap
from pathlib import Path, PurePosixPath

import pytest
from extract_version import (  # registered on sys.path via pythonpath in pyproject.toml
    attr_to_file_and_variable,
    extract_version,
    file_and_variable_to_attr,
    file_to_module,
    main,
    module_to_file,
)
from packaging.version import InvalidVersion, Version


class TestModuleToFile:
    """Tests for module_to_file: dotted module name -> file path."""

    def test_module_file(self, tmp_path):
        """A module name resolving to a .py file returns that file."""
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "utils.py").touch()

        assert module_to_file("pkg.utils", tmp_path) == tmp_path / "pkg" / "utils.py"

    def test_package_dir(self, tmp_path):
        """A module name resolving to a directory returns its __init__.py."""
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "__init__.py").touch()

        assert module_to_file("pkg", tmp_path) == tmp_path / "pkg" / "__init__.py"

    def test_nested_module(self, tmp_path):
        """Dotted paths with multiple components resolve correctly."""
        (tmp_path / "pkg" / "sub").mkdir(parents=True)
        (tmp_path / "pkg" / "sub" / "__version__.py").touch()

        assert (
            module_to_file("pkg.sub.__version__", tmp_path)
            == tmp_path / "pkg" / "sub" / "__version__.py"
        )

    def test_nonexistent_appends_py(self, tmp_path):
        """When the path is not a directory, .py is appended (even if the file doesn't exist)."""
        assert (
            module_to_file("pkg.missing", tmp_path) == tmp_path / "pkg" / "missing.py"
        )


class TestFileToModule:
    """Tests for file_to_module: file path -> dotted module name."""

    def test_py_file(self, tmp_path):
        assert file_to_module(tmp_path / "pkg" / "utils.py", tmp_path) == "pkg.utils"

    def test_init_py(self, tmp_path):
        """__init__.py is stripped, yielding the package name."""
        assert file_to_module(tmp_path / "pkg" / "__init__.py", tmp_path) == "pkg"

    def test_nested_file(self, tmp_path):
        assert (
            file_to_module(tmp_path / "pkg" / "sub" / "__version__.py", tmp_path)
            == "pkg.sub.__version__"
        )

    def test_directory(self, tmp_path):
        """A directory path (no suffix) is converted directly."""
        assert file_to_module(tmp_path / "pkg" / "sub", tmp_path) == "pkg.sub"

    def test_relative_file_with_absolute_root(self, tmp_path):
        """Absolute root + relative file_path → file_path used directly."""
        assert file_to_module(Path("pkg/utils.py"), tmp_path) == "pkg.utils"

    def test_relative_both(self):
        """Both relative → file_path.relative_to(package_root)."""
        assert file_to_module(Path("src/pkg/utils.py"), Path("src")) == "pkg.utils"

    def test_relative_root_absolute_file(self, tmp_path):
        """Relative root + absolute file_path → relative_to(root.resolve())."""
        root = Path(".")
        abs_file = root.resolve() / "pkg" / "utils.py"
        assert file_to_module(abs_file, root) == "pkg.utils"

    def test_default_root(self):
        """Default package_root is Path('.'), so relative paths work directly."""
        assert file_to_module(Path("pkg/utils.py")) == "pkg.utils"


class TestAttrToFileAndVariable:
    """Tests for attr_to_file_and_variable: attr string -> (file_path, variable)."""

    def test_module_file(self, tmp_path):
        """attr 'pkg.__version__.__version__' -> (pkg/__version__.py, '__version__')."""
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "__version__.py").touch()

        file_path, variable = attr_to_file_and_variable(
            "pkg.__version__.__version__", tmp_path
        )
        assert file_path == tmp_path / "pkg" / "__version__.py"
        assert variable == "__version__"

    def test_package_init(self, tmp_path):
        """attr 'pkg.__version__' -> (pkg/__init__.py, '__version__')."""
        (tmp_path / "pkg").mkdir()
        (tmp_path / "pkg" / "__init__.py").touch()

        file_path, variable = attr_to_file_and_variable("pkg.__version__", tmp_path)
        assert file_path == tmp_path / "pkg" / "__init__.py"
        assert variable == "__version__"

    def test_no_dot_raises(self):
        with pytest.raises(ValueError, match="expected 'module.attribute' form"):
            attr_to_file_and_variable("nodots")


class TestFileAndVariableToAttr:
    """Tests for file_and_variable_to_attr: (file_path, variable) -> attr string."""

    def test_module_file(self, tmp_path):
        attr = file_and_variable_to_attr(
            tmp_path / "pkg" / "__version__.py", "__version__", tmp_path
        )
        assert attr == "pkg.__version__.__version__"

    def test_package_init(self, tmp_path):
        attr = file_and_variable_to_attr(
            tmp_path / "pkg" / "__init__.py", "__version__", tmp_path
        )
        assert attr == "pkg.__version__"

    def test_roundtrip_with_attr_to_file_and_variable(self, tmp_path):
        """attr_to_file_and_variable and file_and_variable_to_attr are inverses."""
        (tmp_path / "pkg" / "sub").mkdir(parents=True)
        (tmp_path / "pkg" / "sub" / "__version__.py").touch()

        original = "pkg.sub.__version__.__version__"
        file_path, variable = attr_to_file_and_variable(original, tmp_path)
        reconstructed = file_and_variable_to_attr(file_path, variable, tmp_path)
        assert reconstructed == original


def pyproject_static(directory: Path, version: Version):
    toml_path = directory / "pyproject.toml"
    content = f"""\
        [project]
        name = "test"
        version = "{version}"
        """
    toml_path.write_text(textwrap.dedent(content))


def pyproject_attr(
    directory: Path, attr: str, version: Version, package_root: Path | None = None
):
    toml_path = directory / "pyproject.toml"

    content = f"""\
        [project]
        name = "test"
        dynamic = ["version"]

        [tool.setuptools.dynamic]
        version = {{attr = "{attr}"}}
        """

    if isinstance(package_root, str):
        package_root = Path(package_root)

    if package_root is None:
        package_root = directory
    elif not package_root.is_absolute():
        package_root = directory / package_root

    # We use PurePosixPath because TOMLs always have forward slashes; would break on Windows
    if (relative := package_root.relative_to(directory)) != Path("."):
        content += f"""
        [tool.setuptools.packages.find]
        where = ["{PurePosixPath(relative)}"]
        """

    toml_path.write_text(textwrap.dedent(content))
    file_path, variable = attr_to_file_and_variable(attr, package_root=package_root)
    abs_path = directory / file_path
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(f'{variable} = "{version}"')


def pyproject_file(directory: Path, file_path: Path | str, version: Version):
    toml_path = directory / "pyproject.toml"

    if isinstance(file_path, str):
        file_path = Path(file_path)
    if file_path.is_absolute() == directory.is_absolute():
        relative = file_path.relative_to(directory)
    elif directory.is_absolute():
        relative = file_path
    else:
        raise ValueError("Absolute file_path + relative directory")

    # We use PurePosixPath because TOMLs always have forward slashes; would break on Windows
    content = f"""\
        [project]
        name = "test"
        dynamic = ["version"]

        [tool.setuptools.dynamic]
        version = {{file = "{PurePosixPath(relative)}"}}
        """
    toml_path.write_text(textwrap.dedent(content))

    abs_path = directory / file_path

    abs_path.parent.mkdir(parents=True, exist_ok=True)
    abs_path.write_text(str(version))


class TestExtractVersion:
    """Tests for extract_version and _resolve_attr."""

    def test_static_version(self, tmp_path):
        pyproject_static(tmp_path, Version("2.3.4"))
        assert extract_version(tmp_path) == Version("2.3.4")

    @pytest.mark.parametrize("package_root", ["src", None])
    def test_dynamic_attr_module_file(self, tmp_path, package_root):
        """attr = 'pkg.__version__.__version__' -> pkg/__version__.py"""
        pyproject_attr(
            tmp_path,
            "pkg.__version__.__version__",
            Version("5.6.7"),
            package_root=package_root,
        )
        assert extract_version(tmp_path) == Version("5.6.7")

    @pytest.mark.parametrize(
        "package_root", [Path(), Path("src"), Path("nested") / "src"]
    )
    def test_dynamic_attr_init(self, tmp_path, package_root):
        """attr = 'pkg.__version__' -> pkg/__init__.py"""
        (tmp_path / package_root / "pkg").mkdir(parents=True)
        pyproject_attr(
            tmp_path, "pkg.__version__", Version("3.2.1"), package_root=package_root
        )
        assert extract_version(tmp_path) == Version("3.2.1")

    @pytest.mark.parametrize(
        "file_path",
        [
            "VERSION.txt",
            Path("meta") / "VERSION.txt",
            Path("nested") / "meta" / "VERSION.txt",
        ],
    )
    def test_dynamic_file(self, tmp_path, file_path):
        pyproject_file(tmp_path, file_path, Version("4.5.6"))
        assert extract_version(tmp_path) == Version("4.5.6")

    @pytest.mark.parametrize(
        "toml, match",
        [
            pytest.param(
                """\
                [project]
                name = "test"
                """,
                "no static version",
                id="no_version_and_no_dynamic",
            ),
            pytest.param(
                """\
                [project]
                name = "test"
                dynamic = ["version"]
                """,
                "not configured",
                id="dynamic_without_setuptools_config",
            ),
            pytest.param(
                """\
                [project]
                name = "test"
                dynamic = ["version"]

                [tool.setuptools.dynamic]
                version = {something_else = "bad"}
                """,
                "neither 'attr' nor 'file'",
                id="dynamic_without_attr_or_file",
            ),
            pytest.param(
                """\
                [project]
                name = "test"
                dynamic = ["version"]

                [tool.setuptools.dynamic]
                version = {attr = "nodots"}
                """,
                "expected 'module.attribute' form",
                id="attr_no_dot",
            ),
        ],
    )
    def test_invalid_pyproject(self, tmp_path, toml, match):
        (tmp_path / "pyproject.toml").write_text(textwrap.dedent(toml))
        with pytest.raises(ValueError, match=match):
            extract_version(tmp_path)

    def test_attr_file_not_found(self, tmp_path):
        pyproject_attr(tmp_path, "nonexistent.__version__", Version("1.0.0"))
        (tmp_path / "nonexistent.py").unlink()
        with pytest.raises(FileNotFoundError, match="does not exist"):
            extract_version(tmp_path)

    @pytest.mark.parametrize(
        "source_content, match",
        [
            pytest.param(
                'other_var = "1.0.0"\n',
                "__version__ not found",
                id="variable_not_found",
            ),
            pytest.param(
                "__version__ = compute_version()\n",
                "not a string literal",
                id="non_literal",
            ),
        ],
    )
    def test_attr_unresolvable(self, tmp_path, source_content, match):
        """The attr resolves to a file, but the file content is invalid."""
        attr = "pkg.__version__"
        pyproject_attr(tmp_path, attr, Version("1.0.0"))
        file_path, _ = attr_to_file_and_variable(attr, tmp_path)
        file_path.write_text(source_content)

        with pytest.raises(ValueError, match=match):
            extract_version(tmp_path)

    def test_attr_skips_non_assignment_nodes(self, tmp_path):
        """Non-Assign nodes (e.g. imports) are skipped when searching for the variable."""
        (tmp_path / "pkg").mkdir()
        pyproject_attr(tmp_path, "pkg.__version__", Version("1.2.3"))
        # Prepend an import to exercise the non-Assign branch
        file_path, _ = attr_to_file_and_variable("pkg.__version__", tmp_path)
        file_path.write_text('import os\n__version__ = "1.2.3"\n')

        assert extract_version(tmp_path) == Version("1.2.3")

    def test_invalid_static_version(self, tmp_path):
        """A malformed static version annotates the exception with its source location."""
        toml = """\
            [project]
            name = "test"
            version = "not-a-version"
            """
        (tmp_path / "pyproject.toml").write_text(textwrap.dedent(toml))
        with pytest.raises(InvalidVersion, match="not-a-version") as exc_info:
            extract_version(tmp_path)
        assert any("[project] version" in n for n in exc_info.value.__notes__)

    def test_invalid_attr_version(self, tmp_path):
        """A malformed attr-resolved version annotates the exception with the attr source."""
        pyproject_attr(tmp_path, "pkg.__version__", Version("1.0.0"))
        file_path, _ = attr_to_file_and_variable("pkg.__version__", tmp_path)
        file_path.write_text('__version__ = "also-not-valid"\n')
        with pytest.raises(InvalidVersion, match="also-not-valid") as exc_info:
            extract_version(tmp_path)
        assert any(
            "attr" in n and "pkg.__version__" in n for n in exc_info.value.__notes__
        )

    def test_invalid_file_version(self, tmp_path):
        """A malformed file-read version annotates the exception with the file path."""
        pyproject_file(tmp_path, "VERSION.txt", Version("1.0.0"))
        (tmp_path / "VERSION.txt").write_text("garbage!")
        with pytest.raises(InvalidVersion, match="garbage!") as exc_info:
            extract_version(tmp_path)
        assert any("VERSION.txt" in n for n in exc_info.value.__notes__)


class TestMain:
    """Tests for the CLI entry point."""

    def test_main_writes_github_output(self, tmp_path, monkeypatch, capsys):
        pyproject_static(tmp_path, Version("2.3.4"))
        output_file = tmp_path / "github_output.txt"
        monkeypatch.setenv("GITHUB_OUTPUT", str(output_file))
        monkeypatch.setattr(sys, "argv", ["extract_version.py", str(tmp_path)])

        main()

        captured = capsys.readouterr()
        assert "Extracted version: 2.3.4" in captured.out
        assert output_file.read_text(encoding="utf-8") == "version=2.3.4\n"

    def test_main_without_github_output(self, tmp_path, monkeypatch, capsys):
        pyproject_static(tmp_path, Version("1.0.0"))
        monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
        monkeypatch.setattr(sys, "argv", ["extract_version.py", str(tmp_path)])

        main()

        captured = capsys.readouterr()
        assert "Extracted version: 1.0.0" in captured.out

    def test_main_invalid_project(self, tmp_path, monkeypatch, capsys):
        """main() prints the error to stderr and exits 1 on bad pyproject.toml."""
        toml = """\
            [project]
            name = "test"
            """
        (tmp_path / "pyproject.toml").write_text(textwrap.dedent(toml))
        monkeypatch.setattr(sys, "argv", ["extract_version.py", str(tmp_path)])

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
        captured = capsys.readouterr()
        assert "no static version" in captured.err
