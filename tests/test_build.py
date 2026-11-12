"""--explain --build-info (#230): reading an sdist's pyproject.toml as pip would, and what its build table means."""

import hashlib
import io
import json
import os
import tarfile
import urllib.error

import pytest

from wheelgap import build, cache, pypi
from wheelgap.build import describe, parse
from wheelgap.cli import main
from wheelgap.sources import Package
from wheelgap.targets import build_targets

PYPI = "https://pypi.org/simple"
MATURIN = b'[build-system]\nrequires = ["maturin>=1,<2"]\nbuild-backend = "maturin"\n'
LINUX = build_targets(["3.13"], ["linux-x86_64"])
WINDOWS = build_targets(["3.13"], ["win-amd64"])


def member(name, data=b"", kind=tarfile.REGTYPE, link="", pax=None):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, link
    if pax:
        info.pax_headers = pax
    if kind == tarfile.REGTYPE:
        info.size = len(data)
    return info, data


def sdist(tmp_path, *members, name="pkg-1.0.tar.gz", fmt=tarfile.PAX_FORMAT) -> bytes:
    """A .tar.gz built in tmp_path from (TarInfo, data) pairs, in that order."""
    path = tmp_path / name
    with tarfile.open(path, "w:gz", format=fmt) as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if info.type == tarfile.REGTYPE else None)
    return path.read_bytes()


def fetch(fake_files, data, **kwargs):
    fake_files.files["pkg-1.0.tar.gz"] = data
    return pypi.fetch_sdist_pyproject("https://files.example/pkg-1.0.tar.gz", **kwargs)


# Reading the sdist (pypi.fetch_sdist_pyproject)


def test_reads_the_root_pyproject(tmp_path, fake_files):
    data = sdist(
        tmp_path,
        member("pkg-1.0/sub/pyproject.toml", b'[build-system]\nrequires = ["wrong"]\n'),  # not at the root
        member("pkg-1.0/pyproject.toml", MATURIN.replace(b"\n", b"\r")),  # pip reads text: a lone CR is a newline
        member("pkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\n"),
    )
    assert fetch(fake_files, data)["build-system"]["requires"] == ["maturin>=1,<2"]


def test_no_pyproject_is_none(tmp_path, fake_files):
    assert fetch(fake_files, sdist(tmp_path, member("pkg-1.0/setup.py", b"setup()\n"))) is None


def test_a_repeated_pyproject_with_the_same_content_is_fine(tmp_path, fake_files):
    data = sdist(tmp_path, member("pkg-1.0/pyproject.toml", MATURIN), member("pkg-1.0/./pyproject.toml", MATURIN))
    assert fetch(fake_files, data) is not None


def test_pax_path_overrides_the_name(tmp_path, fake_files):
    renamed = member("pkg-1.0/other.toml", MATURIN, pax={"path": "pkg-1.0/pyproject.toml"})
    assert fetch(fake_files, sdist(tmp_path, renamed)) is not None
    renamed = member("pkg-1.0/pyproject.toml", MATURIN, pax={"path": "pkg-1.0/other.toml"})
    assert fetch(fake_files, sdist(tmp_path, renamed)) is None


