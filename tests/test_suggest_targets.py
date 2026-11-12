import json
import tomllib

import pytest

from wheelgap.cli import main
from wheelgap.dockerfile import final_from
from wheelgap.suggest_targets import runner_target, suggest


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def workflow(tmp_path, text, name="ci.yml"):
    folder = tmp_path / ".github" / "workflows"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / name).write_text(text)


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
        # Not known for sure: never guessed.
        ("self-hosted", None),
        ("ubuntu-latest-arm", None),
        ("ubuntu-24.04-16core", None),
        ("windows-11", None),
        ("macos-10.15", None),
        ("xcode-27", None),
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
             windows-latest]
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
        (path, 18, "job multiline: can't read the value of os"),
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
""",
    )
    found = suggest(tmp_path)
    assert found.pythons == []
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
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "Dockerfile").write_text("FROM python:3.9\n")
    found = suggest(tmp_path)
    # The final stage builds on `base`, so the line is base's FROM, where the image is named.
    assert where(found.targets) == [("musl-aarch64", "Dockerfile", 3), ("linux-x86_64", "deploy/Dockerfile.web", 1)]
    assert found.targets[0].source == "FROM --platform=linux/arm64 python:3.12-alpine (musl 1.2)"
    assert where(found.pythons) == [("3.12", "Dockerfile", 3), ("3.13", "deploy/Dockerfile.web", 1)]
    assert found.config() == {
        "python": ["3.12", "3.13"],
        "targets": ["linux-x86_64", "musl-aarch64"],
        "glibc": "2.36",
        "musl": "1.2",
    }


def test_dockerfile_it_cant_read(tmp_path):
    (tmp_path / "Dockerfile").write_text("ARG BASE=python:3.13\nFROM $BASE\n")
    found = suggest(tmp_path)
    assert found.targets == []
    assert unreadable(found) == [("Dockerfile", None, "$BASE: build arguments are not supported, pass -p/-t instead")]


def test_glibc_is_left_out_when_the_sources_disagree(tmp_path, capsys):
    workflow(tmp_path, "jobs:\n  test:\n    runs-on: ubuntu-latest\n")
    (tmp_path / "Dockerfile").write_text("FROM python:3.13-trixie\n")
    found = suggest(tmp_path)
    assert found.config() == {"python": ["3.13"], "targets": ["linux-x86_64"]}  # the runner's glibc isn't read
    code, out, _ = run(capsys, "--suggest-targets", str(tmp_path))
    assert code == 0
    assert "# glibc not set: the linux-* targets don't all name the same one (Dockerfile:1 has 2.41)" in out


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
        "Suggested settings for pyproject.toml (nothing was changed or checked):\n"
        "\n"
        "[tool.wheelgap]\n"
        'python = ["3.13"]\n'
        'targets = ["linux-aarch64", "win-amd64"]\n'
    )
    snippet = out.split("checked):\n\n", 1)[1]
    assert tomllib.loads(snippet) == {
        "tool": {"wheelgap": {"python": ["3.13"], "targets": ["linux-aarch64", "win-amd64"]}}
    }
    code, out, _ = run(capsys, "--suggest-targets", ".", "--json")
    data = json.loads(out)
    assert data["targets"][0] == {
        "target": "linux-aarch64",
        "file": ".github/workflows/test.yaml",
        "line": 6,
        "from": "runs-on: ubuntu-24.04-arm (matrix.os, line 3)",
    }
    assert data["config"] == {"python": ["3.13"], "targets": ["linux-aarch64", "win-amd64"]}
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


def test_final_from_line_numbers():
    text = "# syntax=docker/dockerfile:1\nFROM python:3.12 AS a\nRUN <<EOF\nFROM python:3.9\nEOF\nFROM \\\n  a\n"
    assert final_from(text) == ("python:3.12", None, 2)
    assert final_from("FROM python:3.11\n\nFROM \\\n# comment\n python:3.13-slim\n") == ("python:3.13-slim", None, 3)
