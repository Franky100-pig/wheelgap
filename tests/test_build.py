"""--explain --build-info (#230): reading an sdist's pyproject.toml as pip would, and what its build table means."""

import gzip
import hashlib
import io
import json
import os
import socketserver
import tarfile
import threading
import time
import urllib.error
from contextlib import contextmanager

import pytest

from wheelgap import build, cache, pypi
from wheelgap.build import describe, parse
from wheelgap.cli import main
from wheelgap.sources import Package, Source, load_inputs
from wheelgap.targets import build_targets

PYPI = "https://pypi.org/simple"
FILES = "https://files.pythonhosted.org/packages/ab/cd"
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


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def fetch(fake_files, data, **kwargs):
    """The parsed pyproject.toml (dropping whether the download was verified)."""
    fake_files.files["pkg-1.0.tar.gz"] = data
    return pypi.fetch_sdist_pyproject("https://files.example/pkg-1.0.tar.gz", **kwargs)[0]


# Reading the sdist (pypi.fetch_sdist_pyproject)


def test_reads_the_root_pyproject(tmp_path, fake_files):
    data = sdist(
        tmp_path,
        member("pkg-1.0/sub/pyproject.toml", b'[build-system]\nrequires = ["wrong"]\n'),  # not at the root
        member("pkg-1.0/pyproject.toml", MATURIN.replace(b"\n", b"\r")),  # pip reads text: a lone CR is a newline
        member("pkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\n"),
        member("pkg-1.0/tests/data/café.txt", b""),  # non-ASCII names are fine without links
    )
    assert fetch(fake_files, data)["build-system"]["requires"] == ["maturin>=1,<2"]


def test_absolute_names_lose_their_slash(tmp_path, fake_files):
    assert fetch(fake_files, sdist(tmp_path, member("/pkg-1.0/pyproject.toml", MATURIN))) is not None


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


PLAIN = member("pkg-1.0/pyproject.toml", MATURIN)


