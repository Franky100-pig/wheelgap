"""`--suggest-targets`: the targets and Pythons a project already names in its CI workflows and Dockerfiles.

It only suggests. Workflows are read with a conservative line scan (the standard library has no YAML parser):
anything it can't read for sure is reported as unreadable, never guessed. Dockerfiles go through
`read_dockerfile`, so they're read exactly as `--dockerfile` reads them.
"""

from __future__ import annotations

import itertools
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .dockerfile import DockerfileError, final_from, read_dockerfile
from .targets import DISTROS, PLATFORMS


@dataclass(frozen=True)
class Found:
    value: str  # a target such as linux-aarch64, or a Python such as 3.13
    path: str  # relative to the project folder, with forward slashes
    line: int
    source: str  # what was read there
    glibc: str | None = None  # the libc version the source names, if any
    musl: str | None = None
    macos: str | None = None  # the macOS version a runner label names


@dataclass(frozen=True)
class Unreadable:
    path: str
    line: int | None
    reason: str


# The settings that every source of a target family must agree on before the snippet sets them.
_SHARED = (("glibc", "linux-"), ("musl", "musl-"), ("macos", "macos-"))


@dataclass
class Suggestions:
    targets: list[Found] = field(default_factory=list)
    pythons: list[Found] = field(default_factory=list)
    unreadable: list[Unreadable] = field(default_factory=list)

    def config(self) -> dict[str, object]:
        """The [tool.wheelgap] keys to suggest: each target and Python once; glibc, musl and macos only when every
        source of those targets names the same version."""
        config: dict[str, object] = {}
        if self.pythons:
            config["python"] = sorted({f.value for f in self.pythons}, key=_python_order)
        if self.targets:
            seen = {f.value for f in self.targets}
            config["targets"] = [name for name in PLATFORMS if name in seen]
            for key, prefix in _SHARED:
                versions = {getattr(f, key) for f in self.targets if f.value.startswith(prefix)}
                if len(versions) == 1 and (version := versions.pop()) is not None:
                    config[key] = version
        return config

    def left_out(self) -> list[str]:
        """Why glibc, musl or macos isn't in the snippet although a source names one."""
        config = self.config()
        notes = []
        for key, prefix in _SHARED:
            sources = [f for f in self.targets if f.value.startswith(prefix)]
            if key in config or not any(getattr(f, key) for f in sources):
                continue
            groups: dict[str, list[str]] = {}
            for f in sources:
                groups.setdefault(getattr(f, key) or "not named", []).append(_where(f))
            says = "; ".join(
                f"{version} at {', '.join(places[:2])}{f' and {len(places) - 2} more' * (len(places) > 2)}"
                for version, places in groups.items()
            )
            notes.append(f"# {key} not set: the {prefix}* targets don't all name the same one ({says})")
        return notes


def _python_order(version: str) -> tuple[int, int, str]:
    major, minor = version.rstrip("t").split(".")
    return int(major), int(minor), version


# GitHub-hosted runner labels, as listed (2026-11-12) at
# https://docs.github.com/en/actions/reference/runners/github-hosted-runners
# and, for the larger macOS runners, https://docs.github.com/en/actions/reference/runners/larger-runners
# Labels are case-insensitive. Anything else (self-hosted, larger Linux/Windows runners, which take custom names)
# can't be read.
_RUNNERS = [
    (re.compile(r"ubuntu-\d+\.\d+-arm"), "linux-aarch64"),
    (re.compile(r"ubuntu-(?:latest|slim|\d+\.\d+)"), "linux-x86_64"),
    (re.compile(r"windows-11(?:-vs\d+)?-arm"), "win-arm64"),
    (re.compile(r"windows-(?:latest|\d{4}(?:-vs\d+)?)"), "win-amd64"),
    # macos-latest has been Apple silicon since 2024; -large runners are Intel, -xlarge (and xcode-*) Apple silicon.
    (re.compile(r"macos-(?:latest|\d+)-xlarge|macos-latest|xcode-\d+(?:-xlarge)?"), "macos-arm64"),
    (re.compile(r"macos-(?:latest|\d+)-large|macos-\d+-intel|macos-(?:1[0-3])"), "macos-x86_64"),
    (re.compile(r"macos-(?:1[4-9]|[2-9]\d)"), "macos-arm64"),
]
# The OS a pinned label runs (a -latest label moves, so it names none): Ubuntu's glibc, from the distro table, and
# the macOS version, which is the newest a macOS wheel may need there.
_UBUNTU_RE = re.compile(r"ubuntu-(\d+\.\d+)(?:-arm)?")
_MACOS_RE = re.compile(r"macos-(\d+)(?:-intel|-large|-xlarge)?")


