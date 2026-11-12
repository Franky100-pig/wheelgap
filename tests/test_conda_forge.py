import gzip
import io
import json
import threading
import time
import tracemalloc
import urllib.error

import pytest
from conftest import FAKE_CONDA_FORGE, conda_file, cp
from test_cli import _assert_matches_output_schema, run

from wheelgap import cache, condaforge, pypi
from wheelgap.targets import Target

API = "https://api.anaconda.org/package/conda-forge/"
FLAGS = ["-p", "3.13", "--explain", "--conda-forge"]


def conda_calls(calls):
    return [url for url in calls if url.startswith(API)]


def json_hints(capsys, *args):
    code, out, err = run(capsys, *args, "--json")
    data = json.loads(out)
    _assert_matches_output_schema(data)
    return {p["name"]: p.get("conda_forge") for p in data["packages"]}, err


def test_match_is_a_hint_only(capsys, fake_pypi):
    # nowheels 0.9 fits linux-64 and linux-aarch64 on 3.13. Its osx-arm64 build is marked broken, its win-64 build is
    # for 3.12, its ppc64le build is of 0.8, and musl never matches.
    args = ["nowheels==0.9"]
    for t in ("linux-aarch64", "musl-x86_64", "macos-arm64", "win-amd64", "linux-ppc64le"):
        args += ["-t", t]
    code_plain, out_plain, err_plain = run(capsys, *args, "-p", "3.13", "--explain")
    assert not conda_calls(fake_pypi)
    code, out, err = run(capsys, *args, *FLAGS)
    assert conda_calls(fake_pypi) == [API + "nowheels"]
    hint = "nowheels: conda-forge has a package named nowheels with 0.9 builds for linux-aarch64\n\n"
    assert hint in out
    assert code == code_plain == 1
    assert out.replace(hint, "") == out_plain and err == err_plain  # the statuses and the summary don't change


def test_json_key_under_explain_only(capsys, fake_pypi):
    args = [
        "nowheels==0.9",
        "winonly==311",
        "-p",
        "3.13",
        "-t",
        "linux-x86_64",
        "-t",
        "linux-aarch64",
        "-t",
        "musl-x86_64",
    ]
    hints, _ = json_hints(capsys, *args, "--explain", "--conda-forge")
    assert hints == {
        "nowheels": {
            "name": "nowheels",
            "version": "0.9",
            "builds": {"linux-x86_64": ["linux-64"], "linux-aarch64": ["linux-aarch64"]},
        },
        # A noarch build with gaps on glibc and musl: glibc only.
        "winonly": {
            "name": "winonly",
            "version": "311",
            "builds": {"linux-x86_64": ["noarch"], "linux-aarch64": ["noarch"]},
        },
    }
    code, out, _ = run(capsys, *args, "--json", "--explain", "--conda-forge")
    plain_code, plain, _ = run(capsys, *args, "--json", "--explain")
    data = json.loads(out)
    assert all("conda_forge" not in p for p in json.loads(plain)["packages"])
    for p in data["packages"]:
        del p["conda_forge"]
    assert data == json.loads(plain) and code == plain_code  # nothing else changes: statuses, summary, exit code


def test_subdirs_are_the_live_ones():
    # conda-forge has no current Python on linux-32, linux-armv7l, linux-s390x or win-32.
    assert condaforge.SUBDIRS == {
        "linux-x86_64": "linux-64",
        "linux-aarch64": "linux-aarch64",
        "linux-ppc64le": "linux-ppc64le",
        "linux-riscv64": "linux-riscv64",
        "macos-x86_64": "osx-64",
        "macos-arm64": "osx-arm64",
        "win-amd64": "win-64",
        "win-arm64": "win-arm64",
    }


ALL_SUBDIRS = [*condaforge.SUBDIRS.values(), "linux-32", "linux-armv7l", "linux-s390x", "win-32"]


@pytest.mark.parametrize(
    "platform", [*condaforge.SUBDIRS, "linux-i686", "linux-armv7l", "linux-s390x", "win32", "musl-x86_64"]
)
def test_each_platform_matches_its_own_subdir_only(capsys, fake_pypi, monkeypatch, platform):
    # A build on every subdir: each target matches exactly its own, and the dead or missing ones none.
    monkeypatch.setitem(FAKE_CONDA_FORGE, "nowheels", [conda_file("0.9", s, *cp(13)) for s in ALL_SUBDIRS])
    hints, _ = json_hints(capsys, "nowheels==0.9", "-t", platform, *FLAGS)
    expected = condaforge.SUBDIRS.get(platform)
    assert hints["nowheels"] == (
        {"name": "nowheels", "version": "0.9", "builds": {platform: [expected]}} if expected else None
    )