@pytest.mark.parametrize(
    "members",
    [
        # Read to the end: pip extracts every member, and the last one for a path wins.
        [member("pkg-1.0/pyproject.toml", MATURIN), member("pkg-1.0/pyproject.toml", b"[build-system]\n")],
        # One top folder, or pip doesn't strip it.
        [member("a/pyproject.toml", MATURIN), member("b/README", b"")],
        [member("pyproject.toml", MATURIN), member("pkg-1.0/pyproject.toml", MATURIN)],
        # Links and other files by that name.
        [member("pkg-1.0/pyproject.toml", kind=tarfile.SYMTYPE, link="other.toml")],
        [
            member("pkg-1.0/other.toml", MATURIN),
            member("pkg-1.0/pyproject.toml", kind=tarfile.LNKTYPE, link="pkg-1.0/other.toml"),
        ],
        [member("pkg-1.0/pyproject.toml", kind=tarfile.DIRTYPE)],
        [member("pkg-1.0/pyproject.toml", MATURIN), member("pkg-1.0/PyProject.toml", MATURIN)],  # macOS, Windows
        # A member written through a link, which could land on pyproject.toml.
        [member("pkg-1.0/a", kind=tarfile.SYMTYPE, link="."), member("pkg-1.0/a/pyproject.toml", MATURIN)],
        [member("pkg-1.0/x", kind=tarfile.SYMTYPE, link="pyproject.toml"), member("pkg-1.0/x", b"[x]\n")],
        # What pip's tarfile filter refuses.
        [member("pkg-1.0/x", kind=tarfile.SYMTYPE, link="../../etc/passwd"), member("pkg-1.0/pyproject.toml", MATURIN)],
        [member("pkg-1.0/x", kind=tarfile.SYMTYPE, link="/etc/passwd"), member("pkg-1.0/pyproject.toml", MATURIN)],
        [member("pkg-1.0/dev", kind=tarfile.CHRTYPE), member("pkg-1.0/pyproject.toml", MATURIN)],
        [member("pkg-1.0/sub/../pyproject.toml", MATURIN)],
        [member("pkg-1.0\\pyproject.toml", MATURIN)],  # a separator on Windows only
        [member("pkg-1.0/pyproject.toml", MATURIN + b"#" * pypi.PYPROJECT_MAX_BYTES)],
        [member("pkg-1.0/pyproject.toml", b"[build-system\n")],  # pip can't parse it either
        [member("pkg-1.0/pyproject.toml", b"\xff\xfe")],  # nor read it as UTF-8
    ],
    ids=[
        "duplicate",
        "two-top-folders",
        "root-and-top",
        "symlink",
        "hardlink",
        "folder",
        "case",
        "link-folder",
        "write-through-link",
        "link-out",
        "absolute-link",
        "device",
        "dotdot",
        "backslash",
        "oversized",
        "bad-toml",
        "bad-utf8",
    ],
)
def test_no_answer_where_pip_could_read_another_file(tmp_path, fake_files, members):
    with pytest.raises(pypi.SdistRefused):
        fetch(fake_files, sdist(tmp_path, *members))


def test_not_a_tarball(fake_files):
    with pytest.raises(pypi.SdistRefused):
        fetch(fake_files, b"<html>not found</html>")


def test_sparse_files_are_refused(tmp_path, fake_files):
    with pytest.raises(pypi.SdistRefused):
        fetch(fake_files, sdist(tmp_path, member("pkg-1.0/x", kind=tarfile.GNUTYPE_SPARSE), fmt=tarfile.GNU_FORMAT))


@pytest.mark.parametrize("fmt", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT])
def test_header_cap(tmp_path, fake_files, monkeypatch, fmt):
    monkeypatch.setattr(pypi, "SDIST_MAX_HEADER", 200)
    data = sdist(tmp_path, member("pkg-1.0/" + "x" * 300, b""), member("pkg-1.0/pyproject.toml", MATURIN), fmt=fmt)
    with pytest.raises(pypi.SdistRefused, match="header"):
        fetch(fake_files, data)


def test_download_cap(tmp_path, fake_files, monkeypatch):
    monkeypatch.setattr(pypi, "SDIST_MAX_BYTES", 64 * 1024)
    data = sdist(tmp_path, member("pkg-1.0/pyproject.toml", MATURIN), member("pkg-1.0/big", os.urandom(256 * 1024)))
    with pytest.raises(pypi.SdistRefused, match="too big"):
        fetch(fake_files, data)


def test_unpacked_cap(tmp_path, fake_files, monkeypatch):
    # Zeros compress to almost nothing, so only a cap on the unpacked bytes stops this one.
    monkeypatch.setattr(pypi, "SDIST_MAX_UNPACKED", 1024 * 1024)
    data = sdist(tmp_path, member("pkg-1.0/pyproject.toml", MATURIN), member("pkg-1.0/zeros", bytes(8 << 20)))
    with pytest.raises(pypi.SdistRefused, match="too big"):
        fetch(fake_files, data)


