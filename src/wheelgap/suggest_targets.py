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
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import NoReturn

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
_MATRIX_REF_RE = re.compile(r"\$\{\{[ \t]*matrix\.([A-Za-z_][A-Za-z0-9_-]*)[ \t]*\}\}")
_WS = " \t"  # YAML's whitespace: never NBSP or other Unicode spaces, which are ordinary characters to it
# A block scalar header with an anchor or tag (`&a |`, `!!str >-`): where its lines end isn't read, so it's refused.
_PROPERTY_BLOCK_RE = re.compile(r"(?:[&!][^ \t]*[ \t]+)+[|>][-+0-9]*")
# Line breaks YAML 1.1 or 1.2 knows besides \n (\r\n is fine): the lines they make aren't the ones split on here.
_ODD_BREAK_RE = re.compile("\r(?!\n)|[\x85\u2028\u2029]")
_BLOCK_SCALAR_RE = re.compile(r"[|>][-+0-9]*")
_NUMBER_RE = re.compile(r"[-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?", re.ASCII)
# Plain scalars YAML 1.1 or 1.2 reads as something other than the string (0x10 is 16, yes may be true): GitHub's
# reading of them isn't certain, so they're never compared or suggested.
_AMBIGUOUS_RE = re.compile(r"[-+]?0[xo][0-9a-fA-F_]+|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN)|(?i:yes|no|on|off|y|n)")
_DOCUMENT_RE = re.compile(r"(---|\.\.\.)(?:[ \t]|$)")

_CONTAINERFILES = ("Dockerfile", "Containerfile")
_TEMPLATE_SUFFIXES = (".template", ".tmpl", ".tpl", ".j2", ".jinja", ".jinja2", ".in")
# Folders whose Dockerfiles are test inputs, examples or build output rather than what the project ships.
_SKIP_DIRS = {
    "node_modules",
    "__pycache__",
    "site-packages",
    "tests",
    "test",
    "fixtures",
    "examples",
    "example",
    "docs",
}
_SKIP_DIRS |= {"target", "build", "dist"}
# Files named like a Dockerfile that are something else: its docs, or code about Dockerfiles (dockerfile.py).
_NOT_CONTAINERFILES = (".md", ".rst", ".txt", ".py", ".pyc", ".sh", ".json", ".yml", ".yaml", ".toml", ".dockerignore")


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
    next_line: bool = False  # the value starts on the line after the key

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
    """Drop a trailing `# comment` (a # at the start or after a space or tab, outside quotes)."""
    return _scan(text, _FlowState(), continued=False)


def _split_key(text: str) -> tuple[str, str] | None:
    """`key: value` -> (key, value), or None when the line isn't a plain mapping key. A quoted key with escapes
    (`"arch\\x69tecture":`) can't be read, so it's an error rather than a key that's never matched."""
    if not text or text[0] in "[{&*!|>%@`-?":
        return None
    if text[0] in "\"'":
        quote, i, escaped = text[0], 1, False
        while i < len(text) and text[i] != quote or text[i : i + 2] == quote * 2 == "''":
            if quote == '"' and text[i] == "\\" or quote == "'" and text[i : i + 2] == "''":
                escaped, i = True, i + 1
            i += 1
        if i >= len(text) or not text[i + 1 :].startswith(":"):
            return None
        rest = text[i + 2 :]
        if rest and rest[0] not in " \t":
            return None
        if escaped:
            raise _Unsure(f"can't read the escapes in the key {text[: i + 1]}")
        return text[1:i], rest.strip(_WS)
    m = re.search(r":(?:[ \t]|$)", text)
    if not m:
        return None
    return text[: m.start()].rstrip(_WS), text[m.end() :].strip(_WS)


@dataclass
class _FlowState:
    """Where a quoted value or flow collection is, partway through: the open quote and the bracket depth."""

    quote: str | None = None
    depth: int = 0
    spans_quote: bool = False  # a quoted string continued onto another line

    @property
    def done(self) -> bool:
        return self.quote is None and self.depth == 0


def _starts(text: str, i: int, flow: bool) -> bool:
    """Whether a quote or bracket at text[i] starts a scalar or collection, as YAML has it: at the start of the text,
    after `[`, `{` or `,` inside a flow collection, or after `: ` or `- ` (with a space or tab between). Anywhere else
    (`x:'y`, `it's`, `a"b`) it's an ordinary character of a plain scalar."""
    j = i - 1
    while j >= 0 and text[j] in _WS:
        j -= 1
    if j < 0:
        return True
    previous = text[j]
    if flow and previous in "[{,":
        return True
    if j == i - 1:
        return False
    return previous == ":" or previous == "-" and (j == 0 or text[j - 1] in _WS)