def test_noarch_os_constraint(capsys, fake_pypi, monkeypatch):
    monkeypatch.setitem(FAKE_CONDA_FORGE, "forked", [conda_file("1.0", "noarch", "python >=3.8", "__win")])
    _, out, _ = run(capsys, "forked==1.0", "-t", "linux-x86_64", "-t", "win-amd64", *FLAGS)
    assert "forked: conda-forge has a package named forked with 1.0 builds for win-amd64 (noarch)\n" in out


def test_musl_pyodide_and_mobile_look_nothing_up(capsys, fake_pypi):
    args = ["-t", "musl-x86_64", "-t", "pyodide", "-t", "ios-arm64", "-t", "android-arm64"]
    _, out, _ = run(capsys, "winonly==311", *args, *FLAGS)
    assert "conda-forge" not in out and not conda_calls(fake_pypi)


def test_gap_targets_only(capsys, fake_pypi, monkeypatch):
    # fastlib has a wheel for linux-x86_64: only linux-aarch64 is a gap, so only it gets a hint.
    monkeypatch.setitem(
        FAKE_CONDA_FORGE,
        "fastlib",
        [conda_file("2.0.0", "linux-64", *cp(13)), conda_file("2.0.0", "linux-aarch64", *cp(13))],
    )
    hints, _ = json_hints(capsys, "fastlib==2.0.0", "-t", "linux-x86_64", "-t", "linux-aarch64", *FLAGS)
    assert hints["fastlib"]["builds"] == {"linux-aarch64": ["linux-aarch64"]}


def test_python_and_abi_are_checked(capsys, fake_pypi, monkeypatch):
    monkeypatch.setitem(FAKE_CONDA_FORGE, "nowheels", [conda_file("0.9", "linux-64", *cp(13))])
    args = [
        "nowheels==0.9",
        "-p",
        "3.13",
        "-p",
        "3.13t",
        "-p",
        "3.14",
        "-t",
        "linux-x86_64",
        "--explain",
        "--conda-forge",
    ]
    hints, _ = json_hints(capsys, *args)
    assert hints["nowheels"]["builds"] == {"3.13 linux-x86_64": ["linux-64"]}  # not free-threaded, not 3.14
    monkeypatch.setitem(FAKE_CONDA_FORGE, "nowheels", [conda_file("0.9", "linux-64", *cp(13, "t"))])
    hints, _ = json_hints(capsys, *args, "--no-cache")
    assert hints["nowheels"]["builds"] == {"3.13t linux-x86_64": ["linux-64"]}


def T(python, platform="linux-x86_64"):
    return Target(int(python[0]), int(python.rstrip("t").split(".")[1]), python.endswith("t"), platform)


def B(subdir="linux-64", abi=None, python=None, os=()):
    return {"subdir": subdir, "abi": abi, "python": python, "os": list(os)}


@pytest.mark.parametrize(
    "build,target,fits",
    [
        (B(abi="cp313", python=[]), T("3.13"), True),
        (B(abi="cp313", python=[]), T("3.13t"), False),
        (B(abi="cp313t", python=["3.13.*"]), T("3.13t"), True),
        (B(abi="cp313t", python=[]), T("3.13"), False),
        (B(abi="cp313", python=[]), T("3.14"), False),
        (B(abi="pypy39_pp73", python=[]), T("3.9"), False),
        (B(abi="cp313", python=["3.12.*"]), T("3.13"), False),  # constraints that disagree: never
        (B("noarch", python=[">=3.10"]), T("3.13t"), True),  # pure Python works free-threaded too
        (B("noarch", python=[">=3.10"]), T("3.9"), False),
        (B("noarch", python=[]), T("3.13"), True),  # a bare `python` dependency
        (B("noarch"), T("3.13"), False),  # no python dependency: not a Python build (tzdata)
        (B("noarch", python=[], os=["__unix"]), T("3.13", "win-amd64"), False),
        (B("noarch", python=[], os=["__unix"]), T("3.13", "macos-arm64"), True),
        (B("noarch", python=[], os=["__osx"]), T("3.13", "linux-x86_64"), False),
        (B(python=[">=3.8,<3.9.0a0"]), T("3.8"), True),  # an old compiled build without python_abi
        (B(python=[">=3.8,<3.9.0a0"]), T("3.8t"), False),
        (B(python=[">=3.8,<3.9.0a0"]), T("3.9"), False),
        (B(python=[]), T("3.13"), False),  # compiled, with no way to tell its Python
        (B(python=["2.7*"]), T("3.13"), False),
        (B(python=["~=3.13"]), T("3.13"), False),  # can't be told: never counts
        (B("osx-64", abi="cp313", python=[]), T("3.13"), False),  # another subdir
        (B("win-32", abi="cp313", python=[]), T("3.13", "win32"), False),  # a dead subdir
    ],
)
def test_fits(build, target, fits):
    assert condaforge.fits(build, target) is fits