@pytest.mark.parametrize(
    "members",
    [
        # Read to the end: pip extracts every member, and the last one for a path wins.
        [PLAIN, member("pkg-1.0/pyproject.toml", b"[build-system]\n")],
        # One top folder, or pip doesn't strip it.
        [member("a/pyproject.toml", MATURIN), member("b/README", b"")],
        [member("pyproject.toml", MATURIN), PLAIN],
        [member("./pkg-1.0/pyproject.toml", MATURIN)],  # pip strips "." and finds no pyproject.toml
        # Where pip itself fails.
        [member("pkg-1.0", b"x"), PLAIN],  # a file in place of the top folder
        [PLAIN, member("pkg-1.0/pyproject.toml/x", b"")],
        [member("pkg-1.0/x", kind=b"Z"), PLAIN],  # a type pip doesn't know
        # Links and other files by that name.
        [member("pkg-1.0/pyproject.toml", kind=tarfile.SYMTYPE, link="other.toml")],
        [
            member("pkg-1.0/other.toml", MATURIN),
            member("pkg-1.0/pyproject.toml", kind=tarfile.LNKTYPE, link="pkg-1.0/other.toml"),
        ],
        [member("pkg-1.0/pyproject.toml", kind=tarfile.DIRTYPE)],
        [PLAIN, member("pkg-1.0/PyProject.toml", MATURIN)],  # macOS and Windows ignore case
        [member("pkg-1.0", kind=tarfile.SYMTYPE, link="."), PLAIN],  # a link as the top folder
        # A member written through a link, which could land on pyproject.toml.
        [member("pkg-1.0/a", kind=tarfile.SYMTYPE, link="."), member("pkg-1.0/a/pyproject.toml", MATURIN)],
        [member("pkg-1.0/A", kind=tarfile.SYMTYPE, link="."), member("pkg-1.0/a/pyproject.toml", MATURIN)],
        [member("pkg-1.0/x", kind=tarfile.SYMTYPE, link="pyproject.toml"), member("pkg-1.0/x", b"[x]\n")],
        # Links with non-ASCII names, which macOS folds in ways a comparison can't follow (é and e + U+0301).
        [member("pkg-1.0/café", kind=tarfile.SYMTYPE, link="."), PLAIN],
        [member("pkg-1.0/café/x", b""), member("pkg-1.0/l", kind=tarfile.LNKTYPE, link="pkg-1.0/café/x")],
        [member("pkg-1.0/x", b""), member("pkg-1.0/l", kind=tarfile.SYMTYPE, link="x"), member("pkg-1.0/ß", b"")],
        # What pip's tarfile filter refuses.
        [member("pkg-1.0/x", kind=tarfile.SYMTYPE, link="../../etc/passwd"), PLAIN],
        [member("pkg-1.0/x", kind=tarfile.SYMTYPE, link="/etc/passwd"), PLAIN],
        [member("pkg-1.0/x", kind=tarfile.LNKTYPE, link="other-1.0/y"), PLAIN],  # into another top folder
        [member("pkg-1.0/dev", kind=tarfile.CHRTYPE), PLAIN],
        [member("pkg-1.0/sub/../pyproject.toml", MATURIN)],
        # Names Windows reads as others.
        [PLAIN, member("pkg-1.0/a\\..\\pyproject.toml", b"[x]\n")],  # pyproject.toml itself, on Windows
        [member("pkg-1.0/x", kind=tarfile.SYMTYPE, link="a\\b"), PLAIN],
        [member("pkg-1.0/pyproject.toml.", MATURIN)],
        [member("pkg-1.0/pyproject.toml ", MATURIN)],
        [member("pkg-1.0/pyproject.toml:x", MATURIN)],
        [member("pkg-1.0/PYPROJ~1.TOM", MATURIN)],
        # The file itself.
        [member("pkg-1.0/pyproject.toml", MATURIN + b"#" * pypi.PYPROJECT_MAX_BYTES)],
        [member("pkg-1.0/pyproject.toml", b"[build-system\n")],  # pip can't parse it either
        [member("pkg-1.0/pyproject.toml", b"\xff\xfe")],  # nor read it as UTF-8
    ],
    ids=[
        "duplicate",
        "two-top-folders",
        "root-and-top",
        "dot-top",
        "file-as-top",
        "under-pyproject",
        "unknown-type",
        "symlink",
        "hardlink",
        "folder",
        "case",
        "top-link",
        "link-folder",
        "link-folder-case",
        "write-through-link",
        "nfc-link",
        "hardlink-non-ascii",
        "link-then-non-ascii",
        "link-out",
        "absolute-link",
        "hardlink-other-top",
        "device",
        "dotdot",
        "backslash",
        "backslash-link",
        "trailing-dot",
        "trailing-space",
        "colon",
        "tilde",
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
    with pytest.raises(pypi.SdistRefused, match="sparse"):  # before tarfile reads its map
        fetch(fake_files, sdist(tmp_path, member("pkg-1.0/x", kind=tarfile.GNUTYPE_SPARSE), fmt=tarfile.GNU_FORMAT))


@pytest.mark.parametrize("fmt", [tarfile.PAX_FORMAT, tarfile.GNU_FORMAT])
def test_header_cap(tmp_path, fake_files, monkeypatch, fmt):
    monkeypatch.setattr(pypi, "SDIST_MAX_HEADER", 200)
    data = sdist(tmp_path, member("pkg-1.0/" + "x" * 300, b""), PLAIN, fmt=fmt)
    with pytest.raises(pypi.SdistRefused, match="header"):
        fetch(fake_files, data)


def test_all_headers_cap(tmp_path, fake_files, monkeypatch):
    monkeypatch.setattr(pypi, "SDIST_MAX_HEADERS", 20_000)
    many = [member(f"pkg-1.0/{i}", b"", pax={"comment": "x" * 1000}) for i in range(30)]
    with pytest.raises(pypi.SdistRefused, match="header"):
        fetch(fake_files, sdist(tmp_path, *many, PLAIN))
    assert fetch(fake_files, sdist(tmp_path, *many[:10], PLAIN)) is not None


def test_download_cap(tmp_path, fake_files, monkeypatch):
    monkeypatch.setattr(pypi, "SDIST_MAX_BYTES", 64 * 1024)
    data = sdist(tmp_path, PLAIN, member("pkg-1.0/big", os.urandom(256 * 1024)))
    with pytest.raises(pypi.SdistRefused, match="too big"):
        fetch(fake_files, data)


def test_declared_size_cap(fake_files, monkeypatch):
    class Big(io.BytesIO):
        headers = {"Content-Length": str(pypi.SDIST_MAX_BYTES + 1)}

    fake_files.files["pkg-1.0.tar.gz"] = lambda: Big(b"never read")
    with pytest.raises(pypi.SdistRefused, match="too big") as e:
        pypi.fetch_sdist_pyproject("https://files.example/pkg-1.0.tar.gz")
    assert e.value.verified  # its size won't change


def test_unpacked_cap(tmp_path, fake_files, monkeypatch):
    # Zeros compress to almost nothing, so only a cap on the unpacked bytes stops this one.
    monkeypatch.setattr(pypi, "SDIST_MAX_UNPACKED", 1024 * 1024)
    data = sdist(tmp_path, PLAIN, member("pkg-1.0/zeros", bytes(8 << 20)))
    with pytest.raises(pypi.SdistRefused, match="too big"):
        fetch(fake_files, data)


def test_sha256_covers_the_whole_download(tmp_path, fake_files):
    # A second gzip member after the tar, bigger than gzip's read-ahead: the tar ends long before the file does.
    data = sdist(tmp_path, PLAIN) + gzip.compress(os.urandom(512 * 1024))
    fake_files.files["pkg-1.0.tar.gz"] = data
    url = "https://files.example/pkg-1.0.tar.gz"
    assert pypi.fetch_sdist_pyproject(url, sha256=sha(data).upper())[1] is True
    assert pypi.fetch_sdist_pyproject(url)[1] is False  # nothing to check it against
    with pytest.raises(pypi.SdistHashMismatch):
        pypi.fetch_sdist_pyproject(url, sha256="0" * 64)


def test_refusals_count_only_once_the_hash_matches(tmp_path, fake_files):
    url = "https://files.example/pkg-1.0.tar.gz"
    fake_files.files["pkg-1.0.tar.gz"] = data = b"<html>a cache page</html>"
    with pytest.raises(pypi.SdistHashMismatch):  # a corrupted download, not a refused sdist
        pypi.fetch_sdist_pyproject(url, sha256="0" * 64)
    with pytest.raises(pypi.SdistRefused) as e:
        pypi.fetch_sdist_pyproject(url)
    assert not e.value.verified
    with pytest.raises(pypi.SdistRefused) as e:
        pypi.fetch_sdist_pyproject(url, sha256=sha(data))
    assert e.value.verified
    fake_files.files["pkg-1.0.tar.gz"] = data = sdist(tmp_path, member("pkg-1.0/pyproject.toml", b"[x\n"))
    with pytest.raises(pypi.SdistRefused, match="invalid pyproject.toml") as e:
        pypi.fetch_sdist_pyproject(url, sha256=sha(data))
    assert e.value.verified


@pytest.mark.parametrize("error", [RecursionError, MemoryError])
def test_runaway_headers_are_refused(fake_files, monkeypatch, tmp_path, error):
    def boom(stream):
        raise error

    monkeypatch.setattr(pypi, "_root_pyproject", boom)
    data = sdist(tmp_path, PLAIN)
    with pytest.raises(pypi.SdistRefused) as e:
        fetch(fake_files, data, sha256=sha(data))
    assert e.value.verified


class Dribble:
    """A response that gives `step` bytes per read1, each read taking `cost` seconds on a fake clock."""

    def __init__(self, data, clock=None, step=16, cost=5):
        self.data, self.clock, self.step, self.cost = io.BytesIO(data), clock, step, cost

    def read1(self, n):
        if self.clock is not None:
            self.clock[0] += self.cost
        return self.data.read(min(n, self.step))

    def read(self, n=-1):
        raise AssertionError("read() can block for many network reads: read1() only")

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        pass


@pytest.fixture
def clock(monkeypatch):
    now = [0.0]
    monkeypatch.setattr(pypi, "_clock", lambda: now[0])
    return now


def test_short_reads_are_fine(tmp_path, fake_files):
    # One byte per network read: gzip still gets its 2-byte magic number in one call.
    data = sdist(tmp_path, PLAIN)
    fake_files.files["pkg-1.0.tar.gz"] = lambda: Dribble(data, step=1)
    assert pypi.fetch_sdist_pyproject("https://files.example/pkg-1.0.tar.gz", sha256=sha(data)) == (
        {"build-system": {"requires": ["maturin>=1,<2"], "build-backend": "maturin"}},
        True,
    )


def test_a_slow_server_hits_the_deadline(tmp_path, fake_files, clock):
    data = sdist(tmp_path, PLAIN)
    fake_files.files["pkg-1.0.tar.gz"] = lambda: Dribble(data, clock)
    with pytest.raises(pypi.DeadlineExceeded):
        pypi.fetch_sdist_pyproject("https://files.example/pkg-1.0.tar.gz", deadline=30.0)
    assert clock[0] == 35  # stopped at the first read past the deadline


def test_the_timeout_never_outlasts_the_deadline(tmp_path, fake_files, clock):
    fake_files.files["pkg-1.0.tar.gz"] = sdist(tmp_path, PLAIN)
    pypi.fetch_sdist_pyproject("https://files.example/pkg-1.0.tar.gz", deadline=3.0, timeout=10.0)
    pypi.fetch_sdist_pyproject("https://files.example/pkg-1.0.tar.gz", deadline=30.0, timeout=10.0)
    assert fake_files.timeouts == [3.0, 10.0]


def test_the_sdist_request_is_watched(tmp_path, fake_files, monkeypatch):
    watched = []

    @contextmanager
    def watch(deadline):
        watched.append(deadline)
        yield

    monkeypatch.setattr(pypi, "_deadline_watch", watch)
    fake_files.files["pkg-1.0.tar.gz"] = sdist(tmp_path, PLAIN)
    pypi.fetch_sdist_pyproject("https://files.example/pkg-1.0.tar.gz", deadline=1e9)
    assert watched == [1e9]


class Drip(socketserver.BaseRequestHandler):
    """Sends a status line, then a header one byte every 0.3 s: each read is quick, the response never ends."""

    def handle(self):
        try:
            self.request.sendall(b"HTTP/1.1 200 OK\r\n")
            for _ in range(100):
                self.request.sendall(b"X")
                time.sleep(0.3)
        except OSError:
            pass  # the client hung up


def test_dripped_headers_stop_at_the_deadline():
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Drip)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        start = time.monotonic()
        with pytest.raises(pypi.DeadlineExceeded):
            url = f"http://127.0.0.1:{server.server_address[1]}/x"
            pypi.fetch_json(url, timeout=5, attempts=1, deadline=pypi._clock() + 1.0)
        assert time.monotonic() - start < 4
    finally:
        server.shutdown()
        server.server_close()


def test_stream_mode_keeps_no_headers(tmp_path, fake_files, monkeypatch):
    held = []
    real_next = tarfile.TarFile.next

    def next_(self):
        held.append(len(self.members))
        return real_next(self)

    monkeypatch.setattr(tarfile.TarFile, "next", next_)
    fetch(fake_files, sdist(tmp_path, *[member(f"pkg-1.0/{i}", b"") for i in range(20)], PLAIN))
    assert max(held) <= 1


@pytest.mark.parametrize(
    "url", ["http://files.example/pkg-1.0.tar.gz", "https://files.example/pkg-1.0.zip", "file:///pkg-1.0.tar.gz"]
)
def test_only_https_tar_gz(url, fake_files):
    with pytest.raises(ValueError):
        pypi.fetch_sdist_pyproject(url)
    assert fake_files.calls == []


def test_one_attempt_only(tmp_path, fake_files, monkeypatch):
    monkeypatch.setattr(pypi.time, "sleep", lambda s: pytest.fail("retried"))
    busy = urllib.error.HTTPError("u", 503, "busy", {"Retry-After": "1"}, None)  # type: ignore[arg-type]
    fake_files.files["pkg-1.0.tar.gz"] = busy
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
        (["flit"], "flit_core.buildapi", False, "no compiler (flit)"),
        (["flit"], "flit.buildapi", False, "no compiler (flit)"),
        (["flit_core"], "flit.buildapi", False, "at least flit-core"),  # flit.buildapi is in flit, not flit-core
        (["poetry-core>=1"], "poetry.core.masonry.api", False, "no compiler (poetry-core>=1)"),
        (["setuptools"], "flit_core.buildapi", False, "at least setuptools"),  # who provides the backend?
        (["flit_core @ https://example.com/f.tar.gz"], "flit_core.buildapi", False, "at least flit-core (direct URL)"),
        (["flit_core"], None, False, "at least flit-core"),  # no build-backend: setuptools' legacy backend
        (["flit_core"], "flit_core.buildapi", True, "at least flit-core, in-tree backend flit_core.buildapi"),
        (["flit_core", "maturin"], "flit_core.buildapi", False, "at least a Rust toolchain (maturin), flit-core"),
        ([], "flit_core.buildapi", False, None),
        ([], None, False, None),
        (["Foo_Bar[B,a]>=1"], None, False, "at least foo-bar[a,b]>=1"),
        (["foo @ https://example.com/foo.tar.gz"], None, False, "at least foo (direct URL)"),
        (["foo===`x`</details>"], None, False, "at least foo"),  # === takes any text: only the name is shown
    ],
)
def test_describe(requires, backend, backend_path, expected):
    assert describe(info(requires, backend, backend_path), LINUX) == expected


