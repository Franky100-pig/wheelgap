"""Keep the documentation site (docs/, built by MkDocs) from drifting away from the code.

These read the Markdown sources directly, so they don't need MkDocs installed.
"""

import importlib.util
import json
import re
from pathlib import Path

import pytest

from wheelgap import cli

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
ANSI = re.compile(r"\x1b\[[0-9;]*m")
FLAG = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")


def headings(path: Path) -> list[str]:
    """The Markdown headings in a file, without the leading #s. Fenced code blocks are left out, so a shell
    comment like '# --json' isn't a heading."""
    out = []
    fenced = False
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith(("```", "~~~")):
            fenced = not fenced
        elif not fenced and (m := re.match(r"#{1,6} (.*)", line)):
            out.append(m.group(1))
    return out


def heading_tokens(page: str) -> set[str]:
    """The `code` spans in a docs page's headings, e.g. {"-p", "--python"} for '### `-p`, `--python` {#python}'."""
    return {token for heading in headings(DOCS / page) for token in re.findall(r"`([^`]+)`", heading)}


def help_flags(capsys, monkeypatch, *argv: str) -> set[str]:
    """The --flags that `wheelgap ARGV --help` prints, unwrapped and without color."""
    monkeypatch.setenv("COLUMNS", "1000")  # argparse wraps at the terminal width, and may break at a hyphen
    monkeypatch.setenv("NO_COLOR", "1")
    monkeypatch.setenv("PYTHON_COLORS", "0")
    with pytest.raises(SystemExit) as exc:
        cli.main([*argv, "--help"])
    assert exc.value.code == 0
    flags = set(FLAG.findall(ANSI.sub("", capsys.readouterr().out)))
    assert "--help" in flags
    return flags


def test_cli_reference_has_a_section_for_every_flag(capsys, monkeypatch):
    """docs/cli.md has a heading naming each flag `wheelgap --help` lists, and no heading for a flag that's gone."""
    flags = help_flags(capsys, monkeypatch)
    documented = {t for t in heading_tokens("cli.md") if t.startswith("--")}
    assert flags - documented == set(), "add a section to docs/cli.md for these flags"
    assert documented - flags == set(), "docs/cli.md has sections for flags wheelgap --help doesn't list"


def test_diff_page_names_every_diff_flag(capsys, monkeypatch):
    """docs/diff.md mentions each flag `wheelgap diff --help` lists."""
    text = (DOCS / "diff.md").read_text(encoding="utf-8")
    missing = {f for f in help_flags(capsys, monkeypatch, "diff") - {"--help"} if f"`{f}" not in text}
    assert missing == set(), "describe these flags in docs/diff.md"


def test_configuration_page_has_a_section_for_every_key():
    """docs/configuration.md has a heading for each [tool.wheelgap] key load_config() accepts, and only those."""
    keys = set(cli.LIST_KEYS) | set(cli.STR_KEYS) | set(cli.BOOL_KEYS)
    documented = heading_tokens("configuration.md")
    assert keys - documented == set(), "add a section to docs/configuration.md for these keys"
    assert documented - keys == set(), "docs/configuration.md has sections for keys [tool.wheelgap] doesn't accept"


@pytest.mark.parametrize("key", sorted({*cli.LIST_KEYS, *cli.STR_KEYS, *cli.BOOL_KEYS}))
def test_every_documented_config_key_is_accepted(tmp_path, key):
    """The key lists the test above checks are the ones load_config() reads (an unknown key is an error)."""
    value = {"android-api": "24", "fail-on": "missing"}.get(key, "x")
    if key in cli.BOOL_KEYS:
        value = True
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f"[tool.wheelgap]\n{json.dumps(key)} = {json.dumps(value)}\n", encoding="utf-8")
    assert key in cli.load_config(pyproject)


def load_schema_hook():
    spec = importlib.util.spec_from_file_location("schema_reference", DOCS / "hooks" / "schema_reference.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_output_page_renders_every_schema_key():
    """docs/output.md gets the --json reference from docs/wheelgap-output.schema.json, with every key in it."""
    hook = load_schema_hook()
    assert hook.PLACEHOLDER in (DOCS / "output.md").read_text(encoding="utf-8")
    table = hook.render()
    schema = json.loads((DOCS / "wheelgap-output.schema.json").read_text(encoding="utf-8"))

    def names(node):
        if isinstance(node, dict):
            yield from node.get("properties", {})
            for value in node.values():
                yield from names(value)
        elif isinstance(node, list):
            for value in node:
                yield from names(value)

    for name in set(names(schema)):
        assert re.search(rf"[`.\]]{re.escape(name)}`", table), name


def github_anchor(heading: str) -> str:
    """The id GitHub gives a Markdown heading: lowercase, punctuation dropped, spaces as hyphens."""
    text = re.sub(r"[^\w\- ]", "", heading.strip().lower())
    return text.replace(" ", "-")


def anchors(path: Path) -> set[str]:
    return {github_anchor(heading) for heading in headings(path)}


def test_readme_keeps_the_anchors_other_files_link_to():
    """llms.txt and others deep-link to README sections on GitHub (and PyPI shows the README): keep them."""
    readme = anchors(ROOT / "README.md")
    linked = set()
    for name in ("llms.txt", "docs/recipes.md", "docs/mcp.md"):
        text = (ROOT / name).read_text(encoding="utf-8")
        linked |= set(re.findall(r"github\.com/wheelgap/wheelgap(?:/blob/main/README\.md)?#([\w-]+)", text))
    assert linked, "expected llms.txt to link to README sections"
    assert linked - readme == set()


def test_llms_txt_links_to_docs_that_exist():
    """llms.txt links to files under docs/ on GitHub, some with an #anchor: keep those paths and headings."""
    text = (ROOT / "llms.txt").read_text(encoding="utf-8")
    links = re.findall(r"github\.com/wheelgap/wheelgap/blob/main/(docs/[\w./-]+?)(?:#([\w-]+))?\)", text)
    assert links
    for path, anchor in links:
        assert (ROOT / path).is_file(), path
        if anchor:
            assert anchor in anchors(ROOT / path), f"{path}#{anchor}"


def test_readme_keeps_the_mcp_registry_marker():
    """The MCP Registry checks that the PyPI package's README names the server."""
    name = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))["name"]
    assert f"<!-- mcp-name: {name} -->" in (ROOT / "README.md").read_text(encoding="utf-8")
