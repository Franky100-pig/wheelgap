"""--explain's build info (#230): reading an sdist's pyproject.toml, and what its build requirements mean."""

import io
import json
import os
import tarfile

import pytest

from wheelgap import pypi
from wheelgap.build import BuildInfo, describe
from wheelgap.cli import main

URL = "https://files.example/pkg-1.0.tar.gz"
MATURIN = b'[build-system]\nrequires = ["maturin>=1,<2"]\nbuild-backend = "maturin"\n'


def member(name, data=b"", kind=tarfile.REGTYPE, link=""):
    info = tarfile.TarInfo(name)
    info.type, info.linkname = kind, link
    if kind == tarfile.REGTYPE:
        info.size = len(data)
    return info, data


def sdist(tmp_path, *members, name="pkg-1.0.tar.gz") -> bytes:
    """A .tar.gz built in tmp_path from (TarInfo, data) pairs, in that order."""
    path = tmp_path / name
    with tarfile.open(path, "w:gz") as tar:
        for info, data in members:
            tar.addfile(info, io.BytesIO(data) if info.type == tarfile.REGTYPE else None)
    return path.read_bytes()


def serve(fake_files, data, name="pkg-1.0.tar.gz"):
    fake_files.files[name] = data
    return f"https://files.example/{name}"


def test_reads_the_top_level_pyproject(tmp_path, fake_files):
    data = sdist(
        tmp_path,
        member("pkg-1.0/PKG-INFO", b"Metadata-Version: 2.1\n"),
        member("pkg-1.0/sub/pyproject.toml", b'[build-system]\nrequires = ["wrong"]\n'),  # not top-level
        member("pkg-1.0/pyproject.toml", MATURIN),
    )
    assert pypi.fetch_sdist_pyproject(serve(fake_files, data))["build-system"]["requires"] == ["maturin>=1,<2"]


