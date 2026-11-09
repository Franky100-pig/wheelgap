import pytest
from packaging.utils import parse_wheel_filename

from wheelgap.targets import (
    DEFAULT_PLATFORMS,
    NATIVE_PLATFORMS,
    PLATFORMS,
    Target,
    TargetError,
    build_targets,
    manylinux_platforms,
    parse_python,
    resolve_platform,
)


def accepts(target: Target, filename: str) -> bool:
    return target.accepts(parse_wheel_filename(filename)[3])


def t(platform: str, py: str = "3.13", **kw) -> Target:
    ma, mi, ft = parse_python(py)
    return Target(ma, mi, ft, platform, **kw)


@pytest.mark.parametrize(
    "spec,expected",
    [("3.13", (3, 13, False)), ("3.14t", (3, 14, True)), ("cp314t", (3, 14, True)), ("312", (3, 12, False))],
)
def test_parse_python(spec, expected):
    assert parse_python(spec) == expected


def test_parse_python_rejects_garbage():
    with pytest.raises(TargetError):
        parse_python("latest")


@pytest.mark.parametrize(
    "alias,expected",
    [
        ("alpine", "musl-x86_64"),
        ("linux-arm64", "linux-aarch64"),
        ("linux/amd64", "linux-x86_64"),
        ("linux/arm64", "linux-aarch64"),
        ("linux/arm64/v8", "linux-aarch64"),
        (" LINUX/ARM64/V8 ", "linux-aarch64"),
        ("windows", "win-amd64"),
        ("MACOS_INTEL", "macos-x86_64"),
    ],
)
def test_aliases(alias, expected):
    assert resolve_platform(alias) == expected


def test_unknown_platform():
    with pytest.raises(TargetError, match="unknown target"):
        resolve_platform("solaris")


def test_manylinux_includes_legacy_aliases():
    plats = manylinux_platforms("x86_64", (2, 28))
    assert plats[0] == "manylinux_2_28_x86_64"
    assert "manylinux2014_x86_64" in plats and "manylinux1_x86_64" in plats
    assert "manylinux1_aarch64" not in manylinux_platforms("aarch64", (2, 28))
    assert "manylinux2014_aarch64" in manylinux_platforms("aarch64", (2, 28))


def test_glibc_ceiling():
    assert accepts(t("linux-x86_64", glibc=(2, 36)), "x-1-cp313-cp313-manylinux_2_34_x86_64.whl")
    assert not accepts(t("linux-x86_64", glibc=(2, 31)), "x-1-cp313-cp313-manylinux_2_34_x86_64.whl")
    assert accepts(t("linux-x86_64"), "x-1-cp313-cp313-manylinux2014_x86_64.whl")


def test_glibc_and_musl_dont_mix():
    assert not accepts(t("musl-x86_64"), "x-1-cp313-cp313-manylinux_2_17_x86_64.whl")
    assert not accepts(t("linux-x86_64"), "x-1-cp313-cp313-musllinux_1_1_x86_64.whl")
    assert accepts(t("musl-x86_64"), "x-1-cp313-cp313-musllinux_1_1_x86_64.whl")


def test_arch_matters():
    assert not accepts(t("linux-aarch64"), "x-1-cp313-cp313-manylinux_2_17_x86_64.whl")
    assert not accepts(t("win-arm64"), "x-1-cp313-cp313-win_amd64.whl")
    assert accepts(t("win-arm64"), "x-1-cp313-cp313-win_arm64.whl")


def test_macos_universal2_and_minimum_version():
    assert accepts(t("macos-arm64"), "x-1-cp313-cp313-macosx_10_13_universal2.whl")
    assert accepts(t("macos-x86_64"), "x-1-cp313-cp313-macosx_10_9_x86_64.whl")
    assert not accepts(t("macos-arm64", macos=(14, 0)), "x-1-cp313-cp313-macosx_15_0_arm64.whl")
    assert accepts(t("macos-arm64", macos=(15, 0)), "x-1-cp313-cp313-macosx_15_0_arm64.whl")


def test_python_version_matters():
    assert not accepts(t("linux-x86_64", "3.14"), "x-1-cp313-cp313-manylinux_2_17_x86_64.whl")
    assert accepts(t("linux-x86_64", "3.14"), "x-1-cp310-abi3-manylinux_2_17_x86_64.whl")
    assert not accepts(t("linux-x86_64", "3.11"), "x-1-cp312-abi3-manylinux_2_17_x86_64.whl")


def test_free_threading():
    ft = t("linux-x86_64", "3.14t")
    assert accepts(ft, "x-1-cp314-cp314t-manylinux_2_17_x86_64.whl")
    assert not accepts(ft, "x-1-cp314-cp314-manylinux_2_17_x86_64.whl")
    assert not accepts(ft, "x-1-cp310-abi3-manylinux_2_17_x86_64.whl")
    assert not accepts(t("linux-x86_64", "3.14"), "x-1-cp314-cp314t-manylinux_2_17_x86_64.whl")


