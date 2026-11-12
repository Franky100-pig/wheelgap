"""Text from inputs must never run as a GitHub Actions workflow command in a CI log."""

import io
import json
import re
import sys

import pytest

from wheelgap.cli import main
from wheelgap.logsafe import CommandGuard, guarded_output, neutralize, write_command

ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def commands(text):
    """Lines the Actions runner would read as workflow commands: `::` first (after whitespace; colour codes too, to
    be safe) or `##[` anywhere. Lines end at LF, CR or CRLF, as the runner reads them."""
    return [
        line for line in re.split(r"\r\n|\r|\n", text) if ANSI.sub("", line).lstrip().startswith("::") or "##[" in line
    ]


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


TOML_LOCKS = {
    "uv.lock": 'version = 1\n\n[[package]]\nname = {name}\nversion = "1.0"\n'
    'source = {{ registry = "https://pypi.org/simple" }}\n',
    "poetry.lock": '[[package]]\nname = {name}\nversion = "1.0"\nfiles = []\n\n[metadata]\nlock-version = "2.1"\n',
    "pdm.lock": '[metadata]\ngroups = ["default"]\n\n[[package]]\nname = {name}\nversion = "1.0"\nfiles = []\n',
    "pylock.toml": 'lock-version = "1.0"\ncreated-by = "test"\n\n[[packages]]\nname = {name}\nversion = "1.0"\n',
}


@pytest.mark.parametrize("name", ["::warning file=a,line=1::pwned", "x\n::error::pwn", "a ##[error]b", "x_", ""])
@pytest.mark.parametrize("lockfile", [*TOML_LOCKS, "Pipfile.lock"])
def test_invalid_package_names_are_input_errors(capsys, fake_pypi, tmp_path, lockfile, name):
    # pip can't install a name that isn't PEP 508, so it's bad input (exit 2), not a package with a gap.
    if lockfile == "Pipfile.lock":
        text = json.dumps({"default": {name: {"version": "==1.0"}}})
    else:
        text = TOML_LOCKS[lockfile].format(name=json.dumps(name))  # a JSON string is a TOML basic string
    (tmp_path / lockfile).write_text(text, encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path / lockfile), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2
    message = f"{lockfile}: a package has the name {name!r}, which isn't a valid package name (PEP 508)"
    assert neutralize(message) in err
    assert not commands(out + err)
    assert fake_pypi == []


