"""--conda-forge: say when conda-forge has a package of the same name with a build that fits a gap's target.

Only a hint under --explain: a conda-forge build doesn't make `pip install` work, and a conda package of the same name
may be another project. So it never changes a status, the summary or the exit code.

A build counts for a target when it's of the checked version, on conda-forge's main label and not marked broken, for
the target's conda subdir (or noarch), and for the target's Python: the same CPython minor version and ABI
(free-threaded or not) by its python_abi dependency, or a noarch build whose python constraints admit that Python.
A build whose Python can't be told never counts.
"""

from __future__ import annotations

import json
import operator
import re
import threading
import time
import urllib.error
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import quote

from packaging.version import InvalidVersion, Version

from . import cache, pypi
from .sources import Package, Source
from .targets import PLATFORMS, Target

# One package's files on anaconda.org, each with its version, labels and attrs (subdir, depends).
API = "https://api.anaconda.org/package/conda-forge"
# Uncompressed, numpy's answer is about 7.5 MB and pyarrow's over 60 MB; gzipped (streamed), about a sixteenth.
# Parsed, an answer takes about 3.5 times its size, so with 3 workers the worst case is about 3 x 16 x 4.5 MB, under
# 250 MB. Past the cap the package gets no hint, and that's cached like an answer.
MAX_BYTES = 16 * 1024 * 1024
WORKERS = 3
# Seconds per socket operation, and each request is tried once: a hint isn't worth a retry. anaconda.org can take
# 6 to 16 seconds to start answering for a big package such as numpy.
TIMEOUT = 15.0
DEADLINE = 30.0  # seconds for all the lookups of a run; past it, the rest are dropped
NOARCH = "noarch"
TOO_LARGE = cache.TOO_LARGE

# wheelgap platform -> conda subdir. Only the subdirs where conda-forge builds current CPython: it has no Python for
# linux-32, linux-armv7l or linux-s390x, and win-32 stopped at 3.6. musl, Pyodide, iOS and Android never match.
SUBDIRS = {
    "linux-x86_64": "linux-64",
    "linux-aarch64": "linux-aarch64",
    "linux-ppc64le": "linux-ppc64le",
    "linux-riscv64": "linux-riscv64",
    "macos-x86_64": "osx-64",
    "macos-arm64": "osx-arm64",
    "win-amd64": "win-64",
    "win-arm64": "win-arm64",
}
# A noarch build that depends on one of these virtual packages installs only on those OSes (wheelgap's families).
NOARCH_OS = {"__win": {"windows"}, "__unix": {"manylinux", "macos"}, "__linux": {"manylinux"}, "__osx": {"macos"}}

# A build, cut down to what matching needs: {"subdir", "abi" (python_abi's build string, e.g. cp314t, or None),
# "python" (the version constraints of its python dependencies, or None when it has none), "os" (NOARCH_OS keys)}.
Build = dict


class Unreachable(Exception):
    """anaconda.org couldn't be reached (DNS, a refused or timed-out connection): the other lookups would fail too."""


def index_allows(simple_index: str | None) -> bool:
    """Whether this run may look anything up: not when packages come from another index (--index-url, or
    WHEELGAP_INDEX_URL set to a mirror), which may hold private names alongside PyPI's."""
    return simple_index is None and pypi.index_url() == pypi.DEFAULT_INDEX


def may_send(pkg: Package, source: Source) -> bool:
    """Whether the package's name may go to anaconda.org: it's positively on PyPI. Either its lock says it came from
    PyPI (uv.lock's registry, pylock.toml's index), or this run found it on pypi.org and its input names no other
    index (Source.pypi_only). Anything else, such as a pdm.lock, a pylock.toml entry with no index, or a requirements
    file with an --index-url line, may hold private names, so it's never sent."""
    if pkg.skip:
        return False
    return (pkg.registry is not None and pypi.is_pypi(pkg.registry)) or (pkg.found_on_pypi and source.pypi_only)


def wanted(targets: Iterable[Target]) -> bool:
    """Whether any of these gap targets could match a conda-forge build."""
    return any(t.platform in SUBDIRS for t in targets)


def _same_version(a: str, b: str) -> bool:
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return a == b


