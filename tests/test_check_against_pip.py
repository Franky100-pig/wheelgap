"""scripts/check_against_pip.py (#226), offline: the pip arguments it builds and how it reports, with a fake pip."""

import importlib.util
import io
import sys
from pathlib import Path

import pytest

from wheelgap import make_targets


def _script():
    path = Path(__file__).parents[1] / "scripts" / "check_against_pip.py"
    spec = importlib.util.spec_from_file_location("wheelgap_check_against_pip", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


script = _script()


def _target(platform, python="3.14", **versions):
    return make_targets(python, platform, **versions)[0]


def test_pip_platforms_for_linux_x86_64_are_agents_md_example():
    # AGENTS.md: `seq 36 -1 5`, then manylinux2014, manylinux2010 and manylinux1.
    expected = [f"manylinux_2_{m}_x86_64" for m in range(36, 4, -1)]
    expected += ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"]
    assert script.pip_platforms(_target("linux-x86_64")) == expected


@pytest.mark.parametrize(
    ("platform", "versions", "expected"),
    [
        ("linux-aarch64", {}, [f"manylinux_2_{m}_aarch64" for m in range(36, 16, -1)] + ["manylinux2014_aarch64"]),
        (
            "linux-x86_64",
            {"glibc": "2.17"},
            [f"manylinux_2_{m}_x86_64" for m in range(17, 4, -1)]
            + ["manylinux2014_x86_64", "manylinux2010_x86_64", "manylinux1_x86_64"],
        ),
        (
            "linux-x86_64",
            {"glibc": "2.12"},
            [f"manylinux_2_{m}_x86_64" for m in range(12, 4, -1)] + ["manylinux2010_x86_64", "manylinux1_x86_64"],
        ),
        ("linux-armv7l", {"glibc": "2.17"}, ["manylinux_2_17_armv7l", "manylinux2014_armv7l", "linux_armv7l"]),
        ("musl-aarch64", {}, ["musllinux_1_2_aarch64", "musllinux_1_1_aarch64"]),
        ("win-arm64", {}, ["win_arm64"]),
        ("win-amd64", {}, ["win_amd64"]),
        ("macos-arm64", {}, ["macosx_14_0_arm64"]),
    ],
)
def test_pip_platforms(platform, versions, expected):
    assert script.pip_platforms(_target(platform, **versions)) == expected


@pytest.mark.parametrize("platform", ["linux-riscv64", "pyodide"])
def test_pip_platforms_refuses_targets_agents_md_has_no_tags_for(platform):
    with pytest.raises(ValueError):
        script.pip_platforms(_target(platform, python="3.13"))


def test_pip_args_free_threaded():
    args = script.pip_args("numpy", "2.3.4", _target("win-amd64", python="3.14t"), "/tmp/x")
    assert args[:3] == ["download", "--no-deps", "--only-binary=:all:"]
    assert args[-1] == "numpy==2.3.4"
    assert args[args.index("--abi") + 1] == "cp314t"
    assert args[args.index("--python-version") + 1] == "3.14"
    assert args[args.index("-d") + 1] == "/tmp/x"


def test_default_targets_have_distinct_labels():
    labels = [script.label(t) for t in script.default_targets()]
    assert len(set(labels)) == len(labels) == 9
    assert "3.14 linux-x86_64 glibc 2.17" in labels
    for t in script.default_targets():
        script.pip_platforms(t)  # every default target has pip tags


def _fake_pip(wheels):
    """A pip that "downloads" a wheel for the (package, platform) pairs in `wheels`; None means pip errors."""
    asked = []

    def pip(args):
        name = args[-1].split("==")[0]
        platform = args[args.index("--platform") + 1]
        dest = Path(args[args.index("-d") + 1])
        assert dest.is_dir() and not any(dest.iterdir())  # emptied after every download
        asked.append((name, platform))
        answer = wheels.get((name, platform), False)
        if answer is None:
            return script.PipAnswer(error="pip: connection reset")
        if answer:
            (dest / f"{name}-1.whl").write_bytes(b"")  # left behind for check_one to clear
            return script.PipAnswer(wheel=f"{name}-1.whl")
        return script.PipAnswer()

    pip.asked = asked
    return pip


def _run(packages, targets, wheels):
    pip = _fake_pip(wheels)
    out = io.StringIO()
    code = script.report(script.compare(packages, targets, pip, workers=2), out=out)
    return code, out.getvalue(), pip.asked


def test_agreement_exits_0(fake_pypi):
    # fastlib 2.0.0 has a cp313 manylinux_2_17_x86_64 wheel and an sdist; nowheels is sdist-only.
    targets = make_targets("3.13", ["linux-x86_64", "win-amd64"])
    code, out, asked = _run(["fastlib", "nowheels"], targets, {("fastlib", "manylinux_2_36_x86_64"): True})
    assert code == 0
    assert out.startswith("4 checks: 4 agree with pip, 0 disagree, 0 couldn't be compared.")
    assert len(asked) == 4


def test_disagreement_exits_1_with_table_and_pip_command(fake_pypi):
    targets = make_targets("3.13", ["linux-x86_64", "win-amd64"])
    # pip finds a wheel wheelgap doesn't see on Windows, and none where wheelgap sees one on Linux.
    code, out, _ = _run(["fastlib"], targets, {("fastlib", "win_amd64"): True})
    assert code == 1
    assert "2 checks: 0 agree with pip, 2 disagree" in out
    assert "fastlib  2.0.0    3.13 linux-x86_64  wheel     no wheel" in out
    assert "fastlib  2.0.0    3.13 win-amd64     source    fastlib-1.whl" in out
    assert "pip download --no-deps --only-binary=:all: --index-url https://pypi.org/simple -d /tmp/wg-check" in out
    assert "--platform win_amd64 --python-version 3.13 --implementation cp --abi cp313 fastlib==2.0.0" in out


def test_unchecked_exits_2_and_skips_pip_for_failed_lookups(fake_pypi):
    targets = make_targets("3.13", ["win-amd64"])
    code, out, asked = _run(["not-on-index", "fastlib"], targets, {("fastlib", "win_amd64"): None})
    assert code == 2
    assert "0 agree with pip, 0 disagree, 2 couldn't be compared" in out
    assert "not-on-index  -        3.13 win-amd64  not on index" in out
    assert "pip: connection reset" in out
    assert asked == [("fastlib", "win_amd64")]  # pip isn't asked about a package wheelgap couldn't look up


def test_disagreement_wins_over_unchecked(fake_pypi):
    targets = make_targets("3.13", ["win-amd64"])
    code, _, _ = _run(["not-on-index", "fastlib"], targets, {("fastlib", "win_amd64"): True})
    assert code == 1
