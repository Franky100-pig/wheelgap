import json
import os
import tomllib

import pytest

from wheelgap.cli import main
from wheelgap.dockerfile import final_from
from wheelgap.suggest_targets import (
    _Flow,
    _runner_os,
    _same,
    _Scalar,
    _split_key,
    _starts,
    _strip_comment,
    _unquote,
    _Unsure,
    runner_target,
    suggest,
)


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def workflow(tmp_path, text, name="ci.yml"):
    folder = tmp_path / ".github" / "workflows"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text, encoding="utf-8")  # non-ASCII cases: Windows would write cp1252


def where(items):
    return [(f.value, f.path, f.line) for f in items]


def unreadable(found):
    return [(u.path, u.line, u.reason) for u in found.unreadable]


@pytest.mark.parametrize(
    ("label", "target"),
    [
        ("ubuntu-latest", "linux-x86_64"),
        ("ubuntu-24.04", "linux-x86_64"),
        ("ubuntu-22.04", "linux-x86_64"),
        ("ubuntu-slim", "linux-x86_64"),
        ("Ubuntu-Latest", "linux-x86_64"),  # labels are case-insensitive
        ("ubuntu-24.04-arm", "linux-aarch64"),
        ("ubuntu-22.04-arm", "linux-aarch64"),
        ("windows-latest", "win-amd64"),
        ("windows-2025", "win-amd64"),
        ("windows-2025-vs2026", "win-amd64"),
        ("windows-11-arm", "win-arm64"),
        ("windows-11-vs2026-arm", "win-arm64"),
        ("macos-13", "macos-x86_64"),
        ("macos-15-intel", "macos-x86_64"),
        ("macos-latest-large", "macos-x86_64"),
        ("macos-15-large", "macos-x86_64"),
        ("macos-14", "macos-arm64"),
        ("macos-15", "macos-arm64"),
        ("macos-26", "macos-arm64"),
        ("macos-latest", "macos-arm64"),
        ("macos-latest-xlarge", "macos-arm64"),
        ("macos-13-xlarge", "macos-arm64"),
        ("xcode-27", "macos-arm64"),
        ("xcode-27-xlarge", "macos-arm64"),
        # Not known for sure: never guessed.
        ("self-hosted", None),
        ("ubuntu-latest-arm", None),
        ("ubuntu-24.04-16core", None),
        ("windows-11", None),
        ("macos-10.15", None),
        ("xcode-latest", None),
        ("my-runner", None),
    ],
)
def test_runner_target(label, target):
    assert runner_target(label) == target


def test_literal_matrix_with_include_and_exclude(tmp_path):
    workflow(
        tmp_path,
        """\
name: CI
on: [push]
jobs:
  test:
    runs-on: ${{ matrix.os }}  # from the matrix
    strategy:
      matrix:
        os: [ubuntu-latest, windows-11-arm, 'macos-latest']
        python-version:
          - "3.12"
          - "3.13"
        include:
          - os: ubuntu-24.04-arm
            python-version: "3.14t"
        exclude:
          - os: windows-11-arm
    steps:
      - uses: actions/checkout@v5
      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          python-version: ${{ matrix.python-version }}
      - run: |
          echo "runs-on: windows-latest"
          echo "python-version: 3.9"
  lint:
    runs-on: "ubuntu-24.04"
    steps:
      - uses: astral-sh/setup-uv@v7
        with:
          python-version: '3.11.4'
  release:
    uses: ./.github/workflows/release.yml
""",
    )
    found = suggest(tmp_path)
    path = ".github/workflows/ci.yml"
    # windows-11-arm is excluded from every combination, so it isn't suggested; the include adds an arm runner.
    assert where(found.targets) == [
        ("linux-x86_64", path, 8),
        ("macos-arm64", path, 8),
        ("linux-aarch64", path, 13),
        ("linux-x86_64", path, 27),
    ]
    assert where(found.pythons) == [
        ("3.12", path, 10),
        ("3.13", path, 11),
        ("3.14t", path, 14),
        ("3.11", path, 31),
    ]
    assert found.targets[0].source == "runs-on: ubuntu-latest (matrix.os, line 5)"
    assert found.unreadable == []
    assert found.config() == {
        "python": ["3.11", "3.12", "3.13", "3.14t"],
        "targets": ["linux-x86_64", "linux-aarch64", "macos-arm64"],
    }


def test_matrix_it_cant_read_is_listed_not_guessed(tmp_path):
    workflow(
        tmp_path,
        """\
jobs:
  plan:
    runs-on: ubuntu-latest
  wheels:
    needs: plan
    runs-on: ${{ matrix.os }}
    strategy:
      matrix: ${{ fromJSON(needs.plan.outputs.matrix) }}
  computed:
    runs-on: ${{ matrix.runner }}
    strategy:
      matrix:
        runner: ${{ fromJSON(inputs.runners) }}
  multiline:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest,
             [windows-latest]]
  nomatrix:
    runs-on: ${{ matrix.os }}
  expression:
    runs-on: ${{ inputs.os == 'arm' && 'ubuntu-24.04-arm' || 'ubuntu-24.04' }}
  self:
    runs-on: [self-hosted, linux, ARM64]
  group:
    runs-on:
      group: big-runners
  custom:
    runs-on: build-box-1
  container:
    runs-on: ubuntu-latest
    container: python:3.13-alpine
  ambiguous:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, macos-latest]
        exclude:
          - os: MACOS-LATEST
""",
    )
    found = suggest(tmp_path)
    path = ".github/workflows/ci.yml"
    assert where(found.targets) == [("linux-x86_64", path, 3)]
    assert unreadable(found) == [
        (path, 8, "job wheels: the matrix is computed (${{ fromJSON(needs.plan.outputs.matrix) }})"),
        (path, 13, "job computed: matrix.runner is computed (${{ fromJSON(inputs.runners) }})"),
        (path, 18, "job multiline: can't read [ubuntu-latest, [windows-latest]]"),
        (path, 21, "job nomatrix: the job has no strategy.matrix"),
        (
            path,
            23,
            "job expression: runs-on: ${{ inputs.os == 'arm' && 'ubuntu-24.04-arm' || 'ubuntu-24.04' }} "
            "is an expression",
        ),
        (path, 25, "job self: runs-on [self-hosted, linux, ARM64] isn't a single label"),
        (path, 27, "job group: runs-on with several labels or a group isn't a single label"),
        (path, 30, "job custom: build-box-1 isn't a GitHub-hosted runner wheelgap knows"),
        (path, 33, "job container: runs in a container, whose platform isn't read"),
        (
            path,
            40,
            "job ambiguous: can't tell whether GitHub counts 'macos-latest' and 'MACOS-LATEST' as the same value",
        ),
    ]


def test_matrix_with_only_include(tmp_path):
    # With no other keys, each include entry is a combination of its own.
    workflow(
        tmp_path,
        """\
jobs:
  build:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        include:
          - os: windows-latest
            arch: x64
          - os: macos-14
            arch: arm64
""",
    )
    found = suggest(tmp_path)
    assert where(found.targets) == [
        ("win-amd64", ".github/workflows/ci.yml", 7),
        ("macos-arm64", ".github/workflows/ci.yml", 9),
    ]


def test_matrix_with_flow_mappings(tmp_path):
    workflow(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest]
        python: ["3.12", "3.13"]
        include:
          - { os: macos-latest, python: "3.13" }
          - {os: windows-latest, python: '3.13'}
          - { os: macos-26-intel, python: 3.10 }
""",
    )
    found = suggest(tmp_path)
    path = ".github/workflows/ci.yml"
    assert where(found.targets) == [
        ("linux-x86_64", path, 6),
        ("macos-arm64", path, 9),
        ("win-amd64", path, 10),
        ("macos-x86_64", path, 11),
    ]
    assert found.unreadable == []


def test_include_overwrites_what_an_earlier_include_added(tmp_path):
    # os isn't an original matrix key, so the second include overwrites the first on every combination:
    # GitHub never runs on windows-latest here.
    workflow(
        tmp_path,
        """\
