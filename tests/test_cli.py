import json
import sys
import sysconfig

import pytest

from wheelgap import cli
from wheelgap.cli import main
from wheelgap.targets import ALIASES, PLATFORMS


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_gaps_exit_1(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "uvproj"), "-p", "3.13", "-t", "linux-x86_64,musl-aarch64")
    assert code == 1
    assert "fastlib" in out and "● source" in out
    assert "purewheel" not in out  # fine everywhere, so hidden
    assert "have gaps" in out
    assert "skipped 2 non-index packages" in out
    assert "\033[" not in out  # no colour when not a TTY


def test_docker_platform_names_on_cli(capsys, fixtures):
    code, out, err = run(
        capsys,
        str(fixtures / "uvproj"),
        "-p",
        "3.13",
        "-t",
        "linux/amd64,linux/arm64,linux/arm64/v8",
        "--json",
    )
    assert code == 1
    assert not err
    data = json.loads(out)
    assert [target["label"] for target in data["targets"]] == ["linux-x86_64", "linux-aarch64"]


def test_exit_zero_and_all(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "uvproj"), "-p", "3.13", "--all", "--exit-zero")
    assert code == 0
    assert "purewheel" in out and "–" in out


def test_clean_exit_0(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "pylockproj"), "-p", "3.13", "-t", "linux-aarch64")
    assert code == 0
    assert "The 1 package has a wheel for 1 target." in out


def test_quiet_reports_only_the_gap_summary_and_names(capsys, fixtures):
    result = run(capsys, str(fixtures / "armgap"), "-p", "3.13", "-t", "musl-aarch64", "--quiet", "--all")
    assert result == (
        1,
        "2 of 2 packages have gaps: 1 will build from source, 1 can't install at all.\ngaps: armlib, newglibc\n",
        "",
    )


def test_quiet_with_no_gaps_is_silent(capsys, fixtures):
    assert run(capsys, str(fixtures / "pylockproj"), "-p", "3.13", "-t", "linux-aarch64", "-q") == (0, "", "")


def test_quiet_preserves_json_and_exit_status(capsys, fixtures):
    args = (str(fixtures / "armgap"), "-p", "3.13", "-t", "musl-aarch64", "--json")
    assert run(capsys, *args, "--quiet") == run(capsys, *args)


def test_quiet_preserves_list_targets_output(capsys):
    assert run(capsys, "--list-targets", "--quiet") == run(capsys, "--list-targets")


def test_quiet_still_reports_bad_input(capsys, fixtures):
    code, out, err = run(capsys, str(fixtures / "empty"), "--quiet")
    assert code == 2 and not out and "no lockfile found" in err


def test_quiet_preserves_fail_on_missing(capsys, fake_pypi):
    code, out, err = run(capsys, "nowheels", "-p", "3.13", "-t", "linux", "--fail-on", "missing", "--quiet")
    assert code == 0 and not err
    summary, names = out.splitlines()
    assert "will build from source" in summary
    assert names == "gaps: nowheels"


def test_quiet_from_pyproject(capsys, fixtures, tmp_path):
    (tmp_path / "pylock.toml").write_bytes((fixtures / "pylockproj" / "pylock.toml").read_bytes())
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nquiet = true\n", encoding="utf-8")
    assert run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-aarch64") == (0, "", "")


def test_pyproject_quiet_must_be_boolean(capsys, tmp_path):
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nquiet = "true"\n', encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path))
    assert code == 2 and not out and "quiet must be a boolean" in err