def test_pure_needs_its_provider_on_the_target():
    flit_on_windows = info(["flit_core; sys_platform == 'win32'"], "flit_core.buildapi")
    assert describe(flit_on_windows, WINDOWS) == "no compiler (flit-core)"
    assert describe(flit_on_windows, LINUX) is None


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


def test_a_cached_answer_is_checked_again():
    assert info(["setuptools"], "evil\n::error::x") is None
    assert info(["foo`bar"]) is None


# Which releases are looked up (build.where)


LOCK = Source("uv.lock")  # a lockfile: each package's registry says where it's from
NAMES = Source("command line", pypi_only=True)  # names wheelgap looks up on PyPI itself


def pkg(registry=PYPI, url=f"{FILES}/x-1.0.tar.gz", files=("x-1.0.tar.gz",), sha256=None, **kwargs):
    return Package("x", "1.0", files=list(files), registry=registry, sdist_url=url, sdist_sha256=sha256, **kwargs)


FOUND = pkg(registry=None, url=None, found_on_pypi=True)  # looked up on pypi.org this run


@pytest.mark.parametrize(
    ("package", "expected"),
    [
        (pkg(), ((PYPI, "x", "1.0", ""), f"{FILES}/x-1.0.tar.gz")),
        (pkg(sha256="ab" * 32), ((PYPI, "x", "1.0", "ab" * 32), f"{FILES}/x-1.0.tar.gz")),
        (pkg(registry=PYPI + "/"), ((PYPI, "x", "1.0", ""), f"{FILES}/x-1.0.tar.gz")),
        (pkg(registry="https://acme.example/simple"), None),  # a private index
        (pkg(registry=None), None),  # a pylock.toml entry with no index
        (pkg(registry=None, url=None), None),  # a pdm.lock or poetry.lock entry
        (pkg(url=f"{FILES}/other-1.0.tar.gz"), None),  # not the release's sdist
        (pkg(url=f"{FILES}/other-1.0.tar.gz", files=["other-1.0.tar.gz"]), None),  # another release's sdist
        (pkg(url="https://169.254.169.254/x-1.0.tar.gz"), None),  # a lock doesn't choose where to connect
        (pkg(url="https://evil.example/x-1.0.tar.gz"), None),
        (pkg(url="http://files.pythonhosted.org/x-1.0.tar.gz"), None),
        (pkg(url="https://files.pythonhosted.org:8443/x-1.0.tar.gz"), None),
        (pkg(files=["x-1.0.zip"], url=f"{FILES}/x-1.0.zip"), None),
        (pkg(files=["x-1.0.tar.gz", "x-1.0.zip"]), None),  # which one pip takes isn't certain
        (pkg(files=["x-1.0-py3-none-any.whl"]), None),
    ],
)
def test_where(package, expected):
    assert build.where(package, LOCK) == expected