def test_sha256(tmp_path, fake_files):
    data = sdist(tmp_path, member("pkg-1.0/pyproject.toml", MATURIN))
    assert fetch(fake_files, data, sha256=hashlib.sha256(data).hexdigest().upper()) is not None
    with pytest.raises(pypi.SdistHashMismatch):
        fetch(fake_files, data, sha256="0" * 64)


class Slow:
    """A response that sends a few bytes per read, each read taking 5 s on a fake clock."""

    def __init__(self, data, clock):
        self.data, self.clock = io.BytesIO(data), clock

    def read1(self, n):
        self.clock[0] += 5
        return self.data.read(min(n, 16))

    read = read1

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(pypi, "_clock", lambda: now[0])
    return now


def test_a_slow_server_hits_the_deadline(tmp_path, fake_files, clock):
    data = sdist(tmp_path, member("pkg-1.0/pyproject.toml", MATURIN))
    fake_files.files["pkg-1.0.tar.gz"] = lambda: Slow(data, clock)
    with pytest.raises(pypi.DeadlineExceeded):
        pypi.fetch_sdist_pyproject("https://files.example/pkg-1.0.tar.gz", deadline=30.0)
    assert clock[0] == 35  # stopped at the first read past the deadline


@pytest.mark.parametrize(
    "url", ["http://files.example/pkg-1.0.tar.gz", "https://files.example/pkg-1.0.zip", "file:///pkg-1.0.tar.gz"]
)
def test_only_https_tar_gz(url, fake_files):
    with pytest.raises(ValueError):
        pypi.fetch_sdist_pyproject(url)
    assert fake_files.calls == []


def test_one_attempt_only(tmp_path, fake_files, monkeypatch):
    monkeypatch.setattr(pypi.time, "sleep", lambda s: pytest.fail("retried"))
    fake_files.files["pkg-1.0.tar.gz"] = urllib.error.HTTPError("u", 503, "busy", {"Retry-After": "1"}, None)  # type: ignore[arg-type]
    with pytest.raises(urllib.error.HTTPError):
        pypi.fetch_sdist_pyproject("https://files.example/pkg-1.0.tar.gz")
    assert len(fake_files.calls) == 1


# What the build table means (build.parse and build.describe)


def info(requires, backend=None, backend_path=False, build_script=False):
    return build._info(
        {"requires": requires, "backend": backend, "backend_path": backend_path, "build_script": build_script}
    )


@pytest.mark.parametrize(
    ("requires", "backend", "backend_path", "expected"),
    [
        (["maturin>=1,<2"], "maturin", False, "at least a Rust toolchain (maturin<2,>=1)"),
        (["setuptools", "setuptools-rust"], None, False, "at least a Rust toolchain (setuptools-rust), setuptools"),
        (
            ["scikit-build-core>=0.10", "pybind11"],
            "scikit_build_core.build",
            False,
            "at least CMake and a C/C++ compiler (scikit-build-core>=0.10), pybind11",
        ),
        (["scikit-build", "setuptools"], None, False, "at least CMake and a C/C++ compiler (scikit-build), setuptools"),
        (["cmake", "setuptools"], None, False, "at least CMake and a C/C++ compiler (cmake), setuptools"),
        (["meson-python", "Cython>=3"], "mesonpy", False, "at least a C/C++ compiler (meson-python, cython>=3)"),
        (
            ["Cython; python_version<'3.13'", "Cython>=3; python_version>='3.13'"],
            None,
            False,
            "at least a C/C++ compiler (cython>=3)",
        ),
        (["setuptools>=61", "wheel"], "setuptools.build_meta", False, "at least setuptools>=61, wheel"),
        (["hatchling"], "hatchling.build", False, "at least hatchling"),
        (["foo-backend"], "foo", False, "at least foo-backend"),
        (["flit_core >=3.2,<4"], "flit_core.buildapi", False, "no compiler (flit-core<4,>=3.2)"),
        (["flit"], "flit.buildapi", False, "no compiler (flit)"),
        (["poetry-core>=1"], "poetry.core.masonry.api", False, "no compiler (poetry-core>=1)"),
        (["flit_core"], None, False, "at least flit-core"),  # no build-backend: setuptools' legacy backend
        (["flit_core"], "flit_core.buildapi", True, "at least flit-core, in-tree backend flit_core.buildapi"),
        (["flit_core", "maturin"], "flit_core.buildapi", False, "at least a Rust toolchain (maturin), flit-core"),
        ([], "flit_core.buildapi", False, "no compiler"),
        ([], None, False, None),
        (["Foo_Bar[B,a]>=1"], None, False, "at least foo-bar[a,b]>=1"),
        (["foo @ https://example.com/foo.tar.gz"], None, False, "at least foo (direct URL)"),
        (["foo===`x`</details>"], None, False, "at least foo"),  # === takes any text: only the name is shown
    ],
)
def test_describe(requires, backend, backend_path, expected):
    assert describe(info(requires, backend, backend_path), LINUX) == expected