jobs:
  build:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        python: ["3.12", "3.13"]
        include:
          - os: windows-latest
          - os: ubuntu-latest
""",
    )
    assert where(suggest(tmp_path).targets) == [("linux-x86_64", ".github/workflows/ci.yml", 9)]


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        ("3.10", "unquoted 3.10 is the number 3.1 in YAML (quote it)"),
        ("'3.x'", "python-version 3.x isn't a CPython version wheelgap can read"),
        ("pypy3.10", "python-version pypy3.10 isn't a CPython version wheelgap can read"),
        ("'>=3.11'", "python-version >=3.11 isn't a CPython version wheelgap can read"),
        ("|\n            3.12\n            3.13", "python-version: a multi-line value"),
    ],
)
def test_python_versions_it_cant_read(tmp_path, value, reason):
    workflow(
        tmp_path,
        f"""\
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: {value}
""",
    )
    found = suggest(tmp_path)
    assert found.pythons == []
    assert unreadable(found) == [(".github/workflows/ci.yml", 7, f"job test: {reason}")]


def test_setup_python_without_a_version(tmp_path):
    workflow(
        tmp_path,
        """\
jobs:
  a:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v6
  b:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version-file: pyproject.toml
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
""",
    )
    found = suggest(tmp_path)
    assert where(found.pythons) == [("3.13", ".github/workflows/ci.yml", 14)]  # the next step is still read
    assert unreadable(found) == [
        (
            ".github/workflows/ci.yml",
            5,
            "job a: setup-python with no python-version (it reads .python-version, or uses the runner's)",
        ),
        (".github/workflows/ci.yml", 11, "job b: setup-python reads pyproject.toml, which isn't read here"),
    ]


def test_multi_stage_dockerfile(tmp_path):
    (tmp_path / "Dockerfile").write_text(
        "FROM python:3.13-slim AS build\n"
        "RUN pip wheel -w /wheels .\n"
        "FROM --platform=linux/arm64 \\\n"
        "    python:3.12-alpine AS base\n"
        "FROM base AS runtime\n"
        "COPY --from=build /wheels /wheels\n"
    )
    (tmp_path / "deploy").mkdir()
    (tmp_path / "deploy" / "Dockerfile.web").write_text("FROM python:3.13-bookworm\n")
    (tmp_path / "deploy" / "Dockerfile.dockerignore").write_text("*.pyc\n")
    (tmp_path / "deploy" / "worker.Dockerfile").write_text("FROM python:3.14-trixie\n")
    (tmp_path / "dockerfile.py").write_text("from __future__ import annotations\n")  # not a Dockerfile
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "Dockerfile").write_text("FROM python:3.9\n")
    found = suggest(tmp_path)
    # The final stage builds on `base`, so the line is base's FROM, where the image is named.
    assert where(found.targets) == [
        ("musl-aarch64", "Dockerfile", 3),
        ("linux-x86_64", "deploy/Dockerfile.web", 1),
        ("linux-x86_64", "deploy/worker.Dockerfile", 1),
    ]
    assert found.targets[0].source == "FROM --platform=linux/arm64 python:3.12-alpine (musl 1.2)"
    assert where(found.pythons) == [
        ("3.12", "Dockerfile", 3),
        ("3.13", "deploy/Dockerfile.web", 1),
        ("3.14", "deploy/worker.Dockerfile", 1),
    ]
    assert found.unreadable == []
    # The two Debian images name different glibc versions, so neither is suggested.
    assert found.config() == {
        "python": ["3.12", "3.13", "3.14"],
        "targets": ["linux-x86_64", "musl-aarch64"],
        "musl": "1.2",
    }


def test_dockerfile_it_cant_read(tmp_path):
    (tmp_path / "Dockerfile").write_text("ARG BASE=python:3.13\nFROM $BASE\n")
    found = suggest(tmp_path)
    assert found.targets == []
    assert unreadable(found) == [("Dockerfile", None, "$BASE: build arguments are not supported")]


def test_glibc_is_left_out_when_the_sources_disagree(tmp_path, capsys):
    workflow(tmp_path, "jobs:\n  test:\n    runs-on: ubuntu-latest\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.13-trixie\n")
    found = suggest(tmp_path)
    assert found.config() == {"python": ["3.13"], "targets": ["linux-x86_64"]}  # the runner's glibc isn't read
    code, out, _ = run(capsys, "--suggest-targets", str(tmp_path))
    assert code == 0
    assert (
        "# glibc not set: the linux-* targets don't all name the same one "
        "(not named at .github/workflows/ci.yml:3; 2.41 at Dockerfile:1)"
    ) in out


def test_empty_project(tmp_path, capsys, fake_pypi):
    code, out, err = run(capsys, "--suggest-targets", str(tmp_path))
    assert code == 0
    assert out == (
        "Targets: none found\nPythons: none found\n\n"
        "No targets found, so there's nothing to suggest. Nothing was changed or checked.\n"
    )
    assert err == ""
    code, out, _ = run(capsys, "--suggest-targets", str(tmp_path), "--json")
    assert code == 0
    assert json.loads(out) == {"targets": [], "python": [], "unreadable": [], "config": {}}
    assert fake_pypi == []


def test_cli_prints_sources_and_a_snippet(tmp_path, capsys, monkeypatch, fake_pypi):
    workflow(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-24.04-arm, windows-latest]
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
""",
        name="test.yaml",
    )
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nbogus = 1\n")  # not read: nothing is checked
    monkeypatch.chdir(tmp_path)
    code, out, err = run(capsys, "--suggest-targets")
    assert code == 0
    assert err == ""
    assert out == (
        "Targets:\n"
        "  linux-aarch64\n"
        "    .github/workflows/test.yaml:6  runs-on: ubuntu-24.04-arm (matrix.os, line 3)\n"
        "  win-amd64\n"
        "    .github/workflows/test.yaml:6  runs-on: windows-latest (matrix.os, line 3)\n"
        "Pythons:\n"
        "  3.13\n"
        "    .github/workflows/test.yaml:10  python-version: 3.13\n"
        "\n"
        "Suggested settings for pyproject.toml (nothing was changed or checked). They combine every job, lint,\n"
        "release and bot jobs too, so keep only the platforms you ship to:\n"
        "\n"
        "[tool.wheelgap]\n"
        'python = ["3.13"]\n'
        'targets = ["linux-aarch64", "win-amd64"]\n'
        'glibc = "2.39"\n'
    )
    snippet = out.split("ship to:\n\n", 1)[1]
    assert tomllib.loads(snippet) == {
        "tool": {"wheelgap": {"python": ["3.13"], "targets": ["linux-aarch64", "win-amd64"], "glibc": "2.39"}}
    }
    code, out, _ = run(capsys, "--suggest-targets", ".", "--json")
    data = json.loads(out)
    assert data["targets"][0] == {
        "target": "linux-aarch64",
        "file": ".github/workflows/test.yaml",
        "line": 6,
        "from": "runs-on: ubuntu-24.04-arm (matrix.os, line 3)",
    }
    assert data["config"] == {"python": ["3.13"], "targets": ["linux-aarch64", "win-amd64"], "glibc": "2.39"}
    assert fake_pypi == []


