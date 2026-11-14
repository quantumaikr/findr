"""every data file the package reads is covered by the package-data globs (a missing one silently drops a feature when installed)"""
import fnmatch
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_every_data_file_under_findr_is_in_package_data():
    pd = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]
    miss = []
    for f in (ROOT / "findr").rglob("*"):
        if f.is_file() and f.suffix not in (".py", ".pyc") and "__pycache__" not in f.parts:
            pkg = ".".join(f.parent.relative_to(ROOT).parts)
            if not any(fnmatch.fnmatch(f.name, g) for g in pd.get(pkg, [])):
                miss.append(str(f.relative_to(ROOT)))
    assert not miss, miss


def test_files_loaded_by_name_exist_and_are_covered():
    pd = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["tool"]["setuptools"]["package-data"]
    for n in ("doctype_heads.json", "xling_extra.json"):
        assert (ROOT / "findr/core" / n).is_file() and any(fnmatch.fnmatch(n, g) for g in pd["findr.core"])