def _build(f: object, version: str) -> Build | None:
    """A file of `version` on the main label and not marked broken, cut down to a Build; None for any other file."""
    if not isinstance(f, dict):
        return None
    attrs, labels, v = f.get("attrs"), f.get("labels"), f.get("version")
    if not (
        isinstance(attrs, dict)
        and isinstance(labels, list)
        and isinstance(v, str)
        and isinstance(attrs.get("subdir"), str)
    ):
        return None
    if "main" not in labels or "broken" in labels or not _same_version(v, version):
        return None
    depends = attrs.get("depends")
    abi, python, os = None, None, []
    for dep in depends if isinstance(depends, list) else []:
        parts = dep.split() if isinstance(dep, str) else []
        if not parts:
            continue
        if parts[0] == "python_abi" and len(parts) >= 3:
            abi = parts[2].removeprefix("*_")
        elif parts[0] == "python":
            python = (python or []) + parts[1:2]  # a bare `python` adds no constraint, but says it's a Python build
        elif parts[0] in NOARCH_OS:
            os.append(parts[0])
    return {"subdir": attrs["subdir"], "abi": abi, "python": python, "os": sorted(set(os))}


def parse_builds(data: object, version: str) -> list[Build]:
    """The builds of `version`, from an anaconda.org package answer."""
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        raise ValueError("unexpected response")
    builds = (b for f in files if (b := _build(f, version)) is not None)
    return list({json.dumps(b, sort_keys=True): b for b in builds}.values())  # one per kind of build


_OPS = {">=": operator.ge, "<=": operator.le, ">": operator.gt, "<": operator.lt, "==": operator.eq, "!=": operator.ne}
_PART = re.compile(r"(>=|<=|==|!=|>|<)?([0-9][0-9A-Za-z.]*?)(\.?\*)?")


def _holds(spec: str, major: int, minor: int) -> bool | None:
    """Whether a conda version constraint (`>=3.10`, `3.13.*`, `>=2.7,<2.8.0a0`, `3.6|3.7`) admits some X.Y.z
    release of Python X.Y. None when it can't be told."""
    results = []
    for alt in spec.split("|"):
        ok_low = ok_high = True  # holds for X.Y.0 / for a late X.Y patch
        for part in alt.split(","):
            m = _PART.fullmatch(part.strip())
            if not m:
                return None
            op, ver, star = m.groups()
            if star or not op:  # a glob, or a bare version (conda reads `3.9` as `3.9.*`)
                if op not in (None, "=="):
                    return None
                comps = ver.split(".")
                if len(comps) > 2 or not all(c.isdigit() for c in comps):
                    return None
                ok = [int(c) for c in comps] == [major, minor][: len(comps)]
                ok_low, ok_high = ok_low and ok, ok_high and ok
                continue
            try:
                bound = Version(ver)
            except InvalidVersion:
                return None
            compare = _OPS[op]
            ok_low = ok_low and compare(Version(f"{major}.{minor}.0"), bound)
            ok_high = ok_high and compare(Version(f"{major}.{minor}.99"), bound)
        results.append(ok_low or ok_high)
    return any(results)


def fits(build: Build, target: Target) -> bool:
    """Whether a build installs on the target's conda subdir and Python (see the module docstring)."""
    own = SUBDIRS.get(target.platform)
    if own is None or build["subdir"] not in (own, NOARCH):
        return False
    family = PLATFORMS[target.platform][0]
    if build["subdir"] == NOARCH and not all(family in NOARCH_OS[os] for os in build["os"]):
        return False
    python, abi = build["python"], build["abi"]
    if python is None and abi is None:
        return False  # not a Python build, or one we can't place
    if any(_holds(spec, target.major, target.minor) is not True for spec in python or []):
        return False
    if abi is not None:
        return abi == f"cp{target.major}{target.minor}{'t' if target.freethreaded else ''}"
    if build["subdir"] == NOARCH:
        return True  # pure Python: any CPython its constraints admit, free-threaded too
    # A compiled build with no python_abi (older ones): its python constraint gives the version, and it's never
    # free-threaded; with no constraint at all, its Python can't be told.
    return bool(python) and not target.freethreaded