def test_where_follows_known_pypi(monkeypatch):
    # The rule --conda-forge follows (origin.known_pypi): a name looked up on pypi.org counts only if its input names
    # no other index, and nothing counts with another index in the run.
    assert build.where(FOUND, NAMES) == (("https://pypi.org/pypi", "x", "1.0", ""), None)
    assert build.where(FOUND, LOCK) is None  # a pdm.lock or pylock.toml re-checked with --online
    assert build.where(pkg(), LOCK, simple_index="https://i.example/simple") is None
    assert build.where(pkg(skip="private index"), LOCK) is None
    monkeypatch.setenv("PIP_INDEX_URL", "https://i.example/simple")
    assert build.where(FOUND, NAMES) is None
    assert build.where(pkg(), LOCK) is not None  # the lock says where it came from
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://mirror.example/pypi")
    assert build.where(pkg(), LOCK) is None


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ('{ url = "u", hash = "sha256:' + "AB" * 32 + '" }', ("u", "ab" * 32)),
        ('{ url = "u", hash = "sha256:xyz" }', ("u", None)),
        ('{ url = "u", hash = "sha512:' + "ab" * 32 + '" }', ("u", None)),
        ('{ url = "u", hash = "' + "ab" * 32 + '" }', ("u", None)),  # which algorithm?
        ('{ url = "u", hash = "sha256:' + "ab" * 31 + '" }', ("u", None)),
        ('{ url = "u" }', ("u", None)),
    ],
)
def test_uv_lock_sdist_sha256(tmp_path, entry, expected):
    (tmp_path / "uv.lock").write_text(
        'version = 1\n\n[[package]]\nname = "x"\nversion = "1.0"\n'
        f'source = {{ registry = "{PYPI}" }}\nsdist = {entry}\n'
    )
    (src,) = load_inputs([str(tmp_path / "uv.lock")])
    assert (src.packages[0].sdist_url, src.packages[0].sdist_sha256) == expected