def test_stops_reading_at_pyproject(tmp_path, fake_files):
    # A big member after pyproject.toml, then the download cut off halfway: nothing past the file is read.
    data = sdist(tmp_path, member("pkg-1.0/pyproject.toml", MATURIN), member("pkg-1.0/big.bin", os.urandom(1 << 20)))
    assert pypi.fetch_sdist_pyproject(serve(fake_files, data[: len(data) // 2])) is not None


def test_no_pyproject_is_none(tmp_path, fake_files):
    data = sdist(tmp_path, member("pkg-1.0/setup.py", b"from setuptools import setup\nsetup()\n"))
    assert pypi.fetch_sdist_pyproject(serve(fake_files, data)) is None


def test_download_cap(tmp_path, fake_files, monkeypatch):
    monkeypatch.setattr(pypi, "SDIST_MAX_BYTES", 64 * 1024)
    data = sdist(tmp_path, member("pkg-1.0/big.bin", os.urandom(256 * 1024)), member("pkg-1.0/pyproject.toml", MATURIN))
    with pytest.raises(pypi.SdistTooBig):
        pypi.fetch_sdist_pyproject(serve(fake_files, data))


def test_unpacked_cap(tmp_path, fake_files, monkeypatch):
    # Zeros compress to almost nothing, so only a cap on the unpacked bytes stops this one.
    monkeypatch.setattr(pypi, "SDIST_MAX_UNPACKED", 1024 * 1024)
    data = sdist(tmp_path, member("pkg-1.0/zeros", bytes(8 << 20)), member("pkg-1.0/pyproject.toml", MATURIN))
    assert len(data) < pypi.SDIST_MAX_BYTES
    with pytest.raises(pypi.SdistTooBig):
        pypi.fetch_sdist_pyproject(serve(fake_files, data))


@pytest.mark.parametrize(
    "trick",
    [
        member("pkg-1.0/pyproject.toml", kind=tarfile.SYMTYPE, link="/etc/passwd"),
        member("pkg-1.0/pyproject.toml", kind=tarfile.LNKTYPE, link="pkg-1.0/other.toml"),
        member("pkg-1.0/pyproject.toml", kind=tarfile.DIRTYPE),
        member("pkg-1.0/pyproject.toml", b"x" * (pypi.PYPROJECT_MAX_BYTES + 1)),
    ],
    ids=["symlink", "hardlink", "folder", "oversized"],
)
def test_tar_tricks_give_no_answer(tmp_path, fake_files, trick):
    # Even with a real pyproject.toml after it: the first member by that name decides.
    data = sdist(tmp_path, member("pkg-1.0/other.toml", MATURIN), trick, member("pkg-1.0/pyproject.toml", MATURIN))
    assert pypi.fetch_sdist_pyproject(serve(fake_files, data)) is None


@pytest.mark.parametrize("name", ["../pyproject.toml", "/pyproject.toml", "./pyproject.toml", "pyproject.toml"])
def test_names_outside_the_top_folder_are_ignored(tmp_path, fake_files, name):
    data = sdist(tmp_path, member(name, MATURIN))
    assert pypi.fetch_sdist_pyproject(serve(fake_files, data)) is None


@pytest.mark.parametrize(
    "url", ["http://files.example/pkg-1.0.tar.gz", "https://files.example/pkg-1.0.zip", "file:///pkg-1.0.tar.gz"]
)
def test_only_https_tar_gz(url, fake_files):
    with pytest.raises(ValueError):
        pypi.fetch_sdist_pyproject(url)
    assert fake_files.calls == []


def test_not_a_tarball_raises(fake_files):
    with pytest.raises(OSError):  # gzip.BadGzipFile
        pypi.fetch_sdist_pyproject(serve(fake_files, b"<html>not found</html>"))


@pytest.mark.parametrize(
    ("requires", "build_script", "expected"),
    [
        (["maturin>=1,<2"], False, "a Rust toolchain (maturin)"),
        (["setuptools", "setuptools-rust"], False, "a Rust toolchain (setuptools-rust), setuptools"),
        (["scikit-build-core>=0.10", "pybind11"], False, "CMake and a C++ compiler (scikit-build-core), pybind11"),
        (["cmake", "setuptools"], False, "CMake and a C++ compiler (cmake), setuptools"),
        (["meson-python", "Cython>=3"], False, "a C compiler (meson-python, Cython)"),
        (
            ["Cython; python_version < '3.13'", "Cython>=3; python_version >= '3.13'", "wheel", "wheel"],
            False,
            "a C compiler (Cython), wheel",
        ),
        (["setuptools>=61", "wheel"], False, "setuptools, wheel"),  # setuptools alone can't prove no compiler
        (["hatchling"], False, "hatchling"),  # hatch.toml may add a build hook
        (["pdm-backend"], False, "pdm-backend"),  # pdm_build.py may compile
        (["foo-backend"], False, "foo-backend"),
        (["flit_core >=3.2,<4"], False, "no compiler (flit_core)"),
        (["poetry-core>=1"], False, "no compiler (poetry-core)"),
        (["poetry-core>=1", "setuptools"], True, "poetry-core, setuptools"),  # [tool.poetry] build script
        (["poetry-core>=1"], True, "poetry-core"),
        (["flit-core", "maturin"], False, "a Rust toolchain (maturin)"),
        (["not a requirement!"], False, "not a requirement!"),
        ([], False, None),
    ],
)
def test_describe(requires, build_script, expected):
    assert describe(BuildInfo(tuple(requires), build_script)) == expected


def run(capsys, *args):
    code = main(list(args))
    out, err = capsys.readouterr()
    return code, out, err


TWO_SOURCE_TARGETS = ("nowheels==0.9", "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--color", "never")


def nowheels_sdist(tmp_path, pyproject=MATURIN):
    return sdist(tmp_path, member("nowheels-0.9/pyproject.toml", pyproject), name="nowheels-0.9.tar.gz")


def test_explain_says_what_the_build_needs_once_per_release(capsys, tmp_path, fake_pypi, fake_files):
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path)
    code, out, _ = run(capsys, *TWO_SOURCE_TARGETS, "--explain")
    assert code == 1
    assert out.count("  build needs: a Rust toolchain (maturin)") == 1  # two source cells, said once
    assert fake_files.calls == ["https://files.example/nowheels-0.9.tar.gz"]

    code, out, _ = run(capsys, *TWO_SOURCE_TARGETS, "--explain", "--json")
    assert code == 1
    assert json.loads(out)["packages"][0]["build_requires"] == ["maturin>=1,<2"]

    code, out, _ = run(capsys, *TWO_SOURCE_TARGETS, "--explain", "--markdown")
    assert "build needs: a Rust toolchain (maturin)" in out


def test_build_info_is_cached_per_release(capsys, tmp_path, fake_pypi, fake_files):
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path)
    run(capsys, *TWO_SOURCE_TARGETS, "--explain", "--no-cache")
    run(capsys, *TWO_SOURCE_TARGETS, "--explain", "--no-cache")
    assert len(fake_files.calls) == 2  # --no-cache neither reads nor saves
    run(capsys, *TWO_SOURCE_TARGETS, "--explain")
    del fake_files.files["nowheels-0.9.tar.gz"]  # a later run doesn't download again
    _, out, _ = run(capsys, *TWO_SOURCE_TARGETS, "--explain")
    assert len(fake_files.calls) == 3
    assert "build needs: a Rust toolchain (maturin)" in out


def test_unknown_backend_is_shown_as_written(capsys, tmp_path, fake_pypi, fake_files):
    pyproject = b'[build-system]\nrequires = ["foo-backend>=2"]\nbuild-backend = "foo"\n'
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path, pyproject)
    _, out, _ = run(capsys, *TWO_SOURCE_TARGETS, "--explain")
    assert "  build needs: foo-backend\n" in out


