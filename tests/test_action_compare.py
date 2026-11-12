"""scripts/action_compare.py: the GitHub Action's compare-to report and PR comment (#201)."""

import html
import importlib.util
import json
import re
from pathlib import Path

import pytest

from wheelgap.check import CELL
from wheelgap.diff import main as diff_main

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "actioncompare"
ARGS = ["-p", "3.13", "-t", "linux-x86_64,musl-aarch64"]


def _load():
    spec = importlib.util.spec_from_file_location("wheelgap_action_compare", ROOT / "scripts" / "action_compare.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ac = _load()
INERT = re.compile(r"^[A-Za-z0-9 \-&#;]*$")


def fixture_diff(fake_pypi, capsys, old="base", new="head"):
    code = diff_main([str(FIXTURES / old / "uv.lock"), str(FIXTURES / new / "uv.lock"), *ARGS, "--json"])
    assert fake_pypi == []  # the Action's test workflow runs these fixtures; nothing is looked up
    return code, json.loads(capsys.readouterr().out)


def test_cells_match_wheelgaps_text_output():
    assert ac.CELLS == {status.value: shown for status, (shown, _) in CELL.items()}


def test_fixtures_report_the_dropped_platform_and_the_new_gap(fake_pypi, capsys):
    code, data = fixture_diff(fake_pypi, capsys)
    assert code == 1
    text = ac.report(data, "", "uv.lock", "1a2b3c4", base_branch=True)
    assert text.splitlines() == [
        "### wheelgap diff",
        "",
        "<p>Compared <code>uv&#46;lock</code> with the base branch (<code>1a2b3c4</code>)."
        " 1 package got worse, 1 new package has gaps.</p>",
        "",
        "<table>",
        "<tr><th>Package</th><th>Version</th><th>Target</th><th>Status</th></tr>",
        "<tr><td><code>armlib</code></td><td><code>1&#46;4&#46;0</code> → <code>1&#46;5&#46;0</code></td>"
        "<td><code>musl-aarch64</code></td><td>✓ → ● source</td></tr>",
        "<tr><td><code>nativeext</code></td><td>(new) <code>0&#46;3</code></td>"
        "<td><code>linux-x86&#95;64</code></td><td>(new) → ● source</td></tr>",
        "<tr><td><code>nativeext</code></td><td>(new) <code>0&#46;3</code></td>"
        "<td><code>musl-aarch64</code></td><td>(new) → ● source</td></tr>",
        "</table>",
    ]


def test_nothing_worse(fake_pypi, capsys):
    code, data = fixture_diff(fake_pypi, capsys, old="head", new="head")
    assert code == 0
    text = ac.report(data, "", "uv.lock", "origin/main")
    assert "<p>Compared <code>uv&#46;lock</code> with <code>origin&#47;main</code>. Nothing got worse.</p>" in text
    assert "<table>" not in text


@pytest.mark.parametrize(
    "text",
    [
        "@octocat",
        "a|b",
        "<script>alert(1)</script>",
        "1.0\n::error::pwn ##[error]x",
        "www.example.com",
        "_emph_ *b* `code` [l](http://x) #12 :smile:",
        "-->",
        "1.0\u202egnp.exe",
    ],
)
def test_untrusted_text_is_inert(text):
    escaped = ac.esc(text)
    assert INERT.match(escaped)
    shown = html.unescape(escaped)
    # Exactly the text, except that control and format characters are shown as \uXXXX.
    assert shown == text.replace("\n", "\\u000a").replace("\u202e", "\\u202e")


def test_long_text_is_cut():
    assert html.unescape(ac.esc("x" * 500)) == "x" * ac.MAX_TEXT + "…"


def test_a_hostile_package_stays_in_its_cell():
    data = {
        "worse": [
            {
                "name": "evil|</td></table>\n\n# hi",
                "old_versions": ["1.0"],
                "new_versions": ["2.0\n\n@octocat"],
                "targets": {"linux-x86_64": {"old_status": "wheel", "new_status": "<b>source"}},
            }
        ],
        "new": [],
        "unchecked": ["@someone"],
    }
    text = ac.report(data, "", "uv.lock", "main")
    table = text[text.index("<table>") : text.index("</table>")]
    assert "" not in table.splitlines()  # a blank line would end the HTML block
    assert table.count("<td>") == 4 and table.count("<tr>") == 2
    assert "@" not in text and "<b>" not in text and "|" not in text and "\n#" not in text


def test_many_rows_stay_under_the_comment_limit():
    data = {
        "worse": [
            {
                "name": f"pkg-{i}-" + "%" * 90,
                "old_versions": ["1.0"],
                "new_versions": ["2.0"],
                "targets": {"linux-x86_64": {"old_status": "wheel", "new_status": "source"}},
            }
            for i in range(300)
        ],
        "new": [],
        "unchecked": [],
    }
    text = ac.report(data, "", "uv.lock", "main")
    assert len(text) < ac.MAX_BODY
    assert re.search(r'<tr><td colspan="4">and \d+ more rows</td></tr>\n</table>', text)


@pytest.mark.parametrize(
    "content, code, says",
    [
        ('{"error": {"message": "can\'t read x:uv.lock from git: <bad>", "type": "GitInputError"}}', 2, "couldn"),
        ("", 2, "exited 2 without a report"),
        ('{"version": "0.9.0", "packages": []}', 1, "exited 1"),  # not a diff report
    ],
)
def test_errors_are_reported_not_guessed(tmp_path, content, code, says):
    path = tmp_path / "diff.json"
    path.write_text(content, encoding="utf-8")
    data, problem = ac.load(str(path), code)
    assert data is None and says in html.unescape(problem)
    assert "<bad>" not in problem
    assert "Nothing got worse" not in ac.report(data, problem, "uv.lock", "main")


def test_marker_names_the_lockfile_and_cant_close_the_comment():
    assert ac.marker("uv.lock") == "<!-- wheelgap-compare lockfile=uv.lock -->"
    assert ac.marker("a--b/uv.lock") == "<!-- wheelgap-compare lockfile=a%2D%2Db/uv.lock -->"
    assert ac.marker('x -->y"\\') == "<!-- wheelgap-compare lockfile=x%20%2D%2D%3Ey%22%5C -->"


def test_main_writes_the_report_and_comment_and_logs_only_counts(fake_pypi, capsys, tmp_path):
    code, data = fixture_diff(fake_pypi, capsys)
    (tmp_path / "diff.json").write_text(json.dumps(data), encoding="utf-8")
    out = {name: tmp_path / name for name in ("report.md", "comment.json", "marker.txt")}
    argv = ["--diff-json", str(tmp_path / "diff.json"), "--exit-code", str(code), "--lockfile", "uv.lock"]
    argv += ["--base", "1a2b3c4", "--base-branch", "--markdown", str(out["report.md"])]
    argv += ["--payload", str(out["comment.json"]), "--marker", str(out["marker.txt"])]
    assert ac.main(argv) == 0
    log = capsys.readouterr().out
    assert log == "wheelgap: 1 package got worse, 1 new package has gaps. The job summary has the details.\n"
    report = out["report.md"].read_text(encoding="utf-8")
    assert "<code>armlib</code>" in report
    marker = out["marker.txt"].read_text(encoding="utf-8")
    assert json.loads(out["comment.json"].read_text(encoding="utf-8")) == {"body": f"{marker}\n{report}"}