@pytest.mark.parametrize("from_config", [False, True])
def test_quiet_conflicts_with_explain(capsys, tmp_path, from_config):
    args = ["--explain"]
    if from_config:
        (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nquiet = true\n", encoding="utf-8")
        args.append(str(tmp_path))
    else:
        args.append("--quiet")
    with pytest.raises(SystemExit) as caught:
        main(args)
    assert caught.value.code == 2
    error = capsys.readouterr().err.splitlines()[-1]
    assert "--quiet" in error and "--explain" in error


def test_json(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "uvproj"), "-p", "3.13,3.14t", "-t", "win-amd64", "--json")
    assert code == 1
    data = json.loads(out)
    assert [t["label"] for t in data["targets"]] == ["3.13 win-amd64", "3.14t win-amd64"]
    fastlib = next(p for p in data["packages"] if p["name"] == "fastlib")
    assert fastlib["results"] == {"3.13 win-amd64": "wheel", "3.14t win-amd64": "source"}
    assert {"name": "gitdep", "reason": "git"} in data["skipped"]
    assert data["summary"]["with_gaps"] >= 1


def test_color_always(capsys, fixtures):
    _, out, _ = run(capsys, str(fixtures / "uvproj"), "-p", "3.13", "--color", "always")
    assert "\033[33m" in out


def test_adhoc_packages(capsys, fake_pypi):
    code, out, _ = run(capsys, "nowheels", "purewheel", "-p", "3.13", "-t", "linux")
    assert code == 1
    assert "nowheels" in out and "0.9" in out


@pytest.mark.parametrize("input_name", ["pipenvproj", "pipenvproj/Pipfile.lock"])
def test_pipfile_lock_json(capsys, fixtures, fake_pypi, input_name):
    code, out, err = run(
        capsys, str(fixtures / input_name), "-p", "3.13", "-t", "linux-x86_64,musl-aarch64,win-amd64", "--json"
    )
    assert code == 1
    assert not err
    data = json.loads(out)
    assert data["sources"] == ["Pipfile.lock"]
    packages = {p["name"]: p for p in data["packages"]}
    assert packages["fastlib"]["version"] == "2.0.0"
    assert packages["fastlib"]["results"] == {"linux-x86_64": "wheel", "musl-aarch64": "source", "win-amd64": "source"}
    assert packages["winonly"]["results"] == {"linux-x86_64": "n/a", "musl-aarch64": "n/a", "win-amd64": "wheel"}
    assert packages["purewheel"]["results"] == {"linux-x86_64": "wheel", "musl-aarch64": "wheel", "win-amd64": "wheel"}
    assert data["skipped"] == [
        {"name": "gitdep", "reason": "git"},
        {"name": "localpkg", "reason": "path"},
        {"name": "archivepkg", "reason": "file"},
        {"name": "editablepkg", "reason": "editable"},
    ]
    assert sorted(fake_pypi) == [
        "https://pypi.org/pypi/fastlib/2.0.0/json",
        "https://pypi.org/pypi/purewheel/3.1/json",
        "https://pypi.org/pypi/winonly/311/json",
    ]


@pytest.mark.parametrize("input_name", ["pdmproj", "pdmproj/pdm.lock"])
def test_pdm_lock_json(capsys, fixtures, fake_pypi, input_name):
    code, out, err = run(capsys, str(fixtures / input_name), "-p", "3.13", "-t", "linux-x86_64,musl-aarch64", "--json")
    assert code == 1
    assert not err
    data = json.loads(out)
    assert data["sources"] == ["pdm.lock"]
    packages = {p["name"]: p for p in data["packages"]}
    assert packages["fastlib"]["results"] == {"linux-x86_64": "source", "musl-aarch64": "wheel"}
    assert "backport" not in packages  # marker excludes it on 3.13
    assert data["skipped"] == [{"name": "localpkg", "reason": "path"}]
    assert not fake_pypi


def test_pdm_lock_entry_without_files_is_looked_up(capsys, fake_pypi, tmp_path):
    (tmp_path / "pdm.lock").write_text(
        '[[package]]\nname = "purewheel"\nversion = "3.1"\n\n'
        '[[package]]\nname = "nowheels"\nversion = "0.9"\nfiles = []\n',
        encoding="utf-8",
    )
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--json")
    packages = {p["name"]: p for p in json.loads(out)["packages"]}
    assert packages["purewheel"]["results"] == {"linux-x86_64": "wheel"}  # filled in from PyPI
    assert packages["nowheels"]["results"] == {"linux-x86_64": "missing"}  # an empty list is taken as recorded
    assert fake_pypi == ["https://pypi.org/pypi/purewheel/3.1/json"]
    assert code == 1