def test_poetry_build_script_is_not_pure():
    table = {"build-system": {"requires": ["poetry-core"], "build-backend": "poetry.core.masonry.api"}}
    assert describe(build._info(parse(table)), LINUX) == "no compiler (poetry-core)"
    table["tool"] = {"poetry": {"build": {"script": "build.py"}}}
    assert parse(table)["build_script"] is True
    assert describe(build._info(parse(table)), LINUX) == "at least poetry-core"


def test_markers_count_on_the_targets_that_build_it():
    windows_only = info(["maturin; sys_platform == 'win32'", "setuptools"])
    assert describe(windows_only, LINUX) == "at least setuptools"
    assert describe(windows_only, WINDOWS) == "at least a Rust toolchain (maturin), setuptools"
    assert describe(windows_only, LINUX + WINDOWS) == "at least a Rust toolchain (maturin), setuptools"


@pytest.mark.parametrize(
    "system",
    [
        None,
        {},  # no requires: pip refuses it
        {"requires": "maturin"},
        {"requires": ["maturin", 1]},
        {"requires": ["maturin\n::error file=x::boom"]},  # pip refuses an invalid requirement too
        {"requires": ["setuptools", "foo`bar"]},
        {"requires": ["setuptools"], "build-backend": "evil\n::error::x"},
        {"requires": ["setuptools"], "build-backend": 1},
        {"requires": ["setuptools"], "backend-path": "."},
    ],
)
def test_parse_gives_no_answer(system):
    assert parse({} if system is None else {"build-system": system}) is None


# Which releases are looked up (build.where)


def pkg(registry=PYPI, url="https://files.example/x-1.0.tar.gz", files=("x-1.0.tar.gz",), **kwargs):
    return Package("x", "1.0", files=list(files), registry=registry, sdist_url=url, **kwargs)


@pytest.mark.parametrize(
    ("package", "expected"),
    [
        (pkg(), ((PYPI, "x", "1.0"), "https://files.example/x-1.0.tar.gz", None)),
        (pkg(sdist_sha256="ab" * 32), ((PYPI, "x", "1.0"), "https://files.example/x-1.0.tar.gz", "ab" * 32)),
        (pkg(registry="https://acme.example/simple"), None),  # a private index
        (pkg(registry=None), None),  # a pylock.toml entry with no index
        (pkg(registry=None, url=None), None),  # a pdm.lock or poetry.lock entry
        (pkg(url="https://files.example/other-1.0.tar.gz"), None),  # not the release's sdist
        (pkg(files=["x-1.0.zip"], url="https://files.example/x-1.0.zip"), None),
        (pkg(files=["x-1.0.tar.gz", "x-1.0.zip"]), None),  # which one pip takes isn't certain
        (pkg(files=["x-1.0-py3-none-any.whl"]), None),
        (
            pkg(registry=None, url=None, looked_up_on="https://pypi.org/pypi"),
            (("https://pypi.org/pypi", "x", "1.0"), None, None),
        ),
        (pkg(registry=None, url=None, looked_up_on="https://mirror.example/pypi"), None),  # not this run's index
    ],
)
def test_where(package, expected):
    assert build.where(package) == expected


