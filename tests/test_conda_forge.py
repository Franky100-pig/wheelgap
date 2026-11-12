import gzip
import io
import json

import pytest
from test_cli import _assert_matches_output_schema, run

from wheelgap import cache, condaforge, pypi
from wheelgap.targets import PLATFORMS

API = "https://api.anaconda.org/package/conda-forge/"


def conda_calls(calls):
    return [url for url in calls if url.startswith(API)]


def test_match_is_a_hint_only(capsys, fake_pypi):
    args = ["nowheels==0.9", "-p", "3.13", "-t", "linux-aarch64", "-t", "musl-x86_64", "-t", "macos-arm64", "--explain"]
    code_plain, out_plain, err_plain = run(capsys, *args)
    assert not conda_calls(fake_pypi)
    code, out, err = run(capsys, *args, "--conda-forge")
    assert conda_calls(fake_pypi) == [API + "nowheels"]
    # osx-arm64 has only a broken build, and musl never matches.
    hint = "nowheels: conda-forge has a package named nowheels with builds for linux-aarch64\n\n"
    assert hint in out
    assert code == code_plain == 1
    assert out.replace(hint, "") == out_plain and err == err_plain  # the statuses and the summary don't change


def test_json_key_under_explain_only(capsys, fake_pypi):
    args = ["nowheels==0.9", "winonly==311", "-p", "3.13", "-t", "linux-x86_64", "-t", "linux-aarch64", "--json"]
    code, out, _ = run(capsys, *args, "--explain", "--conda-forge")
    data = json.loads(out)
    _assert_matches_output_schema(data)
    found = {p["name"]: p.get("conda_forge") for p in data["packages"]}
    assert found == {
        "nowheels": {"name": "nowheels", "builds": {"linux-x86_64": ["linux-64"], "linux-aarch64": ["linux-aarch64"]}},
        "winonly": {"name": "winonly", "builds": {"linux-x86_64": ["noarch"], "linux-aarch64": ["noarch"]}},
    }
    plain_code, plain, _ = run(capsys, *args, "--explain")
    assert all("conda_forge" not in p for p in json.loads(plain)["packages"])
    for p in data["packages"]:
        del p["conda_forge"]
    assert data == json.loads(plain) and code == plain_code  # nothing else changes: statuses, summary, exit code


def test_noarch_counts_for_conda_platforms_only(capsys, fake_pypi):
    code, out, _ = run(capsys, "winonly==311", "-p", "3.13", "-t", "linux-x86_64", "--explain", "--conda-forge")
    assert "winonly: conda-forge has a package named winonly with builds for noarch (any platform)" in out
    fake_pypi.clear()
    # musl, Pyodide and mobile have no conda subdir: not even a noarch build matches, so nothing is looked up.
    args = ["-t", "musl-x86_64", "-t", "pyodide", "-t", "ios-arm64", "-t", "android-arm64"]
    code, out, _ = run(capsys, "winonly==311", "-p", "3.13", *args, "--explain", "--conda-forge", "--no-cache")
    assert "conda-forge" not in out and not conda_calls(fake_pypi)


@pytest.mark.parametrize("spec", ["forked==1.0", "fastlib==2.0.0"])  # not on conda-forge (404); a lookup that fails
def test_no_match_or_failure_prints_nothing(capsys, fake_pypi, spec):
    args = [spec, "-p", "3.13", "-t", "linux-aarch64", "--explain"]
    code_plain, out_plain, err_plain = run(capsys, *args)
    code, out, err = run(capsys, *args, "--conda-forge")
    assert len(conda_calls(fake_pypi)) == 1
    assert (code, out, err) == (code_plain, out_plain, err_plain)


def test_no_matching_subdir_prints_nothing(capsys, fake_pypi):
    # conda-forge has nowheels, but not for win-amd64.
    code, out, _ = run(capsys, "nowheels==0.9", "-p", "3.13", "-t", "win-amd64", "--explain", "--conda-forge")
    assert conda_calls(fake_pypi) == [API + "nowheels"] and "conda-forge" not in out


def test_markdown_shows_the_hint(capsys, fake_pypi):
    _, out, _ = run(
        capsys, "nowheels==0.9", "-p", "3.13", "-t", "linux-aarch64", "--markdown", "--explain", "--conda-forge"
    )
    assert "nowheels: conda-forge has a package named nowheels with builds for linux-aarch64" in out


PRIVATE_UV_LOCK = (
    'version = 1\nrequires-python = ">=3.12"\n\n'
    '[[package]]\nname = "app"\nversion = "0"\nsource = { editable = "." }\n'
    'dependencies = [{ name = "nowheels" }]\n\n'
    '[[package]]\nname = "nowheels"\nversion = "0.9"\n'
    'source = { registry = "https://pkgs.example.com/simple" }\n'
    'sdist = { url = "https://pkgs.example.com/nowheels-0.9.tar.gz" }\n'
)