def test_pylock_sdist_sha256(tmp_path):
    (tmp_path / "pylock.toml").write_text(
        'lock-version = "1.0"\ncreated-by = "t"\n\n[[packages]]\nname = "x"\nversion = "1.0"\n'
        f'sdist = {{ url = "u", hashes = {{ sha256 = "{"CD" * 32}" }} }}\n'
    )
    (src,) = load_inputs([str(tmp_path / "pylock.toml")])
    assert (src.packages[0].sdist_url, src.packages[0].sdist_sha256) == ("u", "cd" * 32)


# Looking releases up (build.lookup, build.lookup_all)


def phase(deadline=1e9):
    return build._Phase(deadline=deadline)


def x_sdist(tmp_path, *members):
    return sdist(tmp_path, *(members or [member("x-1.0/pyproject.toml", MATURIN)]), name="x-1.0.tar.gz")


def test_only_verified_answers_are_cached(tmp_path, fake_files):
    data = x_sdist(tmp_path, member("x-1.0/README", b""))  # no pyproject.toml: that won't change
    fake_files.files["x-1.0.tar.gz"] = data
    assert build.lookup(pkg(), LOCK, phase(), read_cache=True, write_cache=True) is None
    assert cache.load_build(PYPI, "x", "1.0", "") is None  # a lock's URL without a sha256: never cached
    p = pkg(sha256=sha(data))
    assert build.lookup(p, LOCK, phase(), read_cache=True, write_cache=True) is None
    assert cache.load_build(PYPI, "x", "1.0", sha(data)) == {"answer": None}
    calls = len(fake_files.calls)
    assert build.lookup(p, LOCK, phase(), read_cache=True, write_cache=True) is None
    assert len(fake_files.calls) == calls  # from the cache
    assert build.lookup(p, LOCK, phase(), read_cache=False, write_cache=True) is None
    assert len(fake_files.calls) == calls + 1