@pytest.mark.parametrize(
    "served",
    [None, b"not a tarball", b'[build-system]\nrequires = "maturin"\n'],
    ids=["404", "garbage", "bad-requires"],
)
def test_no_answer_changes_nothing(capsys, tmp_path, fake_pypi, fake_files, served):
    if served is not None:
        fake_files.files["nowheels-0.9.tar.gz"] = (
            served if served.startswith(b"not") else nowheels_sdist(tmp_path, served)
        )
    want = run(capsys, *TWO_SOURCE_TARGETS, "--explain", "--no-build-info")
    assert run(capsys, *TWO_SOURCE_TARGETS, "--explain") == want
    code, out, err = run(capsys, *TWO_SOURCE_TARGETS, "--explain", "--json")
    assert code == want[0] and err == ""
    assert json.loads(out)["packages"][0]["build_requires"] is None


def test_no_build_info_and_offline_download_nothing(capsys, tmp_path, fake_pypi, fake_files):
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path)
    _, out, _ = run(capsys, *TWO_SOURCE_TARGETS, "--explain", "--no-build-info")
    assert "build needs" not in out and fake_files.calls == []

    lock = tmp_path / "uv.lock"
    lock.write_text(
        'version = 1\nrequires-python = ">=3.13"\n\n[[package]]\nname = "nowheels"\nversion = "0.9"\n'
        'source = { registry = "https://pypi.org/simple" }\n'
        'sdist = { url = "https://files.example/nowheels-0.9.tar.gz", hash = "sha256:00", size = 1 }\n'
    )
    args = (str(lock), "-p", "3.13", "-t", "linux-x86_64", "--explain")
    _, out, _ = run(capsys, *args)
    assert "build needs: a Rust toolchain (maturin)" in out
    fake_pypi.clear()
    fake_files.calls.clear()
    _, out, _ = run(capsys, *args, "--offline", "--no-cache")
    assert "build needs" not in out
    assert fake_pypi == [] and fake_files.calls == []


def test_build_info_is_only_for_source_cells(capsys, tmp_path, fake_pypi, fake_files):
    # fastlib has an sdist but a Linux wheel: no source cell there, so no download.
    fake_files.files["fastlib-2.0.0.tar.gz"] = sdist(
        tmp_path, member("fastlib-2.0.0/pyproject.toml", MATURIN), name="fastlib-2.0.0.tar.gz"
    )
    _, out, _ = run(capsys, "fastlib==2.0.0", "-p", "3.13", "-t", "linux-x86_64", "--explain", "--all", "--json")
    assert json.loads(out)["packages"][0]["build_requires"] is None
    assert fake_files.calls == []


def test_no_build_info_with_index_url(capsys, tmp_path, fake_pypi, fake_simple, fake_files):
    # A private index's packages aren't looked up on the JSON API for their sdist.
    fake_files.files["nowheels-0.9.tar.gz"] = nowheels_sdist(tmp_path)
    _, out, _ = run(capsys, *TWO_SOURCE_TARGETS, "--explain", "--index-url", "https://index.example/simple")
    assert "nowheels 0.9 on linux-x86_64: ● source" in out
    assert "build needs" not in out
    assert fake_pypi == [] and fake_files.calls == []
