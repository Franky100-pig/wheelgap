"""Real lockfiles from open-source projects (#227), read offline: tests/fixtures/real/ and its sources.toml.

scripts/check_real_lockfiles.py has the checks; the weekly lockfile-corpus workflow runs them on each project's
current lockfile.
"""

import importlib.util
import re
import sys
import urllib.error
from collections import Counter
from pathlib import Path

import pytest

import wheelgap
from wheelgap.check import Status
from wheelgap.sources import SourceError, file_kind


def _script():
    path = Path(__file__).parents[1] / "scripts" / "check_real_lockfiles.py"
    spec = importlib.util.spec_from_file_location("wheelgap_check_real_lockfiles", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _script()
REAL = Path(__file__).parent / "fixtures" / "real"

# file: (packages wheelgap reads, (files, wheels) it reads from them, {skipped package: why}, some (name, version) pairs
# it must read). The file counts match the URLs in each lock, except that webcolors' pdm.lock lists coverage twice
# (as coverage and coverage[toml]), and wheelgap reads it once. A Pipfile.lock records no filenames.
EXPECTED = {
    "pluggy/uv.lock": (
        22,
        (264, 243),
        {"pluggy": "editable"},
        [("pytest", "9.1.1"), ("pre-commit", "4.6.0"), ("pyyaml", "6.0.3")],
    ),
    "iniconfig/uv.lock": (
        14,
        (230, 217),
        {"iniconfig": "editable"},
        [("hypothesis", "6.168.5"), ("hypothesis", "6.170.15"), ("pytest", "8.4.2")],
    ),
    "tomlkit/poetry.lock": (54, (485, 431), {}, [("filelock", "3.19.1"), ("filelock", "4.0.12"), ("sphinx", "4.5.0")]),
    "cleo/poetry.lock": (53, (376, 323), {}, [("pytest", "8.0.2"), ("mypy", "1.8.0"), ("towncrier", "23.11.0")]),
    "webcolors/pdm.lock": (10, (150, 140), {}, [("pytest", "8.4.2"), ("coverage", "7.11.0"), ("pluggy", "1.6.0")]),
    "pdfly/pylock.toml": (104, (103, 102), {"pdfly": "directory"}, [("cryptography", "46.0.5"), ("pypdf", "6.9.0")]),
    "pipenv/Pipfile.lock": (85, (0, 0), {}, [("virtualenv", "21.15.1"), ("sphinx", "9.0.4"), ("pytest", "9.2.0")]),
}


def test_manifest_lists_every_copy_with_its_provenance():
    entries = script.manifest()
    assert sorted(e["file"] for e in entries) == sorted(EXPECTED)
    on_disk = sorted(str(p.relative_to(REAL).as_posix()) for p in REAL.glob("*/*"))
    assert on_disk == sorted(EXPECTED)
    for e in entries:
        assert re.fullmatch(r"[0-9a-f]{40}", e["commit"]), e
        assert e["licence"] in {"MIT", "BSD-2-Clause", "BSD-3-Clause", "Apache-2.0"}, e
        assert Path(e["path"]).name == Path(e["file"]).name, e
        assert (REAL / e["file"]).stat().st_size < 100_000, e  # keep the copies small
    # Two uv.lock, two poetry.lock (Poetry 1.x and 2.x), a pdm.lock, a pylock.toml and a Pipfile.lock.
    kinds = Counter(file_kind(REAL / e["file"]) for e in entries)
    assert kinds == {"uv": 2, "poetry": 2, "pdm": 1, "pylock": 1, "pipfile": 1}
    generated = {(REAL / f).read_text(encoding="utf-8").split("\n")[0] for f in EXPECTED if f.endswith("poetry.lock")}
    assert sorted(major for line in generated for major in re.findall(r"by Poetry (\d)\.", line)) == ["1", "2"]


@pytest.mark.parametrize("file", sorted(EXPECTED))
def test_real_lockfile_reads_cleanly(file):
    count, (files, wheels), skipped, known = EXPECTED[file]
    source, problems = script.problems(REAL / file)
    assert problems == []
    assert source is not None
    assert len(source.packages) == count
    read_files = [f for p in source.packages for f in p.files or []]
    assert (len(read_files), sum(f.endswith(".whl") for f in read_files)) == (files, wheels)
    assert {p.name: p.skip for p in source.packages if p.skip} == skipped
    read = {(p.key, p.version) for p in source.packages}
    assert set(known) <= read


@pytest.mark.parametrize("file", sorted(EXPECTED))
def test_real_lockfile_offline_has_no_unknowns(file, fake_pypi):
    if file.endswith("Pipfile.lock"):  # it records hashes, not filenames: every package needs the index
        with pytest.raises(SourceError, match="needs the index for 85 packages"):
            wheelgap.check_lockfile(REAL / file, script.TARGETS, offline=True)
    else:
        results = wheelgap.check_lockfile(REAL / file, script.TARGETS, offline=True)
        assert results
        assert not [r.name for r in results if Status.UNKNOWN in r.statuses.values()]
    assert fake_pypi == []


UV_HEAD = 'version = 1\nrevision = 3\nrequires-python = ">=3.10"\n\n'
UV_ROOT = '[[package]]\nname = "app"\nversion = "0.1.0"\nsource = { editable = "." }\n'


@pytest.mark.parametrize(
    ("name", "text", "problem"),
    [
        ("uv.lock", "this isn't = = toml", "can't read it: TOMLDecodeError"),
        ("uv.lock", UV_HEAD, "no packages"),
        (
            "uv.lock",
            UV_HEAD + UV_ROOT + 'dependencies = [{ name = "a" }]\n\n[[package]]\nname = "a"\nversion = "1.0"\n'
            'source = { registry = "https://pypi.org/simple" }\n',
            "a 1.0: no files read (an empty list)",
        ),
        (
            "uv.lock",
            UV_HEAD + UV_ROOT + 'dependencies = [{ name = "a" }]\n\n[[package]]\nname = "a"\nversion = "one"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            'wheels = [{ url = "https://x/a-1.0-py3-none-any.whl" }]\n',
            "a: version 'one' doesn't parse",
        ),
        (
            "uv.lock",
            UV_HEAD + UV_ROOT + 'dependencies = [{ name = "a", marker = "os_name =! \'nt\'" }]\n\n'
            '[[package]]\nname = "a"\nversion = "1.0"\nsource = { registry = "https://pypi.org/simple" }\n'
            'wheels = [{ url = "https://x/a-1.0-py3-none-any.whl" }]\n',
            "dependency on a: marker \"os_name =! 'nt'\" doesn't parse",
        ),
        (
            "uv.lock",
            UV_HEAD + UV_ROOT + '\n[[package]]\nname = "orphan"\nversion = "1.0"\n'
            'source = { registry = "https://pypi.org/simple" }\n'
            'wheels = [{ url = "https://x/orphan-1.0-py3-none-any.whl" }]\n',
            "orphan 1.0: n/a on every target",
        ),
        (
            "uv.lock",
            UV_HEAD + UV_ROOT + 'dependencies = [{ name = "a" }]\n\n[[package]]\nname = "a"\nversion = "1.0"\n'
            'source = { registry = "https://pypi.org/simple" }\nsdist = { url = "https://x/a-1.0.tar.gz" }\n'
            'wheel = [{ url = "https://x/a-1.0-py3-none-any.whl" }]\n',  # `wheels` renamed
            "no wheels read, only sdists (1)",
        ),
        (
            "pylock.toml",
            'lock-version = "1.0"\n[[packages]]\nname = "a"\nversion = "1.0"\n'
            '[[packages.wheels]]\nurl = "https://x/a-1.0.whl"\n',
            "a 1.0: wheel name 'a-1.0.whl' doesn't parse",
        ),
        (
            "pylock.toml",
            'lock-version = "1.0"\n[[packages]]\nname = "a"\nversion = "1.0"\n[packages.sdist]\nurl = "https://x/a-1.0.exe"\n',
            "a 1.0: 'a-1.0.exe' is neither a wheel nor an sdist",
        ),
        (
            "pdm.lock",
            '[metadata]\nlock_version = "4.5.0"\n[[package]]\nname = "a"\nversion = "1.0"\ngit = "https://x/a.git"\n',
            "a 1.0: skipped as git",
        ),
        (
            "poetry.lock",
            '[[package]]\nname = "a"\nversion = "1.0"\nmarkers = "python_version >> \'3\'"\n'
            'files = [{ file = "a-1.0-py3-none-any.whl", hash = "sha256:00" }]\n'
            '[metadata]\nlock-version = "2.1"\n',
            "a 1.0: marker \"python_version >> '3'\" doesn't parse",
        ),
        (
            "Pipfile.lock",
            '{"_meta": {}, "default": {"a": {"version": "==1.0"}, "b": {"version": "==1.0", "index": "private"}}}',
            "b 1.0: skipped as private index",
        ),
    ],
)
def test_problems_catches_misreads(tmp_path, name, text, problem):
    path = tmp_path / name
    path.write_text(text, encoding="utf-8")
    _, problems = script.problems(path)
    assert any(p.startswith(problem) for p in problems), problems


def test_main_names_the_failing_project(tmp_path, monkeypatch, capsys):
    def download(repo, path, folder):
        if repo == "pytest-dev/pluggy":
            raise urllib.error.URLError("no route")
        if repo == "python-poetry/tomlkit":
            broken = folder / "poetry.lock"
            broken.write_text('[[package]]\nname = "a"\nversion = "x.y"\n', encoding="utf-8")
            return broken
        return REAL / next(e["file"] for e in script.manifest() if e["repo"] == repo)

    monkeypatch.setattr(script, "download", download)
    assert script.main([]) == 1
    out = capsys.readouterr().out
    assert "FAIL pytest-dev/pluggy uv.lock (default branch)\n    can't download it: <urlopen error no route>" in out
    assert "FAIL python-poetry/tomlkit poetry.lock (default branch)\n    a: version 'x.y' doesn't parse" in out
    assert "ok   pypa/pipenv Pipfile.lock (default branch): 85 packages" in out
    assert out.endswith(
        "2 lockfiles failed: pytest-dev/pluggy uv.lock (default branch); "
        "python-poetry/tomlkit poetry.lock (default branch)\n"
    )


def test_main_pinned_reads_the_copies_offline(monkeypatch, capsys):
    monkeypatch.setattr(script, "download", lambda *a: pytest.fail("--pinned downloaded something"))
    assert script.main(["--pinned", "cleo", "pdfly"]) == 0
    out = capsys.readouterr().out
    assert out == (
        "ok   python-poetry/cleo poetry.lock (pinned copy): 53 packages\n"
        "ok   py-pdf/pdfly pylock.toml (pinned copy): 104 packages\n"
    )
