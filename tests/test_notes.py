"""--explain shows the coverage tracker's notes for a package with a gap (wheelgap.notes), from the fake tracker in
conftest.py: never the network."""

import json
import threading
import time
import urllib.error

import pytest

from wheelgap import notes
from wheelgap.cli import main

PYPI = "https://pypi.org/simple"
NOTE = {
    "platforms": ["linux-x86_64"],
    "kind": "alternative",
    "text": "pkg has no Linux wheels; pkg-binary does: `pip install pkg-binary`.",
    "links": ["https://example.org/pkg#install"],
    "versions": None,
    "updated": "2026-11-11",
}
HEAD = "pkg 1.0: note from the wheelgap tracker for"


def note(**changes) -> dict:
    return {**NOTE, **changes}


def uv_lock(tmp_path, *packages: tuple[str, str, str]) -> str:
    """A uv.lock of sdist-only packages (a gap on every target), each (name, version, registry)."""
    text = 'version = 1\nrequires-python = ">=3.13"\n'
    for name, version, registry in packages or [("pkg", "1.0", PYPI)]:
        text += (
            f'\n[[package]]\nname = "{name}"\nversion = "{version}"\nsource = {{ registry = "{registry}" }}\n'
            f'sdist = {{ url = "https://files.example/{name}-{version}.tar.gz", hash = "sha256:00", size = 1 }}\n'
        )
    (tmp_path / "uv.lock").write_text(text, encoding="utf-8")
    return str(tmp_path)


def run(capsys, *argv):
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def explain_json(capsys, *argv) -> dict:
    code, out, err = run(capsys, *argv, "--json", "--explain")
    assert code == 1 and not err
    return {p["name"]: p for p in json.loads(out)["packages"]}


def test_note_shown_for_a_matching_target_and_not_another(capsys, tmp_path, tracker):
    tracker.add("pkg", note(), note(platforms=["macos-arm64"], text="Only about macOS."))
    folder = uv_lock(tmp_path)
    code, out, err = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64,win-amd64", "--explain")
    assert code == 1 and not err
    # Under the explanation, once per package, with the gap targets it covers, its text as is, and its links.
    assert out.index("pkg 1.0 on win-amd64:") < out.index(HEAD)
    assert f"{HEAD} linux-x86_64 (alternative)\n  - {NOTE['text']}\n    https://example.org/pkg#install\n" in out
    assert out.count(NOTE["text"]) == 1
    assert "Only about macOS." not in out
    assert tracker.calls == [f"{notes.TRACKER}/pkg.json"]

    pkg = explain_json(capsys, folder, "-p", "3.13", "-t", "linux-x86_64,win-amd64")["pkg"]
    assert pkg["notes"] == [{**NOTE, "targets": ["linux-x86_64"]}]