def test_pure_python_everywhere():
    for plat in PLATFORMS:
        assert accepts(t(plat, "3.14t"), "x-1-py3-none-any.whl"), plat
    assert not accepts(t("linux-x86_64"), "x-1-py2-none-any.whl")


def test_marker_env():
    env = t("win-arm64", "3.14").marker_env
    assert env["sys_platform"] == "win32" and env["platform_machine"] == "ARM64"
    assert env["python_version"] == "3.14"
    assert t("musl-aarch64").marker_env["platform_machine"] == "aarch64"


def test_build_targets_defaults_and_all():
    ts = build_targets(["3.13"], [])
    assert [x.platform for x in ts] == DEFAULT_PLATFORMS
    ts = build_targets(["3.13,3.14t"], ["all"])
    assert len(ts) == 2 * len(NATIVE_PLATFORMS)
    ts = build_targets(["3.13"], ["alpine,linux", "alpine"])
    assert [x.platform for x in ts] == ["musl-x86_64", "linux-x86_64"]


def test_build_targets_accepts_docker_platform_list():
    ts = build_targets(["3.13"], ["linux/amd64,linux/arm64,linux/arm64/v8"])
    assert [x.platform for x in ts] == ["linux-x86_64", "linux-aarch64"]


def test_build_targets_bad_glibc():
    with pytest.raises(TargetError):
        build_targets(["3.13"], [], glibc="new")


def test_pyodide_tags_per_python():
    (t313,) = build_targets(["3.13"], ["pyodide"])
    assert accepts(t313, "x-1-cp313-cp313-pyodide_2025_0_wasm32.whl")
    assert accepts(t313, "x-1-cp313-cp313-pyemscripten_2025_0_wasm32.whl")  # PEP 783 spelling of the same ABI
    assert accepts(t313, "x-1-py3-none-any.whl")
    assert not accepts(t313, "x-1-cp313-cp313-pyemscripten_2026_0_wasm32.whl")  # ABIs don't mix
    assert not accepts(t313, "x-1-cp313-cp313-manylinux_2_17_x86_64.whl")
    (t314,) = build_targets(["3.14"], ["wasm"])
    assert accepts(t314, "x-1-cp314-cp314-pyemscripten_2026_0_wasm32.whl")
    assert not accepts(t314, "x-1-cp313-cp313-pyodide_2025_0_wasm32.whl")
    (t312,) = build_targets(["3.12"], ["emscripten"])
    assert accepts(t312, "x-1-cp312-cp312-pyodide_2024_0_wasm32.whl")


def test_pyodide_marker_env():
    (t,) = build_targets(["3.13"], ["pyodide"])
    assert t.marker_env["sys_platform"] == "emscripten"
    assert t.marker_env["platform_machine"] == "wasm32"
    assert not t.builds_sdists


def test_pyodide_unknown_python_and_free_threading():
    with pytest.raises(TargetError, match="no Pyodide build for Python 3.10"):
        build_targets(["3.10"], ["pyodide"])
    with pytest.raises(TargetError, match="no Pyodide build for Python 3.14t"):
        build_targets(["3.14t"], ["pyodide"])


def test_all_does_not_include_pyodide():
    assert "pyodide" not in [t.platform for t in build_targets(["3.13"], ["all"])]
    assert len(build_targets(["3.13"], ["all"])) == 8


def test_python_37_uses_the_m_abi():
    # CPython 3.7 and older built pymalloc ("m") ABIs, so numpy's cp37-cp37m wheels install there.
    assert accepts(t("linux-x86_64", "3.7"), "numpy-1.21.6-cp37-cp37m-manylinux_2_12_x86_64.manylinux2010_x86_64.whl")
    assert not accepts(t("linux-x86_64", "3.8"), "x-1-cp38-cp38m-manylinux_2_17_x86_64.whl")


def test_abi3t_wheels_on_free_threaded_315():
    wheel = "x-1.0-cp315-abi3.abi3t-manylinux_2_28_x86_64.whl"
    assert accepts(t("linux-x86_64", "3.15t"), wheel)  # needs packaging >= 26.3 (PEP 803)
    assert not accepts(t("linux-x86_64", "3.14t"), wheel)
    assert not accepts(t("linux-x86_64", "3.15t"), "x-1.0-cp312-abi3-manylinux_2_28_x86_64.whl")


def test_version_floors_accept_a_major_only():
    (target,) = build_targets(["3.13"], ["macos"], macos="15")
    assert target.macos == (15, 0)