def test_where_on_the_mirror_this_run_uses(monkeypatch):
    mirror = "https://mirror.example/simple"
    assert build.where(pkg(registry=mirror)) is None
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example/pypi")
    assert build.where(pkg(registry=mirror))[0] == (mirror, "x", "1.0")


# Looking releases up (build.lookup, build.lookup_all)


def phase():
    return build._Phase(deadline=1e9)


def test_answers_are_cached_but_not_network_failures(tmp_path, fake_files):
    p = pkg()
    fake_files.files["x-1.0.tar.gz"] = urllib.error.URLError("down")
    assert build.lookup(p, phase(), read_cache=True, write_cache=True) is None
    assert cache.load_build(PYPI, "x", "1.0") is None  # a failure isn't an answer
    fake_files.files["x-1.0.tar.gz"] = sdist(tmp_path, member("x-1.0/README", b""), name="x-1.0.tar.gz")
    assert build.lookup(p, phase(), read_cache=True, write_cache=True) is None
    assert cache.load_build(PYPI, "x", "1.0") == {"answer": None}  # no pyproject.toml: that won't change
    calls = len(fake_files.calls)
    assert build.lookup(p, phase(), read_cache=True, write_cache=True) is None
    assert len(fake_files.calls) == calls  # from the cache
    assert build.lookup(p, phase(), read_cache=False, write_cache=True) is None
    assert len(fake_files.calls) == calls + 1


def test_refused_sdists_are_cached(tmp_path, fake_files):
    fake_files.files["x-1.0.tar.gz"] = b"not a tarball"
    assert build.lookup(pkg(), phase(), read_cache=True, write_cache=True) is None
    assert cache.load_build(PYPI, "x", "1.0") == {"answer": None}


def test_hash_mismatch_is_no_answer_and_not_cached(tmp_path, fake_files):
    fake_files.files["x-1.0.tar.gz"] = sdist(tmp_path, member("x-1.0/pyproject.toml", MATURIN), name="x-1.0.tar.gz")
    assert build.lookup(pkg(sdist_sha256="0" * 64), phase(), write_cache=True) is None
    assert cache.load_build(PYPI, "x", "1.0") is None
    assert build.lookup(pkg(), phase(), write_cache=True) is not None


@pytest.mark.parametrize(
    "answer",
    [
        {"requires": ["maturin"], "backend": None, "backend_path": False},
        {"requires": "maturin", "backend": None, "backend_path": False, "build_script": False},
        {"requires": [1], "backend": None, "backend_path": False, "build_script": False},
        {"requires": [], "backend": 1, "backend_path": False, "build_script": False},
        {"requires": [], "backend": None, "backend_path": 0, "build_script": False},
        "x",
    ],
)
def test_load_build_validates(answer):
    cache.save_build(PYPI, "x", "1.0", answer)
    assert cache.load_build(PYPI, "x", "1.0") is None


def test_load_build_age(monkeypatch):
    good = {"requires": ["maturin"], "backend": None, "backend_path": False, "build_script": False}
    cache.save_build(PYPI, "x", "1.0", good)
    assert cache.load_build(PYPI, "x", "1.0") == {"answer": good}
    now = cache._now()
    monkeypatch.setattr(cache, "_now", lambda: now + cache.MAX_AGE + 1)
    assert cache.load_build(PYPI, "x", "1.0") is None


def test_the_first_connection_failure_stops_the_rest(fake_files, monkeypatch):
    monkeypatch.setattr(build, "WORKERS", 1)
    fake_files.files["a-1.0.tar.gz"] = urllib.error.URLError("down")
    a = Package("a", "1.0", files=["a-1.0.tar.gz"], registry=PYPI, sdist_url="https://files.example/a-1.0.tar.gz")
    b = Package("b", "1.0", files=["b-1.0.tar.gz"], registry=PYPI, sdist_url="https://files.example/b-1.0.tar.gz")
    assert build.lookup_all([a, b]) == ({}, None)
    assert fake_files.calls == ["https://files.example/a-1.0.tar.gz"]