def test_cli_errors(tmp_path, capsys):
    code, _, err = run(capsys, "--suggest-targets", str(tmp_path / "nope"))
    assert code == 2
    assert "nope: not a folder" in err
    with pytest.raises(SystemExit):
        main(["--suggest-targets", str(tmp_path), "--markdown"])
    assert "no Markdown form" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main([str(tmp_path), "--suggest-targets"])
    assert "--suggest-targets DIR" in capsys.readouterr().err
    for flags in (["-p", "3.13"], ["-t", "linux"], ["--dockerfile", "Dockerfile"], ["--list-targets"]):
        with pytest.raises(SystemExit):
            main(["--suggest-targets", str(tmp_path), *flags])
        assert f"--suggest-targets only suggests, so it can't be used with {flags[0]}" in capsys.readouterr().err


def test_final_from_line_numbers():
    text = "# syntax=docker/dockerfile:1\nFROM python:3.12 AS a\nRUN <<EOF\nFROM python:3.9\nEOF\nFROM \\\n  a\n"
    assert final_from(text) == ("python:3.12", None, 2)
    assert final_from("FROM python:3.11\n\nFROM \\\n# comment\n python:3.13-slim\n") == ("python:3.13-slim", None, 3)


CI = ".github/workflows/ci.yml"


def read(tmp_path, text):
    workflow(tmp_path, text)
    found = suggest(tmp_path)
    return where(found.targets), [(line, reason) for _, line, reason in unreadable(found)], found


@pytest.mark.parametrize(
    ("label", "os_"),
    [
        ("ubuntu-22.04", ("2.35", None)),
        ("ubuntu-22.04-arm", ("2.35", None)),
        ("ubuntu-24.04", ("2.39", None)),
        ("ubuntu-24.04-arm", ("2.39", None)),
        ("ubuntu-latest", (None, None)),  # moves between releases
        ("ubuntu-26.04", (None, None)),  # not in the distro table yet
        ("macos-13", (None, "13.0")),
        ("macos-14", (None, "14.0")),
        ("macos-15-intel", (None, "15.0")),
        ("macos-15-xlarge", (None, "15.0")),
        ("macos-15-large", (None, "15.0")),
        ("macos-latest", (None, None)),
        ("windows-2025", (None, None)),
    ],
)
def test_runner_os(label, os_):
    assert _runner_os(label) == os_


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('"3.13"', ("3.13", True)),
        ("'it''s'", ("it's", True)),
        ("ubuntu-latest", ("ubuntu-latest", False)),
        ('"a\\b"', "can't read the escapes"),
        ('"a"b"', "can't read"),
        ("'a'b'", "can't read"),
        ('"abc', "can't read"),
        ("'", "can't read"),
        ("", "no value"),
        ("*alias", "can't read"),
        ("&anchor x", "can't read"),
        ("- x", "can't read"),
        ("? x", "can't read"),
        ("!!str x", "can't read"),
    ],
)
def test_unquote(text, expected):
    if isinstance(expected, tuple):
        scalar = _unquote(text, 1)
        assert (scalar.text, scalar.quoted) == expected
    else:
        with pytest.raises(_Unsure, match=expected):
            _unquote(text, 1)


# Each case was checked against PyYAML 6.0.3 (`yaml.safe_load("x: " + text)`): where the reader gives a value, it's
# PyYAML's; where PyYAML reads something else (a mapping, nesting, an anchor) or errors, the reader refuses.
FLOWS = [
    ("[a, 'b, c', \"d\"]", ["a", "b, c", "d"]),
    ("[it's, a\"b]", ["it's", 'a"b']),  # a quote inside a plain item is literal
    ("[windows-latest, it's]", ["windows-latest", "it's"]),
    ("{os: it's, python: a\"b}", {"os": "it's", "python": 'a"b'}),
    ("[a,]", ["a"]),
    ("[a:b]", ["a:b"]),
    ("[a b, c]", ["a b", "c"]),
    ("[ a , b ]", ["a", "b"]),
    ("[]", []),
    ("['it''s']", ["it's"]),
    ('[{os: x, python: "3.13"}, {os: y}]', [{"os": "x", "python": "3.13"}, {"os": "y"}]),
    ("[{os: a}, b]", [{"os": "a"}, "b"]),
    ("{os: x, python: '3.13'}", {"os": "x", "python": "3.13"}),
    ("[k: v]", None),  # [{k: v}]
    ('["x": y]', None),  # [{x: y}]
    ("[?b]", None),  # [{b: null}]
    ("{a:b}", None),  # {"a:b": null}
    ("[a,#b]", None),  # an error
    ("[a #b]", None),  # an error
    ("[a, [b]]", None),
    ("[a, {b: [c]}]", None),
    ("[a,,b]", None),
    ('["a" b]', None),
    ("[- a]", None),
    ('["a\\tb"]', None),  # an escape
    ("[&x a]", None),
    ("[*x]", None),
    ("[!!str a]", None),
    ("[a", None),
    ("['a]", None),
]


def _plain(value):
    if isinstance(value, list):
        return [_plain(v) for v in value]
    if isinstance(value, dict):
        return {k: _plain(v) for k, v in value.items()}
    return value.text


@pytest.mark.parametrize(("text", "expected"), FLOWS)
def test_flow(text, expected):
    if expected is None:
        with pytest.raises(_Unsure):
            _Flow(text, 1).read()
    else:
        assert _plain(_Flow(text, 1).read()) == expected


@pytest.mark.parametrize(("text", "expected"), FLOWS)
def test_flow_matches_pyyaml(text, expected):
    yaml = pytest.importorskip("yaml")
    if expected is not None:
        assert yaml.safe_load("x: " + text)["x"] == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("a # c", "a"),
        ("a#b", "a#b"),
        ("# c", ""),
        ('"a # b" # c', '"a # b"'),
        ("'it''s # x' # c", "'it''s # x'"),
        ('"a\\" # b" # c', '"a\\" # b"'),
        ("key: it's # c", "key: it's"),
        ("a\t# c", "a"),
    ],
)
def test_strip_comment(text, expected):
    assert _strip_comment(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('"on": push', ("on", "push")),
        ("'a b': c", ("a b", "c")),
        ('"runs-on":', ("runs-on", "")),
        ('"a"x: y', None),
        ('"a":x', None),
        ('"a', None),
        ("a:b", None),
        ("a:", ("a", "")),
        ("key: v: w", ("key", "v: w")),
        ("[a]: b", None),
        ("plain text", None),
    ],
)
def test_split_key(text, expected):
    assert _split_key(text) == expected


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        (_Scalar("3.12", True, 1), _Scalar("3.12", True, 2), True),
        (_Scalar("3.12", False, 1), _Scalar("3.12", False, 2), True),
        (_Scalar("ubuntu", False, 1), _Scalar("windows", False, 2), False),
        (_Scalar("3.12", True, 1), _Scalar("3.12", False, 2), None),  # a string and a number
        (_Scalar("Linux", False, 1), _Scalar("linux", False, 2), None),  # case
        (_Scalar("true", False, 1), _Scalar("true", True, 2), None),  # a boolean and a string
        (_Scalar("0x10", False, 1), _Scalar("0x10", False, 2), None),  # 16 to YAML 1.2
        (_Scalar("yes", False, 1), _Scalar("yes", False, 2), None),  # true to YAML 1.1
    ],
)
def test_same(a, b, expected):
    if expected is None:
        with pytest.raises(_Unsure):
            _same(a, b)
    else:
        assert _same(a, b) is expected


JOB = "jobs:\n  test:\n    runs-on: ubuntu-latest\n"