def test_pdm_lock_marker_on_older_python(capsys, fixtures, fake_pypi):
    code, out, err = run(capsys, str(fixtures / "pdmproj"), "-p", "3.12", "-t", "linux-x86_64", "--json")
    assert code == 1
    assert not err
    packages = {p["name"]: p for p in json.loads(out)["packages"]}
    assert packages["backport"]["results"] == {"linux-x86_64": "source"}
    assert not fake_pypi


def test_pipfile_lock_in_current_directory(capsys, fixtures, fake_pypi, monkeypatch):
    monkeypatch.chdir(fixtures / "pipenvproj")
    code, out, err = run(capsys, "-p", "3.13", "-t", "linux-x86_64")
    assert code == 0
    assert not err
    assert "skipped 4 non-index packages" in out
    assert "editablepkg (editable)" in out
    assert "All 2 packages have a wheel for 1 target." in out


def test_malformed_pipfile_lock_exits_2(capsys, tmp_path, fake_pypi):
    path = tmp_path / "Pipfile.lock"
    path.write_text("{invalid", encoding="utf-8")
    code, _, err = run(capsys, str(path))
    assert code == 2
    assert "JSONDecodeError" in err
    assert not fake_pypi


def test_errors_exit_2(capsys, fixtures):
    code, _, err = run(capsys, str(fixtures / "empty"))
    assert code == 2 and "no lockfile found" in err
    code, _, err = run(capsys, str(fixtures / "uvproj"), "-t", "solaris")
    assert code == 2 and "unknown target" in err
    code, _, err = run(capsys, str(fixtures / "uvproj"), "-p", "four")
    assert code == 2


def test_list_targets(capsys):
    code, out, err = run(capsys, "--list-targets")
    assert code == 0 and not err
    lines = out.splitlines()
    assert lines[0].split() == ["name", "aliases", "accepts"]
    alpine = next(line for line in lines if line.startswith("musl-x86_64 "))
    assert "alpine" in alpine and "musl <= 1.2" in alpine
    assert "linux/arm64" in out


def test_list_targets_json_includes_every_platform_and_alias(capsys):
    code, out, err = run(capsys, "--list-targets", "--json")
    assert code == 0 and not err
    rows = json.loads(out)
    assert [row["name"] for row in rows] == list(PLATFORMS)
    assert {alias: row["name"] for row in rows for alias in row["aliases"]} == ALIASES
    by_name = {row["name"]: row for row in rows}
    assert "glibc <= 2.36" in by_name["linux-x86_64"]["accepts"]
    assert "macOS <= 14.0" in by_name["macos-arm64"]["accepts"]
    assert by_name["pyodide"]["accepts"] == "pyodide wasm32, Python 3.11, 3.12, 3.13, 3.14 (not in -t all)"
    assert by_name["linux-s390x"]["accepts"] == "manylinux s390x, glibc <= 2.36 (not in -t all)"
    assert by_name["linux-aarch64"]["accepts"] == "manylinux aarch64, glibc <= 2.36"


def test_list_targets_respects_compatibility_overrides(capsys):
    flags = ("--list-targets", "--glibc", "2.28", "--musl", "1.1", "--macos", "13.0")
    code, out, err = run(capsys, *flags)
    assert code == 0 and not err
    assert "glibc <= 2.28" in out

    code, out, err = run(capsys, *flags, "--json")
    assert code == 0 and not err
    by_name = {row["name"]: row["accepts"] for row in json.loads(out)}
    assert "glibc <= 2.28" in by_name["linux-aarch64"]
    assert "musl <= 1.1" in by_name["musl-aarch64"]
    assert "macOS <= 13.0" in by_name["macos-x86_64"]