def test_failures_are_not_cached(tmp_path, fake_files):
    data = x_sdist(tmp_path)
    p = pkg(sha256=sha(data))
    fake_files.files["x-1.0.tar.gz"] = urllib.error.URLError("down")
    assert build.lookup(p, LOCK, phase(), write_cache=True) is None
    fake_files.files["x-1.0.tar.gz"] = b"<html>corrupted</html>"  # not the file the lock names
    assert build.lookup(p, LOCK, phase(), write_cache=True) is None
    assert cache.load_build(PYPI, "x", "1.0", sha(data)) is None
    fake_files.files["x-1.0.tar.gz"] = data
    assert build.lookup(p, LOCK, phase(), write_cache=True) is not None
    assert cache.load_build(PYPI, "x", "1.0", sha(data))["answer"]["requires"] == ["maturin>=1,<2"]


def test_refused_sdists_are_cached(fake_files):
    data = b"not a tarball"
    fake_files.files["x-1.0.tar.gz"] = data
    assert build.lookup(pkg(sha256=sha(data)), LOCK, phase(), write_cache=True) is None
    assert cache.load_build(PYPI, "x", "1.0", sha(data)) == {"answer": None}


def test_one_lock_cant_answer_for_another(tmp_path, fake_files):
    # The same release, as two locks give it: each answer belongs to its own sha256.
    flit = x_sdist(tmp_path, member("x-1.0/pyproject.toml", b'[build-system]\nrequires = ["flit_core"]\n'))
    honest = pkg(sha256="ab" * 32)
    cache.save_build(PYPI, "x", "1.0", sha(flit), parse({"build-system": {"requires": ["flit_core"]}}))
    assert cache.load_build(*build.where(honest, LOCK)[0]) is None
    fake_files.files["x-1.0.tar.gz"] = x_sdist(tmp_path)
    good, unhashed = pkg(sha256=sha(fake_files.files["x-1.0.tar.gz"])), pkg()
    found, _ = build.lookup_all([(good, LOCK), (honest, LOCK), (unhashed, LOCK)])
    assert len(fake_files.calls) == 3  # one lookup each
    assert set(found) == {id(good), id(unhashed)}  # the download isn't the file the honest lock names


def test_one_lookup_per_release(tmp_path, fake_files):
    fake_files.files["x-1.0.tar.gz"] = x_sdist(tmp_path)
    same = [pkg(), pkg(), pkg(registry=PYPI + "/")]
    found, _ = build.lookup_all([(p, LOCK) for p in same])
    assert set(found) == {id(p) for p in same} and len(fake_files.calls) == 1