@pytest.mark.parametrize(
    ("text", "targets", "refused"),
    [
        # Documents.
        ("--- # a comment\n" + JOB, [("linux-x86_64", CI, 4)], []),
        ("---\n" + JOB + "...\n", [("linux-x86_64", CI, 4)], []),
        ("--- on: push\n" + JOB, [], [(1, "content on the --- line")]),
        (JOB + "---\n" + JOB, [], [(4, "more than one YAML document")]),
        (JOB + "...\nname: x\n", [], [(5, "content after the end of the YAML document")]),
        # The file and its jobs.
        (JOB + "jobs:\n  b:\n    runs-on: windows-latest\n", [], [(4, "jobs is set twice")]),
        ("name: x\n", [], [(None, "no jobs")]),
        ("jobs: {test: {runs-on: ubuntu-latest}}\n", [], [(1, "can't read the jobs")]),
        ("jobs:\n  - test\n", [], [(1, "can't read the jobs")]),
        ("jobs:\n\ttest:\n", [], [(2, "tab in indentation")]),
        (
            JOB + "  test:\n    runs-on: windows-latest\n  other:\n    runs-on: macos-14\n",
            [("macos-arm64", CI, 7)],
            [(2, "job test is defined more than once"), (4, "job test is defined more than once")],
        ),
        # A quoted value over several lines is joined, so it hides nothing; it's only refused where it's read.
        ('name: "a\n  b"\n' + JOB, [("linux-x86_64", CI, 5)], []),
        ("env:\n  A: 'x\n    y'\n" + JOB, [("linux-x86_64", CI, 6)], []),
        (
            'jobs:\n  test:\n    runs-on: "ubuntu-\n      latest"\n',
            [],
            [(3, "job test: runs-on: a quoted value over several lines")],
        ),
        ("name: [a,\n  b\n", [], [(1, "[a, isn't closed before the end of the file")]),
        # A job.
        (
            "jobs:\n  test: *job\n",
            [],
            [(2, "job test: can't read the job (neither its runner nor its Python is read)")],
        ),
        (
            "jobs:\n  test:\n    <<: *defaults\n    runs-on: ubuntu-latest\n",
            [],
            [(2, "job test: can't read the job (neither its runner nor its Python is read)")],
        ),
        ("jobs:\n  test:\n    steps: []\n", [], [(2, "job test: no runs-on line")]),
        ("jobs:\n  call:\n    uses: ./.github/workflows/x.yml\n", [], []),
        ('jobs:\n  test:\n    name: "Build\n      it"\n    runs-on: windows-latest\n', [("win-amd64", CI, 5)], []),
        # Steps.
        (
            JOB + "    steps: [{uses: actions/setup-python@v6}]\n",
            [],
            [
                (
                    4,
                    "job test: steps: [{uses: actions/setup-python@v6}] isn't read "
                    "(neither its runner nor its Python is read)",
                )
            ],
        ),
        (
            JOB + "    steps: *steps\n",
            [],
            [(4, "job test: steps: *steps isn't read (neither its runner nor its Python is read)")],
        ),
        (
            JOB + "    steps:\n      - { uses: actions/setup-python@v6, with: {python-version: '3.13'} }\n",
            [],
            [
                (
                    5,
                    "job test: can't read the step { uses: actions/setup-python@v6, with: {python-version: '3.13'} } "
                    "(neither its runner nor its Python is read)",
                )
            ],
        ),
        (
            JOB + "    steps:\n      - uses: actions/setup-python@v6\n        with: { python-version: '3.13' }\n",
            [],
            [(6, "job test: with: { python-version: '3.13' } isn't read (neither its runner nor its Python is read)")],
        ),
        (
            # The continued quote swallows the next line, as YAML does: setup-python has no python-version.
            JOB + "    steps:\n      - uses: actions/setup-python@v6\n        with:\n"
            '          cache: "pip\n          python-version: 3.9\n          "\n',
            [("linux-x86_64", CI, 3)],
            [(5, "job test: setup-python with no python-version (it reads .python-version, or uses the runner's)")],
        ),
        (
            # The same with a flow list.
            JOB + "    steps:\n      - uses: actions/setup-python@v6\n        with:\n"
            "          x: [a,\n          python-version: 3.9\n          ]\n",
            [("linux-x86_64", CI, 3)],
            [(5, "job test: setup-python with no python-version (it reads .python-version, or uses the runner's)")],
        ),
        # The runner.
        (
            "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n        os: ubuntu-latest\n",
            [],
            [(5, "job test: the matrix has values that aren't a plain list")],
        ),
        (
            "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n        - ubuntu-latest\n",
            [],
            [(5, "job test: can't read the matrix")],
        ),
        (
            "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
            "        os:\n          - ${{ inputs.os }}\n",
            [],
            [(7, "job test: the matrix has a computed value (${{ inputs.os }})")],
        ),
        (
            "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
            "        os: [ubuntu-latest]\n        include: x\n",
            [],
            [(5, "job test: can't read the matrix's include")],
        ),
        (
            "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
            "        os: [ubuntu-latest]\n        include:\n          - os: [windows-latest]\n",
            [],
            [(5, "job test: the matrix's include has values that aren't plain")],
        ),
        (
            "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
            "        os: [ubuntu-latest]\n        include:\n          - os: ${{ inputs.os }}\n",
            [],
            [(8, "job test: the matrix has a computed value (${{ inputs.os }})")],
        ),
        (
            "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
            "        os: [ubuntu-latest]\n        exclude:\n          - arch: x86\n",
            [],
            [(5, "job test: the matrix excludes a key it doesn't have")],
        ),
        (
            "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
            "        os: [ubuntu-latest]\n        exclude:\n          - os: ubuntu-latest\n",
            [],
            [(5, "job test: the matrix is empty (neither its runner nor its Python is read)")],
        ),
        (
            "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
            "        python: ['3.13']\n        include:\n          - os: ubuntu-latest\n            python: '3.12'\n",
            [],
            [(3, "job test: matrix.os isn't set in every matrix combination")],
        ),
        (
            "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
            "        os: [ubuntu-latest]\n        include:\n          - OS: windows-latest\n",
            [],
            [(5, "job test: the matrix has keys that differ only in case (OS, os)")],
        ),
        (
            "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
            "        os: [ubuntu-latest]\n        n: [0x10, 16]\n        exclude:\n          - n: 16\n",
            [],
            [(7, "job test: can't tell how GitHub reads the unquoted 0x10 (quote it)")],
        ),
        (
            "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n"
            "        os: [ubuntu-latest]\n        python: ['3.12', 3.13]\n"
            "        exclude:\n          - python: '3.13'\n",
            [],
            [(9, "job test: can't tell whether GitHub counts '3.13' and '3.13' as the same value")],
        ),
        (
            "jobs:\n  test:\n    runs-on: ${{ matrix['os'] }}\n",
            [],
            [(3, "job test: runs-on: ${{ matrix['os'] }} is an expression")],
        ),
        (
            "jobs:\n  test:\n    runs-on: >-\n      ubuntu-latest\n",
            [],
            [(3, "job test: runs-on: a multi-line value")],
        ),
        (
            "jobs:\n  test:\n    runs-on: 0x10\n",
            [],
            [(3, "job test: can't tell how GitHub reads the unquoted 0x10 (quote it)")],
        ),
    ],
)
def test_refusals(tmp_path, text, targets, refused):
    got_targets, got_refused, _ = read(tmp_path, text)
    assert got_targets == targets
    assert got_refused == refused