def test_note_for_another_platform_only_shows_nothing(capsys, tmp_path, tracker):
    tracker.add("pkg", note(platforms=["macos-arm64"]))
    code, out, _ = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain")
    assert code == 1 and HEAD not in out
    assert explain_json(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64")["pkg"]["notes"] == []


def test_all_platforms_note(capsys, tmp_path, tracker):
    tracker.add("pkg", note(platforms=["all"]))
    folder = uv_lock(tmp_path)
    code, out, _ = run(capsys, folder, "-p", "3.13,3.14t", "-t", "linux-x86_64,win-amd64", "--explain")
    assert f"{HEAD} linux-x86_64, win-amd64 (alternative)" in out
    pkg = explain_json(capsys, folder, "-p", "3.13,3.14t", "-t", "linux-x86_64,win-amd64")["pkg"]
    assert pkg["notes"][0]["targets"] == [
        "3.13 linux-x86_64",
        "3.13 win-amd64",
        "3.14t linux-x86_64",
        "3.14t win-amd64",
    ]


@pytest.mark.parametrize(
    "checked,versions,shown",
    [
        ("1.0", None, True),  # no versions: every release
        ("1.0", ">=1", True),
        ("2.9.1", ">=2.8, <2.10", True),
        ("0.9", ">=1", False),  # the tracker scanned 1.0, but you pinned an older release it doesn't cover
        ("0.9", "<1", True),
        ("1.0", "==1.*", True),
        ("1.0rc1", "<2", False),  # the tracker can't match a pre-release, so neither does wheelgap
        ("1.0.post1", ">=1", False),
        ("1.0+local", ">=1", False),
        ("1.0", "not a specifier", False),
    ],
)
def test_versions(capsys, tmp_path, tracker, checked, versions, shown):
    tracker.add("pkg", note(versions=versions), version="1.0")
    data = explain_json(capsys, uv_lock(tmp_path, ("pkg", checked, PYPI)), "-p", "3.13", "-t", "linux-x86_64")
    assert bool(data["pkg"]["notes"]) is shown


def test_versions_need_a_known_version():
    (parsed,) = notes.parse({"schema": 1, "name": "pkg", "notes": [note(versions="<2")]}, "pkg")
    assert parsed.applies_to("1.0") and not parsed.applies_to(None)


PDM_LOCK = """[metadata]
groups = ["default"]

[[package]]
name = "pkg"
version = "1.0"
files = [{file = "pkg-1.0.tar.gz", hash = "sha256:00"}]
"""


@pytest.mark.parametrize(
    "name,text",
    [
        ("uv.lock", f'version = 1\n[[package]]\nname = "pkg"\nversion = "1.0"\nsource = {{ registry = "{PYPI}" }}\n'),
        ("pylock.toml", f'lock-version = "1.0"\n[[packages]]\nname = "pkg"\nversion = "1.0"\nindex = "{PYPI}/"\n'),
    ],
)
def test_looked_up_for_a_lock_that_says_pypi(capsys, tmp_path, tracker, name, text):
    sdist = 'sdist = { url = "https://files.example/pkg-1.0.tar.gz", hashes = { sha256 = "00" } }\n'
    if name == "uv.lock":
        sdist = 'sdist = { url = "https://files.example/pkg-1.0.tar.gz", hash = "sha256:00", size = 1 }\n'
    (tmp_path / name).write_text(text + sdist, encoding="utf-8")
    tracker.add("pkg", note())
    assert explain_json(capsys, str(tmp_path / name), "-p", "3.13", "-t", "linux-x86_64")["pkg"]["notes"]


def test_looked_up_for_a_name_wheelgap_found_on_pypi(capsys, tmp_path, tracker, fake_pypi):
    (tmp_path / "requirements.txt").write_text("nowheels==0.9\n", encoding="utf-8")
    tracker.add("nowheels", note())
    data = explain_json(capsys, str(tmp_path / "requirements.txt"), "-p", "3.13", "-t", "linux-x86_64")
    assert data["nowheels"]["notes"] and fake_pypi and tracker.calls


@pytest.mark.parametrize(
    "name,text",
    [
        # A private index: the name may be internal.
        ("uv.lock", None),
        # pdm.lock records no index, and neither does a pylock.toml entry without one.
        ("pdm.lock", PDM_LOCK),
        (
            "pylock.toml",
            'lock-version = "1.0"\n[[packages]]\nname = "pkg"\nversion = "1.0"\n'
            'sdist = { url = "https://files.example/pkg-1.0.tar.gz", hashes = { sha256 = "00" } }\n',
        ),
        (
            "pylock.toml",
            'lock-version = "1.0"\n[[packages]]\nname = "pkg"\nversion = "1.0"\nindex = "https://private.example/simple"\n'
            'sdist = { url = "https://files.example/pkg-1.0.tar.gz", hashes = { sha256 = "00" } }\n',
        ),
    ],
)
def test_nothing_fetched_for_a_package_not_known_to_be_on_pypi(capsys, tmp_path, tracker, name, text):
    if text is None:
        uv_lock(tmp_path, ("pkg", "1.0", "https://private.example/simple"))
    else:
        (tmp_path / name).write_text(text, encoding="utf-8")
    tracker.add("pkg", note())
    code, out, err = run(capsys, str(tmp_path / name), "-p", "3.13", "-t", "linux-x86_64", "--explain")
    assert code == 1 and "pkg 1.0 on linux-x86_64" in out and HEAD not in out
    assert tracker.calls == []


def test_nothing_fetched_for_a_pdm_entry_wheelgap_looked_up(capsys, tmp_path, tracker, fake_pypi):
    """Even when wheelgap had to look a pdm.lock entry up on PyPI, its lock doesn't say it came from there."""
    (tmp_path / "pdm.lock").write_text(
        '[metadata]\ngroups = ["default"]\n\n[[package]]\nname = "nowheels"\nversion = "0.9"\n', encoding="utf-8"
    )
    tracker.add("nowheels", note())
    code, out, _ = run(capsys, str(tmp_path / "pdm.lock"), "-p", "3.13", "-t", "linux-x86_64", "--explain")
    assert code == 1 and fake_pypi and "nowheels 0.9 on linux-x86_64" in out
    assert tracker.calls == []


@pytest.mark.parametrize(
    "flags",
    [
        ["--no-notes"],
        ["--offline"],
        ["--index-url", "https://private.example/simple"],
        [],  # no --explain
    ],
)
def test_nothing_fetched_with(capsys, tmp_path, tracker, flags):
    tracker.add("pkg", note())
    explain = [] if not flags else ["--explain"]
    code, out, err = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", *explain, *flags)
    assert code == 1 and HEAD not in out
    assert tracker.calls == []


def test_offline_in_config_implies_no_notes(capsys, tmp_path, tracker):
    tracker.add("pkg", note())
    (tmp_path / "pyproject.toml").write_text("[tool.wheelgap]\noffline = true\n", encoding="utf-8")
    run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain")
    assert tracker.calls == []


@pytest.mark.parametrize(
    "respond",
    [
        None,  # a 404: not in the tracker's top 5,000
        lambda name: (_ for _ in ()).throw(urllib.error.HTTPError("u", 500, "Server Error", {}, None)),
        lambda name: (_ for _ in ()).throw(ValueError("not JSON")),
        lambda name: {"schema": 2, "name": name, "notes": [NOTE]},  # a format this version doesn't read
        lambda name: {"schema": 1, "name": "other", "notes": [NOTE]},
        lambda name: ["not", "an", "object"],
    ],
)
def test_failed_lookup_prints_nothing_extra(capsys, tmp_path, tracker, respond):
    folder = uv_lock(tmp_path)
    expected = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--no-notes")
    tracker.respond = respond
    assert run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain") == expected
    assert expected[0] == 1 and not expected[2]
    assert len(tracker.calls) == 1


@pytest.mark.parametrize(
    "bad",
    [
        {"text": "red \x1b[31mtext"},
        {"text": "two\nlines"},
        {"text": "x" * 500},
        {"text": " "},
        {"links": ["http://example.org/plain-http"]},
        {"links": ["https://user@example.org/"]},
        {"links": ["https://example.org/a b"]},
        {"links": []},
        {"platforms": ["all", "linux-x86_64"]},
        {"platforms": "linux-x86_64"},
        {"kind": "Alternative!"},
        {"updated": "yesterday"},
        {"versions": 3},
        {"extra": "key"},
    ],
)
def test_malformed_note_is_dropped(capsys, tmp_path, tracker, bad):
    tracker.add("pkg", note(**bad), note(text="The good one."))
    pkg = explain_json(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64")["pkg"]
    assert [n["text"] for n in pkg["notes"]] == ["The good one."]


def test_markdown_note_cannot_inject_markdown_or_html(capsys, tmp_path, tracker):
    text = "```` </details><script>alert(1)</script> [click](https://evil.example) **bold** <!-- @mention"
    tracker.add("pkg", note(text=text))
    code, out, _ = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--markdown")
    assert code == 1
    lines = out.splitlines()
    fences = [i for i, line in enumerate(lines) if line and set(line) == {"`"}]
    assert len(fences) == 2 and len(lines[fences[0]]) == 5 == len(lines[fences[1]])  # longer than the note's ````
    (at,) = [i for i, line in enumerate(lines) if text in line]
    assert fences[0] < at < fences[1]  # inside the code block, where Markdown and HTML are shown as text
    assert lines[at] == f"  - {text}"  # and as written: nothing to escape there
    assert "<script>" not in "\n".join(lines[: fences[0]] + lines[fences[1] :])


def test_markdown_fence_stays_three_backticks_without_notes(capsys, tmp_path):
    code, out, _ = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain", "--markdown")
    assert out.count("\n```\n") == 2


def test_text_output_escapes_nothing(capsys, tmp_path, tracker):
    text = "Use `pkg[binary]` *not* <pkg> & _pkg_ \\ | #1"
    tracker.add("pkg", note(text=text))
    _, out, _ = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain")
    assert f"  - {text}\n" in out


def test_one_short_attempt_per_request(capsys, tmp_path, tracker):
    folder = uv_lock(tmp_path, ("pkg", "1.0", PYPI), ("other", "2.0", PYPI))
    run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain")
    assert sorted(tracker.calls) == [f"{notes.TRACKER}/other.json", f"{notes.TRACKER}/pkg.json"]
    assert all(attempts == 1 and timeout <= 10 for timeout, attempts in tracker.options)


def _fail(name):
    raise urllib.error.URLError("connection refused")


CUT = "wheelgap: note: couldn't reach wheelgap.github.io in time"


def test_stops_after_the_first_connection_failure(capsys, tmp_path, tracker, monkeypatch):
    monkeypatch.setattr(notes, "WORKERS", 1)
    tracker.respond = _fail
    folder = uv_lock(tmp_path, ("a", "1.0", PYPI), ("b", "1.0", PYPI), ("c", "1.0", PYPI))
    code, out, err = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain")
    assert code == 1 and len(tracker.calls) == 1
    assert err.count(CUT) == 1 and len(err.splitlines()) == 1
    tracker.calls.clear()
    code, out, err = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--json", "-q")
    assert code == 1 and json.loads(out) and err == ""  # -q drops the note


def test_deadline_for_all_lookups(capsys, tmp_path, tracker, monkeypatch):
    monkeypatch.setattr(notes, "DEADLINE", 0.2)
    release = threading.Event()
    tracker.respond = lambda name: release.wait(10) and {"schema": 1, "name": name, "notes": [NOTE]}
    try:
        start = time.monotonic()
        code, out, err = run(capsys, uv_lock(tmp_path), "-p", "3.13", "-t", "linux-x86_64", "--explain")
        assert time.monotonic() - start < 5
        assert code == 1 and HEAD not in out and CUT in err
    finally:
        release.set()


def test_cached_for_a_day(capsys, tmp_path, tracker):
    tracker.add("pkg", note())
    folder = uv_lock(tmp_path, ("pkg", "1.0", PYPI), ("gone", "1.0", PYPI))  # gone: a 404, cached as no notes
    for _ in range(2):
        _, out, _ = run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain")
        assert HEAD in out
    assert len(tracker.calls) == 2
    run(capsys, folder, "-p", "3.13", "-t", "linux-x86_64", "--explain", "--no-cache")
    assert len(tracker.calls) == 4