def runner_target(label: str) -> str | None:
    """The target a GitHub-hosted runner label runs on, or None for a label wheelgap doesn't know."""
    for pattern, target in _RUNNERS:
        if pattern.fullmatch(label.lower()):
            return target
    return None


def _runner_os(label: str) -> tuple[str | None, str | None]:
    """(glibc, macOS version) of a pinned runner label, where wheelgap knows it."""
    lower = label.lower()
    if (m := _UBUNTU_RE.fullmatch(lower)) and (distro := DISTROS.get(f"ubuntu-{m.group(1)}")):
        return distro[1], None
    if m := _MACOS_RE.fullmatch(lower):
        return None, f"{m.group(1)}.0"
    return None, None


# actions/setup-python's `architecture` (x86, x64 or arm64; the runner's own when unset:
# https://github.com/actions/setup-python/blob/main/action.yml and src/setup-python.ts) where the result is certain:
# the runner's own architecture, 32-bit Python on x64 Windows, and Intel Python under Rosetta on Apple silicon.
_ARCHITECTURES = {
    ("linux-x86_64", "x64"): "linux-x86_64",
    ("linux-aarch64", "arm64"): "linux-aarch64",
    ("win-amd64", "x64"): "win-amd64",
    ("win-amd64", "x86"): "win32",
    ("win-arm64", "arm64"): "win-arm64",
    ("macos-x86_64", "x64"): "macos-x86_64",
    ("macos-arm64", "arm64"): "macos-arm64",
    ("macos-arm64", "x64"): "macos-x86_64",
}
# core.getBooleanInput, which setup-python reads `freethreaded` with, takes these and nothing else.
_TRUE, _FALSE = ("true", "True", "TRUE"), ("false", "False", "FALSE")

# setup-python and setup-uv versions read for sure: 3.13, 3.13.1, 3.14t. Others (3.x, pypy3.10, >=3.11) aren't.
_PYTHON_RE = re.compile(r"3\.(\d+)(?:\.\d+)?(t?)", re.ASCII)
_PYTHON_ACTIONS = ("actions/setup-python", "astral-sh/setup-uv")
_MATRIX_REF_RE = re.compile(r"\$\{\{\s*matrix\.([A-Za-z_][A-Za-z0-9_-]*)\s*\}\}")
_BLOCK_SCALAR_RE = re.compile(r"[|>][-+0-9]*")
_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", re.ASCII)
# Plain scalars YAML 1.1 or 1.2 reads as something other than the string (0x10 is 16, yes may be true): GitHub's
# reading of them isn't certain, so they're never compared or suggested.
_AMBIGUOUS_RE = re.compile(r"[-+]?0[xo][0-9a-fA-F_]+|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)|(?i:yes|no|on|off|y|n)")
_DOCUMENT_RE = re.compile(r"(---|\.\.\.)(?:[ \t]|$)")

_CONTAINERFILES = ("Dockerfile", "Containerfile")
_TEMPLATE_SUFFIXES = (".template", ".tmpl", ".tpl", ".j2", ".jinja", ".jinja2", ".in")
# Folders whose Dockerfiles are test inputs, examples or build output rather than what the project ships.
_SKIP_DIRS = {"node_modules", "__pycache__", "site-packages", "tests", "fixtures", "examples", "docs"}
_SKIP_DIRS |= {"target", "build", "dist"}