def test_exclude_needs_every_value_to_match(tmp_path):
    # The exclude takes out windows with 3.12 only, so windows stays (with 3.13), and so does 3.12 (on ubuntu).
    targets, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, windows-latest]
        python: ["3.12", "3.13"]
        exclude:
          - os: windows-latest
            python: "3.12"
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: ${{ matrix.python }}
""",
    )
    assert targets == [("linux-x86_64", CI, 6), ("win-amd64", CI, 6)]
    assert [f.value for f in found.pythons] == ["3.12", "3.13"]
    assert refused == []


def test_multi_line_flow_lists(tmp_path):
    targets, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [
          ubuntu-latest,  # a comment
          "windows-latest",

          macos-14
        ]
        python: ["3.12",
                 "3.13"]
        include: [
          {os: ubuntu-24.04-arm, python: "3.14t"}]
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: ${{ matrix.python }}
""",
    )
    # A flow collection over several lines is read as one value, from its first line.
    assert refused == []
    assert targets == [
        ("linux-x86_64", CI, 6),
        ("macos-arm64", CI, 6),
        ("win-amd64", CI, 6),
        ("linux-aarch64", CI, 14),
    ]
    assert [(f.value, f.line) for f in found.pythons] == [("3.12", 12), ("3.13", 12), ("3.14t", 14)]
    workflow(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [
          ubuntu-latest,  # a comment
          "windows-latest",

          macos-14
        ]
        python: ["3.12",
                 '3.13']
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: ${{ matrix.python }}
""",
    )
    found = suggest(tmp_path)
    assert where(found.targets) == [("linux-x86_64", CI, 6), ("macos-arm64", CI, 6), ("win-amd64", CI, 6)]
    assert where(found.pythons) == [("3.12", CI, 12), ("3.13", CI, 12)]
    assert found.unreadable == []


def test_block_scalars_are_skipped(tmp_path):
    targets, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - run: >-
          runs-on: windows-latest
            - uses: actions/setup-python@v6
      - name: check
        run: |
          python-version: 3.9
  docs:
    description: |+
      jobs:
        x:
          runs-on: macos-14

    runs-on: windows-latest
  odd:
    steps:
      - |
        - uses: actions/setup-python@v6
    runs-on: macos-14
""",
    )
    assert targets == [("linux-x86_64", CI, 3), ("win-amd64", CI, 17)]
    assert found.pythons == []
    assert refused == [(20, "job odd: can't read the step | (neither its runner nor its Python is read)")]


def test_uses_is_case_insensitive_and_versions_are_strict(tmp_path):
    _, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: Actions/Setup-Python@v6
        with:
          python-version: "3.13"
      - uses: actions/setup-python@v6
        with:
          python-version: "3.14-dev"
      - uses: actions/setup-python
        with:
          python-version: "3.12.4"
""",
    )
    assert where(found.pythons) == [("3.13", CI, 7), ("3.12", CI, 13)]
    assert refused == [(10, "job test: python-version 3.14-dev isn't a CPython version wheelgap can read")]


@pytest.mark.parametrize(
    ("runner", "inputs", "targets", "refused"),
    [
        ("windows-latest", "architecture: x86", [("win32", CI, 3)], []),
        ("windows-2022", "architecture: x64", [("win-amd64", CI, 3)], []),
        ("macos-14", "architecture: x64", [("macos-x86_64", CI, 3)], []),
        ("macos-14", "architecture: arm64", [("macos-arm64", CI, 3)], []),
        ("macos-13", "architecture: x64", [("macos-x86_64", CI, 3)], []),
        ("ubuntu-24.04-arm", "architecture: arm64", [("linux-aarch64", CI, 3)], []),
        ("windows-11-arm", "architecture: arm64", [("win-arm64", CI, 3)], []),
        ("windows-latest", "Architecture: x86", [("win32", CI, 3)], []),  # input names are case-insensitive
        ("ubuntu-latest", "architecture: x64", [("linux-x86_64", CI, 3)], []),
        (
            "windows-latest",
            "architecture: arm64",
            [],
            [(3, "setup-python's architecture: arm64 on windows-latest isn't read")],
        ),
        (
            "windows-11-arm",
            "architecture: x64",
            [],
            [(3, "setup-python's architecture: x64 on windows-11-arm isn't read")],
        ),
        (
            "ubuntu-latest",
            "architecture: x86",
            [],
            [(3, "setup-python's architecture: x86 on ubuntu-latest isn't read")],
        ),
        (
            "windows-latest",
            "architecture: X86",
            [],
            [(3, "setup-python's architecture: X86 on windows-latest isn't read")],
        ),
        (
            "windows-latest",
            "architecture: ${{ matrix.arch }}",
            [],
            [(3, "the job has no strategy.matrix")],
        ),
    ],
)
def test_setup_python_architecture(tmp_path, runner, inputs, targets, refused):
    got_targets, got_refused, found = read(
        tmp_path,
        f"""\
jobs:
  test:
    runs-on: {runner}
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
          {inputs}
""",
    )
    assert got_targets == targets
    assert got_refused == [(line, f"job test: {reason}") for line, reason in refused]
    assert [f.value for f in found.pythons] == ["3.13"]


def test_architecture_on_a_matrix_of_runners(tmp_path):
    targets, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [windows-latest, ubuntu-latest]
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
      - uses: actions/setup-python@v6
        with:
          python-version: "3.12"
          architecture: x86
""",
    )
    # The first setup-python uses the runner's own architecture, the second 32-bit x86, which only Windows has.
    assert targets == [("linux-x86_64", CI, 6), ("win-amd64", CI, 6), ("win32", CI, 6)]
    assert found.targets[2].source == "runs-on: windows-latest (matrix.os, line 3), architecture: x86"
    assert refused == [(6, "job test: setup-python's architecture: x86 on ubuntu-latest isn't read")]


@pytest.mark.parametrize(
    ("inputs", "pythons", "refused"),
    [
        ("freethreaded: true", ["3.13t"], []),
        ("freethreaded: 'True'", ["3.13t"], []),
        ("freethreaded: FALSE", ["3.13"], []),
        ("FreeThreaded: true", ["3.13t"], []),
        ("freethreaded: yes", [], [(8, "freethreaded: yes isn't true or false")]),
        ("freethreaded: ${{ matrix.ft }}", [], [(8, "freethreaded: ${{ matrix.ft }} is an expression")]),
    ],
)
def test_setup_python_freethreaded(tmp_path, inputs, pythons, refused):
    targets, got_refused, found = read(
        tmp_path,
        f"""\
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
          {inputs}
""",
    )
    assert [f.value for f in found.pythons] == pythons
    assert got_refused == [(line, f"job test: {reason}") for line, reason in refused]
    assert targets == [("linux-x86_64", CI, 3)]


def test_freethreaded_needs_313(tmp_path):
    _, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: ${{ matrix.python }}
          freethreaded: true
    strategy:
      matrix:
        python: ["3.12", "3.14", "3.14t"]
""",
    )
    assert [(f.value, f.source) for f in found.pythons] == [
        ("3.14t", "python-version: 3.14 (matrix.python, line 7), freethreaded: true"),
        ("3.14t", "python-version: 3.14t (matrix.python, line 7)"),
    ]
    assert refused == [
        (
            11,
            "job test: python-version: 3.12 (matrix.python, line 7), freethreaded: true asks for a free-threaded 3.12, "
            "but free-threading starts at 3.13",
        )
    ]


def test_setup_uv_python_arch(tmp_path):
    targets, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: windows-latest
    steps:
      - uses: astral-sh/setup-uv@v7
        with:
          python-version: "3.13"
          python-arch: x86
""",
    )
    assert targets == []
    assert refused == [(8, "job test: setup-uv's python-arch: x86 isn't read, so the job's target isn't read")]
    assert [f.value for f in found.pythons] == ["3.13"]


