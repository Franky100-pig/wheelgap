"""--explain: the coverage tracker's notes for a package with a gap, such as "use `psycopg2-binary`".

Notes record what a scan can't know: a binary extra, a system library to install, a conda-forge build. The tracker
publishes them in each package's file, https://wheelgap.github.io/tracker/data/packages/<name>.json (its format:
https://github.com/wheelgap/tracker/blob/main/notes/README.md). They come from a public repository, so they're
untrusted: a note that isn't exactly in the published format is dropped whole, never shown in part.

Looking a package up sends its name to wheelgap.github.io, so the CLI only asks for packages known to come from PyPI
(see from_pypi), and never with --offline, --index-url or --no-notes. The lookups are optional extras, so they're
bounded: one attempt each, a short timeout, a deadline for them all, and none after the first connection failure.
"""

from __future__ import annotations

import http.client
import queue
import re
import threading
import time
import urllib.error
from dataclasses import dataclass
from urllib.parse import urlsplit

from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from . import cache, pypi
from .sources import Package

TRACKER = "https://wheelgap.github.io/tracker/data/packages"
SCHEMA = 1  # of the tracker's package files: it goes up if a field changes meaning or goes away
TIMEOUT = 10.0  # seconds, for each request (one attempt, no retries)
DEADLINE = 15.0  # seconds, for all the lookups of a run
WORKERS = 16
MAX_TEXT = 500  # the format's limit: shorter than this
# A normalized name is also a safe path segment: no slashes, dots or leading dashes.
_NAME = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*", re.ASCII)
_KIND = re.compile(r"[a-z]+(-[a-z]+)*", re.ASCII)
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", re.ASCII)


@dataclass(frozen=True)
class Note:
    platforms: tuple[str, ...]  # wheelgap platform names, or ("all",)
    kind: str  # workaround, alternative, upstream-issue, other-source
    text: str
    links: tuple[str, ...]
    versions: str | None  # a version specifier, or None for every release
    updated: str  # YYYY-MM-DD

    def covers(self, platform: str) -> bool:
        return self.platforms == ("all",) or platform in self.platforms

    def applies_to(self, version: str | None) -> bool:
        """Whether the note is about this release. A note limited to some releases is shown only when the version
        is known and is a plain release the specifier admits, as the tracker matches it: never on a guess."""
        if self.versions is None:
            return True
        if version is None or not self.versions.strip():
            return False
        try:
            v = Version(version)
            if v.epoch or v.pre or v.post is not None or v.dev is not None or v.local:
                return False  # the tracker hides such notes for these releases too
            return SpecifierSet(self.versions).contains(v)
        except (InvalidVersion, InvalidSpecifier):
            return False

    def as_json(self) -> dict:
        return {
            "platforms": list(self.platforms),
            "kind": self.kind,
            "text": self.text,
            "links": list(self.links),
            "versions": self.versions,
            "updated": self.updated,
        }


def _line(value: object) -> bool:
    """A non-empty string with no line breaks, control or format characters (which could rewrite a terminal)."""
    return isinstance(value, str) and bool(value.strip()) and value.isprintable()


def _link(value: object) -> bool:
    if not _line(value) or not isinstance(value, str) or " " in value or not value.startswith("https://"):
        return False
    try:
        parts = urlsplit(value)
        return bool(parts.hostname) and "@" not in parts.netloc
    except ValueError:
        return False


def _note(raw: object) -> Note | None:
    """A published note, or None if it isn't exactly in the format."""
    if not isinstance(raw, dict) or set(raw) != {"platforms", "kind", "text", "links", "versions", "updated"}:
        return None
    platforms, links, versions = raw["platforms"], raw["links"], raw["versions"]
    if not isinstance(platforms, list) or not platforms or not all(_line(p) and " " not in p for p in platforms):
        return None
    if "all" in platforms and len(platforms) != 1:
        return None
    if not isinstance(links, list) or not links or not all(_link(link) for link in links):
        return None
    if versions is not None and not _line(versions):
        return None
    kind, text, updated = raw["kind"], raw["text"], raw["updated"]
    if not (isinstance(kind, str) and _KIND.fullmatch(kind)) or not (
        isinstance(updated, str) and _DATE.fullmatch(updated)
    ):
        return None
    if not _line(text) or len(text) >= MAX_TEXT:
        return None
    return Note(tuple(platforms), kind, text, tuple(links), versions, updated)