def _scan(text: str, state: _FlowState, continued: bool = True) -> str:
    """Read on through one line, tracking quotes and flow collections; return the line up to any comment. For a line
    that continues a value (`continued`), a `]` or `}` with nothing open is an error."""
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
        elif ch == "#" and (i == 0 or text[i - 1] in _WS):
            return text[:i].rstrip(_WS)
        elif ch in "\"'[{" and _starts(text, i, state.depth > 0):
            if ch in "[{":
                state.depth += 1
            else:
                state.quote = ch
        elif ch in "]}" and state.depth > 0:
            state.depth -= 1
        elif ch in "]}" and continued:
            raise _Unsure(f"can't read {text.strip(_WS)}")
        i += 1
    if state.quote is not None:
        state.spans_quote = True
    return text.rstrip(_WS)


def _continued(value: str) -> _FlowState | None:
    """The state at the end of the line, when the value is a quoted string or flow collection that doesn't end there."""
    if not value or value[0] not in "\"'[{":
        return None
    state = _FlowState()
    _scan(value, state, continued=False)
    return None if state.done else state


def _parse_tree(text: str) -> _Node:
    """The workflow as a tree of lines, by indentation. Block scalars (`run: |`) are skipped, and a quoted value or
    flow collection over several lines is joined onto its first line."""
    if m := _ODD_BREAK_RE.search(text):
        raise _Unsure(f"a line break YAML may read differently ({m.group()!r})", text[: m.start()].count("\n") + 1)
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
            if not stripped.strip(_WS) or indent > block_indent:
                continue
            block_indent = None
        if not stripped.strip(_WS) or stripped.lstrip(_WS).startswith("#"):
            continue
        if stripped.startswith("\t") or "\t" in line[:indent]:
            raise _Unsure("tab in indentation", number)
        if indent == 0 and stripped.startswith("%"):
            directive = _strip_comment(stripped)
            if started or not re.fullmatch(r"%YAML[ \t]+1\.2", directive):
                raise _Unsure(f"the {directive} directive isn't read (only %YAML 1.2 before the document is)", number)
            continue
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
        is_item = body == "-" or body.startswith("- ")
        base = indent + 0.5 if is_item else indent
        parent = next(n for n in reversed(stack) if n.indent < base)
        if parent.value or parent.is_text:
            # A further line of the parent's (plain) value: text, whatever it looks like. It never starts a quoted
            # value or flow collection of its own.
            while stack[-1] is not parent:
                stack.pop()
            parent.children.append(_Node(number, indent, None, body, is_text=True))
            continue
        nodes: list[_Node] = []
        try:
            pair = _split_key(body[1:].lstrip(" ") if is_item else body)
        except _Unsure as e:
            raise _Unsure(e.reason, number) from None
        if is_item:
            rest = body[1:].lstrip(" ")
            item = _Node(number, indent + 0.5, None, rest)
            nodes.append(item)
            if pair is not None:
                item.value = ""
                key_column = indent + len(body) - len(rest)
                nodes.append(_Node(number, key_column, pair[0], pair[1]))
                block_column = key_column
            else:
                block_column = indent
        elif pair is not None:
            nodes.append(_Node(number, indent, pair[0], pair[1]))
            block_column = indent
        else:
            while stack[-1].indent >= indent:
                stack.pop()
            parent = stack[-1]
            if parent.key is not None and not parent.value and not parent.children and body[0] not in "?:":
                # `key:` with its value on the next line: the value belongs to the key. A block scalar header there
                # isn't read, and a scalar there is refused wherever it's read.
                if _BLOCK_SCALAR_RE.fullmatch(body) or _PROPERTY_BLOCK_RE.fullmatch(body):
                    raise _Unsure(f"a block scalar ({body}) on the line after {parent.key}: isn't read", number)
                parent.value, parent.next_line = body, True
                nodes = []
                last = parent
            else:
                nodes.append(_Node(number, indent, None, body, is_text=True))
            block_column = indent
        for node in nodes:
            while stack[-1].indent >= node.indent:
                stack.pop()
            stack[-1].children.append(node)
            stack.append(node)
        if nodes:
            last = nodes[-1]
        if last.is_text:
            continue
        if _PROPERTY_BLOCK_RE.fullmatch(last.value):
            raise _Unsure(f"a block scalar with an anchor or tag ({last.value}) isn't read", number)
        if (state := _continued(last.value)) is not None:
            parts = [last.value]
            while not state.done:
                if number >= len(lines):
                    raise _Unsure(f"{last.value} isn't closed before the end of the file", last.line)
                try:
                    parts.append(_scan(lines[number].strip(_WS), state))
                except _Unsure as e:
                    raise _Unsure(e.reason, number + 1) from None
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