def fetch(key: str, version: str) -> list[Build] | str | None:
    """Ask anaconda.org: the builds of `version` ([] for no such package or version), TOO_LARGE, or None for an answer
    that can't be used (403, 429, 5xx, too slow, damaged). Raises Unreachable when anaconda.org can't be reached."""
    try:
        data = pypi.fetch_json(f"{API}/{quote(key, safe='')}", timeout=TIMEOUT, attempts=1, max_bytes=MAX_BYTES)
    except urllib.error.HTTPError as e:
        return [] if e.code == 404 else None  # only a 404 says there's no such package
    except pypi.ResponseTooLarge:
        return TOO_LARGE
    except urllib.error.URLError as e:  # urllib's wrapper for failing to connect at all
        raise Unreachable from e
    except Exception:  # a slow or cut-off answer (TimeoutError, a reset), a damaged body, bad JSON
        return None
    try:
        return parse_builds(data, version)
    except ValueError:
        return None


def look_up(key: str, version: str, read_cache: bool = False, write_cache: bool = False) -> list[Build] | str | None:
    """fetch(), through the 24-hour cache. Answers (builds, none, too large) are cached; failures aren't."""
    if read_cache and (cached := cache.load_builds(key, version)) is not None:
        return cached
    found = fetch(key, version)
    if found is not None and write_cache:
        cache.save_builds(key, version, found)
    return found


@dataclass
class Lookups:
    found: dict[tuple[str, str], list[Build]]  # (name, version) -> builds, for the lookups that gave an answer
    unchecked: list[str]  # names with no usable answer: failed, too large, or dropped
    stopped: bool  # anaconda.org stopped answering, or the deadline passed, so some lookups were dropped


def look_up_all(
    wanted: Iterable[tuple[str, str]], read_cache: bool = False, write_cache: bool = False, deadline: float = DEADLINE
) -> Lookups:
    """Look up each (name, version), at most WORKERS at a time. The first lookup that can't reach anaconda.org stops the
    rest, and so does the deadline. Workers are daemon threads, so a stalled request can't hold up the run's exit."""
    todo: deque[tuple[str, str]] = deque(dict.fromkeys(wanted))
    asked = list(todo)
    results: dict[tuple[str, str], list[Build] | str | None] = {}
    lock = threading.Lock()
    finished, stop = threading.Event(), threading.Event()

    def worker() -> None:
        while not stop.is_set():
            with lock:
                if not todo:
                    return
                item = todo.popleft()
            try:
                result = look_up(*item, read_cache, write_cache)
            except Unreachable:
                stop.set()
                finished.set()
                return
            with lock:
                if not stop.is_set():
                    results[item] = result
                    if len(results) == len(asked):
                        finished.set()

    if asked:
        start = time.monotonic()
        for _ in range(min(WORKERS, len(asked))):
            threading.Thread(target=worker, daemon=True).start()
        finished.wait(max(0.0, deadline - (time.monotonic() - start)))
        stop.set()
    with lock:
        found = {item: r for item, r in results.items() if isinstance(r, list)}
        unchecked = list(dict.fromkeys(name for name, v in asked if (name, v) not in found))
        return Lookups(found, unchecked, len(results) < len(asked))


@dataclass
class Hint:
    name: str  # the conda-forge name looked up: the canonical PyPI name
    version: str  # the checked version, which every build counted is of
    builds: dict[Target, list[str]]  # gap target -> the subdirs of the builds that fit it (its own and/or noarch)


def hint(pkg: Package, targets: Iterable[Target], found: dict[tuple[str, str], list[Build]]) -> Hint | None:
    """The hint for a package's gap targets, or None when no build fits any of them. The caller checks may_send."""
    if not pkg.version or not (builds := found.get((pkg.key, pkg.version))):
        return None
    matched = {}
    for t in targets:
        subdirs = sorted({b["subdir"] for b in builds if fits(b, t)}, key=lambda s: s == NOARCH)
        if subdirs:
            matched[t] = subdirs
    return Hint(pkg.key, pkg.version, matched) if matched else None


def hint_text(pkg: Package, h: Hint, multi_py: bool) -> str:
    """`numpy: conda-forge has a package named numpy with 2.3.4 builds for linux-riscv64, win-arm64 (noarch)`."""
    labels = [t.label(multi_py) + (" (noarch)" if subdirs == [NOARCH] else "") for t, subdirs in h.builds.items()]
    return f"{pkg.name}: conda-forge has a package named {h.name} with {h.version} builds for {', '.join(labels)}"