def test_missing_package_name_is_an_input_error(capsys, fake_pypi, tmp_path):
    (tmp_path / "uv.lock").write_text('version = 1\n\n[[package]]\nversion = "1.0"\n', encoding="utf-8")
    code, _, err = run(capsys, str(tmp_path / "uv.lock"), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 2
    assert "uv.lock: a package has no name" in err


VERSION = "1.0\n::error::pwn \r::add-mask::x ##[error]legacy"
UV_LOCK = (
    'version = 1\nrequires-python = ">=3.12"\n\n[[package]]\nname = "demo"\nversion = "0.1.0"\n'
    'source = {{ editable = "." }}\ndependencies = [{{ name = "b" }}]\n\n'
    '[[package]]\nname = "b"\nversion = {version}\nsource = {{ registry = "https://pypi.org/simple" }}\n'
    'sdist = {{ url = "https://files.example/b-1.0.tar.gz", hash = "sha256:00" }}\n'
)


@pytest.mark.parametrize(
    "flags", [[], ["--explain"], ["--all"], ["-q"], ["--markdown", "--explain"], ["--json", "--explain"]]
)
@pytest.mark.parametrize("color", ["always", "never"])
def test_lockfile_text_cant_run_workflow_commands(capsys, fake_pypi, tmp_path, flags, color):
    (tmp_path / "uv.lock").write_text(UV_LOCK.format(version=json.dumps(VERSION)), encoding="utf-8")
    code, out, err = run(capsys, str(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--color", color, *flags)
    assert code == 1
    if "-q" not in flags:  # which shows no versions
        assert "pwn" in out  # still shown, just not as a command
    assert not commands(out + err)
    if "--json" in flags:  # the escapes are JSON's own, so the value is unchanged
        assert json.loads(out)["packages"][0]["version"] == VERSION


def test_diff_cant_run_workflow_commands(capsys, fake_pypi, tmp_path):
    (tmp_path / "old").mkdir()
    wheel = 'wheels = [{ url = "https://files.example/b-1.0-py3-none-any.whl", hash = "sha256:00" }]\n'
    (tmp_path / "old" / "uv.lock").write_text(UV_LOCK.format(version='"1.0"') + wheel, encoding="utf-8")
    (tmp_path / "uv.lock").write_text(UV_LOCK.format(version=json.dumps(VERSION)), encoding="utf-8")
    code, out, err = run(capsys, "diff", str(tmp_path / "old"), str(tmp_path), "-p", "3.13", "-t", "linux-x86_64")
    assert code == 1
    assert "pwn" in out + err
    assert not commands(out + err)


@pytest.mark.parametrize(
    "line,where",
    [
        ("./##[error]path\nnowheels==0.9", "out"),  # a path requirement: shown in the skipped list
        ("-e ./x\n::warning::y", "err"),  # the second line isn't a requirement pip can parse
        ("nowheels[##[error]x]", "err"),  # quoted in the parse error
    ],
)
def test_requirements_text_cant_run_workflow_commands(capsys, fake_pypi, tmp_path, line, where):
    (tmp_path / "requirements.txt").write_text(line + "\n", encoding="utf-8")
    _, out, err = run(capsys, str(tmp_path / "requirements.txt"), "-p", "3.13", "-t", "linux-x86_64")
    assert "u0023" in (out if where == "out" else err) or "u003a" in (out if where == "out" else err)
    assert not commands(out + err)


@pytest.mark.parametrize(
    "line,expected",
    [
        ("::error::x", "\\u003a:error::x"),
        ("  ::error::x", "  \\u003a:error::x"),
        ("\x1b[1m::error::x\x1b[0m", "\x1b[1m\\u003a:error::x\x1b[0m"),
        ("a ##[error]b ##[warning]c", "a \\u0023#[error]b \\u0023#[warning]c"),
        ('  "version": "##[x"', '  "version": "\\u0023#[x"'),  # still valid JSON, with the same value
        # Normal output is never changed.
        ("numpy  2.3.4  ✓  ● source", "numpy  2.3.4  ✓  ● source"),
        ("  via: a → b", "  via: a → b"),
        ("a: b::c # [x]", "a: b::c # [x]"),
        ("", ""),
    ],
)
def test_neutralize(line, expected):
    assert neutralize(line) == expected
    assert not commands(neutralize(line))


def test_command_guard_holds_partial_lines():
    raw = io.StringIO()
    guard = CommandGuard(raw)
    guard.write(":")
    guard.flush()  # a partial line stays held: the rest may make it a command
    assert raw.getvalue() == ""
    guard.write(":error::x\r")  # a CR may be half a CRLF
    guard.write("\n#")
    guard.write("#[warning]y\rok\n::tail")
    assert raw.getvalue() == "\\u003a:error::x\r\n\\u0023#[warning]y\rok\n"
    guard.finish()
    assert raw.getvalue().endswith("ok\n\\u003a:tail")
    assert not commands(raw.getvalue())


def test_guarded_output_restores_streams_and_nests(capsys):
    out, err = sys.stdout, sys.stderr
    with guarded_output():
        guard = sys.stdout
        assert isinstance(guard, CommandGuard) and guard.isatty() == out.isatty()
        with guarded_output():
            assert sys.stdout is guard  # not wrapped twice
        print("::error::x")
        print("partial", end="")
    assert (sys.stdout, sys.stderr) == (out, err)
    assert capsys.readouterr().out == "\\u003a:error::x\npartial"


def test_write_command_passes_the_guard(capsys):
    with guarded_output():
        print("::error::from a lockfile")
        write_command("::error file=uv.lock,line=3::from wheelgap", sys.stdout)
    assert capsys.readouterr().out == "\\u003a:error::from a lockfile\n::error file=uv.lock,line=3::from wheelgap\n"