@pytest.mark.parametrize(
    "spec,python,holds",
    [
        (">=3.10", (3, 13), True),
        ("<=3.9", (3, 9), True),
        ("<3.9.0a0", (3, 9), False),
        (">=3.9.1", (3, 9), True),
        (">3.9", (3, 9), True),  # 3.9.1 and later
        ("!=3.9.0", (3, 9), True),
        ("==3.9", (3, 9), True),
        ("3.6|3.7", (3, 7), True),
        ("3.6|3.7", (3, 8), False),
        ("2.7*", (2, 7), True),
        ("3.13.*", (3, 13), True),
        ("3.13.*", (3, 1), False),
        ("3", (3, 13), True),
        (">=3.8,<3.9.0a0", (3, 8), True),
        ("3.9.1", (3, 9), None),  # one patch release
        ("~=3.9", (3, 9), None),
        (">=3.9.*", (3, 9), None),
        (">=x", (3, 9), None),
    ],
)
def test_holds(spec, python, holds):
    assert condaforge._holds(spec, *python) is holds


def test_parse_builds_keeps_the_version_s_main_unbroken_builds():
    files = [
        conda_file("0.9", "linux-64", *cp(13)),
        conda_file("0.9", "linux-64", *cp(13)),  # the same kind of build twice: kept once
        conda_file("0.9", "osx-arm64", *cp(13), labels=("main", "broken")),
        conda_file("0.9", "win-64", *cp(13), labels=("cf202003",)),
        conda_file("0.8", "linux-aarch64", *cp(13)),
        conda_file("0.9.0", "noarch", "python >=3.8", "__unix >=0"),  # 0.9.0 is 0.9
        {"version": "0.9", "attrs": {"subdir": "linux-64"}},  # no labels
        {"version": "0.9", "labels": ["main"], "attrs": {}},  # no subdir
        "junk",
    ]
    assert condaforge.parse_builds({"files": files}, "0.9") == [
        {"subdir": "linux-64", "abi": "cp313", "python": [], "os": []},
        {"subdir": "noarch", "abi": None, "python": [">=3.8"], "os": ["__unix"]},
    ]


@pytest.mark.parametrize("data", [[], {}, {"files": None}, {"files": "x"}])
def test_unexpected_answers_are_a_failed_lookup(monkeypatch, data):
    monkeypatch.setattr(pypi, "fetch_json", lambda url, **kw: data)
    assert condaforge.fetch("numpy", "1.0") is None


def test_markdown_shows_the_hint(capsys, fake_pypi):
    _, out, _ = run(capsys, "nowheels==0.9", "-t", "linux-aarch64", "--markdown", *FLAGS)
    assert "nowheels: conda-forge has a package named nowheels with 0.9 builds for linux-aarch64" in out


def uv_lock(registry):
    return (
        'version = 1\nrequires-python = ">=3.12"\n\n'
        '[[package]]\nname = "app"\nversion = "0"\nsource = { editable = "." }\n'
        'dependencies = [{ name = "nowheels" }]\n\n'
        f'[[package]]\nname = "nowheels"\nversion = "0.9"\nsource = {{ registry = "{registry}" }}\n'
        'sdist = { url = "https://files.example/nowheels-0.9.tar.gz" }\n'
    )