def test_runner_versions_go_in_the_snippet_when_they_agree(tmp_path, capsys):
    workflow(
        tmp_path,
        """\
jobs:
  a:
    runs-on: ubuntu-24.04
  b:
    runs-on: ubuntu-24.04-arm
  c:
    runs-on: macos-14
  d:
    runs-on: macos-14-xlarge
""",
    )
    assert suggest(tmp_path).config() == {
        "targets": ["linux-x86_64", "linux-aarch64", "macos-arm64"],
        "glibc": "2.39",
        "macos": "14.0",
    }
    workflow(tmp_path, "jobs:\n  e:\n    runs-on: ubuntu-22.04\n  f:\n    runs-on: macos-latest\n", name="more.yml")
    found = suggest(tmp_path)
    assert found.config() == {"targets": ["linux-x86_64", "linux-aarch64", "macos-arm64"]}
    assert found.left_out() == [
        "# glibc not set: the linux-* targets don't all name the same one "
        "(2.39 at .github/workflows/ci.yml:3, .github/workflows/ci.yml:5; 2.35 at .github/workflows/more.yml:3)",
        "# macos not set: the macos-* targets don't all name the same one "
        "(14.0 at .github/workflows/ci.yml:7, .github/workflows/ci.yml:9; not named at .github/workflows/more.yml:5)",
    ]
    workflow(tmp_path, "jobs:\n" + "".join(f"  j{n}:\n    runs-on: ubuntu-latest\n" for n in range(4)), name="more.yml")
    assert (
        suggest(tmp_path)
        .left_out()[0]
        .endswith(
            "(2.39 at .github/workflows/ci.yml:3, .github/workflows/ci.yml:5; "
            "not named at .github/workflows/more.yml:3, .github/workflows/more.yml:5 and 2 more)"
        )
    )


def test_dockerfile_sources_and_skipped_folders(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.13-slim\n")
    (tmp_path / "Containerfile").write_text("FROM --platform=linux/arm64 python:3.13-alpine\n")
    (tmp_path / "win.Dockerfile").write_text("FROM python:3.13-windowsservercore\n")
    (tmp_path / "Dockerfile.j2").write_text("FROM {{ base }}\n")
    (tmp_path / "Dockerfile.template").write_text("FROM %%BASE%%\n")
    for skipped in ("tests", "fixtures", "examples", "docs", "target", "build", "dist", ".devcontainer"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "Dockerfile").write_text("FROM python:3.9\n")
    (tmp_path / "venv").mkdir()
    (tmp_path / "venv" / "pyvenv.cfg").write_text("home = /usr\n")
    (tmp_path / "venv" / "Dockerfile").write_text("FROM python:3.9\n")
    found = suggest(tmp_path)
    assert [(f.value, f.path, f.source) for f in found.targets] == [
        ("musl-aarch64", "Containerfile", "FROM --platform=linux/arm64 python:3.13-alpine (musl 1.2)"),
        ("linux-x86_64", "Dockerfile", "FROM python:3.13-slim (no --platform: read as linux/amd64, glibc 2.41)"),
        ("win-amd64", "win.Dockerfile", "FROM python:3.13-windowsservercore"),
    ]
    assert unreadable(found) == [
        ("Dockerfile.j2", None, "a template, filled in before it's built: not read"),
        ("Dockerfile.template", None, "a template, filled in before it's built: not read"),
    ]


@pytest.mark.skipif(not hasattr(os, "geteuid") or os.geteuid() == 0, reason="needs file permissions to apply")
def test_files_and_folders_it_cant_open(tmp_path):
    workflow(tmp_path, JOB)
    workflow(tmp_path, JOB, name="secret.yml")
    (tmp_path / ".github" / "workflows" / "secret.yml").chmod(0)
    (tmp_path / "locked").mkdir()
    (tmp_path / "locked" / "Dockerfile").write_text("FROM python:3.13\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.13\n")
    (tmp_path / "Dockerfile").chmod(0)
    (tmp_path / "locked").chmod(0)
    try:
        found = suggest(tmp_path)
    finally:
        (tmp_path / "locked").chmod(0o755)
        (tmp_path / "Dockerfile").chmod(0o644)
        (tmp_path / ".github" / "workflows" / "secret.yml").chmod(0o644)
    assert where(found.targets) == [("linux-x86_64", CI, 3)]
    assert unreadable(found) == [
        (".github/workflows/secret.yml", None, "couldn't read the file (Permission denied)"),
        ("locked", None, "couldn't open the folder (Permission denied)"),
        ("Dockerfile", None, "couldn't read the file (Permission denied)"),
    ]


def test_the_same_refusal_is_listed_once(tmp_path):
    _, refused, _ = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix: ${{ fromJSON(inputs.matrix) }}
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: ${{ matrix.python }}
""",
    )
    assert refused == [(5, "job test: the matrix is computed (${{ fromJSON(inputs.matrix) }})")]


def test_cli_says_the_snippet_combines_every_job(tmp_path, capsys):
    workflow(tmp_path, JOB)
    code, out, _ = run(capsys, "--suggest-targets", str(tmp_path))
    assert code == 0
    assert "They combine every job, lint,\nrelease and bot jobs too, so keep only the platforms you ship to:" in out


SETUP = "jobs:\n  test:\n    runs-on: windows-latest\n    steps:\n"
WHOLE_JOB = " (neither its runner nor its Python is read)"


@pytest.mark.parametrize(
    ("steps", "refused"),
    [
        # A setup step whose uses can't be read might set the architecture: the job isn't read at all.
        ("      - uses: *sp\n", (5, "can't read the uses: *sp of a step")),
        (
            "      - uses: &sp actions/setup-python@v6\n",
            (5, "can't read the uses: &sp actions/setup-python@v6 of a step"),
        ),
        (
            "      - uses: !!str actions/setup-python@v6\n",
            (5, "can't read the uses: !!str actions/setup-python@v6 of a step"),
        ),
        ("      - uses: >-\n          actions/setup-python@v6\n", (5, "can't read the uses: >- of a step")),
        (
            "      - uses:\n          actions/setup-python@v6\n",
            (5, "can't read the uses: actions/setup-python@v6 of a step"),
        ),
        (
            '      - uses: "actions\\x2fsetup-python@v6"\n',
            (5, 'can\'t read the uses: "actions\\x2fsetup-python@v6" of a step'),
        ),
        ("      - uses: actions/setup-python@v6\n        with: {}\n        with:\n", (7, "with is set twice")),
        (
            "      - uses: actions/setup-python@v6\n        with:\n"
            "          architecture: x64\n          Architecture: x86\n",
            (8, "with: sets architecture twice"),
        ),
        (
            "      - uses: actions/setup-python@v6\n        if: matrix.arch == 'x86'\n        with:\n"
            "          python-version: '3.13'\n          architecture: x86\n",
            (6, "the actions/setup-python step has if: matrix.arch == 'x86', so it may not run"),
        ),
        (
            "      - uses: astral-sh/setup-uv@v7\n        if: runner.os == 'Linux'\n        with:\n"
            "          python-version: '3.13'\n",
            (6, "the astral-sh/setup-uv step has if: runner.os == 'Linux', so it may not run"),
        ),
        ("      - uses: ${{ inputs.action }}\n", (5, "uses: ${{ inputs.action }} is an expression")),
    ],
)
def test_setup_steps_it_cant_read_refuse_the_job(tmp_path, steps, refused):
    targets, got, found = read(tmp_path, SETUP + steps)
    assert targets == []
    assert found.pythons == []
    assert got == [(refused[0], f"job test: {refused[1]}{WHOLE_JOB}")]


def test_step_keys_are_case_sensitive(tmp_path):
    # `Uses:` isn't `uses:` to Actions, so this isn't a setup-python step and its with: changes nothing.
    targets, refused, found = read(
        tmp_path, SETUP + "      - Uses: actions/setup-python@v6\n        with:\n          architecture: x86\n"
    )
    assert targets == [("win-amd64", CI, 3)]
    assert found.pythons == [] and refused == []


def test_a_step_with_if_and_no_interpreter_inputs_is_read(tmp_path):
    targets, refused, found = read(
        tmp_path,
        SETUP + "      - uses: actions/setup-python@v6\n        if: always()\n        with:\n          cache: pip\n",
    )
    assert targets == [("win-amd64", CI, 3)]
    assert refused == [
        (5, "job test: setup-python with no python-version (it reads .python-version, or uses the runner's)")
    ]


@pytest.mark.parametrize(
    ("line", "key"),
    [
        ('          "arch\\x69tecture": x86\n', '"arch\\x69tecture"'),
        ('          "free\\x74hreaded": true\n', '"free\\x74hreaded"'),
        ("          'it''s': x\n", "'it''s'"),
    ],
)
def test_quoted_keys_with_escapes_refuse_the_file(tmp_path, line, key):
    targets, refused, _ = read(tmp_path, SETUP + "      - uses: actions/setup-python@v6\n        with:\n" + line)
    assert targets == []
    assert refused == [(7, f"can't read the escapes in the key {key}")]
    _, refused, _ = read(tmp_path, '"contain\\x65r": x\n' + JOB)
    assert refused == [(1, 'can\'t read the escapes in the key "contain\\x65r"')]


def test_free_threading_starts_at_313(tmp_path):
    _, refused, found = read(
        tmp_path,
        SETUP + "      - uses: actions/setup-python@v6\n        with:\n          python-version: 3.12t\n"
        "      - uses: actions/setup-python@v6\n        with:\n          python-version: 3.13t\n",
    )
    assert [f.value for f in found.pythons] == ["3.13t"]
    assert refused == [
        (7, "job test: python-version: 3.12t asks for a free-threaded 3.12t, but free-threading starts at 3.13")
    ]


def test_freethreaded_is_a_setup_python_input_only(tmp_path):
    _, _, found = read(
        tmp_path,
        SETUP
        + "      - uses: astral-sh/setup-uv@v7\n        with:\n"
        + "          python-version: '3.13'\n          freethreaded: true\n",
    )
    assert [f.value for f in found.pythons] == ["3.13"]


@pytest.mark.parametrize(
    ("header", "refused"),
    [
        ("%YAML 1.2\n---\n", []),
        ("%YAML 1.1\n---\n", [(1, "the %YAML 1.1 directive isn't read (only %YAML 1.2 before the document is)")]),
        (
            "%TAG ! tag:example.com,2000:\n---\n",
            [(1, "the %TAG ! tag:example.com,2000: directive isn't read (only %YAML 1.2 before the document is)")],
        ),
        ("  ---\n", [(1, "can't read ---")]),  # indented: not a document marker
        ("---x\n", [(1, "can't read ---x")]),
        ("----\n", [(1, "can't read ----")]),
        ("? jobs\n", [(1, "can't read ? jobs")]),
    ],
)
def test_directives_and_markers(tmp_path, header, refused):
    targets, got, _ = read(tmp_path, header + JOB)
    assert got == refused
    assert targets == ([] if refused else [("linux-x86_64", CI, header.count("\n") + 3)])


def test_architecture_from_a_literal_matrix(tmp_path):
    # As in numpy's windows.yml: each combination has its own runner and architecture.
    targets, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        include:
          - os: windows-2022
            architecture: x86
          - os: windows-11-arm
            architecture: arm64
          - os: ubuntu-latest
            architecture: x86
          - {os: macos-14, architecture: x64, name: it's}
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
          architecture: ${{ matrix.architecture }}
""",
    )
    assert targets == [("win32", CI, 7), ("win-arm64", CI, 9), ("macos-x86_64", CI, 13)]
    assert found.targets[0].source == "runs-on: windows-2022 (matrix.os, line 3), architecture: x86 (matrix)"
    assert found.targets[2].macos == "14.0"  # the runner's macOS stays with the Intel target
    assert refused == [(11, "job test: setup-python's architecture: x86 on ubuntu-latest isn't read")]