class _Unsure(Exception):
    def __init__(self, reason: str, line: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.line = line


@dataclass
class _Node:
    """A line of a workflow: `key: value`, a list item (`- value`, key None) or text it can't read (is_text)."""

    line: int
    indent: float
    key: str | None
    value: str
    is_text: bool = False
    children: list[_Node] = field(default_factory=list)
    broken: bool = False  # a quoted value over several lines

    def get(self, key: str) -> _Node | None:
        found = [c for c in self.children if c.key == key and not c.is_text]
        if len(found) > 1:
            raise _Unsure(f"{key} is set twice", found[1].line)
        return found[0] if found else None

    def walk(self) -> list[_Node]:
        return [self, *(n for c in self.children for n in c.walk())]


@dataclass(frozen=True)
class _Scalar:
    text: str
    quoted: bool
    line: int

    def typed(self) -> tuple[str, object]:
        """The YAML 1.2 core schema type and value, so `3.10` and `"3.10"` differ as they do to GitHub."""
        if self.quoted:
            return "str", self.text
        if _AMBIGUOUS_RE.fullmatch(self.text):
            raise _Unsure(f"can't tell how GitHub reads the unquoted {self.text} (quote it)", self.line)
        lower = self.text.lower()
        if lower in ("", "~", "null"):
            return "null", None
        if lower in ("true", "false"):
            return "bool", lower == "true"
        if _NUMBER_RE.fullmatch(self.text):
            return "num", float(self.text)
        return "str", self.text

    def plain_text(self) -> str:
        """The text GitHub passes on: an unquoted 3.10 is the number 3.1, so it can't be read as written."""
        kind, value = self.typed()
        if kind == "num" and not self.text.isdigit() and repr(value) != self.text:
            raise _Unsure(f"unquoted {self.text} is the number {value:g} in YAML (quote it)", self.line)
        return self.text


def _strip_comment(text: str) -> str:
    """Drop a trailing `# comment` (a # at the start or after a space, outside quotes)."""
    quote = None
    i = 0
    while i < len(text):
        ch = text[i]
        if quote == '"' and ch == "\\":
            i += 2
            continue
        if quote == "'" and text[i : i + 2] == "''":
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'" and (i == 0 or text[i - 1] in " \t[,:{"):
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
        i += 1
    return text.rstrip()


def _split_key(text: str) -> tuple[str, str] | None:
    """`key: value` -> (key, value), or None when the line isn't a plain mapping key."""
    if not text or text[0] in "[{&*!|>%@`-?":
        return None
    if text[0] in "\"'":
        end = text.find(text[0], 1)
        if end < 0 or not text[end + 1 :].startswith(":"):
            return None
        rest = text[end + 2 :]
        if rest and rest[0] not in " \t":
            return None
        return text[1:end], rest.strip()
    m = re.search(r":(?:[ \t]|$)", text)
    if not m:
        return None
    return text[: m.start()].rstrip(), text[m.end() :].strip()


@dataclass
class _FlowState:
    """Where a quoted value or flow collection is, partway through: the open quote and the bracket depth."""

    quote: str | None = None
    depth: int = 0
    spans_quote: bool = False  # a quoted string continued onto another line

    @property
    def done(self) -> bool:
        return self.quote is None and self.depth == 0


def _scan(text: str, state: _FlowState) -> str:
    """Read on through one line of a quoted value or flow collection; return the line up to any comment."""
    previous = "[" if state.depth else ""  # a quote can start a continued line in a flow collection
    i = 0
    while i < len(text):
        ch = text[i]
        if state.quote == '"':
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                state.quote = None
        elif state.quote == "'":
            if ch == "'":
                if text[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                state.quote = None
        elif ch in "\"'" and (previous in ("", "[", "{", ",", ":")):
            state.quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            return text[:i].rstrip()
        elif ch in "[{":
            state.depth += 1
        elif ch in "]}":
            state.depth -= 1
            if state.depth < 0:
                raise _Unsure(f"can't read {text.strip()}")
        if not ch.isspace():
            previous = ch
        i += 1
    if state.quote is not None:
        state.spans_quote = True
    return text.rstrip()


def _continued(value: str) -> _FlowState | None:
    """The state at the end of the line, when the value is a quoted string or flow collection that doesn't end there."""
    if not value or value[0] not in "\"'[{":
        return None
    state = _FlowState()
    _scan(value, state)
    return None if state.done else state


def _parse_tree(text: str) -> _Node:
    """The workflow as a tree of lines, by indentation. Block scalars (`run: |`) are skipped, and a quoted value or
    flow collection over several lines is joined onto its first line."""
    root = _Node(0, -1, None, "")
    stack = [root]
    lines = [raw.rstrip("\r") for raw in text.split("\n")]
    block_indent: int | None = None  # skipping a block scalar's lines indented more than this
    started = ended = False  # content seen; a `...` document end seen
    number = 0
    while number < len(lines):
        line = lines[number]
        number += 1
        stripped = line.lstrip(" ")
        indent = len(line) - len(stripped)
        if block_indent is not None:
            if not stripped.strip() or indent > block_indent:
                continue
            block_indent = None
        if not stripped.strip() or stripped.startswith("#"):
            continue
        if stripped.startswith("\t") or "\t" in line[:indent]:
            raise _Unsure("tab in indentation", number)
        if indent == 0 and (marker := _DOCUMENT_RE.match(stripped)):
            if _strip_comment(stripped[marker.end() :]):
                raise _Unsure(f"content on the {marker.group(1)} line", number)
            if marker.group(1) == "---" and started:
                raise _Unsure("more than one YAML document", number)
            ended = marker.group(1) == "..."
            continue
        if ended:
            raise _Unsure("content after the end of the YAML document", number)
        started = True
        body = _strip_comment(stripped)
        nodes: list[_Node] = []
        if body == "-" or body.startswith("- "):
            rest = body[1:].lstrip(" ")
            item = _Node(number, indent + 0.5, None, rest)
            nodes.append(item)
            if (pair := _split_key(rest)) is not None:
                item.value = ""
                key_column = indent + len(body) - len(rest)
                nodes.append(_Node(number, key_column, pair[0], pair[1]))
                block_column = key_column
            else:
                block_column = indent
        elif (pair := _split_key(body)) is not None:
            nodes.append(_Node(number, indent, pair[0], pair[1]))
            block_column = indent
        else:
            nodes.append(_Node(number, indent, None, body, is_text=True))
            block_column = indent
        for node in nodes:
            while stack[-1].indent >= node.indent:
                stack.pop()
            stack[-1].children.append(node)
            stack.append(node)
        last = nodes[-1]
        if (state := _continued(last.value)) is not None:
            parts = [last.value]
            while not state.done:
                if number >= len(lines):
                    raise _Unsure(f"{last.value} isn't closed before the end of the file", last.line)
                parts.append(_scan(lines[number].strip(" \t"), state))
                number += 1
            last.value = " ".join(p for p in parts if p)
            last.broken = state.spans_quote or last.value[0] in "\"'"
        elif _BLOCK_SCALAR_RE.fullmatch(last.value):
            block_indent = block_column
    return root


def _unquote(text: str, line: int) -> _Scalar:
    if not text:
        raise _Unsure("no value", line)
    if text[0] in ("'", '"'):
        quote = text[0]
        if len(text) < 2 or text[-1] != quote:
            raise _Unsure(f"can't read {text}", line)
        inner = text[1:-1]
        if quote == '"':
            if "\\" in inner:
                raise _Unsure(f"can't read the escapes in {text}", line)
            if '"' in inner:
                raise _Unsure(f"can't read {text}", line)
        elif "'" in inner.replace("''", ""):
            raise _Unsure(f"can't read {text}", line)
        else:
            inner = inner.replace("''", "'")
        return _Scalar(inner, True, line)
    if text[0] in "[]{}&*!|>%@`" or text.startswith(("- ", "? ")) or text in ("-", "?"):
        raise _Unsure(f"can't read {text}", line)
    return _Scalar(text, False, line)


def _flow_items(text: str, line: int, close: str) -> list[str]:
    """The comma-separated items of `[...]` or `{...}`, holding scalars only."""
    if not text.endswith(close):
        raise _Unsure(f"can't read {text}", line)
    inner = text[1:-1].strip()
    if any(c in inner for c in "[]{}"):
        raise _Unsure(f"can't read {text}", line)
    items: list[str] = []
    current = ""
    quote = None
    for ch in inner:
        if quote:
            current += ch
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
            current += ch
        elif ch == ",":
            items.append(current.strip())
            current = ""
        else:
            current += ch
    if quote:
        raise _Unsure(f"can't read {text}", line)
    if current.strip():
        items.append(current.strip())
    if any(not item for item in items):
        raise _Unsure(f"can't read {text}", line)
    return items


def _flow_list(text: str, line: int) -> list[_Scalar]:
    """`[a, "b", 'c']`."""
    return [_unquote(item, line) for item in _flow_items(text, line, "]")]


def _flow_mapping(text: str, line: int) -> dict[str, _Scalar]:
    """`{ os: macos-latest, python: "3.14" }`."""
    out: dict[str, _Scalar] = {}
    for item in _flow_items(text, line, "}"):
        pair = _split_key(item)
        if pair is None or pair[0] in out:
            raise _Unsure(f"can't read {text}", line)
        out[pair[0]] = _unquote(pair[1], line)
    return out


def _scalar(node: _Node) -> _Scalar:
    if node.broken:
        raise _Unsure(f"{node.key}: a quoted value over several lines", node.line)
    if node.children or node.is_text:
        raise _Unsure(f"can't read the value of {node.key or 'a list item'}", node.line)
    if _BLOCK_SCALAR_RE.fullmatch(node.value):
        raise _Unsure(f"{node.key}: a multi-line value", node.line)
    return _unquote(node.value, node.line)


def _data(node: _Node) -> object:
    """A matrix value: a scalar, a list of scalars, a list of flat mappings or a mapping of those."""
    if node.is_text:
        raise _Unsure(f"can't read {node.value}", node.line)
    if node.value:
        if node.children:
            raise _Unsure(f"can't read the value of {node.key or 'a list item'}", node.line)
        if node.broken:
            raise _Unsure(f"can't read {node.value} (a quoted value over several lines)", node.line)
        if node.value.startswith("["):
            return _flow_list(node.value, node.line)
        if node.value.startswith("{"):
            return _flow_mapping(node.value, node.line)
        return _scalar(node)
    if not node.children:
        raise _Unsure(f"{node.key or 'a list item'} has no value", node.line)
    if all(c.key is None and not c.is_text for c in node.children):
        return [_data(c) for c in node.children]
    if any(c.key is None for c in node.children):
        raise _Unsure(f"can't read {node.key or 'a list item'}", node.line)
    out: dict[str, object] = {}
    for c in node.children:
        if c.key in out:
            raise _Unsure(f"{c.key} is set twice", c.line)
        out[str(c.key)] = _data(c)
    return out


def _same(a: _Scalar, b: _Scalar) -> bool:
    """Equal as GitHub compares matrix values; unsure where type or case would decide it."""
    strict = a.typed() == b.typed()
    if strict != (a.text.lower() == b.text.lower()):
        raise _Unsure(f"can't tell whether GitHub counts {a.text!r} and {b.text!r} as the same value", b.line)
    return strict


def _matrix(job: _Node, where: _Node) -> tuple[list[dict[str, _Scalar]], int]:
    """The job's matrix combinations, worked out as GitHub does, and the matrix's line."""
    strategy = job.get("strategy")
    node = strategy.get("matrix") if strategy is not None else None
    if node is None:
        raise _Unsure("the job has no strategy.matrix", where.line)
    if node.value:
        raise _Unsure(f"the matrix is computed ({node.value})", node.line)
    data = _data(node)
    if not isinstance(data, dict):
        raise _Unsure("can't read the matrix", node.line)

    def scalars(key: str, value: object) -> list[_Scalar]:
        if isinstance(value, _Scalar) and "${{" in value.text:
            raise _Unsure(f"matrix.{key} is computed ({value.text})", value.line)
        if not isinstance(value, list) or not value or not all(isinstance(v, _Scalar) for v in value):
            raise _Unsure("the matrix has values that aren't a plain list", node.line)
        for v in value:
            if "${{" in v.text:
                raise _Unsure(f"the matrix has a computed value ({v.text})", v.line)
        return value

    def entries(key: str) -> list[dict[str, _Scalar]]:
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(e, dict) for e in value):
            raise _Unsure(f"can't read the matrix's {key}", node.line)
        out = []
        for entry in value:
            for v in entry.values():
                if not isinstance(v, _Scalar):
                    raise _Unsure(f"the matrix's {key} has values that aren't plain", node.line)
                if "${{" in v.text:
                    raise _Unsure(f"the matrix has a computed value ({v.text})", v.line)
            out.append(entry)
        return out

    dims = {key: scalars(key, value) for key, value in data.items() if key not in ("include", "exclude")}
    include, exclude = entries("include"), entries("exclude")
    keys = {*dims, *(k for e in include + exclude for k in e)}
    if len({k.lower() for k in keys}) != len(keys):
        raise _Unsure(f"the matrix has keys that differ only in case ({', '.join(sorted(keys))})", node.line)
    if any(k not in dims for e in exclude for k in e):
        raise _Unsure("the matrix excludes a key it doesn't have", node.line)
    # https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/run-job-variations
    # Excludes first (an exclude matches every combination that has all its values); then each include is added to
    # every original combination whose original values it doesn't change (later includes may overwrite what earlier
    # ones added), or else becomes a combination of its own.
    combos: list[dict[str, _Scalar]] = []
    if dims:
        for values in itertools.product(*dims.values()):
            combo = dict(zip(dims, values, strict=True))
            if not any(all(_same(combo[k], v) for k, v in e.items()) for e in exclude):
                combos.append(combo)
    extra = []
    for entry in include:
        matching = [c for c in combos if all(k not in dims or _same(c[k], v) for k, v in entry.items())]
        for c in matching:
            c.update(entry)
        if not matching:
            extra.append(dict(entry))
    return combos + extra, node.line


def _matrix_values(job: _Node, key: str, where: _Node) -> list[_Scalar]:
    combos, line = _matrix(job, where)
    if not combos:
        raise _Unsure("the matrix is empty", line)
    if not all(key in c for c in combos):
        raise _Unsure(f"matrix.{key} isn't set in every matrix combination", where.line)
    values: dict[tuple[int, str], _Scalar] = {}
    for c in combos:
        values.setdefault((c[key].line, c[key].text), c[key])
    return list(values.values())


def _values(job: _Node, node: _Node) -> list[tuple[_Scalar, str]]:
    """The values a `key: literal` or `key: ${{ matrix.KEY }}` takes, each with how it was found."""
    value = _scalar(node)
    if m := _MATRIX_REF_RE.fullmatch(value.text):
        return [
            (v, f"{node.key}: {v.text} (matrix.{m.group(1)}, line {node.line})")
            for v in _matrix_values(job, m.group(1), node)
        ]
    if "${{" in value.text:
        raise _Unsure(f"{node.key}: {value.text} is an expression", node.line)
    return [(value, f"{node.key}: {value.text}")]


def _literal(node: _Node) -> str:
    """A setup input that must be written out: an expression (matrix.* included) can't be read."""
    value = _scalar(node)
    if "${{" in value.text:
        raise _Unsure(f"{node.key}: {value.text} is an expression", node.line)
    return value.text


# An architecture a job's setup step sets: the input's value (None: the runner's own), or why it can't be read.
_Arch = str | None | _Unsure


def _read_job(job: _Node, path: str, out: Suggestions) -> None:
    def unsure(e: _Unsure) -> None:
        out.unreadable.append(Unreadable(path, e.line, f"job {job.key}: {e.reason}"))

    try:
        if job.value or job.get("<<") is not None:
            raise _Unsure("can't read the job", job.line)
        if broken := next((n for n in job.walk() if n.broken), None):
            raise _Unsure(f"a quoted value over several lines ({broken.key}), so the job isn't read", broken.line)
        steps = job.get("steps")
        if steps is not None:
            if steps.value and steps.value != "[]":
                raise _Unsure(f"steps: {steps.value} isn't read", steps.line)
            if odd := next((s for s in steps.children if s.key is not None or s.is_text or s.value), None):
                raise _Unsure(f"can't read the step {odd.value or odd.key}", odd.line)
    except _Unsure as e:
        unsure(_Unsure(e.reason + " (neither its runner nor its Python is read)", e.line))
        return

    archs: list[_Arch] = []
    for step in steps.children if steps is not None else []:
        try:
            _read_step(job, step, path, out, archs)
        except _Unsure as e:
            unsure(e)

    try:
        runs_on = job.get("runs-on")
        if runs_on is None:
            if job.get("uses") is None:  # a reusable workflow call has no runs-on of its own
                raise _Unsure("no runs-on line", job.line)
            return
        if (container := job.get("container")) is not None:
            raise _Unsure("runs in a container, whose platform isn't read", container.line)
        if runs_on.children or runs_on.value.startswith(("[", "{")):
            raise _Unsure(
                f"runs-on {runs_on.value or 'with several labels or a group'} isn't a single label", runs_on.line
            )
        if unread := next((a for a in archs if isinstance(a, _Unsure)), None):
            raise _Unsure(f"{unread.reason}, so the job's target isn't read", unread.line)
        for value, source in _values(job, runs_on):
            try:
                label = value.plain_text()
            except _Unsure as e:
                unsure(e)
                continue
            if (target := runner_target(label)) is None:
                unsure(_Unsure(f"{label} isn't a GitHub-hosted runner wheelgap knows", value.line))
                continue
            glibc, macos = _runner_os(label)
            for arch in dict.fromkeys(archs or [None]):
                if arch is None:
                    out.targets.append(Found(target, path, value.line, source, glibc=glibc, macos=macos))
                elif (mapped := _ARCHITECTURES.get((target, str(arch)))) is None:
                    unsure(_Unsure(f"setup-python's architecture: {arch} on {label} isn't read", value.line))
                else:
                    detail = f"{source}, architecture: {arch}"
                    out.targets.append(Found(mapped, path, value.line, detail, glibc=glibc, macos=macos))
    except _Unsure as e:
        unsure(e)


def _read_step(job: _Node, step: _Node, path: str, out: Suggestions, archs: list[_Arch]) -> None:
    """The Python an actions/setup-python or astral-sh/setup-uv step installs, and the architecture it asks for."""
    uses = step.get("uses")
    if uses is None:
        return
    action = _scalar(uses).text.split("@", 1)[0].lower()
    if action not in _PYTHON_ACTIONS:
        return
    setup_python = action == "actions/setup-python"
    with_ = step.get("with")
    if with_ is not None and (with_.value or any(c.key is None for c in with_.children)):
        archs.append(_Unsure(f"with: {with_.value or '...'} isn't read", with_.line))
        raise _Unsure(f"with: {with_.value or '...'} isn't read", with_.line)
    inputs = {key: with_.get(key) if with_ is not None else None for key in ("architecture", "python-arch")}
    if setup_python and (node := inputs["architecture"]) is not None:
        try:
            archs.append(_literal(node))
        except _Unsure as e:
            archs.append(_Unsure(f"architecture: {node.value} isn't read", e.line))
    elif setup_python:
        archs.append(None)
    elif (node := inputs["python-arch"]) is not None:
        archs.append(_Unsure(f"setup-uv's python-arch: {node.value} isn't read", node.line))

    version = with_.get("python-version") if with_ is not None else None
    if version is None:
        if setup_python:
            file = with_.get("python-version-file") if with_ is not None else None
            if file is not None:
                raise _Unsure(f"setup-python reads {_scalar(file).text}, which isn't read here", file.line)
            raise _Unsure(
                "setup-python with no python-version (it reads .python-version, or uses the runner's)", step.line
            )
        return
    freethreaded = False
    if setup_python and (node := with_.get("freethreaded") if with_ is not None else None) is not None:
        text = _literal(node)
        if text not in _TRUE + _FALSE:
            raise _Unsure(f"freethreaded: {text} isn't true or false", node.line)
        freethreaded = text in _TRUE
    for value, source in _values(job, version):
        try:
            text = value.plain_text()
        except _Unsure as e:
            out.unreadable.append(Unreadable(path, e.line, f"job {job.key}: {e.reason}"))
            continue
        if not (m := _PYTHON_RE.fullmatch(text)):
            reason = f"job {job.key}: python-version {text} isn't a CPython version wheelgap can read"
            out.unreadable.append(Unreadable(path, value.line, reason))
            continue
        python = f"3.{m.group(1)}{m.group(2)}"
        if freethreaded and not python.endswith("t"):
            if int(m.group(1)) < 13:
                reason = f"job {job.key}: freethreaded: true with {text}, which has no free-threaded build"
                out.unreadable.append(Unreadable(path, value.line, reason))
                continue
            python, source = python + "t", f"{source}, freethreaded: true"
        out.pythons.append(Found(python, path, value.line, source))


def read_workflow(path: Path, name: str, out: Suggestions) -> None:
    try:
        text = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError:
        out.unreadable.append(Unreadable(name, None, "not UTF-8"))
        return
    except OSError as e:
        out.unreadable.append(Unreadable(name, None, f"couldn't read the file ({e.strerror or e})"))
        return
    try:
        root = _parse_tree(text)
        outside = [n for top in root.children if top.key != "jobs" for n in top.walk()]
        if broken := next((n for n in outside if n.broken), None):
            raise _Unsure(f"a quoted value over several lines ({broken.key})", broken.line)
        jobs = root.get("jobs")
        if jobs is None:
            raise _Unsure("no jobs")
        if jobs.value or jobs.broken or any(j.key is None for j in jobs.children):
            raise _Unsure("can't read the jobs", jobs.line)
    except _Unsure as e:
        out.unreadable.append(Unreadable(name, e.line, e.reason))
        return
    ids = [j.key for j in jobs.children]
    for job in jobs.children:
        if ids.count(job.key) > 1:
            out.unreadable.append(Unreadable(name, job.line, f"job {job.key} is defined more than once"))
        else:
            _read_job(job, name, out)


def _container_kind(name: str) -> str | None:
    """ "file" for a Dockerfile or Containerfile (Dockerfile, Dockerfile.prod, prod.Dockerfile, also lowercase),
    "template" for one that a tool fills in first (Dockerfile.j2), None for anything else."""
    lower = name.lower()
    if lower.endswith(".dockerignore"):
        return None
    if not (
        name in _CONTAINERFILES
        or lower in ("dockerfile", "containerfile")
        or name.startswith(tuple(f"{base}{sep}" for base in _CONTAINERFILES for sep in ".-_"))
        or lower.endswith((".dockerfile", ".containerfile"))
    ):
        return None
    return "template" if lower.endswith(_TEMPLATE_SUFFIXES) else "file"


def dockerfiles(folder: Path, out: Suggestions) -> list[Path]:
    """Every Dockerfile and Containerfile in the project, outside hidden folders, virtual environments and the
    folders in _SKIP_DIRS. Templates and folders that can't be opened are noted as unreadable."""

    def failed(e: OSError) -> None:
        where = Path(e.filename).relative_to(folder).as_posix() if e.filename else "."
        out.unreadable.append(Unreadable(where, None, f"couldn't open the folder ({e.strerror or e})"))

    found: list[Path] = []
    for top, dirs, files in os.walk(folder, onerror=failed):
        dirs[:] = sorted(
            d
            for d in dirs
            if not d.startswith(".") and d not in _SKIP_DIRS and not (Path(top) / d / "pyvenv.cfg").exists()
        )
        for f in sorted(files):
            kind = _container_kind(f)
            if kind == "template":
                name = (Path(top) / f).relative_to(folder).as_posix()
                out.unreadable.append(Unreadable(name, None, "a template, filled in before it's built: not read"))
            elif kind == "file":
                found.append(Path(top) / f)
    return found


def read_docker(path: Path, name: str, out: Suggestions) -> None:
    try:
        image = read_dockerfile(path)
        base, platform, line = final_from(path.read_bytes().decode("utf-8-sig"))
    except DockerfileError as e:
        out.unreadable.append(Unreadable(name, None, str(e).removeprefix(f"{path}: ")))
        return
    except OSError as e:
        out.unreadable.append(Unreadable(name, None, f"couldn't read the file ({e.strerror or e})"))
        return
    source = f"FROM {f'--platform={platform} ' if platform else ''}{base}"
    notes = []
    if not platform and not image.platform.startswith("win"):
        notes.append("no --platform: read as linux/amd64")
    if image.glibc or image.musl:
        notes.append(f"glibc {image.glibc}" if image.glibc else f"musl {image.musl}")
    detail = source + (f" ({', '.join(notes)})" if notes else "")
    out.targets.append(Found(image.platform, name, line, detail, image.glibc, image.musl))
    out.pythons.append(Found(image.python, name, line, source))


def suggest(folder: Path) -> Suggestions:
    """Read the project's GitHub Actions workflows and Dockerfiles. Raises NotADirectoryError for a missing folder."""
    if not folder.is_dir():
        raise NotADirectoryError(f"{folder}: not a folder")
    out = Suggestions()
    workflows = folder / ".github" / "workflows"
    if workflows.is_dir():
        try:
            paths = sorted(workflows.iterdir())
        except OSError as e:
            paths = []
            out.unreadable.append(
                Unreadable(".github/workflows", None, f"couldn't open the folder ({e.strerror or e})")
            )
        for path in paths:
            if path.suffix in (".yml", ".yaml") and path.is_file():
                read_workflow(path, path.relative_to(folder).as_posix(), out)
    for path in dockerfiles(folder, out):
        read_docker(path, path.relative_to(folder).as_posix(), out)
    out.unreadable = list(dict.fromkeys(out.unreadable))
    return out


def _where(item: Found | Unreadable) -> str:
    return f"{item.path}:{item.line}" if item.line is not None else item.path


def snippet(config: dict[str, object]) -> list[str]:
    # JSON strings and lists of them are TOML too (the values are plain ASCII names).
    return ["[tool.wheelgap]", *(f"{key} = {json.dumps(value)}" for key, value in config.items())]


def render_text(found: Suggestions) -> list[str]:
    lines: list[str] = []
    for title, items in (("Targets", found.targets), ("Pythons", found.pythons)):
        if not items:
            lines.append(f"{title}: none found")
            continue
        lines.append(f"{title}:")
        values = sorted({f.value for f in items}, key=list(PLATFORMS).index if title == "Targets" else _python_order)
        for value in values:
            lines.append(f"  {value}")
            seen = dict.fromkeys((_where(f), f.source) for f in items if f.value == value)
            width = max(len(where) for where, _ in seen)
            lines.extend(f"    {where:<{width}}  {source}" for where, source in seen)
    if found.unreadable:
        lines.append("Couldn't read (not guessed):")
        lines.extend(f"  {_where(u)}  {u.reason}" for u in found.unreadable)
    config = found.config()
    if not config.get("targets"):
        lines += ["", "No targets found, so there's nothing to suggest. Nothing was changed or checked."]
        return lines
    lines += [
        "",
        "Suggested settings for pyproject.toml (nothing was changed or checked). They combine every job, lint,",
        "release and bot jobs too, so keep only the platforms you ship to:",
        "",
    ]
    lines += snippet(config)
    lines += found.left_out()
    return lines


def to_json(found: Suggestions) -> dict[str, object]:
    return {
        "targets": [{"target": f.value, "file": f.path, "line": f.line, "from": f.source} for f in found.targets],
        "python": [{"python": f.value, "file": f.path, "line": f.line, "from": f.source} for f in found.pythons],
        "unreadable": [{"file": u.path, "line": u.line, "reason": u.reason} for u in found.unreadable],
        "config": found.config(),
    }