def pylock(index):
    line = f'index = "{index}"\n' if index else ""
    return (
        'lock-version = "1.0"\ncreated-by = "pip"\n\n[[packages]]\nname = "nowheels"\nversion = "0.9"\n'
        + line
        + 'sdist = { url = "https://files.example/nowheels-0.9.tar.gz", hashes = { sha256 = "00" } }\n'
    )


def pipfile(sources, index=None):
    entry = {"version": "==0.9"} | ({"index": index} if index else {})
    return json.dumps({"_meta": {"sources": sources}, "default": {"nowheels": entry}})


PYPI_SOURCE = {"name": "pypi", "url": "https://pypi.org/simple", "verify_ssl": True}
CORP_SOURCE = {"name": "corp", "url": "https://pypi.corp.example/simple", "verify_ssl": True}
PYPROJECT = "[project]\nname = 'app'\nversion = '0'\ndependencies = ['nowheels==0.9']\n"
PDM_LOCK = (
    '[metadata]\ngroups = ["default"]\nstrategy = ["inherit_metadata"]\n\n[[package]]\nname = "nowheels"\n'
    'version = "0.9"\nrequires_python = ">=3.9"\nfiles = [{file = "nowheels-0.9.tar.gz", hash = "sha256:00"}]\n'
)
POETRY_LOCK = (
    '[[package]]\nname = "nowheels"\nversion = "0.9"\ndescription = ""\noptional = false\npython-versions = ">=3.9"\n'
    'groups = ["main"]\nfiles = [{file = "nowheels-0.9.tar.gz", hash = "sha256:00"}]\n'
)


@pytest.mark.parametrize(
    "files,sent",
    [
        ({"requirements.txt": "nowheels==0.9\n"}, True),
        ({"requirements.txt": "--index-url https://pypi.corp.example/simple\nnowheels==0.9\n"}, False),
        ({"requirements.txt": "-i https://pypi.corp.example/simple\nnowheels==0.9\n"}, False),
        ({"requirements.txt": "--find-links ./wheels\nnowheels==0.9\n"}, False),
        (
            {
                "requirements.txt": "-r more.txt\nnowheels==0.9\n",
                "more.txt": "--extra-index-url https://x.example/simple\n",
            },
            False,
        ),
        ({"pyproject.toml": PYPROJECT}, True),
        ({"pyproject.toml": PYPROJECT + "\n[[tool.uv.index]]\nurl = 'https://pypi.corp.example/simple'\n"}, False),
        ({"Pipfile.lock": pipfile([PYPI_SOURCE], "pypi")}, True),
        ({"Pipfile.lock": pipfile([PYPI_SOURCE])}, True),  # the only source is PyPI
        ({"Pipfile.lock": pipfile([CORP_SOURCE, PYPI_SOURCE])}, False),  # no index: maybe the default, corp
        ({"Pipfile.lock": pipfile([])}, False),
        ({"uv.lock": uv_lock("https://pypi.org/simple")}, True),
        ({"uv.lock": uv_lock("https://pkgs.example.com/simple")}, False),
        ({"pylock.toml": pylock("https://pypi.org/simple")}, True),
        ({"pylock.toml": pylock(None)}, False),
        ({"pdm.lock": PDM_LOCK}, False),
        ({"poetry.lock": POETRY_LOCK}, False),
    ],
)
def test_only_names_known_to_be_on_pypi_are_sent(capsys, fake_pypi, tmp_path, files, sent):
    for name, text in files.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path / next(iter(files))), "-t", "linux-aarch64", *FLAGS)
    assert code == 1 and "nowheels" in out  # a gap either way
    assert conda_calls(fake_pypi) == [API + "nowheels"] * sent
    assert ("conda-forge has a package named nowheels" in out) is sent
    assert ("didn't look up 1 package whose index isn't known to be PyPI (nowheels)" in err) is not sent


def test_command_line_names_and_gaps_only(capsys, fake_pypi):
    # purewheel has a wheel everywhere: no gap, so its name is never sent.
    _, out, _ = run(capsys, "purewheel==3.1", "nowheels==0.9", "-t", "linux-aarch64", *FLAGS)
    assert conda_calls(fake_pypi) == [API + "nowheels"] and "named nowheels" in out