def test_architecture_from_the_matrix_on_a_fixed_runner(tmp_path):
    targets, refused, found = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ubuntu-24.04
    strategy:
      matrix:
        arch: [x64, arm64]
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
          architecture: ${{ matrix.arch }}
""",
    )
    assert targets == [("linux-x86_64", CI, 3)]
    assert found.targets[0].glibc == "2.39"
    assert refused == [(3, "job test: setup-python's architecture: arm64 on ubuntu-24.04 isn't read")]


def test_continued_lines_follow_yaml_quoting_and_comments(tmp_path):
    targets, refused, _ = read(
        tmp_path,
        """\
env:
  A: "x \\" # not a comment
    y"
  B: 'it''s # not a comment
    either'
jobs:
  test:
    runs-on: ${{ matrix.os }}
    strategy:
      matrix:
        os: [ubuntu-latest, # windows-latest,
             macos-14]
""",
    )
    assert refused == []
    assert targets == [("linux-x86_64", CI, 11), ("macos-arm64", CI, 11)]


def test_dockerfile_names(tmp_path):
    for name in ("dockerfile.dev", "Containerfile.prod", "Dockerfile.md", "dockerfile.py", "Dockerfile.dockerignore"):
        (tmp_path / name).write_text("FROM python:3.13-slim\n")
    for skipped in ("test", "example"):
        (tmp_path / skipped).mkdir()
        (tmp_path / skipped / "Dockerfile").write_text("FROM python:3.9\n")
    assert [f.path for f in suggest(tmp_path).targets] == ["Containerfile.prod", "dockerfile.dev"]


def test_values_on_the_next_line(tmp_path):
    # Valid YAML that real workflows use (run:, key:, needs:): the value belongs to its key, never read as a line of
    # its own, and refused where wheelgap would read it.
    targets, refused, _ = read(
        tmp_path,
        """\
jobs:
  test:
    runs-on: ubuntu-latest
    needs:
      [
        lint,
      ]
    steps:
      - run:
          tox -e py -- -v
      - uses: actions/cache@v4
        with:
          key:
            ${{ runner.os }}-x
  other:
    runs-on:
      windows-latest