def test_private_index_package_is_never_sent(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(PRIVATE_UV_LOCK, encoding="utf-8")
    code, out, _ = run(
        capsys, str(tmp_path / "uv.lock"), "-p", "3.13", "-t", "linux-aarch64", "--explain", "--conda-forge"
    )
    assert code == 1 and "nowheels" in out  # a gap...
    assert not conda_calls(fake_pypi) and "conda-forge" not in out  # ...whose name stays private


def test_other_index_runs_look_nothing_up(capsys, fake_pypi, fake_simple, monkeypatch):
    args = ["nowheels==0.9", "-p", "3.13", "-t", "linux-aarch64", "--explain", "--conda-forge"]
    _, out, err = run(capsys, *args, "--index-url", "https://pkgs.example.com/simple")
    assert fake_simple.calls and not conda_calls(fake_pypi) and "conda-forge has" not in out
    assert "--conda-forge looks nothing up" in err
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example.com/pypi")
    _, out, err = run(capsys, *args)
    assert fake_pypi and not conda_calls(fake_pypi) and "--conda-forge looks nothing up" in err


def test_needs_explain_and_refuses_offline(capsys, fake_pypi, fixtures):
    with pytest.raises(SystemExit) as e:
        main_args = ["nowheels==0.9", "--conda-forge"]
        run(capsys, *main_args)
    assert e.value.code == 2 and "pass --explain too" in capsys.readouterr().err
    lock = str(fixtures / "uvproj" / "uv.lock")
    code, _, err = run(capsys, lock, "--offline", "--explain", "--conda-forge")
    assert code == 2 and "--offline: --conda-forge would look packages up on anaconda.org" in err
    assert not fake_pypi


def test_lookups_are_cached(capsys, fake_pypi):
    args = ["nowheels==0.9", "forked==1.0", "fastlib==2.0.0", "-p", "3.13", "-t", "linux-aarch64", "--explain"]
    run(capsys, *args, "--conda-forge")
    assert sorted(conda_calls(fake_pypi)) == [API + "fastlib", API + "forked", API + "nowheels"]
    fake_pypi.clear()
    _, out, _ = run(capsys, *args, "--conda-forge")
    # A match and a 404 are answers, kept for a day; a failed lookup is asked again.
    assert conda_calls(fake_pypi) == [API + "fastlib"] and "builds for linux-aarch64" in out
    assert cache.load_subdirs(condaforge.API, "nowheels") == ["linux-64", "linux-aarch64"]
    assert cache.load_subdirs(condaforge.API, "forked") == []
    fake_pypi.clear()
    run(capsys, *args, "--conda-forge", "--no-cache")
    assert len(conda_calls(fake_pypi)) == 3


def test_every_conda_subdir_is_a_glibc_macos_or_windows_platform():
    for platform, subdir in condaforge.SUBDIRS.items():
        assert PLATFORMS[platform][0] in ("manylinux", "macos", "windows"), platform
        assert subdir.split("-")[0] in ("linux", "osx", "win"), subdir
    missing = {p for p, (family, _) in PLATFORMS.items() if family in ("manylinux", "macos", "windows")}
    assert missing == set(condaforge.SUBDIRS)


@pytest.mark.parametrize(
    "data",
    [[], {}, {"files": None}, {"files": "x"}],
)
def test_unexpected_answers_are_a_failed_lookup(monkeypatch, data):
    monkeypatch.setattr(pypi, "fetch_json", lambda url, **kw: data)
    assert condaforge.look_up("numpy") is None


def test_parse_subdirs_reads_the_main_label_only():
    files = [
        {"attrs": {"subdir": "linux-64"}, "labels": ["main"]},
        {"attrs": {"subdir": "osx-arm64"}, "labels": ["broken"]},
        {"attrs": {"subdir": "win-64"}},  # no labels: not counted
        {"attrs": {}, "labels": ["main"]},
        "junk",
    ]
    assert condaforge.parse_subdirs({"files": files}) == ["linux-64"]


class FakeResponse(io.BytesIO):
    def __init__(self, body, encoding=None):
        super().__init__(body)
        self.headers = {"Content-Encoding": encoding} if encoding else {}

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_capped_fetch_json_accepts_gzip_and_refuses_large_answers(monkeypatch):
    body = json.dumps({"files": [{"x": "y" * 100}]}).encode()
    seen = []

    def serve(response):
        def urlopen(req, timeout):
            seen.append(dict(req.header_items()))
            return response

        monkeypatch.setattr(pypi._OPENER, "open", urlopen)

    serve(FakeResponse(gzip.compress(body), "gzip"))
    assert pypi.fetch_json("https://x.example/p", max_bytes=len(body)) == json.loads(body)
    assert seen[-1].get("Accept-encoding") == "gzip"
    serve(FakeResponse(body))
    assert pypi.fetch_json("https://x.example/p", max_bytes=len(body)) == json.loads(body)
    serve(FakeResponse(body))
    with pytest.raises(pypi.ResponseTooLarge):  # too large as sent
        pypi.fetch_json("https://x.example/p", max_bytes=len(body) - 1)
    serve(FakeResponse(gzip.compress(body), "gzip"))
    with pytest.raises(pypi.ResponseTooLarge):  # small gzipped, too large decoded
        pypi.fetch_json("https://x.example/p", max_bytes=len(body) - 1)
    serve(FakeResponse(body, "br"))
    with pytest.raises(ValueError, match="Content-Encoding"):
        pypi.fetch_json("https://x.example/p", max_bytes=len(body))
    # Uncapped (every PyPI lookup), nothing changes: no gzip is asked for.
    monkeypatch.setattr(
        pypi._OPENER, "open", lambda req, timeout: seen.append(dict(req.header_items())) or FakeResponse(body)
    )
    assert pypi.fetch_json("https://x.example/p") == json.loads(body)
    assert "Accept-encoding" not in seen[-1]