def test_same_name_from_pypi_and_a_private_index(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(uv_lock("https://pkgs.example.com/simple"), encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path / "uv.lock"), "nowheels==0.9", "-t", "linux-aarch64", *FLAGS, "--json")
    hints = [p.get("conda_forge") for p in json.loads(out)["packages"]]
    # The command line's nowheels is PyPI's and gets the hint; the lock's private one doesn't, though it's the same key.
    assert sorted(hints, key=bool) == [
        None,
        {"name": "nowheels", "version": "0.9", "builds": {"linux-aarch64": ["linux-aarch64"]}},
    ]
    assert conda_calls(fake_pypi) == [API + "nowheels"]
    assert "didn't look up 1 package" in err


def test_other_index_runs_look_nothing_up(capsys, fake_pypi, fake_simple, monkeypatch):
    args = ["nowheels==0.9", "-t", "linux-aarch64", *FLAGS]
    _, out, err = run(capsys, *args, "--index-url", "https://pkgs.example.com/simple")
    assert fake_simple.calls and not conda_calls(fake_pypi) and "conda-forge has" not in out
    assert "--conda-forge looks nothing up" in err
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example.com/pypi")
    _, out, err = run(capsys, *args)
    assert fake_pypi and not conda_calls(fake_pypi) and "--conda-forge looks nothing up" in err


def test_needs_explain_and_refuses_offline(capsys, fake_pypi, fixtures):
    with pytest.raises(SystemExit) as e:
        run(capsys, "nowheels==0.9", "--conda-forge")
    assert e.value.code == 2 and "pass --explain too" in capsys.readouterr().err
    code, _, err = run(capsys, str(fixtures / "uvproj" / "uv.lock"), "--offline", *FLAGS)
    assert code == 2 and "--offline: --conda-forge would look packages up on anaconda.org" in err
    assert not fake_pypi


@pytest.mark.parametrize(
    "failure",
    [
        urllib.error.HTTPError(API + "nowheels", 403, "Forbidden", {}, None),  # type: ignore[arg-type]
        urllib.error.HTTPError(API + "nowheels", 429, "Too Many Requests", {}, None),  # type: ignore[arg-type]
        urllib.error.HTTPError(API + "nowheels", 503, "Unavailable", {}, None),  # type: ignore[arg-type]
        ValueError("bad JSON"),
        TimeoutError("timed out"),  # a slow answer fails this package only
        ConnectionResetError("reset"),
        gzip.BadGzipFile("damaged"),
    ],
)
def test_a_failed_lookup_is_a_note_and_not_cached(capsys, fake_pypi, monkeypatch, failure):
    monkeypatch.setitem(FAKE_CONDA_FORGE, "nowheels", failure)
    args = ["nowheels==0.9", "-t", "linux-aarch64", "-p", "3.13", "--explain"]
    code_plain, out_plain, err_plain = run(capsys, *args)
    code, out, err = run(capsys, *args, "--conda-forge")
    assert (code, out) == (code_plain, out_plain)
    assert err == err_plain + err[len(err_plain) :] and "couldn't check 1 package (nowheels)" in err
    run(capsys, *args, "--conda-forge")
    assert conda_calls(fake_pypi) == [API + "nowheels"] * 2  # asked again: a failure isn't an answer
    assert cache.load_builds("nowheels", "0.9") is None


def test_quiet_suppresses_the_notes(capsys, fake_pypi, monkeypatch, tmp_path):
    monkeypatch.setitem(FAKE_CONDA_FORGE, "nowheels", TimeoutError("timed out"))
    (tmp_path / "requirements.txt").write_text("-i https://x.example/simple\nforked==1.0\n", encoding="utf-8")
    args = ["nowheels==0.9", str(tmp_path / "requirements.txt"), "-t", "linux-aarch64", *FLAGS, "--json"]
    _, _, err = run(capsys, *args)
    assert "couldn't check" in err and "didn't look up" in err
    _, _, err = run(capsys, *args, "-q")
    assert "conda-forge" not in err
    _, _, err = run(capsys, *args, "-q", "--index-url", "https://x.example/simple")
    assert "conda-forge" not in err