class _Flow:
    """A strict reader for one flow collection: `[a, "b", 'c']`, `{os: x, python: "3.13"}`, or a list of such
    mappings. Anything else (nesting, `k: v` or `?` in a list, escapes, comments, anchors) can't be read. As in YAML, a
    quote only quotes at the start of an item: `it's` and `a"b` are plain."""

    def __init__(self, text: str, line: int) -> None:
        self.text, self.pos, self.line = text, 0, line

    def fail(self) -> NoReturn:
        raise _Unsure(f"can't read {self.text}", self.line)

    def peek(self, n: int = 0) -> str:
        return self.text[self.pos + n : self.pos + n + 1]

    def spaces(self) -> None:
        while self.peek() in (" ", "\t") and self.peek():
            self.pos += 1

    def read(self) -> object:
        self.spaces()
        value = self.seq() if self.peek() == "[" else self.mapping() if self.peek() == "{" else self.fail()
        self.spaces()
        if self.pos != len(self.text):
            self.fail()
        return value

    def items(self, close: str, item: Callable[[], None]) -> None:
        self.pos += 1
        while True:
            self.spaces()
            if self.peek() == close:
                self.pos += 1
                return
            item()
            self.spaces()
            if self.peek() == ",":
                self.pos += 1
            elif self.peek() != close:
                self.fail()

    def seq(self) -> list[object]:
        out: list[object] = []
        self.items("]", lambda: out.append(self.mapping() if self.peek() == "{" else self.scalar(key=False)))
        return out

    def mapping(self) -> dict[str, _Scalar]:
        out: dict[str, _Scalar] = {}

        def pair() -> None:
            key = self.scalar(key=True)
            if self.peek() != ":" or self.peek(1) not in (" ", "\t") or key.text in out:
                self.fail()
            self.pos += 1
            self.spaces()
            out[key.text] = self.scalar(key=False)

        self.items("}", pair)
        return out

    def scalar(self, key: bool) -> _Scalar:
        """A quoted or plain scalar. In a list, `k: v` would be a mapping; as a key, it stops at the `:`."""
        start, ch = self.pos, self.peek()
        if ch in ("'", '"'):
            self.pos += 1
            while True:
                c = self.peek()
                if not c or c == "\\" and ch == '"':
                    self.fail()
                self.pos += 1
                if c == ch:
                    if ch == "'" and self.peek() == "'":
                        self.pos += 1
                        continue
                    break
            value = _unquote(self.text[start : self.pos], self.line)
            self.spaces()
            if not key and self.peek() == ":":
                self.fail()
            return value
        if not ch or ch in "[]{},#&*!|>%@`?" or ch == "-" and self.peek(1) in ("", " ", "\t", ",", "]", "}"):
            self.fail()
        while (c := self.peek()) and c not in ",]}":
            if c in "[{" or c == "#" and self.text[self.pos - 1] in " \t":
                self.fail()
            if c == ":" and self.peek(1) in ("", " ", "\t", ",", "]", "}"):
                if not key:
                    self.fail()
                break
            self.pos += 1
        return _Scalar(self.text[start : self.pos].rstrip(" \t"), False, self.line)


def _scalar(node: _Node) -> _Scalar:
    if node.next_line:
        raise _Unsure(f"{node.key}: a value on the line after the key", node.line)
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
        if node.value.startswith(("[", "{")):  # also on the line after the key (Prettier writes them so)
            return _Flow(node.value, node.line).read()
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
        if isinstance(value, _Scalar) and "${{" in value.text:
            raise _Unsure(f"the matrix's {key} is computed ({value.text})", value.line)
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


def _unread_lines(node: _Node) -> None:
    """Refuse a line that's neither a key nor a list item where one belongs (a `? key`, a stray scalar): it could hold
    a key the scan would otherwise miss. Under a key with a value, such lines just continue that value."""
    for child in node.children:
        if child.is_text and not node.value:
            raise _Unsure(f"can't read {child.value}", child.line)
        if not child.value:
            _unread_lines(child)


@dataclass(frozen=True)
class _Setup:
    """An actions/setup-python or astral-sh/setup-uv step, with its inputs by lowercase name (as Actions reads them)."""

    step: _Node
    action: str
    inputs: dict[str, _Node]

    @property
    def setup_python(self) -> bool:
        return self.action == "actions/setup-python"