def test_sha256_from_the_json_api(tmp_path, fake_files, monkeypatch):
    data = x_sdist(tmp_path)
    fake_files.files["x-1.0.tar.gz"] = data
    asked = []

    def fetch_json(url, timeout=20.0, attempts=4, deadline=None):
        asked.append((attempts, timeout, deadline))
        entry = {"filename": "x-1.0.tar.gz", "url": "https://files.example/x-1.0.tar.gz", "digests": {"sha256": digest}}
        return {"urls": [entry]}

    monkeypatch.setattr(pypi, "fetch_json", fetch_json)
    looked_up = Package("x", "1.0", files=["x-1.0.tar.gz"], found_on_pypi=True)
    digest = "0" * 64
    assert build.lookup(looked_up, NAMES, phase(), write_cache=True) is None
    assert cache.load_build("https://pypi.org/pypi", "x", "1.0", "") is None
    digest = None  # an index without digests: an answer, but not one to keep
    assert build.lookup(looked_up, NAMES, phase(), write_cache=True) is not None
    assert cache.load_build("https://pypi.org/pypi", "x", "1.0", "") is None
    digest = sha(data)
    assert build.lookup(looked_up, NAMES, phase(), write_cache=True) is not None
    assert cache.load_build("https://pypi.org/pypi", "x", "1.0", "") is not None
    assert asked == [(1, build.TIMEOUT, 1e9)] * 3


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
    cache.save_build(PYPI, "x", "1.0", "", answer)
    assert cache.load_build(PYPI, "x", "1.0", "") is None


def test_load_build_age_and_keys(monkeypatch):
    good = {"requires": ["maturin"], "backend": None, "backend_path": False, "build_script": False}
    cache.save_build(PYPI, "x", "1.0", "", good)
    assert cache.load_build(PYPI, "x", "1.0", "") == {"answer": good}
    assert cache.load_build(PYPI, "x", "1.0", "ab" * 32) is None
    cache.save_files(PYPI, "x", "1.0", [{"filename": "x-1.0.tar.gz"}])
    assert cache.load_build(PYPI, "x", "1.0", "") == {"answer": good}  # file lists and build info apart
    assert cache.load_files(PYPI, "x", "1.0") == [{"filename": "x-1.0.tar.gz"}]
    now = cache._now()
    monkeypatch.setattr(cache, "_now", lambda: now + cache.MAX_AGE + 1)
    assert cache.load_build(PYPI, "x", "1.0", "") is None


def test_a_bad_backend_in_the_cache_is_no_answer(fake_files):
    bad = {"requires": ["flit_core"], "backend": "x\n::error::y", "backend_path": False, "build_script": False}
    cache.save_build(PYPI, "x", "1.0", "ab" * 32, bad)
    assert build.lookup(pkg(sha256="ab" * 32), LOCK, phase(), read_cache=True) is None
    assert fake_files.calls == []


def test_no_request_after_the_deadline(fake_files, clock):
    p = phase(deadline=10.0)
    clock[0] = 10.0
    assert build.lookup(pkg(), LOCK, p) is None
    assert (p.stopped, fake_files.calls) == ("time", [])


def test_the_first_connection_failure_stops_the_rest(fake_files, monkeypatch):
    monkeypatch.setattr(build, "WORKERS", 1)
    fake_files.files["a-1.0.tar.gz"] = urllib.error.URLError("down")
    a = Package("a", "1.0", files=["a-1.0.tar.gz"], registry=PYPI, sdist_url=f"{FILES}/a-1.0.tar.gz")
    b = Package("b", "1.0", files=["b-1.0.tar.gz"], registry=PYPI, sdist_url=f"{FILES}/b-1.0.tar.gz")
    found, note = build.lookup_all([(a, LOCK), (b, LOCK)])
    assert found == {} and note == "build info: stopped after a connection failure; 2 packages not read"
    assert fake_files.calls == [f"{FILES}/a-1.0.tar.gz"]


def test_lookup_all_stops_at_the_deadline(tmp_path, fake_files, clock):
    data = x_sdist(tmp_path)
    fake_files.files["x-1.0.tar.gz"] = lambda: Dribble(data, clock)
    assert build.lookup_all([(pkg(sha256=sha(data)), LOCK)]) == (
        {},
        "build info: stopped after 35 s; 1 package not read",
    )
    assert cache.load_build(PYPI, "x", "1.0", sha(data)) is None


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
        f'sdist = {{ url = "{FILES}/nowheels-0.9.tar.gz"{digest}, size = 1 }}\n'
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
    assert fake_files.calls == [f"{FILES}/nowheels-0.9.tar.gz"]  # the lock's URL: no JSON lookup
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
    _, out, _ = run(capsys, uv_lock(tmp_path, sha256=sha(data)), *TARGETS, "--explain", "--build-info")
    assert "build needs" in out