def test_answers_are_cached_but_not_with_no_cache(capsys, fake_pypi, monkeypatch):
    args = ["nowheels==0.9", "forked==1.0", "-t", "linux-aarch64", *FLAGS]
    run(capsys, *args, "--no-cache")
    assert sorted(conda_calls(fake_pypi)) == [API + "forked", API + "nowheels"]
    assert cache.load_builds("nowheels", "0.9") is None  # --no-cache saves nothing
    run(capsys, *args)
    fake_pypi.clear()
    _, out, _ = run(capsys, *args)
    # A match and a 404 are answers, kept for a day.
    assert not conda_calls(fake_pypi) and "named nowheels with 0.9 builds" in out
    assert cache.load_builds("nowheels", "0.9") == [
        {"subdir": "linux-64", "abi": "cp313", "python": [], "os": []},
        {"subdir": "linux-aarch64", "abi": "cp313", "python": [], "os": []},
        {"subdir": "win-64", "abi": "cp312", "python": [], "os": []},  # kept: matching happens per target
    ]
    assert cache.load_builds("forked", "1.0") == []
    run(capsys, *args, "--no-cache")
    assert len(conda_calls(fake_pypi)) == 2  # --no-cache doesn't read it either


def test_too_large_is_cached(capsys, fake_pypi, monkeypatch):
    monkeypatch.setitem(FAKE_CONDA_FORGE, "nowheels", pypi.ResponseTooLarge("over the cap"))
    args = ["nowheels==0.9", "-t", "linux-aarch64", *FLAGS]
    _, out, err = run(capsys, *args)
    assert "couldn't check 1 package (nowheels)" in err and "named nowheels" not in out
    assert cache.load_builds("nowheels", "0.9") == cache.TOO_LARGE
    _, _, err = run(capsys, *args)
    assert conda_calls(fake_pypi) == [API + "nowheels"] and "couldn't check 1 package" in err  # not fetched again


@pytest.mark.parametrize(
    "value",
    [
        "other text",
        {"subdir": "linux-64"},
        [{"subdir": "linux-64", "abi": None, "python": None}],  # a key missing
        [{"subdir": "linux-64", "abi": None, "python": None, "os": [], "extra": 1}],
        [{"subdir": 1, "abi": None, "python": None, "os": []}],
        [{"subdir": "linux-64", "abi": 1, "python": None, "os": []}],
        [{"subdir": "linux-64", "abi": None, "python": "3.13", "os": []}],
        [{"subdir": "linux-64", "abi": None, "python": [3], "os": []}],
        [{"subdir": "linux-64", "abi": None, "python": None, "os": "__win"}],
        [{"subdir": "linux-64", "abi": None, "python": None, "os": [1]}],
        ["linux-64"],
    ],
)
def test_load_builds_refuses_odd_entries(value):
    cache.save_builds("nowheels", "0.9", value)
    assert cache.load_builds("nowheels", "0.9") is None


def test_conda_forge_entries_are_keyed_apart_from_pypi_ones():
    good = [{"subdir": "linux-64", "abi": "cp313", "python": ["3.13.*"], "os": []}]
    cache.save_builds("nowheels", "0.9", good)
    assert cache.load_builds("nowheels", "0.9") == good
    assert cache.load_files(cache.CONDA_FORGE_KEY, "nowheels", "0.9") is None
    assert cache.CONDA_FORGE_KEY.startswith("condaforge-v1 ")


def test_names_are_quoted(monkeypatch):
    seen = []
    monkeypatch.setattr(pypi, "fetch_json", lambda url, **kw: seen.append((url, kw)) or {"files": []})
    assert condaforge.fetch("a/b?c", "1") == []
    url, kw = seen[0]
    assert url == API + "a%2Fb%3Fc"
    assert kw == {"timeout": condaforge.TIMEOUT, "attempts": 1, "max_bytes": condaforge.MAX_BYTES}
    assert condaforge.TIMEOUT <= 15 and condaforge.DEADLINE <= 30


def test_a_slow_answer_doesn_t_stop_the_rest(monkeypatch):
    monkeypatch.setattr(condaforge, "WORKERS", 1)
    calls = []

    def fetch_json(url, **kw):
        calls.append(url)
        if url.endswith("/a"):
            raise TimeoutError("timed out")
        return {"files": []}

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    lookups = condaforge.look_up_all([("a", "1"), ("b", "1")])
    assert len(calls) == 2 and lookups.unchecked == ["a"] and not lookups.stopped