# The inputs that pick the interpreter: a step that sets one under `if:` may or may not run.
_INTERPRETER_INPUTS = ("python-version", "architecture", "freethreaded", "python-arch")


def _setups(steps: _Node | None) -> list[_Setup]:
    """The job's setup steps. A step whose `uses`, `with` or inputs can't be read for sure refuses the whole job, since
    it might set the architecture or the Python."""
    if steps is None:
        return []
    if steps.value and steps.value != "[]":
        raise _Unsure(f"steps: {steps.value} isn't read", steps.line)
    found = []
    for step in steps.children:
        if step.key is not None or step.is_text or step.value:
            raise _Unsure(f"can't read the step {step.value or step.key}", step.line)
        uses, with_, condition = step.get("uses"), step.get("with"), step.get("if")  # keys are case-sensitive
        if uses is None:
            continue
        try:
            text = _scalar(uses).text
        except _Unsure as e:
            raise _Unsure(f"can't read the uses: {uses.value or '...'} of a step", e.line) from None
        action = text.split("@", 1)[0].lower()
        if "${{" in text:
            raise _Unsure(f"uses: {text} is an expression", uses.line)
        if action not in _PYTHON_ACTIONS:
            continue
        inputs: dict[str, _Node] = {}
        if with_ is not None:
            if with_.value or any(c.key is None for c in with_.children):
                raise _Unsure(f"with: {with_.value or '...'} isn't read", with_.line)
            for c in with_.children:
                name = str(c.key).lower()  # input names are case-insensitive (INPUT_<NAME>)
                if name in inputs:
                    raise _Unsure(f"with: sets {name} twice", c.line)
                inputs[name] = c
        if condition is not None and any(name in inputs for name in _INTERPRETER_INPUTS):
            raise _Unsure(f"the {action} step has if: {condition.value or '...'}, so it may not run", condition.line)
        found.append(_Setup(step, action, inputs))
    return found


# An architecture a job's setup step asks for: None (the runner's own), the input's value, the matrix key it's
# taken from, or why it can't be read.
_Arch = str | None | tuple[str, int] | _Unsure


def _arch(setup: _Setup) -> _Arch:
    if not setup.setup_python:
        if (node := setup.inputs.get("python-arch")) is not None:
            return _Unsure(f"setup-uv's python-arch: {node.value} isn't read", node.line)
        return None
    if (node := setup.inputs.get("architecture")) is None:
        return None
    try:
        value = _scalar(node)
    except _Unsure as e:
        return _Unsure(f"architecture: {node.value} isn't read", e.line)
    if m := _MATRIX_REF_RE.fullmatch(value.text):
        return m.group(1), node.line
    if "${{" in value.text:
        return _Unsure(f"architecture: {value.text} isn't read", node.line)
    try:
        return value.plain_text() or None  # '' is setup-python's default: the runner's own
    except _Unsure as e:
        return e