def parse(data: object, name: str) -> list[Note] | None:
    """The notes in a package file, or None if the file isn't one for `name` in a schema this version reads."""
    if not isinstance(data, dict) or data.get("schema") != SCHEMA or data.get("name") != name:
        return None
    raw = data.get("notes", [])  # files published before notes existed have none
    if not isinstance(raw, list):
        return None
    return [n for n in map(_note, raw) if n is not None]


def from_pypi(package: Package, looked_up: bool) -> bool:
    """Whether the package's name may go to the tracker: wheelgap looked it up on pypi.org itself this run
    (`looked_up`), or the lock says it came from pypi.org (a uv.lock registry, a pylock.toml index). Never for a
    skipped package (private index, URL, path, git), nor one whose lock names no index (pdm.lock, a pylock.toml
    entry without one): it may be a private name."""
    if package.skip or package.error:
        return False
    return looked_up or (package.registry is not None and package.registry.rstrip("/") == pypi.PYPI_SIMPLE)


_CONNECTION = (urllib.error.URLError, OSError, http.client.HTTPException)  # HTTPError is handled before these


def lookup(names: list[str], read_cache: bool = True, write_cache: bool = True) -> tuple[dict[str, list[Note]], bool]:
    """Each name's notes, keyed by name, for the names that could be looked up (a 404 is no notes); and whether
    the lookups were cut short, by the deadline or a connection failure. Nothing here raises: a lookup that fails
    just leaves its name out."""
    found: dict[str, list[Note]] = {}
    todo = []
    for name in dict.fromkeys(names):
        if not _NAME.fullmatch(name):
            continue
        cached = cache.load_notes(TRACKER, name) if read_cache else None
        if cached is not None:
            found[name] = [n for n in map(_note, cached) if n is not None]
        else:
            todo.append(name)
    if not todo:
        return found, False

    work: queue.SimpleQueue[str] = queue.SimpleQueue()
    for name in todo:
        work.put(name)
    done: queue.SimpleQueue[tuple[str, object, bool]] = queue.SimpleQueue()
    stop = threading.Event()

    def worker() -> None:
        while not stop.is_set():
            try:
                name = work.get_nowait()
            except queue.Empty:
                return
            try:
                data: object = pypi.fetch_json(f"{TRACKER}/{name}.json", timeout=TIMEOUT, attempts=1)
            except urllib.error.HTTPError as e:
                data = {"schema": SCHEMA, "name": name, "notes": []} if e.code == 404 else None
            except _CONNECTION:
                stop.set()  # the tracker can't be reached (a firewalled runner): don't try the rest
                done.put((name, None, True))
                return
            except Exception:  # a body that isn't JSON, and the like
                data = None
            done.put((name, data, False))

    # Daemon threads, so a request still waiting on its timeout can't hold up the end of the run.
    for _ in range(min(WORKERS, len(todo))):
        threading.Thread(target=worker, daemon=True).start()
    deadline = time.monotonic() + DEADLINE
    cut = False
    for _ in todo:
        try:
            name, data, failed = done.get(timeout=max(0.0, deadline - time.monotonic()))
        except queue.Empty:
            cut = True
            break
        if failed:
            cut = True
            break
        notes = parse(data, name)
        if notes is not None:
            found[name] = notes
            if write_cache:
                cache.save_notes(TRACKER, name, [n.as_json() for n in notes])
    stop.set()
    return found, cut