def test_first_connection_failure_stops_the_rest(monkeypatch):
    monkeypatch.setattr(condaforge, "WORKERS", 1)
    calls = []

    def fetch_json(url, **kw):
        calls.append(url)
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    lookups = condaforge.look_up_all([("a", "1"), ("b", "1"), ("c", "1")])
    assert calls == [API + "a"] and lookups.stopped and lookups.unchecked == ["a", "b", "c"]


def test_the_deadline_drops_stalled_lookups(monkeypatch):
    release = threading.Event()

    def stall(url, **kw):
        release.wait(10)
        return {"files": []}

    monkeypatch.setattr(pypi, "fetch_json", stall)
    start = time.monotonic()
    try:
        lookups = condaforge.look_up_all([("a", "1"), ("b", "1")], deadline=0.2)
        assert time.monotonic() - start < 2
        assert lookups.stopped and lookups.unchecked == ["a", "b"] and not lookups.found
    finally:
        release.set()


def test_unreachable_is_a_note(capsys, fake_pypi, monkeypatch):
    monkeypatch.setitem(FAKE_CONDA_FORGE, "nowheels", urllib.error.URLError(OSError("no route to host")))
    _, _, err = run(capsys, "nowheels==0.9", "forked==1.0", "-t", "linux-aarch64", *FLAGS)
    assert "anaconda.org stopped answering, or was too slow" in err


class CountingResponse(io.BytesIO):
    def __init__(self, body, encoding=None):
        super().__init__(body)
        self.headers = {"Content-Encoding": encoding} if encoding else {}
        self.taken = 0

    def read(self, size=-1):
        data = super().read(size)
        self.taken += len(data)
        return data

    def read1(self, size=-1):
        data = super().read1(size)
        self.taken += len(data)
        return data

    def readinto(self, b):
        n = super().readinto(b)
        self.taken += n
        return n

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def serve(monkeypatch, response, seen=None):
    def urlopen(req, timeout):
        if seen is not None:
            seen.append(dict(req.header_items()))
        return response

    monkeypatch.setattr(pypi._OPENER, "open", urlopen)
    return response


def test_capped_fetch_json_reads_no_more_than_the_cap(monkeypatch):
    body = json.dumps({"files": ["x" * 1000]}).encode()
    response = serve(monkeypatch, CountingResponse(body))
    with pytest.raises(pypi.ResponseTooLarge):
        pypi.fetch_json("https://x.example/p", max_bytes=100)
    assert response.taken == 101


def test_capped_fetch_json_decodes_a_gzip_bomb_no_further_than_the_cap(monkeypatch):
    bomb = gzip.compress(b"0" * (32 * 1024 * 1024))  # 32 MB of zeros, about 32 KB gzipped
    serve(monkeypatch, CountingResponse(bomb, "gzip"))
    tracemalloc.start()
    try:
        with pytest.raises(pypi.ResponseTooLarge):
            pypi.fetch_json("https://x.example/p", max_bytes=1024 * 1024)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert peak < 8 * 1024 * 1024  # a whole-body read would take 32 MB


def test_capped_fetch_json_accepts_gzip_and_refuses_other_encodings(monkeypatch):
    body = json.dumps({"files": [{"x": "y" * 100}]}).encode()
    seen: list[dict] = []
    serve(monkeypatch, CountingResponse(gzip.compress(body), "gzip"), seen)
    assert pypi.fetch_json("https://x.example/p", max_bytes=len(body)) == json.loads(body)
    assert seen[-1].get("Accept-encoding") == "gzip"
    serve(monkeypatch, CountingResponse(body))
    assert pypi.fetch_json("https://x.example/p", max_bytes=len(body)) == json.loads(body)
    serve(monkeypatch, CountingResponse(gzip.compress(body), "gzip"))
    with pytest.raises(pypi.ResponseTooLarge):  # small gzipped, too large decoded
        pypi.fetch_json("https://x.example/p", max_bytes=len(body) - 1)
    serve(monkeypatch, CountingResponse(body, "br"))
    with pytest.raises(ValueError, match="Content-Encoding"):
        pypi.fetch_json("https://x.example/p", max_bytes=len(body))
    # Uncapped (every PyPI lookup), nothing changes: no gzip is asked for.
    serve(monkeypatch, CountingResponse(body), seen)
    assert pypi.fetch_json("https://x.example/p") == json.loads(body)
    assert "Accept-encoding" not in seen[-1]