def test_mobile_flags_and_config(capsys, fake_pypi, tmp_path, monkeypatch):
    code, out, _ = run(capsys, "--list-targets", "--ios", "16.4", "--android-api", "30", "--json")
    by_name = {row["name"]: row["accepts"] for row in json.loads(out)}
    assert by_name["ios-arm64"] == "ios arm64_iphoneos, iOS <= 16.4 (wheels only; -t mobile, not in -t all)"
    assert by_name["android-x86_64"] == "android x86_64, API level <= 30 (wheels only; -t mobile, not in -t all)"

    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nios = "15.0"\nandroid-api = 28\n')  # an int is fine
    by_name = {row["name"]: row["accepts"] for row in json.loads(run(capsys, "--list-targets", "--json")[1])}
    assert "iOS <= 15.0" in by_name["ios-arm64"] and "API level <= 28" in by_name["android-arm64"]
    code, out, _ = run(capsys, "nowheels==0.9", "purewheel==3.1", "-p", "3.13", "-t", "mobile", "--json")
    data = json.loads(out)
    assert code == 1
    statuses = {p["name"]: set(p["results"].values()) for p in data["packages"]}
    assert statuses == {"nowheels": {"missing"}, "purewheel": {"wheel"}}
    code, _, err = run(capsys, "--list-targets", "--ios", "11.0")
    assert code == 2 and "iOS 11.0 is too old" in err
    code, _, err = run(capsys, "six", "-p", "3.12", "-t", "android")
    assert code == 2 and "need Python 3.13" in err
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nandroid-api = true\n")
    code, _, err = run(capsys, "--list-targets")
    assert code == 2 and 'android-api must be an integer or a string like "24"' in err


def test_list_targets_rejects_invalid_compatibility_version(capsys):
    code, out, err = run(capsys, "--list-targets", "--glibc", "bad")
    assert code == 2 and not out
    assert "can't parse glibc version" in err


def test_defaults_from_pyproject(capsys, fixtures):
    code, out, _ = run(capsys, str(fixtures / "configproj"), "--json")
    assert code == 1
    assert [t["label"] for t in json.loads(out)["targets"]] == ["3.13 win-amd64", "3.14t win-amd64"]


def test_flags_beat_pyproject(capsys, fixtures):
    _, out, _ = run(capsys, str(fixtures / "configproj"), "-p", "3.13", "-t", "linux-x86_64", "--json")
    assert [t["label"] for t in json.loads(out)["targets"]] == ["linux-x86_64"]


def test_bad_pyproject_exit_2(capsys, fixtures, tmp_path):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nglibc = 2.31\n")
    code, _, err = run(capsys, str(tmp_path))
    assert code == 2 and "glibc must be a string" in err


def test_running_python_reports_freethreaded(monkeypatch):
    monkeypatch.setattr(sysconfig, "get_config_var", lambda name: 1 if name == "Py_GIL_DISABLED" else None)
    assert cli.running_python() == f"{sys.version_info.major}.{sys.version_info.minor}t"


@pytest.mark.skipif(not sysconfig.get_config_var("Py_GIL_DISABLED"), reason="needs a free-threaded build")
def test_default_python_on_freethreaded_build(capsys, fixtures):
    code = main([str(fixtures / "pylockproj"), "-t", "linux-aarch64", "--json"])
    assert code in (0, 1)
    assert json.loads(capsys.readouterr().out)["targets"][0]["python"].endswith("t")


def project_with_lock(tmp_path, fixtures):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    return tmp_path


def test_python_version_file_sets_default(capsys, fixtures, tmp_path):
    project = project_with_lock(tmp_path, fixtures)
    (project / ".python-version").write_text("3.14t\n", encoding="utf-8")
    code, out, err = run(capsys, str(project), "-t", "win-amd64", "--json")
    assert code == 1 and not err
    assert [t["python"] for t in json.loads(out)["targets"]] == ["3.14t"]


