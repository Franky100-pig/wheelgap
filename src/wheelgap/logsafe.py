"""Keep text from inputs from running as GitHub Actions workflow commands in a CI log.

The Actions runner reads every line a step prints, on stdout and stderr. A line whose first non-whitespace text is
`::` is a workflow command (`::error::...`, `::add-mask::...`, `::stop-commands::...`), and so is any line with
`##[` anywhere in it (the legacy form). Lines end at LF, CR or CRLF. wheelgap prints package names, versions,
paths and error messages from lockfiles, requirements files, Dockerfiles and index responses, and a pull request can
put anything in those. So the CLI writes through CommandGuard, which changes any line that would be read as a
command:

- `##[` becomes `\\u0023#[`;
- a line starting with `::` (after whitespace and colour codes) gets its first `:` written as `\\u003a`.

Both are JSON escapes, so in `--json` output, where such text can only be inside a string, the decoded values are
unchanged; in text and Markdown the escape shows. Normal output never contains either, so it's never changed.

See ActionCommand.TryParseV2 / TryParse and ActionCommandManager in https://github.com/actions/runner.
"""

from __future__ import annotations

import io
import re
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TextIO

_BREAK = re.compile(r"(\r\n|\r|\n)")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")


def neutralize(line: str) -> str:
    """One line (no line breaks) changed so the runner won't read it as a workflow command."""
    line = line.replace("##[", "\\u0023#[")
    if _ANSI.sub("", line).lstrip().startswith("::"):
        i = line.index("::")  # colour codes have no colons, so this is the one that starts the visible text
        line = line[:i] + "\\u003a" + line[i + 1 :]
    return line


class CommandGuard(io.TextIOBase):
    """A text stream that writes each complete line to `raw` through neutralize().

    A line is held until its line break arrives, since a command can be split over several writes; finish()
    writes what's left at the end.
    """

    def __init__(self, raw: TextIO):
        self.raw = raw
        self._pending = ""

    def write(self, s: str) -> int:
        text = self._pending + s
        end = max(text.rfind("\n"), text.rfind("\r")) + 1
        # A CR at the very end may be the first half of CRLF: keep it until the next write.
        if end and text[end - 1] == "\r" and end == len(text):
            end -= 1
        self._pending = text[end:]
        if end:
            parts = _BREAK.split(text[:end])
            self.raw.write("".join(part if i % 2 else neutralize(part) for i, part in enumerate(parts)))
        return len(s)

    def finish(self) -> None:
        if self._pending:
            pending, self._pending = self._pending, ""
            self.raw.write(neutralize(pending))
        self.raw.flush()

    def flush(self) -> None:
        self.raw.flush()  # a held partial line stays held: it may still turn into a command

    def close(self) -> None:
        pass  # the raw stream isn't ours to close (IOBase's finalizer would also flush it, maybe after it's gone)

    def writable(self) -> bool:
        return True

    def isatty(self) -> bool:
        return self.raw.isatty()

    @property
    def encoding(self) -> str:  # type: ignore[override]
        return self.raw.encoding

    @property
    def errors(self) -> str | None:  # type: ignore[override]
        return self.raw.errors


def write_command(command: str, stream: TextIO) -> None:
    """Write a workflow command wheelgap means to send (such as an annotation it built and escaped itself) on its own
    line, past the guard. Everything else goes through it.

    The command must be one line, so it can only ever carry itself. A partial line the guard is holding is ended
    first (and neutralized as usual), so the command starts its own line.
    """
    if not command.startswith("::") or "\n" in command or "\r" in command:
        raise ValueError(f"not a single workflow command line: {command!r}")
    if isinstance(stream, CommandGuard):
        if stream._pending:
            stream.write("\n")
        stream.raw.write(command + "\n")
    else:
        stream.write(command + "\n")


@contextmanager
def guarded_output() -> Iterator[None]:
    """Route sys.stdout and sys.stderr through CommandGuard for the duration (once, if nested)."""
    if isinstance(sys.stdout, CommandGuard):
        yield
        return
    out, err = sys.stdout, sys.stderr
    guards = CommandGuard(out), CommandGuard(err)
    sys.stdout, sys.stderr = guards
    try:
        yield
    finally:
        sys.stdout, sys.stderr = out, err
        for guard in guards:
            guard.finish()