""",
    )
    assert targets == [("linux-x86_64", CI, 3)]
    assert refused == [(16, "job other: runs-on: a value on the line after the key")]


X86 = (
    "      - uses: actions/setup-python@v6\n        with:\n"
    "          python-version: '3.13'\n          architecture: x86\n"
)


@pytest.mark.parametrize(
    ("before", "label"),
    [
        # A quote after `:` with no space is part of a plain scalar, so it can't swallow the lines after it.
        ("    env: {A: x:'y}\n", "quote after a colon in a flow mapping"),
        ('    env: {A: x:"y, B: z}\n', "double quote after a colon"),
        ("    env:\n      A: x:'y # a comment\n", "quote after a colon in a block mapping"),
        ("    env:\n      A: it's [not a list\n", "quote and bracket inside a plain scalar"),
        # A further line of a plain scalar never starts a quoted value or flow collection of its own.
        ('    steps:\n      - run: echo building\n          "quoted\n', "quote on a continuation line"),
        ("    steps:\n      - run: echo building\n          - 'quoted\n", "- ' on a continuation line"),
        ("    steps:\n      - run: echo building\n          [not, closed\n", "[ on a continuation line"),
        ("    steps:\n      - run: echo building\n          {not: closed\n", "{ on a continuation line"),
    ],
)
def test_quotes_and_brackets_only_open_where_yaml_opens_them(tmp_path, before, label):
    steps = "" if "steps:" in before else "    steps:\n"
    targets, refused, found = read(tmp_path, SETUP.split("    steps:\n")[0] + before + steps + X86)
    assert targets == [("win32", CI, 3)], label
    assert [f.value for f in found.pythons] == ["3.13"]
    assert refused == []


@pytest.mark.parametrize(
    ("text", "flow", "expected"),
    [
        ("'a", False, True),
        ("key: 'a", False, True),
        ("- 'a", False, True),
        ("x:'y", False, False),
        ("it's", False, False),
        ("a 'b", False, False),
        ("a- 'b", False, False),  # a dash inside a word isn't a list item
        ("[a, 'b", True, True),
        ("[a,'b", True, True),
        ("['b", True, True),
        ("{a: 'b", True, True),
        ("[a 'b", True, False),
        ("[a,'b", False, False),  # outside a flow collection a comma is an ordinary character
    ],
)
def test_where_a_quote_starts(text, flow, expected):
    assert _starts(text, text.rindex("'"), flow) is expected


@pytest.mark.parametrize(
    ("line", "refused"),
    [
        ("    env:\n      A: &a |\n        jobs: x\n", (5, "a block scalar with an anchor or tag (&a |) isn't read")),
        (
            "    env:\n      A: !!str >-\n        jobs: x\n",
            (5, "a block scalar with an anchor or tag (!!str >-) isn't read"),
        ),
        (
            "    steps:\n      - &s !!str |\n        x\n",
            (5, "a block scalar with an anchor or tag (&s !!str |) isn't read"),
        ),
        ("    env:\n      A:\n        |\n        x\n", (6, "a block scalar (|) on the line after A: isn't read")),
    ],
)
def test_block_scalars_with_properties_refuse_the_file(tmp_path, line, refused):
    targets, got, _ = read(tmp_path, SETUP.split("    steps:\n")[0] + line)
    assert targets == []
    assert got == [refused]


@pytest.mark.parametrize(
    ("text", "refused"),
    [
        # NBSP isn't whitespace to YAML: it's part of the label, which no runner has.
        (
            "jobs:\n  test:\n    runs-on: windows-latest\u00a0\n",
            [(3, "job test: windows-latest\u00a0 isn't a GitHub-hosted runner wheelgap knows")],
        ),
        (
            "jobs:\n  test:\n    runs-on: windows-latest\u00a0# not a comment\n",
            [(3, "job test: windows-latest\u00a0# not a comment isn't a GitHub-hosted runner wheelgap knows")],
        ),
        (
            "jobs:\n  test:\n    runs-on: ${{\u00a0matrix.os }}\n",
            [(3, "job test: runs-on: ${{\u00a0matrix.os }} is an expression")],
        ),
        # A line starting with NBSP is at column 0: a top-level key, so the job has no runs-on, as YAML reads it.
        ("jobs:\n  test:\n\u00a0   runs-on: windows-latest\n", [(2, "job test: no runs-on line")]),
    ],
)
def test_unicode_spaces_are_ordinary_characters(tmp_path, text, refused):
    targets, got, _ = read(tmp_path, text)
    assert targets == []
    assert got == refused


@pytest.mark.parametrize("brk", ["\r", "\x85", "\u2028", "\u2029"])
def test_other_line_breaks_refuse_the_file(tmp_path, brk):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_bytes(
        ("jobs:\n  test:\n    runs-on: ubuntu-latest # a" + brk + "b\n").encode("utf-8")
    )
    found = suggest(tmp_path)
    assert found.targets == []
    assert unreadable(found) == [(CI, 3, f"a line break YAML may read differently ({brk!r})")]


def test_crlf_and_tab_indented_comments_are_fine(tmp_path):
    (tmp_path / ".github" / "workflows").mkdir(parents=True)
    (tmp_path / ".github" / "workflows" / "ci.yml").write_bytes(
        b"jobs:\r\n\t# a comment\r\n  test:\r\n    runs-on: ubuntu-latest\r\n"
    )
    found = suggest(tmp_path)
    assert where(found.targets) == [("linux-x86_64", CI, 4)]
    assert found.unreadable == []


MATRIX_ARCH = """\
jobs:
  test:
    runs-on: ${{{{ matrix.os }}}}
    strategy:
      matrix:
        include:
{entries}
    steps:
      - uses: actions/setup-python@v6
        with:
          python-version: "3.13"
          architecture: ${{{{ matrix.arch }}}}
"""


def test_a_combination_without_the_architecture_uses_the_runners_own(tmp_path):
    entries = [
        "          - {os: windows-latest, arch: x86}",
        "          - {os: windows-2025}",
        "          - {os: windows-11-arm, arch: ''}",
    ]
    targets, refused, found = read(tmp_path, MATRIX_ARCH.format(entries="\n".join(entries)))
    assert refused == []
    assert targets == [("win32", CI, 7), ("win-amd64", CI, 8), ("win-arm64", CI, 9)]
    # The same matrix in another order gives the same answer, line for line.
    workflow(tmp_path, MATRIX_ARCH.format(entries="\n".join(reversed(entries))))
    again = suggest(tmp_path)
    assert sorted(where(again.targets)) == sorted([("win32", CI, 9), ("win-amd64", CI, 8), ("win-arm64", CI, 7)])


def test_a_refusal_partway_through_the_matrix_keeps_no_targets(tmp_path):
    # The second combination has no os, so runs-on is '' there: nothing from the job is suggested, in either order.
    for entries in (
        ["          - {os: windows-latest}", "          - {arch: x86}"],
        ["          - {arch: x86}", "          - {os: windows-latest}"],
    ):
        targets, refused, _ = read(tmp_path, MATRIX_ARCH.format(entries="\n".join(entries)))
        assert targets == []
        assert refused == [(3, "job test: matrix.os isn't set in every matrix combination")]


@pytest.mark.parametrize(
    ("matrix", "targets", "refused"),
    [
        (
            "        os:\n          [ubuntu-latest, windows-latest]\n",
            [("linux-x86_64", CI, 6), ("win-amd64", CI, 6)],
            [],
        ),
        (
            "        os: [ubuntu-latest]\n        include: ${{ fromJson(inputs.extra) }}\n",
            [],
            [(7, "the matrix's include is computed (${{ fromJson(inputs.extra) }})")],
        ),
        (
            "        os: ['ubuntu-latest,\n          windows-latest']\n",
            [],
            [(6, "can't read ['ubuntu-latest, windows-latest'] (a quoted value over several lines)")],
        ),
        ("        os: [ubuntu-latest,\n          windows-latest]]\n", None, (7, "can't read windows-latest]]")),
        ("        os: ubuntu-latest\n          windows-latest\n", [], [(6, "can't read the value of os")]),
        ("        os:\n", [], [(6, "os has no value")]),
        ("        os:\n          - ubuntu-latest\n          arch: x64\n", [], [(6, "can't read os")]),
        (
            "        include:\n          - os: ubuntu-latest\n            os: windows-latest\n",
            [],
            [(8, "os is set twice")],
        ),
        ("        os: [ubuntu-latest]\n        os: [windows-latest]\n", [], [(7, "os is set twice")]),
        ("        os: {a: b}\n", [], [(5, "the matrix has values that aren't a plain list")]),
        ("        os: [{a : b}]\n", [], [(5, "the matrix has values that aren't a plain list")]),
        ('        os: [{a: "b" c}]\n', [], [(6, 'can\'t read [{a: "b" c}]')]),
    ],
)
def test_matrix_values(tmp_path, matrix, targets, refused):
    text = "jobs:\n  test:\n    runs-on: ${{ matrix.os }}\n    strategy:\n      matrix:\n" + matrix
    got_targets, got_refused, _ = read(tmp_path, text)
    if targets is None:  # the whole file
        assert got_targets == [] and got_refused == [refused]
        return
    assert got_targets == targets
    assert got_refused == [(line, f"job test: {reason}") for line, reason in refused]


def test_a_stray_line_deep_in_a_job_refuses_the_file(tmp_path):
    _, refused, found = read(tmp_path, SETUP + "      - uses: actions/checkout@v5\n        with:\n          ? weird\n")
    assert found.targets == []
    assert refused == [(7, "can't read ? weird")]


def test_python_from_a_matrix_key_some_combinations_lack(tmp_path):
    _, refused, found = read(
        tmp_path,
        "jobs:\n  test:\n    runs-on: ubuntu-latest\n    strategy:\n      matrix:\n        include:\n"
        "          - {python: '3.13'}\n          - {other: x}\n"
        "    steps:\n      - uses: actions/setup-python@v6\n        with:\n"
        "          python-version: ${{ matrix.python }}\n",
    )
    assert found.pythons == []
    assert refused == [(12, "job test: matrix.python isn't set in every matrix combination")]