def test_one_lookup_per_release_and_registry(tmp_path, fake_files):
    fake_files.files["x-1.0.tar.gz"] = sdist(tmp_path, member("x-1.0/pyproject.toml", MATURIN), name="x-1.0.tar.gz")
    found, _ = build.lookup_all([pkg(), pkg(), pkg(registry=PYPI + "/")])
    assert list(found) == [(PYPI, "x", "1.0")] and len(fake_files.calls) == 1


def test_lookup_all_stops_at_the_deadline(tmp_path, fake_files, clock):
    data = sdist(tmp_path, member("x-1.0/pyproject.toml", MATURIN), name="x-1.0.tar.gz")
    fake_files.files["x-1.0.tar.gz"] = lambda: Slow(data, clock)
    assert build.lookup_all([pkg()]) == ({}, 35.0)
    assert cache.load_build(PYPI, "x", "1.0") is None


# The command line


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


def nowheels_sdist(tmp_path, pyproject=MATURIN):
    return sdist(tmp_path, member("nowheels-0.9/pyproject.toml", pyproject), name="nowheels-0.9.tar.gz")


def uv_lock(tmp_path, registry=PYPI, sha256=None):
    digest = f', hash = "sha256:{sha256}"' if sha256 else ""
    (tmp_path / "uv.lock").write_text(
        f'version = 1\nrequires-python = ">=3.13"\n\n[[package]]\nname = "nowheels"\nversion = "0.9"\n'
        f'source = {{ registry = "{registry}" }}\n'
        f'sdist = {{ url = "https://files.example/nowheels-0.9.tar.gz"{digest}, size = 1 }}\n'
    )
    return str(tmp_path / "uv.lock")


TARGETS = ("-p", "3.13", "-t", "linux-x86_64,win-amd64", "--color", "never")


def test_build_info_is_opt_in(capsys, tmp_path, fake_files):
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path)
    lock = uv_lock(tmp_path)
    _, out, _ = run(capsys, lock, *TARGETS, "--explain")
    assert "build needs" not in out and fake_files.calls == []  # a lockfile is still read offline
    _, out, _ = run(capsys, lock, *TARGETS, "--explain", "--json")
    assert "build_requires" not in json.loads(out)["packages"][0]
    _, out, _ = run(capsys, lock, *TARGETS, "--build-info")  # only with --explain
    assert "build needs" not in out and fake_files.calls == []

    code, out, _ = run(capsys, lock, *TARGETS, "--explain", "--build-info")
    assert code == 1
    assert out.count("  build needs: at least a Rust toolchain (maturin<2,>=1)") == 1  # two source cells, said once
    assert fake_files.calls == ["https://files.example/nowheels-0.9.tar.gz"]  # the lock's URL: no JSON lookup
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nbuild-info = true\n")
    _, out, _ = run(capsys, str(tmp_path), *TARGETS, "--explain", "--json")
    assert json.loads(out)["packages"][0]["build_requires"] == ["maturin<2,>=1"]
    _, out, _ = run(capsys, str(tmp_path), *TARGETS, "--explain", "--markdown")
    assert "build needs: at least a Rust toolchain (maturin<2,>=1)" in out


def test_looked_up_packages_use_the_json_api(capsys, tmp_path, fake_pypi, fake_files):
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path)
    _, out, _ = run(capsys, "nowheels==0.9", *TARGETS, "--explain", "--build-info", "--no-cache")
    assert "build needs: at least a Rust toolchain" in out
    assert fake_pypi == ["https://pypi.org/pypi/nowheels/0.9/json"] * 2  # the check, then the sdist's URL


def test_sha256_mismatch_gives_no_answer(capsys, tmp_path, fake_files):
    data = nowheels_sdist(tmp_path)
    fake_files.files["nowheels-0.9.tar.gz"] = data
    _, out, _ = run(capsys, uv_lock(tmp_path, sha256="0" * 64), *TARGETS, "--explain", "--build-info")
    assert "build needs" not in out
    _, out, _ = run(
        capsys, uv_lock(tmp_path, sha256=hashlib.sha256(data).hexdigest()), *TARGETS, "--explain", "--build-info"
    )
    assert "build needs" in out