def _read_job(job: _Node, path: str, out: Suggestions) -> None:
    def unsure(e: _Unsure) -> None:
        out.unreadable.append(Unreadable(path, e.line, f"job {job.key}: {e.reason}"))

    try:
        if job.value or job.get("<<") is not None:
            raise _Unsure("can't read the job", job.line)
        setups = _setups(job.get("steps"))
        if (strategy := job.get("strategy")) is not None and strategy.get("matrix") is not None:
            try:
                every, matrix_line = _matrix(job, job)
            except _Unsure:
                pass  # refused where the matrix is used; a computed one still runs the job
            else:
                if not every:  # GitHub runs no job at all
                    raise _Unsure("the matrix is empty", matrix_line)
    except _Unsure as e:
        unsure(_Unsure(e.reason + " (neither its runner nor its Python is read)", e.line))
        return

    for setup in setups:
        try:
            _read_python(job, setup, path, out)
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
        # Each step that picks an interpreter adds its architecture; a setup-uv step without a Python picks none.
        pickers = [s for s in setups if s.setup_python or {"python-version", "python-arch"} & s.inputs.keys()]
        archs = list(dict.fromkeys(_arch(s) for s in pickers)) or [None]
        if unread := next((a for a in archs if isinstance(a, _Unsure)), None):
            raise _Unsure(f"{unread.reason}, so the job's target isn't read", unread.line)
        label_node = _scalar(runs_on)
        label_key = m.group(1) if (m := _MATRIX_REF_RE.fullmatch(label_node.text)) else None
        if label_key is None and "${{" in label_node.text:
            raise _Unsure(f"runs-on: {label_node.text} is an expression", runs_on.line)
        combos: list[dict[str, _Scalar]] = [{}]
        if label_key is not None or any(isinstance(a, tuple) for a in archs):
            # Each matrix combination runs its own runner with its own architecture.
            combos, line = _matrix(job, runs_on)
            if not combos:
                raise _Unsure("the matrix is empty", line)
        # Everything is worked out before anything is reported, so a refusal partway through the matrix leaves no
        # targets behind and the result doesn't depend on the matrix's order.
        found: set[Found] = set()
        refused: set[tuple[int | None, str]] = set()
        for combo in combos:
            if label_key is None:
                label_value, source = label_node, f"runs-on: {label_node.text}"
            elif label_key in combo:
                label_value = combo[label_key]
                source = f"runs-on: {label_value.text} (matrix.{label_key}, line {runs_on.line})"
            else:
                raise _Unsure(f"matrix.{label_key} isn't set in every matrix combination", runs_on.line)
            try:
                label = label_value.plain_text()
            except _Unsure as e:
                refused.add((e.line, e.reason))
                continue
            if (target := runner_target(label)) is None:
                refused.add((label_value.line, f"{label} isn't a GitHub-hosted runner wheelgap knows"))
                continue
            glibc, macos = _runner_os(label)
            for arch in archs:
                detail = source
                if isinstance(arch, tuple):
                    # A combination without the key (or with '') gives setup-python '', so the runner's own.
                    value = combo.get(arch[0])
                    try:
                        arch = value.plain_text() if value is not None else None
                    except _Unsure as e:
                        refused.add((e.line, e.reason))
                        continue
                    detail += f", architecture: {arch} (matrix)" if arch else ""
                    arch = arch or None
                elif arch is not None:
                    detail += f", architecture: {arch}"
                mapped = target if arch is None else _ARCHITECTURES.get((target, str(arch)))
                if mapped is None:
                    refused.add((label_value.line, f"setup-python's architecture: {arch} on {label} isn't read"))
                    continue
                found.add(Found(mapped, path, label_value.line, detail, glibc=glibc, macos=macos))
        order = list(PLATFORMS)
        for f in sorted(found, key=lambda f: (f.line, order.index(f.value), f.source)):
            if f not in out.targets:
                out.targets.append(f)
        for where, reason in sorted(refused, key=lambda r: (r[0] or 0, r[1])):
            unsure(_Unsure(reason, where))
    except _Unsure as e:
        unsure(e)


def _read_python(job: _Node, setup: _Setup, path: str, out: Suggestions) -> None:
    """The Python an actions/setup-python or astral-sh/setup-uv step installs."""
    version = setup.inputs.get("python-version")
    if version is None:
        if setup.setup_python:
            if (file := setup.inputs.get("python-version-file")) is not None:
                raise _Unsure(f"setup-python reads {_scalar(file).text}, which isn't read here", file.line)
            raise _Unsure(
                "setup-python with no python-version (it reads .python-version, or uses the runner's)", setup.step.line
            )
        return
    freethreaded = False
    if setup.setup_python and (node := setup.inputs.get("freethreaded")) is not None:
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
            python, source = python + "t", f"{source}, freethreaded: true"
        if python.endswith("t") and int(m.group(1)) < 13:
            reason = f"job {job.key}: {source} asks for a free-threaded {text}, but free-threading starts at 3.13"
            out.unreadable.append(Unreadable(path, value.line, reason))
            continue
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
    read_workflow_text(text, name, out)


def read_workflow_text(text: str, name: str, out: Suggestions) -> None:
    try:
        root = _parse_tree(text)
        _unread_lines(root)
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
    """ "file" for a Dockerfile or Containerfile (Dockerfile, dockerfile.dev, Containerfile.prod, prod.Dockerfile),
    "template" for one that a tool fills in first (Dockerfile.j2), None for anything else."""
    lower = name.lower()
    if lower.endswith(_NOT_CONTAINERFILES):
        return None
    bases = ("dockerfile", "containerfile")
    if not (
        lower in bases
        or lower.startswith(tuple(f"{base}{sep}" for base in bases for sep in ".-_"))
        or lower.endswith(tuple(f".{base}" for base in bases))
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
            if not d.startswith(".")
            and d not in _SKIP_DIRS
            and not os.path.exists(os.path.join(top, d, "pyvenv.cfg"))  # False, not an error, when unreadable
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
        reason = str(e).removeprefix(f"{path}: ").removesuffix(", pass -p/-t instead")  # no -p/-t here
        out.unreadable.append(Unreadable(name, None, reason))
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
