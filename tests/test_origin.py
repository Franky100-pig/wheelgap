"""origin.known_pypi: whether a package is positively PyPI's, read from each kind of input."""

import pytest
from test_conda_forge import PDM_LOCK, PIPFILE_CORP, PIPFILE_PYPI, POETRY_LOCK, PYPROJECT, pylock, uv_lock
from test_sources import PIXI_V6

from wheelgap import origin, pypi
from wheelgap.sources import Package, Source, load_inputs

EMPTY: dict[str, str] = {}


def package(tmp_path, files, name="nowheels"):
    """The input's package `name`, after the lookups a run would make (fake_pypi), and its Source."""
    first = next(iter(files))
    if "==" in first:  # a requirement on the command line
        sources = load_inputs([first])
    else:
        for filename, text in files.items():
            (tmp_path / filename).write_text(text, encoding="utf-8")
        sources = load_inputs([str(tmp_path / first)])
    packages = [p for s in sources for p in s.packages]
    pypi.fill_all(packages)
    return next((p, s) for s in sources for p in s.packages if p.key == name)


@pytest.mark.parametrize(
    "files,known",
    [
        ({"nowheels==0.9": ""}, True),  # a name on the command line, found on pypi.org
        ({"requirements.txt": "nowheels==0.9\n"}, True),
        ({"requirements.txt": "--index-url https://pypi.corp.example/simple\nnowheels==0.9\n"}, False),
        ({"requirements.txt": "nowheels==0.9\n", "uv.toml": "index-url = 'https://x.example/simple'\n"}, False),
        ({"pyproject.toml": PYPROJECT}, True),
        ({"pyproject.toml": PYPROJECT + "\n[[tool.uv.index]]\nurl = 'https://pypi.corp.example/simple'\n"}, False),
        ({"pyproject.toml": PYPROJECT, "uv.toml": "[pip]\nindex-url = 'https://x.example/simple'\n"}, False),
        ({"Pipfile.lock": PIPFILE_PYPI}, True),
        ({"Pipfile.lock": PIPFILE_CORP}, False),
        ({"uv.lock": uv_lock("https://pypi.org/simple")}, True),
        ({"uv.lock": uv_lock("https://pkgs.example.com/simple")}, False),
        ({"pylock.toml": pylock("https://pypi.org/simple")}, True),
        ({"pylock.toml": pylock(None)}, False),
        ({"pdm.lock": PDM_LOCK}, False),
        ({"poetry.lock": POETRY_LOCK}, False),
    ],
)
def test_known_pypi_by_input(fake_pypi, tmp_path, files, known):
    pkg, source = package(tmp_path, files)
    assert origin.known_pypi(pkg, source, env=EMPTY) is known


def test_pixi_lock_entries_are_not_known(fake_pypi, tmp_path):
    # Looked up on pypi.org (pixi.lock keeps one file per platform), but a pixi.lock isn't Source.pypi_only.
    pkg, source = package(tmp_path, {"pixi.lock": PIXI_V6.replace("INDEX", "https://pypi.org/simple")}, "purewheel")
    assert pkg.found_on_pypi and not origin.known_pypi(pkg, source, env=EMPTY)


@pytest.mark.parametrize("variable", origin.INDEX_ENV)
def test_an_index_in_the_environment(fake_pypi, tmp_path, variable):
    env = {variable: "https://pypi.corp.example/simple"}
    pkg, source = package(tmp_path, {"requirements.txt": "nowheels==0.9\n"})
    assert origin.known_pypi(pkg, source, env=EMPTY)
    assert not origin.known_pypi(pkg, source, env=env)  # found on pypi.org, but pip or uv may use another index
    assert origin.known_pypi(pkg, source, env={variable: ""})  # an empty variable sets nothing
    pkg, source = package(tmp_path, {"uv.lock": uv_lock("https://pypi.org/simple")})
    assert origin.known_pypi(pkg, source, env=env)  # the lock says it came from pypi.org


@pytest.mark.parametrize(
    "mirror,known", [("https://mirror.example.com/pypi", False), ("https://pypi.org/pypi/", True), ("", True)]
)
def test_wheelgap_index_url_mirror(mirror, known):
    pkg = Package("nowheels", "0.9", registry="https://pypi.org/simple")
    assert origin.known_pypi(pkg, Source("uv.lock"), env={"WHEELGAP_INDEX_URL": mirror}) is known
    assert origin.other_index(env={"WHEELGAP_INDEX_URL": mirror}) == (None if known else "WHEELGAP_INDEX_URL")


def test_index_url_and_skipped_packages():
    pkg = Package("nowheels", "0.9", registry="https://pypi.org/simple")
    assert origin.known_pypi(pkg, Source("uv.lock"), env=EMPTY)
    assert not origin.known_pypi(pkg, Source("uv.lock"), env=EMPTY, simple_index="https://x.example/simple")
    assert origin.other_index("https://x.example/simple", EMPTY) == "--index-url"
    pkg.skip = "private index"
    assert not origin.known_pypi(pkg, Source("uv.lock"), env=EMPTY)


@pytest.mark.parametrize(
    "found_on_pypi,pypi_only,known", [(True, True, True), (True, False, False), (False, True, False)]
)
def test_found_on_pypi_and_pypi_only_both_needed(found_on_pypi, pypi_only, known):
    pkg = Package("nowheels", "0.9", found_on_pypi=found_on_pypi)
    assert origin.known_pypi(pkg, Source("requirements.txt", pypi_only=pypi_only), env=EMPTY) is known


def test_the_default_environment_is_os_environ(monkeypatch):
    pkg = Package("nowheels", "0.9", found_on_pypi=True)
    source = Source("command line", pypi_only=True)
    assert origin.known_pypi(pkg, source)
    monkeypatch.setenv("UV_INDEX", "corp=https://pypi.corp.example/simple")
    assert not origin.known_pypi(pkg, source)