PDM_LOCK = (
    '[metadata]\ngroups = ["default"]\nlock_version = "4.5.0"\n\n'
    '[[package]]\nname = "nowheels"\nversion = "0.9"\n'
    'groups = ["default"]\nfiles = [{file = "nowheels-0.9.tar.gz", hash = "sha256:00"}]\n'
)
PYLOCK = (
    'lock-version = "1.0"\ncreated-by = "test"\n\n[[packages]]\nname = "nowheels"\nversion = "0.9"\n'
    f'sdist = {{name = "nowheels-0.9.tar.gz", url = "{FILES}/nowheels-0.9.tar.gz"}}\n'
)


@pytest.mark.parametrize(
    ("filename", "text", "online"),
    [
        ("uv.lock", None, False),  # filled in by uv_lock() with a private registry
        ("pdm.lock", PDM_LOCK, False),
        ("pdm.lock", PDM_LOCK, True),  # --online looks the files up, but the lock listed them: not PyPI's
        ("pylock.toml", PYLOCK, False),
        ("pylock.toml", PYLOCK, True),
    ],
)
def test_never_looks_up_what_may_not_be_on_pypi(capsys, tmp_path, fake_pypi, fake_files, filename, text, online):
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path)
    if text is None:
        lock = uv_lock(tmp_path, registry="https://acme.example/simple")
    else:
        (tmp_path / filename).write_text(text)
        lock = str(tmp_path / filename)
    _, out, _ = run(capsys, lock, *TARGETS, "--explain", "--build-info", *(["--online"] if online else []))
    assert "nowheels 0.9 on linux-x86_64: ● source" in out
    assert "build needs" not in out
    assert fake_files.calls == []
    assert fake_pypi == (["https://pypi.org/pypi/nowheels/0.9/json"] if online else [])  # the --online check only


def test_pylock_with_the_pypi_index(capsys, tmp_path, fake_files):
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path)
    (tmp_path / "pylock.toml").write_text(PYLOCK.replace('version = "0.9"\n', f'version = "0.9"\nindex = "{PYPI}"\n'))
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
    # ...and with another index in the run, nothing counts as PyPI's (origin.known_pypi).
    _, out, _ = run(capsys, uv_lock(tmp_path), *TARGETS, "--explain", "--build-info", *index)
    assert "build needs" not in out and fake_files.calls == []


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


def test_markers_skip_ignored_targets(capsys, tmp_path, fake_files):
    pyproject = b"[build-system]\nrequires = [\"maturin; sys_platform == 'win32'\", 'setuptools']\n"
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path, pyproject)
    args = (uv_lock(tmp_path), *TARGETS, "--explain", "--build-info", "--json")
    _, out, _ = run(capsys, *args)
    assert json.loads(out)["packages"][0]["build_requires"] == ["maturin", "setuptools"]
    _, out, _ = run(capsys, *args, "--ignore", "nowheels:win-amd64")
    assert json.loads(out)["packages"][0]["build_requires"] == ["setuptools"]


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


def test_failures_change_nothing_but_a_note(capsys, tmp_path, fake_files):
    lock = uv_lock(tmp_path)
    code, out, err = run(capsys, lock, *TARGETS, "--explain")
    fake_files.files["nowheels-0.9.tar.gz"] = urllib.error.URLError("down")
    assert run(capsys, lock, *TARGETS, "--explain", "--build-info") == (
        code,
        out,
        err + "wheelgap: note: build info: stopped after a connection failure; 1 package not read\n",
    )
    del fake_files.files["nowheels-0.9.tar.gz"]  # a 404: the server answered
    assert run(capsys, lock, *TARGETS, "--explain", "--build-info") == (code, out, err)


def test_running_out_of_time_says_so(capsys, tmp_path, fake_files, clock):
    data = nowheels_sdist(tmp_path)
    fake_files.files["nowheels-0.9.tar.gz"] = lambda: Dribble(data, clock)
    code, out, err = run(capsys, uv_lock(tmp_path), *TARGETS, "--explain", "--build-info")
    assert code == 1 and "build needs" not in out
    assert "build info: stopped after 35 s; 1 package not read" in err
    _, _, err = run(capsys, uv_lock(tmp_path), *TARGETS, "--explain", "--build-info", "--json", "-q")
    assert err == ""