def test_python_version_file_several_lines_and_patch_versions(capsys, fixtures, tmp_path):
    project = project_with_lock(tmp_path, fixtures)
    (project / ".python-version").write_text("3.13.1\n# both builds\n\n3.14t\n", encoding="utf-8")
    _, out, _ = run(capsys, str(project), "-t", "win-amd64", "--json")
    assert [t["python"] for t in json.loads(out)["targets"]] == ["3.13", "3.14t"]


def test_flags_beat_python_version_file(capsys, fixtures, tmp_path):
    project = project_with_lock(tmp_path, fixtures)
    (project / ".python-version").write_text("3.14t\n", encoding="utf-8")
    _, out, _ = run(capsys, str(project), "-p", "3.12", "-t", "win-amd64", "--json")
    assert [t["python"] for t in json.loads(out)["targets"]] == ["3.12"]


def test_pyproject_beats_python_version_file(capsys, fixtures, tmp_path):
    project = project_with_lock(tmp_path, fixtures)
    (project / ".python-version").write_text("3.14t\n", encoding="utf-8")
    (project / "pyproject.toml").write_text('[tool.wheelgap]\npython = ["3.12"]\n', encoding="utf-8")
    _, out, _ = run(capsys, str(project), "-t", "win-amd64", "--json")
    assert [t["python"] for t in json.loads(out)["targets"]] == ["3.12"]


def test_python_version_file_skips_unusable_lines(capsys, fixtures, tmp_path):
    project = project_with_lock(tmp_path, fixtures)
    (project / ".python-version").write_text("system\npypy@3.10\n3.13\n", encoding="utf-8")
    code, out, err = run(capsys, str(project), "-t", "win-amd64", "--json")
    assert code == 1
    assert "ignoring 'system'" in err and "ignoring 'pypy@3.10'" in err
    assert [t["python"] for t in json.loads(out)["targets"]] == ["3.13"]


def test_python_version_file_with_nothing_usable_uses_running_python(capsys, fixtures, tmp_path):
    project = project_with_lock(tmp_path, fixtures)
    (project / ".python-version").write_text("system\n", encoding="utf-8")
    _, out, _ = run(capsys, str(project), "-t", "win-amd64", "--json")
    assert [t["python"] for t in json.loads(out)["targets"]] == [cli.running_python()]


def test_gap_summary_counts_each_package_once():
    from wheelgap.check import Row, Status
    from wheelgap.cli import Style, gap_summary
    from wheelgap.sources import Package
    from wheelgap.targets import build_targets

    lin, pyo = build_targets(["3.13"], ["linux", "pyodide"])
    a = Row(Package("a", "1"), {lin: Status.SOURCE, pyo: Status.MISSING})
    b = Row(Package("b", "1"), {lin: Status.UNKNOWN, pyo: Status.UNKNOWN})
    c = Row(Package("c", "1"), {lin: Status.SOURCE, pyo: Status.WHEEL})
    assert gap_summary([a, b, c], 3, Style(False)) == (
        "3 of 3 packages have gaps: 1 will build from source, 1 can't install at all, 1 unknown."
    )
    # singular when only one package in total
    assert gap_summary([a], 1, Style(False)) == ("1 of 1 package has gaps: 1 can't install at all.")


def test_quiet_from_pyproject_allows_json_explain(capsys, fixtures, tmp_path):
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nquiet = true\n", encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "musl-aarch64", "--json", "--explain")
    assert code == 1 and not err  # used to exit 2 with a usage error
    assert "rejected" in json.loads(out)["packages"][0]


def test_no_quiet_overrides_pyproject(capsys, fixtures, tmp_path):
    (tmp_path / "uv.lock").write_bytes((fixtures / "armgap" / "uv.lock").read_bytes())
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nquiet = true\n", encoding="utf-8")
    code, out, _ = run(capsys, str(tmp_path), "-p", "3.13", "-t", "musl-aarch64", "--no-quiet", "--explain")
    assert code == 1 and "via: app → armlib" in out and "is a glibc wheel" in out  # full --explain output