@pytest.mark.parametrize(
    ("filename", "text"),
    [
        ("uv.lock", None),  # filled in by uv_lock() with a private registry
        (
            "pdm.lock",
            '[metadata]\ngroups = ["default"]\nlock_version = "4.5.0"\n\n'
            '[[package]]\nname = "nowheels"\nversion = "0.9"\n'
            'groups = ["default"]\nfiles = [{file = "nowheels-0.9.tar.gz", hash = "sha256:00"}]\n',
        ),
        (
            "pylock.toml",
            'lock-version = "1.0"\ncreated-by = "test"\n\n[[packages]]\nname = "nowheels"\nversion = "0.9"\n'
            'sdist = {name = "nowheels-0.9.tar.gz", url = "https://files.example/nowheels-0.9.tar.gz"}\n',
        ),
    ],
)
def test_never_looks_up_what_may_not_be_on_pypi(capsys, tmp_path, fake_pypi, fake_files, filename, text):
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path)
    if text is None:
        lock = uv_lock(tmp_path, registry="https://acme.example/simple")
    else:
        (tmp_path / filename).write_text(text)
        lock = str(tmp_path / filename)
    _, out, _ = run(capsys, lock, *TARGETS, "--explain", "--build-info")
    assert "nowheels 0.9 on linux-x86_64: ● source" in out
    assert "build needs" not in out
    assert fake_pypi == [] and fake_files.calls == []


def test_pylock_with_the_pypi_index(capsys, tmp_path, fake_files):
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path)
    (tmp_path / "pylock.toml").write_text(
        'lock-version = "1.0"\ncreated-by = "test"\n\n[[packages]]\nname = "nowheels"\nversion = "0.9"\n'
        f'index = "{PYPI}"\nsdist = {{name = "nowheels-0.9.tar.gz", url = "https://files.example/nowheels-0.9.tar.gz"}}\n'
    )
    _, out, _ = run(capsys, str(tmp_path / "pylock.toml"), *TARGETS, "--explain", "--build-info")
    assert "build needs: at least a Rust toolchain" in out


def test_offline_and_private_index_names_download_nothing(capsys, tmp_path, fake_pypi, fake_simple, fake_files):
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path)
    _, out, _ = run(capsys, uv_lock(tmp_path), *TARGETS, "--explain", "--build-info", "--offline")
    assert "build needs" not in out and fake_files.calls == []
    # A name looked up on a private index isn't PyPI's...
    index = ("--index-url", "https://i.example/simple")
    _, out, _ = run(capsys, "nowheels==0.9", *TARGETS, "--explain", "--build-info", *index)
    assert "nowheels 0.9 on linux-x86_64: ● source" in out
    assert "build needs" not in out and fake_pypi == [] and fake_files.calls == []
    # ...while a lock's PyPI package still is.
    _, out, _ = run(capsys, uv_lock(tmp_path), *TARGETS, "--explain", "--build-info", *index)
    assert "build needs" in out and fake_pypi == []


def test_only_source_cells(capsys, tmp_path, fake_pypi, fake_files):
    # fastlib has a Linux wheel and an sdist: its Windows cell is source, its Linux cell a wheel.
    fake_files.files["fastlib-2.0.0.tar.gz"] = sdist(
        tmp_path, member("fastlib-2.0.0/pyproject.toml", MATURIN), name="fastlib-2.0.0.tar.gz"
    )
    args = ("fastlib==2.0.0", "-p", "3.13", "--explain", "--build-info", "--json")
    _, out, _ = run(capsys, *args, "-t", "linux-x86_64", "--all")
    assert json.loads(out)["packages"][0]["build_requires"] is None
    _, out, _ = run(capsys, *args, "-t", "linux-x86_64,win-amd64", "--ignore", "fastlib:win-amd64", "--all")
    assert json.loads(out)["packages"][0]["build_requires"] is None  # an ignored target isn't explained
    assert fake_files.calls == []
    _, out, _ = run(capsys, *args, "-t", "linux-x86_64,win-amd64")
    assert json.loads(out)["packages"][0]["build_requires"] == ["maturin<2,>=1"]
    assert len(fake_files.calls) == 1