def test_quiet_still_says_why_a_package_is_unknown(capsys, fake_pypi):
    code, out, err = run(capsys, "does-not-exist==1.0", "-p", "3.13", "-t", "linux", "-q")
    assert code == 1
    assert "1 unknown" in out
    assert "could not check does-not-exist: not on index" in err


def test_python_version_file_with_a_bom(capsys, fixtures, tmp_path):
    (tmp_path / "uv.lock").write_bytes((fixtures / "uvproj" / "uv.lock").read_bytes())
    (tmp_path / ".python-version").write_text("﻿3.12\n", encoding="utf-8")  # as Notepad saves it
    code, out, err = run(capsys, str(tmp_path), "-t", "win-amd64", "--json")
    assert not err and [t["python"] for t in json.loads(out)["targets"]] == ["3.12"]


def test_list_targets_uses_pyproject_limits(capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text('[tool.wheelgap]\nglibc = "2.17"\n', encoding="utf-8")
    code, out, _ = run(capsys, "--list-targets")
    assert code == 0 and "glibc <= 2.17" in out


def test_simple_index_url_exits_2(capsys, monkeypatch):
    monkeypatch.setenv("WHEELGAP_INDEX_URL", "https://pypi.org/simple")
    code, _, err = run(capsys, "requests==2.32.3", "-t", "linux")
    assert code == 2 and "needs the JSON API" in err


def test_output_is_utf8_on_a_cp1252_pipe(monkeypatch, capsys):
    import io
    import sys as _sys

    raw = io.BytesIO()
    stream = io.TextIOWrapper(raw, encoding="cp1252", errors="strict")
    monkeypatch.setattr(_sys, "stdout", stream)
    assert main(["--list-targets"]) == 0
    stream.flush()
    assert stream.encoding.lower().replace("-", "") == "utf8"


def test_markdown_report(capsys, fake_pypi, monkeypatch):
    monkeypatch.setenv("FORCE_COLOR", "1")  # as in the Action: Markdown must still have no colour codes
    code, out, err = run(
        capsys,
        "fastlib==2.0.0",
        "nowheels==0.9",
        "purewheel==3.1",
        "-p",
        "3.13",
        "-t",
        "linux-x86_64,win-amd64",
        "--markdown",
    )
    assert code == 1 and not err
    lines = out.splitlines()
    assert lines[0] == "### wheelgap: 2 of 3 packages have gaps"
    assert "| package | version | linux-x86\\_64 | win-amd64 |" in lines
    assert "| --- | --- | --- | --- |" in lines
    assert "| fastlib | 2.0.0 | ✓ | ● source |" in lines
    assert "| nowheels | 0.9 | ● source | ● source |" in lines
    assert "purewheel" not in out  # hidden like the text table, unless --all
    assert "2 of 3 packages have gaps: 2 will build from source." in lines
    assert "\033[" not in out and "<details>" not in out


def test_markdown_all_explain_and_quiet(capsys, fake_pypi, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\nquiet = true\n")
    code, out, err = run(
        capsys, "fastlib==2.0.0", "purewheel==3.1", "-p", "3.13", "-t", "win-amd64", "--markdown", "--all", "--explain"
    )
    assert code == 1 and not err  # quiet = true doesn't clash with --explain, as with --json
    assert "| purewheel | 3.1 | ✓ |" in out.splitlines()
    assert "<details><summary>Why (rejected wheels)</summary>" in out
    assert "fastlib-2.0.0-cp313-cp313-manylinux_2_17_x86_64.whl" in out


def test_markdown_clean_run(capsys, fake_pypi):
    code, out, _ = run(
        capsys, "purewheel==3.1", "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--markdown", "--exit-zero"
    )
    assert code == 0
    assert out.splitlines()[0] == "### wheelgap: every package has a wheel for all 2 targets"
    assert "|" not in out


def test_markdown_and_json_conflict(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["six", "--json", "--markdown"])
    assert exc.value.code == 2
    assert "--json and --markdown" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["--list-targets", "--json", "--markdown"])
    assert "--json and --markdown" in capsys.readouterr().err


def test_markdown_escapes_and_fail_on_missing(capsys, fake_pypi, tmp_path):
    req = tmp_path / "req_[a]#b.txt"  # characters every OS allows in a filename
    req.write_text("nowheels==0.9\nhttps://files.example.com/my_pkg-1.0.tar.gz\n")
    code, out, _ = run(capsys, str(req), "-p", "3.13", "-t", "win-amd64", "--markdown", "--fail-on", "missing")
    assert code == 0
    assert "`req_[a]#b.txt`" in out  # paths and URLs go in code spans, shown exactly and never linked
    assert "`https://files.example.com/my_pkg-1.0.tar.gz` (url)" in out
    assert "Source builds are allowed (fail-on missing), so this run passes." in out
    # The Action's summary run adds --exit-zero; the note depends on --fail-on, not on that.
    code, out2, _ = run(capsys, str(req), "-p", "3.13", "-t", "win-amd64", "--markdown", "--exit-zero")
    assert "Source builds are allowed" not in out2
    _, out3, _ = run(
        capsys, str(req), "-p", "3.13", "-t", "win-amd64", "--markdown", "--fail-on", "missing", "--exit-zero"
    )
    assert "Source builds are allowed" in out3


def test_markdown_helpers():
    from wheelgap.cli import _code, _md

    assert _md("./vendor/__x__/ a|b R&amp;D") == "./vendor/\\_\\_x\\_\\_/ a\\|b R\\&amp;D"
    assert _code("a`b") == "``a`b``"
    assert _code("`x`") == "`` `x` ``"
    assert _code("a|b") == "`a|b`"
    assert _code("") == '`""`'


def test_markdown_lookup_errors_and_empty_names(capsys, fake_pypi, tmp_path):
    req = tmp_path / "requirements.txt"
    req.write_text("nosuchpkg==1.0\n-e git+https://x.example/r.git#egg=\n")
    code, out, _ = run(capsys, str(req), "-p", "3.13", "-t", "win-amd64", "--markdown")
    lines = out.splitlines()
    assert any(ln.startswith("Could not check nosuchpkg: `") for ln in lines)
    assert any(
        ln.startswith('Skipped 1 non-index package: `""`') or '`""`' in ln for ln in lines if ln.startswith("Skipped")
    )


# requires-python ==3.12.*, so uv kept only fastlib's cp312 wheel; PyPI (fake_pypi) has a cp313 one.
LOCK_312_ONLY = (
    'version = 1\nrequires-python = "==3.12.*"\n\n'
    '[[package]]\nname = "app"\nversion = "0"\nsource = { editable = "." }\ndependencies = [{ name = "fastlib" }]\n\n'
    '[[package]]\nname = "fastlib"\nversion = "2.0.0"\nsource = { registry = "https://pypi.org/simple" }\n'
    'sdist = { url = "https://files.pythonhosted.org/p/fastlib-2.0.0.tar.gz" }\n'
    'wheels = [{ url = "https://files.pythonhosted.org/p/fastlib-2.0.0-cp312-cp312-manylinux_2_17_x86_64.whl" }]\n'
)


def test_lock_for_another_python_is_rechecked_online(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(LOCK_312_ONLY, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 0, out  # PyPI's cp313 wheel counts; the lock alone would say "source"
    assert "uv.lock is locked for Python ==3.12.*, so it has no wheels for Python 3.13" in err
    assert fake_pypi == ["https://pypi.org/pypi/fastlib/2.0.0/json"]


def test_lock_inside_requires_python_stays_offline(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text(LOCK_312_ONLY, encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path), "-p", "3.12", "-t", "linux-x86_64")
    assert code == 0 and "note:" not in err
    assert fake_pypi == []