def test_no_lookup_for_missing_cells(capsys, tmp_path, fake_files):
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path)
    _, out, _ = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "pyodide", "--explain", "--build-info", "--json")
    assert json.loads(out)["packages"][0]["build_requires"] is None
    assert fake_files.calls == []


def test_no_build_line_under_a_missing_cell(capsys, tmp_path, fake_files):
    # Pyodide can't build an sdist, so the same release is missing there and source on Linux.
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path)
    _, out, _ = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "pyodide,linux-x86_64", "--explain", "--build-info")
    blocks = out.split("\n\n")
    missing = next(b for b in blocks if "on pyodide" in b)
    source = next(b for b in blocks if "on linux-x86_64: ● source" in b)
    assert "build needs" not in missing and "build needs" in source


PAYLOADS = ["\n::error file=x::boom", "`whoami`", "</details>evil", "\x1b[31mred"]


@pytest.mark.parametrize("payload", [p.lstrip("\n") for p in PAYLOADS])  # a newline isn't valid in a marker
def test_requirement_text_is_never_printed(capsys, tmp_path, fake_files, payload):
    # A valid requirement whose marker holds the payload: parsed, never shown.
    requirement = json.dumps(f"maturin; os_name == '{payload}'")
    pyproject = f"[build-system]\nrequires = [{requirement}, 'setuptools']\n"
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path, pyproject.encode())
    for extra in ((), ("--markdown",), ("--json",)):
        _, out, err = run(capsys, uv_lock(tmp_path), *TARGETS, "--explain", "--build-info", *extra)
        assert "at least setuptools" in out or '"setuptools"' in out  # the marker doesn't hold on these targets
        assert payload.strip() not in out + err


@pytest.mark.parametrize("payload", PAYLOADS)
def test_an_invalid_requirement_is_no_answer(capsys, tmp_path, fake_files, payload):
    pyproject = f"[build-system]\nrequires = [{json.dumps('maturin' + payload)}, 'setuptools']\n"
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path, pyproject.encode())
    for extra in ((), ("--markdown",), ("--json",)):
        _, out, err = run(capsys, uv_lock(tmp_path), *TARGETS, "--explain", "--build-info", *extra)
        assert "build needs" not in out
        assert payload.strip() not in out + err


def test_failures_change_nothing(capsys, tmp_path, fake_files):
    lock = uv_lock(tmp_path)
    want = run(capsys, lock, *TARGETS, "--explain")
    fake_files.files["nowheels-0.9.tar.gz"] = urllib.error.URLError("down")
    assert run(capsys, lock, *TARGETS, "--explain", "--build-info") == want
    del fake_files.files["nowheels-0.9.tar.gz"]  # a 404
    assert run(capsys, lock, *TARGETS, "--explain", "--build-info") == want


def test_running_out_of_time_says_so(capsys, tmp_path, fake_files, clock):
    data = nowheels_sdist(tmp_path)
    fake_files.files["nowheels-0.9.tar.gz"] = lambda: Slow(data, clock)
    code, out, err = run(capsys, uv_lock(tmp_path), *TARGETS, "--explain", "--build-info")
    assert code == 1 and "build needs" not in out
    assert "build info: stopped after 35 s" in err
    _, _, err = run(capsys, uv_lock(tmp_path), *TARGETS, "--explain", "--build-info", "--json", "-q")
    assert err == ""


def test_markers_skip_ignored_targets(capsys, tmp_path, fake_files):
    pyproject = b"[build-system]\nrequires = [\"maturin; sys_platform == 'win32'\", 'setuptools']\n"
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path, pyproject)
    args = (uv_lock(tmp_path), *TARGETS, "--explain", "--build-info", "--json")
    _, out, _ = run(capsys, *args)
    assert json.loads(out)["packages"][0]["build_requires"] == ["maturin", "setuptools"]
    _, out, _ = run(capsys, *args, "--ignore", "nowheels:win-amd64")
    assert json.loads(out)["packages"][0]["build_requires"] == ["setuptools"]
